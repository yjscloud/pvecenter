/* ==========================================================================
   pvecenter — 批量还原快照（弹窗）

   为什么是「一张表，一行一台机器」，而不是「填一个快照名套全批」：

   列表页勾选的单位是**机器**，还原的语义也是「这几台各自回到哪一份」。真实环境里
   A 机器的 ``before-upgrade`` 与 B 机器的 ``daily-1103`` 是两条独立的快照链，甚至
   某台一份都没有 —— 「每台都有同名快照」这个前提根本不成立。所以这里逐台列出它
   自己的快照，逐台选目标，最后一次性提交。

   为什么做成弹窗而不是单开一页：用户是在列表里勾完机器顺手做这件事，跳走会丢掉
   列表的筛选 / 滚动位置，回来还得重新找这几台；弹窗里核对完直接提交，上下文不变。
   ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { vmsApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import { Modal } from './ui/Modal';
import { Button } from './ui/Button';
import { Badge } from './ui/Badge';
import { Select } from './ui/Input';
import { Notice } from './ui/EmptyState';
import { BulkResultNotice } from './BulkResultNotice';
import { usePveReady } from '../hooks/usePveStatus';
import { useToast } from '../hooks/useToast';
import { useAuth } from '../hooks/useAuth';
import { invalidateQuota } from '../hooks/quotaCache';
import { formatDateTime } from '../utils/format';
import { useT } from '../i18n';
import type {
  BulkResponse,
  GuestType,
  Snapshot,
  SnapshotBulkRestoreItem,
} from '../api/types';

/** 被勾选的一台机器：弹窗只需要定位与展示用的字段，不拖整个 VmSummary 进来 */
export interface RestoreCandidate {
  type: GuestType;
  node: string;
  vmid: number;
  name: string;
  status: string;
  template?: boolean;
}

interface RestoreRow extends RestoreCandidate {
  key: string;
  /** 已按时间倒序（最新在前） */
  snaps: Snapshot[];
}

/** 键与「快照汇总」的归组口径一致：类型 + 节点 + vmid（容器与虚拟机各一套命名空间） */
function restoreKey(guest: { type: GuestType; node: string; vmid: number }): string {
  return `${guest.type}|${guest.node}|${guest.vmid}`;
}

export interface BulkSnapshotRestoreDialogProps {
  open: boolean;
  /** 打开那一刻的勾选清单：弹窗期间不跟着列表的勾选变化走 */
  guests: RestoreCandidate[];
  onClose: () => void;
  /** 提交完成（逐台结果已返回）—— 列表页据此刷新状态与快照数 */
  onDone?: (response: BulkResponse) => void;
}

export function BulkSnapshotRestoreDialog({
  open,
  guests,
  onClose,
  onDone,
}: BulkSnapshotRestoreDialogProps) {
  const t = useT();
  const toast = useToast();
  const { hasPermission } = useAuth();
  const pveReady = usePveReady();
  const queryClient = useQueryClient();

  /** 行 key → 目标快照名。逐台独立，这是本弹窗的核心状态。 */
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<BulkResponse | null>(null);

  /**
   * 下一次取快照是否绕服务端 60 秒缓存。
   *
   * 还原会截断快照链（目标之后的快照被丢弃），而 PVE 是异步任务：提交后那一刷
   * 会把「还原前」的清单写进缓存。不带 fresh 的话，表里那几份其实已经没了的快照
   * 会继续可选，用户再选就是一条「快照不存在」。
   */
  const freshRef = useRef(false);

  /* 每次重新打开都从干净状态开始：上次的勾选与结果不该带到新一批机器上 */
  useEffect(() => {
    if (!open) return;
    setTargets({});
    setResult(null);
  }, [open]);

  const snapshotsQuery = useQuery({
    queryKey: ['snapshots', 'all'],
    queryFn: async () => {
      const fresh = freshRef.current;
      freshRef.current = false;
      return vmsApi.allSnapshots({ fresh });
    },
    enabled: open && pveReady,
    staleTime: 30_000,
    retry: false,
  });

  /* 一次汇总请求把所有机器的快照取回来（服务端并发 + 60 秒缓存），再按机器归组。
     逐台发 N 个请求会让「打开弹窗」变成 N 次往返，慢得看不下去。 */
  const rows = useMemo<RestoreRow[]>(() => {
    const map = new Map<string, Snapshot[]>();
    for (const snap of snapshotsQuery.data ?? []) {
      if (!snap.node || !snap.vmid) continue;
      const type: GuestType = snap.vm_type === 'lxc' ? 'lxc' : 'qemu';
      const key = restoreKey({ type, node: snap.node, vmid: snap.vmid });
      const list = map.get(key);
      if (list) list.push(snap);
      else map.set(key, [snap]);
    }
    for (const list of map.values()) {
      list.sort((a, b) => (b.snaptime ?? 0) - (a.snaptime ?? 0));
    }
    return guests.map((guest) => ({
      ...guest,
      key: restoreKey(guest),
      snaps: map.get(restoreKey(guest)) ?? [],
    }));
  }, [guests, snapshotsQuery.data]);

  /* 快照清单变了（典型：刚还原成功，目标之后的快照被 PVE 丢弃）就把已经不存在
     的目标清掉。留着的话下拉会显示成「未选择」，提交却仍带着那个旧名字，逐台
     报「快照不存在」—— 明明界面上什么都没选。 */
  useEffect(() => {
    if (snapshotsQuery.isFetching || snapshotsQuery.isError) return;
    setTargets((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const row of rows) {
        const chosen = next[row.key];
        if (chosen && !row.snaps.some((snap) => snap.name === chosen)) {
          delete next[row.key];
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [rows, snapshotsQuery.isFetching, snapshotsQuery.isError]);

  /** 这行为什么不能还原（空串 = 可以）。原因直接显示，不藏在 tooltip 里。 */
  const blockReason = (row: RestoreRow): string => {
    if (!hasPermission(row.type === 'lxc' ? 'ct.snapshot' : 'vm.snapshot')) {
      return t('snapRestore.reasonNoPermission');
    }
    if (row.template) return t('snapRestore.reasonTemplate');
    if (row.snaps.length === 0) return t('snapRestore.reasonNoSnapshot');
    return '';
  };

  const restorable = useMemo(
    () => rows.filter((row) => !blockReason(row)),
    // blockReason 只依赖权限 / 模板 / 快照数，这三样都来自 rows 与 hasPermission
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [rows, hasPermission, t],
  );
  const noSnapshotRows = useMemo(
    () =>
      rows.filter(
        (row) =>
          row.snaps.length === 0 &&
          !row.template &&
          hasPermission(row.type === 'lxc' ? 'ct.snapshot' : 'vm.snapshot'),
      ),
    [rows, hasPermission],
  );
  const runningRows = rows.filter((row) => row.status === 'running');
  const missingTarget = restorable.filter((row) => !targets[row.key]);
  const canSubmit = restorable.length > 0 && missingTarget.length === 0 && !busy;

  /** 空白项填最新快照：只是省点击，填完仍要用户逐行核对再提交 */
  const fillLatest = () => {
    setTargets((prev) => {
      const next = { ...prev };
      for (const row of restorable) {
        if (!next[row.key] && row.snaps.length > 0) next[row.key] = row.snaps[0].name;
      }
      return next;
    });
  };

  const buildItems = (list: RestoreRow[]): SnapshotBulkRestoreItem[] =>
    list
      .filter((row) => targets[row.key])
      .map((row) => ({
        node: row.node,
        vmid: row.vmid,
        type: row.type,
        name: targets[row.key],
      }));

  const runRestore = async (items: SnapshotBulkRestoreItem[]) => {
    if (items.length === 0) return;
    setBusy(true);
    try {
      const response = await vmsApi.bulkRestoreSnapshots(items);
      setResult(response);
      /* 还原会删掉目标之后的快照：快照额度与清单都必须跟着重算 */
      invalidateQuota(queryClient);

      if (response.failed === 0) {
        toast.success(
          t('snapRestore.done'),
          t('snapRestore.processed', { ok: response.ok, failed: response.failed }),
        );
      } else if (response.ok > 0) {
        toast.warning(
          t('snapRestore.partial'),
          t('snapRestore.processed', { ok: response.ok, failed: response.failed }),
        );
      } else {
        toast.error(
          t('snapRestore.failed'),
          t('snapRestore.processed', { ok: response.ok, failed: response.failed }),
        );
      }

      freshRef.current = true;
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['snapshots'] }),
        queryClient.invalidateQueries({ queryKey: ['vms'] }),
        queryClient.invalidateQueries({ queryKey: ['lxc'] }),
      ]);
      onDone?.(response);
    } catch (err) {
      toast.error(t('snapRestore.failed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  /** 只重跑失败的那几台：目标快照名在结果里带着，不必回到表里重选 */
  const retryFailed = () => {
    if (!result) return;
    void runRestore(
      result.results
        .filter((item) => !item.ok)
        .map((item) => ({
          node: item.node,
          vmid: item.vmid,
          type: item.type === 'lxc' ? 'lxc' : 'qemu',
          name: item.name,
        })),
    );
  };

  const listSeparator = t('common.listSeparator');
  const namesOf = (list: RestoreRow[]) =>
    list
      .slice(0, 8)
      .map((row) => row.name || `${row.node}/${row.vmid}`)
      .join(listSeparator);

  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onClose}
      size="xl"
      title={t('snapRestore.title')}
      description={
        snapshotsQuery.isLoading
          ? t('snapRestore.subtitleLoading')
          : t('snapRestore.subtitleSummary', {
              total: rows.length,
              withSnaps: rows.filter((row) => row.snaps.length > 0).length,
              selected: restorable.filter((row) => targets[row.key]).length,
            })
      }
      footer={
        <>
          {result ? null : (
            <Button
              variant="ghost"
              disabled={busy || missingTarget.length === 0}
              onClick={fillLatest}
            >
              {t('snapRestore.fillLatest')}
            </Button>
          )}
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {result ? t('common.close') : t('common.cancel')}
          </Button>
          {result ? null : (
            <Button
              variant="danger"
              disabled={!canSubmit}
              loading={busy}
              onClick={() => void runRestore(buildItems(restorable))}
            >
              {t('snapRestore.submit', { n: restorable.length })}
            </Button>
          )}
        </>
      }
    >
      <Notice tone="info" title={t('snapRestore.noticeTitle')}>
        {t('snapRestore.noticeBody')}
      </Notice>

      {/* 勾选里就有没快照的机器：不是错误，但必须让人知道「这几台没得还原」 */}
      {noSnapshotRows.length > 0 ? (
        <Notice
          tone="warning"
          title={t('snapRestore.noSnapshotTitle', { n: noSnapshotRows.length })}
        >
          {noSnapshotRows.length > 8
            ? t('snapRestore.noSnapshotBodyMore', {
                names: namesOf(noSnapshotRows),
                more: noSnapshotRows.length - 8,
              })
            : t('snapRestore.noSnapshotBody', { names: namesOf(noSnapshotRows) })}
        </Notice>
      ) : null}

      {runningRows.length > 0 ? (
        <Notice
          tone="warning"
          title={t('snapRestore.runningTitle', { n: runningRows.length })}
        >
          {t('snapRestore.runningBody')}
        </Notice>
      ) : null}

      {result ? (
        <BulkResultNotice
          result={result}
          retrying={busy}
          onRetryFailed={retryFailed}
          onClose={() => setResult(null)}
        />
      ) : null}

      {snapshotsQuery.isError ? (
        <Notice tone="danger" title={t('snapRestore.loadErrorTitle')}>
          {t('snapRestore.loadErrorMsg')}
          <div className="mt-8">
            <Button size="sm" variant="secondary" onClick={() => void snapshotsQuery.refetch()}>
              {t('common.retry')}
            </Button>
          </div>
        </Notice>
      ) : null}

      {/* 一行一台机器：机器、类型、状态、它自己的快照份数、要回到哪一份 */}
      <div className="bulk-detail-scroll" style={{ maxHeight: 340 }}>
        <table className="bulk-detail-table">
          <caption className="sr-only">{t('snapRestore.tableCaption')}</caption>
          <thead>
            <tr>
              <th scope="col">{t('snapRestore.colGuest')}</th>
              <th scope="col">{t('snapRestore.colType')}</th>
              <th scope="col">{t('snapRestore.colStatus')}</th>
              <th scope="col">{t('snapRestore.colSnapshots')}</th>
              <th scope="col">{t('snapRestore.colTarget')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const reason = blockReason(row);
              const locked = busy || Boolean(result);
              return (
                <tr key={row.key}>
                  <td>
                    <span className="bulk-detail-name fw-500">
                      {row.name || `VM ${row.vmid}`}
                    </span>
                    <span className="fs-xs text-muted mono">
                      {row.vmid} @ {row.node}
                    </span>
                  </td>
                  <td>
                    <Badge variant={row.type === 'lxc' ? 'info' : 'neutral'} size="sm">
                      {row.type === 'lxc' ? 'CT' : 'VM'}
                    </Badge>
                  </td>
                  <td>
                    <Badge
                      variant={row.status === 'running' ? 'success' : 'neutral'}
                      size="sm"
                      dot
                    >
                      {row.status || t('common.unknown')}
                    </Badge>
                  </td>
                  <td>
                    {row.snaps.length === 0 ? (
                      <span className="fs-xs text-danger">{t('snapRestore.noSnapshots')}</span>
                    ) : (
                      <span className="fs-sm text-secondary">
                        {t('snapRestore.snapshotCount', { n: row.snaps.length })}
                      </span>
                    )}
                  </td>
                  <td>
                    {reason ? (
                      <span className="fs-xs text-muted">{reason}</span>
                    ) : (
                      <Select
                        value={targets[row.key] ?? ''}
                        placeholder={t('snapRestore.targetPlaceholder')}
                        disabled={locked}
                        options={row.snaps.map((snap) => ({
                          label: `${snap.name} · ${formatDateTime(snap.snaptime)}`,
                          value: snap.name,
                        }))}
                        onChange={(event) => {
                          const value = event.target.value;
                          setTargets((prev) => ({ ...prev, [row.key]: value }));
                        }}
                        aria-label={t('snapRestore.targetAria', {
                          name: row.name || `${row.node}/${row.vmid}`,
                        })}
                      />
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 选了机器却没指定快照：按钮已经灰了，这里说清「还差哪几台」 */}
      {missingTarget.length > 0 ? (
        <Notice
          tone="warning"
          title={t('snapRestore.missingTitle', { n: missingTarget.length })}
        >
          {missingTarget.length > 8
            ? t('snapRestore.missingBodyMore', {
                names: namesOf(missingTarget),
                more: missingTarget.length - 8,
              })
            : t('snapRestore.missingBody', { names: namesOf(missingTarget) })}
        </Notice>
      ) : null}

      {/* 下拉里按时间倒序，这里说一句「最新在前」，免得用户以为顺序是随机的 */}
      <p className="fs-xs text-muted">{t('snapRestore.footnote')}</p>
    </Modal>
  );
}
