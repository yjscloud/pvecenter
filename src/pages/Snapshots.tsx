/* ==========================================================================
   pvecenter — 全局快照视图
   ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { lxcApi, vmsApi } from '../api/endpoints';
import { guestPath } from '../api/guests';
import { errorMessage } from '../api/client';
import { PageShell } from '../components/Layout';
import { Card } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button, IconButton } from '../components/ui/Button';
import { Checkbox, Input, Select } from '../components/ui/Input';
import { Table, type Column } from '../components/ui/Table';
import { Notice } from '../components/ui/EmptyState';
import { PveQueryError } from '../components/SetupRequired';
import { BulkResultNotice } from '../components/BulkResultNotice';
import { usePveReady } from '../hooks/usePveStatus';
import { PagerBar, usePaged } from '../hooks/usePaged';
import { useRefreshInterval } from '../hooks/useRefreshInterval';
import { useToast } from '../hooks/useToast';
import { invalidateQuota } from '../hooks/quotaCache';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import {
  IconSnapshot,
  IconRefresh,
  IconTrash,
  IconSearch,
  IconRestart,
} from '../components/Icons';
import { formatDateTime, formatRelative } from '../utils/format';
import { useTaskRunner } from '../hooks/useTaskRunner';
import { useAuth } from '../hooks/useAuth';
import { useT } from '../i18n';
import type { BulkResponse, Snapshot } from '../api/types';

/* ---------------------------------------------------------------------------
   页面
   --------------------------------------------------------------------------- */

/**
 * 同一台机器上可能重名（容器与虚拟机各自一套命名空间），所以 key 里要带类型。
 * 节点也要带：不同节点上的 vmid 会重号。
 *
 * 类型统一归一成 ``qemu`` / ``lxc``：批量删除的逐条结果里后端给的是这两个值，
 * 归一之后「前端记的行」与「结果里的行」才能对得上（否则失败的那条会一直被
 * 当成「还在删」藏着）。
 */
function snapshotKey(s: Snapshot): string {
  /* 连接进 key：两套集群可以有同节点、同 VMID、同快照名的一行 */
  return `${s.connection_id ?? ''}|${s.node}|${s.vmid}|${
    s.vm_type === 'lxc' ? 'lxc' : 'qemu'
  }|${s.name}`;
}

export function Snapshots() {
  const t = useT();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const runner = useTaskRunner();
  const { hasPermission } = useAuth();
  /* 回滚 / 删除快照按**目标类型**判权限：虚拟机 vm.snapshot、容器 ct.snapshot
     （后端 vms.py / lxc.py 就是两套），不再用「是不是非只读用户」的角色粗判。 */
  const canSnapshotOf = (s: Snapshot) =>
    hasPermission(s.vm_type === 'lxc' ? 'ct.snapshot' : 'vm.snapshot');

  const toast = useToast();
  const [search, setSearch] = useState('');
  const [nodeFilter, setNodeFilter] = useState('');
  const [rollbackTarget, setRollbackTarget] = useState<Snapshot | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Snapshot | null>(null);
  const [busy, setBusy] = useState(false);
  /**
   * 正在删除的快照（`节点|vmid|类型|名称`）。
   *
   * PVE 的删除是异步任务：任务一提交 ``useTaskRunner`` 就会先刷一次列表，而那一刻
   * 快照还在 —— 用户确认删除后行仍留在原地，要再点一次才「生效」。这里先把这些
   * 行从渲染数据里摘掉，任务运行期间无论刷新多少次都不会把它放回来；失败时再放开，
   * 它会随真实数据回来（同时有失败 toast），界面始终与 PVE 一致。
   *
   * 单条与批量可能同时在跑（批量提交后「还在删」的那段窗口里，单条按钮又可以点），
   * 所以真正的记账在 ref 里：任何一处增删都整份同步给上面的渲染列表，避免两边各自
   * ``filter`` 时把对方藏着的行放出来。
   */
  const [deleting, setDeleting] = useState<string[]>([]);
  const deletingRef = useRef<Set<string>>(new Set());
  const syncDeleting = () => setDeleting([...deletingRef.current]);

  /* 批量删除的勾选与结果（见表格下方的选择列 / 操作条） */
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkResult, setBulkResult] = useState<BulkResponse | null>(null);

  /* 未配置连接时后端对每个请求都回 428，轮询只会刷一屏没人看的报错
     （配好之后自动恢复） */
  const pveReady = usePveReady();

  /* 虚拟机列表 */
  const vmsQuery = useQuery({
    queryKey: ['vms', 'all'],
    queryFn: () => vmsApi.list(),
    staleTime: 30_000,
  });

  const vms = useMemo(() => vmsQuery.data ?? [], [vmsQuery.data]);

  const nodeOptions = useMemo(() => {
    const set = new Set(vms.map((v) => v.node));
    return [
      { label: t('snapshots.allNodes'), value: '' },
      ...[...set].sort().map((n) => ({ label: n, value: n })),
    ];
  }, [vms]);


  /* 一次向后端要**全量**快照。原先是「前端按 VM 逐台发请求」（并发 6），
     200 台机器就是 200 个浏览器请求，开两个标签页翻倍，而且每台都要等 PVE
     扫一遍快照元数据。后端 ``/api/snapshots`` 已经把这几件事一起做了：
     服务端并发（上限可配）+ 归属过滤 + 60 秒缓存，且变更路径会主动作废。
     轮询间隔仍跟「设置 → 面板设置 → 数据刷新间隔」走。 */
  const refresh = useRefreshInterval();
  /**
   * 下一次取数是否要**绕过服务端缓存**。
   *
   * ``/api/snapshots`` 有 60 秒缓存，而删除快照是 PVE 的异步任务：任务刚受理时
   * 那一刷会把「删除前」的列表写进缓存，任务结束后再刷就命中这份旧数据 ——
   * 列表里那条已经删掉的快照会一直留到缓存过期，用户以为没删掉，只能再点一次。
   * 所以删除完成后这一刷必须带 ``fresh=1``。
   */
  const freshRef = useRef(false);
  const snapshotsQuery = useQuery({
    queryKey: ['snapshots', 'all'],
    queryFn: async () => {
      const fresh = freshRef.current;
      freshRef.current = false;
      const rows = await vmsApi.allSnapshots({ fresh });
      // 后端字段叫 vm_name，页面与列定义里用的是 vmname，这里对齐一次
      return rows.map<Snapshot>((s) => ({ ...s, vmname: s.vmname ?? s.vm_name }));
    },
    enabled: pveReady,
    refetchInterval: pveReady ? refresh : false,
    retry: false,
  });

  /* 过滤 */
  const snapshots = useMemo(() => {
    const all = snapshotsQuery.data ?? [];
    // 节点筛选在聚合结果上做：接口一次给全量，切节点不必重新请求
    const list = nodeFilter ? all.filter((s) => s.node === nodeFilter) : all;
    const q = search.trim().toLowerCase();
    const filtered = q
      ? list.filter(
          (s) =>
            s.name.toLowerCase().includes(q) ||
            (s.description ?? '').toLowerCase().includes(q) ||
            (s.vmname ?? '').toLowerCase().includes(q) ||
            String(s.vmid ?? '').includes(q),
        )
      : list;
    // 已确认删除、任务还在跑的那几行：先不显示（见 deleting 的说明）
    const visible = deleting.length
      ? filtered.filter((s) => !deleting.includes(snapshotKey(s)))
      : filtered;

    return [...visible].sort(
      (a, b) => (b.snaptime ?? 0) - (a.snaptime ?? 0),
    );
  }, [snapshotsQuery.data, search, nodeFilter, deleting]);

  /* 快照会越攒越多（每台机器若干条），和其它列表一样分页。
     切片放在上面那份「已过滤 + 已按时间排序」的列表之后，翻到哪一页都是
     最新的排在前面；搜索词一变就回到第 1 页（resetKey）。 */
  const snapPage = usePaged(snapshots, search);

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['snapshots'] });
    void queryClient.invalidateQueries({ queryKey: ['vms'] });
  };

  /* ---- 批量删除 ---- */
  /* 只有当前用户**能删**的行才给勾：勾上一批、点下去才被告知其中几条无权删除，
     不如从一开始就不让勾。 */
  const deletable = useMemo(
    () =>
      snapshots.filter((s) =>
        hasPermission(s.vm_type === 'lxc' ? 'ct.snapshot' : 'vm.snapshot'),
      ),
    [snapshots, hasPermission],
  );

  const allSelected =
    deletable.length > 0 && deletable.every((s) => selected.has(snapshotKey(s)));
  const someSelected = deletable.some((s) => selected.has(snapshotKey(s)));

  const toggleAll = () => {
    setSelected(
      allSelected ? new Set() : new Set(deletable.map(snapshotKey)),
    );
  };

  const toggleOne = (s: Snapshot) => {
    const key = snapshotKey(s);
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  /* 勾选项按列表顺序展开成快照本身：提交时要 node/vmid/type/name */
  const selectedRows = useMemo(
    () => deletable.filter((s) => selected.has(snapshotKey(s))),
    [deletable, selected],
  );

  /* 列表刷新 / 筛选变化后清掉已不在列表里的勾选：否则「已选 3」但一条都看不到，
     点删除时删的还是旧对象 */
  useEffect(() => {
    const valid = new Set(deletable.map(snapshotKey));
    setSelected((prev) => {
      const next = new Set([...prev].filter((key) => valid.has(key)));
      return next.size === prev.size ? prev : next;
    });
  }, [deletable]);

  /** 把「已受理、PVE 还在删」的行继续藏着，直到列表里真的没有它们。 */
  const holdDeleted = async (keys: string[]) => {
    const pending = new Set(keys);
    const deadline = Date.now() + 180_000;
    while (pending.size > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 5_000));
      try {
        // 不逐条轮询任务：50 个快照就是 50 个任务、每秒 50 次请求，很容易撞上
        // 每 IP 的限流。这里只重拉**整份**快照列表（后端有缓存，删除任务结束时
        // 会主动作废），既能拿到最终结果，代价也小得多。
        await snapshotsQuery.refetch();
      } catch {
        continue;
      }
      const rows =
        queryClient.getQueryData<Snapshot[]>(['snapshots', 'all']) ?? [];
      const present = new Set(rows.map(snapshotKey));
      let changed = false;
      for (const key of [...pending]) {
        if (present.has(key)) continue;
        // 只撤这一批自己的标记：同一时间还可能有单条删除在跑
        pending.delete(key);
        deletingRef.current.delete(key);
        changed = true;
      }
      if (changed) syncDeleting();
    }
    for (const key of keys) deletingRef.current.delete(key);
    if (pending.size > 0) {
      // 到点还没删完（或 PVE 任务失败）：放开标记，让真实列表说话
      toast.warning(
        t('snapshots.bulkDeletePending'),
        t('snapshots.bulkDeletePendingBody', { count: pending.size }),
      );
    }
    syncDeleting();
  };

  /**
   * 批量删除一批快照。
   *
   * 走一次后端批量接口逐条受理，而不是前端循环发 N 个单条删除：快照级归属只在
   * 服务端判，N 次往返还容易撞上限流，中途失败也说不清到底删掉了几条。
   * 确认后这一批先**从渲染里摘掉**（与单条删除同一口径）：PVE 的删除是异步任务，
   * 等任务跑完才消失的话，用户盯着屏幕那几秒会以为没删掉。
   */
  const runBulkDelete = async (rows: Snapshot[]) => {
    const targets = rows.filter((s) => s.node && s.vmid);
    if (targets.length === 0) return;

    const keys = targets.map(snapshotKey);
    let accepted = keys;
    setBusy(true);
    for (const key of keys) deletingRef.current.add(key);
    syncDeleting();
    try {
      const result = await vmsApi.bulkDeleteSnapshots(
        targets.map((s) => ({
          node: s.node as string,
          vmid: s.vmid as number,
          // 容器与虚拟机是两套后端端点与权限点，类型必须带
          type: s.vm_type === 'lxc' ? 'lxc' : 'qemu',
          name: s.name,
          // 全局视图合并了多套集群：不带连接会删到另一台同名节点上的同名快照
          connection_id: s.connection_id,
        })),
      );
      setBulkResult(result);
      /* 快照额度按「名下快照条数」记账：这批删除已经落库（归属标记随请求一起清），
         额度读数必须跟着失效，否则「删了快照额度没还回来」。批量不走 runner，
         得在这里显式作废一次。 */
      invalidateQuota(queryClient);

      /* 没受理的（无权限 / 已被删掉 / PVE 报错）放回列表并留在勾选里，可以直接
         重试；受理成功的继续藏到 PVE 真的删完。 */
      const failed = new Set(
        result.results
          .filter((r) => !r.ok)
          .map((r) => `${r.node}|${r.vmid}|${r.type}|${r.name}`),
      );
      accepted = keys.filter((key) => !failed.has(key));
      for (const key of keys) {
        if (failed.has(key)) deletingRef.current.delete(key);
      }
      syncDeleting();
      setSelected(new Set(keys.filter((key) => failed.has(key))));

      if (result.failed === 0) {
        toast.destructive(
          t('snapshots.bulkDeleteDone'),
          t('snapshots.bulkDeleteProcessed', {
            ok: result.ok,
            failed: result.failed,
          }),
        );
      } else {
        toast.warning(
          t('snapshots.bulkDeletePartial'),
          t('snapshots.bulkDeleteProcessed', {
            ok: result.ok,
            failed: result.failed,
          }),
        );
      }
    } catch (err) {
      for (const key of keys) deletingRef.current.delete(key);
      syncDeleting();
      toast.error(t('snapshots.bulkDeleteFailed'), errorMessage(err));
      return;
    } finally {
      setBusy(false);
    }

    // 受理成功的那批等 PVE 任务结束再放开（进度可在任务队列里看）
    await holdDeleted(accepted);
    invalidate();
  };

  const retryBulkDelete = () => {
    void runBulkDelete(selectedRows);
  };

  /* ---- 表格列 ---- */
  const columns: Array<Column<Snapshot>> = [
    {
      /* 批量删除用的勾选列：锁定（不可隐藏、不参与拖动排序），表头是全选框 */
      key: 'select',
      label: t('snapshots.colSelect'),
      locked: true,
      header: (
        <Checkbox
          checked={allSelected}
          indeterminate={!allSelected && someSelected}
          onChange={toggleAll}
          disabled={deletable.length === 0}
          aria-label={t('snapshots.selectAll')}
        />
      ),
      width: 44,
      render: (s) => (
        <span onClick={(e) => e.stopPropagation()}>
          <Checkbox
            checked={selected.has(snapshotKey(s))}
            onChange={() => toggleOne(s)}
            /* 没权限删的行不给勾：勾完点下去只会得到一条「无权删除」 */
            disabled={!canSnapshotOf(s)}
            aria-label={t('snapshots.selectOne', { name: s.name })}
          />
        </span>
      ),
    },
    {
      key: 'vm',
      header: t('common.vm'),
      render: (s) => (
        <button
          type="button"
          className="link-button"
          onClick={(e) => {
            e.stopPropagation();
            if (s.node && s.vmid) {
              // 容器快照要跳到容器页：虚拟机页拿不到 /lxc 的配置
              navigate(
                guestPath(
                  {
                    node: s.node,
                    vmid: s.vmid,
                    type: s.vm_type,
                    connection_id: s.connection_id,
                  },
                  { tab: 'snapshots' },
                ),
              );
            }
          }}
        >
          <span className="fw-500">{s.vmname || `VM ${s.vmid}`}</span>
          <span className="fs-xs text-muted mono">
            {s.vmid} @ {s.node}
          </span>
        </button>
      ),
      sortable: true,
      sortValue: (s) => s.vmname ?? '',
    },
    {
      key: 'name',
      header: t('snapshots.colName'),
      render: (s) => <span className="mono fw-500">{s.name}</span>,
      sortable: true,
      sortValue: (s) => s.name,
    },
    {
      key: 'description',
      header: t('common.description'),
      render: (s) => (
        <span className="fs-sm text-secondary truncate" title={s.description}>
          {s.description || '—'}
        </span>
      ),
    },
    {
      key: 'snaptime',
      header: t('common.createdAt'),
      render: (s) => (
        <div className="flex flex-col">
          <span className="mono fs-sm">{formatDateTime(s.snaptime)}</span>
          <span className="fs-xs text-muted">{formatRelative(s.snaptime)}</span>
        </div>
      ),
      width: 180,
      sortable: true,
      sortValue: (s) => s.snaptime ?? 0,
    },
    {
      key: 'vmstate',
      header: t('snapshots.colVmstate'),
      width: 90,
      align: 'center',
      render: (s) => (
        <Badge variant={s.vmstate ? 'info' : 'neutral'} size="sm">
          {s.vmstate ? t('common.yes') : t('common.no')}
        </Badge>
      ),
    },
    {
      key: 'parent',
      header: t('snapshots.colParent'),
      render: (s) => (
        <span className="mono fs-sm text-muted">{s.parent || '—'}</span>
      ),
      width: 140,
    },
    {
      key: 'owner',
      header: t('common.owner'),
      width: 90,
      render: (s) => <span className="fs-sm text-secondary">{s.owner || '—'}</span>,
    },
    {
      key: 'actions',
      header: t('common.actions'),
      width: 160,
      align: 'right',
      render: (s) => (
        <span className="row-actions">
          <IconButton
            label={t('snapshots.rollbackLabel', { name: s.name })}
            variant="primary"
            onClick={(e) => {
              e.stopPropagation();
              setRollbackTarget(s);
            }}
            disabled={!canSnapshotOf(s) || busy}
          >
            <IconRestart size={15} />
          </IconButton>
          <IconButton
            label={t('snapshots.deleteLabel', { name: s.name })}
            variant="danger"
            onClick={(e) => {
              e.stopPropagation();
              setDeleteTarget(s);
            }}
            disabled={!canSnapshotOf(s) || busy}
          >
            <IconTrash size={15} />
          </IconButton>
        </span>
      ),
    },
  ];

  /* 统计 */
  const stats = useMemo(() => {
    const list = snapshotsQuery.data ?? [];
    const withState = list.filter((s) => s.vmstate).length;
    const vmsWithSnapshots = new Set(list.map((s) => `${s.node}/${s.vmid}`)).size;
    return { total: list.length, withState, vmsWithSnapshots };
  }, [snapshotsQuery.data]);

  return (
    <PageShell
      title={
        <>
          <IconSnapshot size={20} />
          {t('snapshots.title')}
        </>
      }
      subtitle={
        snapshotsQuery.isLoading
          ? t('snapshots.subtitle.loading')
          : t('snapshots.subtitle.summary', {
              total: stats.total,
              vms: stats.vmsWithSnapshots,
            })
      }
      actions={
        <Button
          variant="secondary"
          icon={<IconRefresh size={15} />}
          onClick={() => {
            void vmsQuery.refetch();
            void snapshotsQuery.refetch();
          }}
          loading={snapshotsQuery.isFetching && !snapshotsQuery.isLoading}
        >
          {t('common.refresh')}
        </Button>
      }
    >
      <Notice tone="info" title={t('snapshots.noticeTitle')}>
        {t('snapshots.noticeBody')}
      </Notice>

      {/* 统计卡片 */}
      <div className="grid grid-3">
        <Card>
          <div className="kpi-label">{t('snapshots.kpi.total')}</div>
          <div className="kpi-value">{stats.total}</div>
        </Card>
        <Card>
          <div className="kpi-label">{t('snapshots.kpi.withState')}</div>
          <div className="kpi-value">{stats.withState}</div>
        </Card>
        <Card>
          <div className="kpi-label">{t('snapshots.kpi.vms')}</div>
          <div className="kpi-value">{stats.vmsWithSnapshots}</div>
        </Card>
      </div>

      {/* 筛选 */}
      <div className="toolbar">
        <div className="toolbar-left">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('snapshots.searchPlaceholder')}
            prefix={<IconSearch size={15} />}
            block={false}
            aria-label={t('snapshots.searchAria')}
          />
          <Select
            value={nodeFilter}
            onChange={(e) => setNodeFilter(e.target.value)}
            options={nodeOptions}
            aria-label={t('snapshots.filterNodeAria')}
          />
        </div>
        <div className="toolbar-right">
          <span className="fs-sm text-muted">
            {t('common.showing', { n: snapshots.length })}
          </span>
        </div>
      </div>

      {/* ---- 批量删除结果回报 ---- */}
      {bulkResult ? (
        <BulkResultNotice
          result={bulkResult}
          retrying={busy}
          onRetryFailed={retryBulkDelete}
          onClose={() => setBulkResult(null)}
        />
      ) : null}

      {/* ---- 批量操作条 ---- */}
      {selectedRows.length > 0 ? (
        <div className="bulk-bar">
          <span>
            {t('snapshots.bulkSelectedPre')}
            <span className="bulk-bar-count">{selectedRows.length}</span>{' '}
            {t('snapshots.bulkSelectedPost')}
          </span>
          <div className="flex items-center gap-8 flex-wrap">
            <Button
              variant="danger"
              icon={<IconTrash size={15} />}
              disabled={busy}
              onClick={() => setBulkDeleteOpen(true)}
            >
              {t('snapshots.bulkDeleteAction')}
            </Button>
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => setSelected(new Set())}
            >
              {t('snapshots.bulkClear')}
            </Button>
          </div>
        </div>
      ) : null}

      {snapshotsQuery.isError ? (
        <PveQueryError
          title={t('snapshots.loadErrorTitle')}
          message={t('snapshots.loadErrorMsg')}
          error={snapshotsQuery.error}
          onRetry={() => void snapshotsQuery.refetch()}
        />
      ) : (
        <Table<Snapshot>
          columns={columns}
          rows={snapPage.rows}
          rowKey={(s) => `${s.node}/${s.vmid}/${s.name}`}
          loading={snapshotsQuery.isLoading}
          caption={t('snapshots.caption')}
          emptyTitle={search ? t('snapshots.emptyNoMatch') : t('snapshots.emptyNone')}
          emptyDescription={
            search
              ? t('snapshots.emptyAdjust')
              : vms.length === 0
                ? t('snapshots.emptyNoVms')
                : t('snapshots.emptyHint')
          }
          emptyAction={
            !search && vms.length > 0 ? (
              <Button variant="primary" onClick={() => navigate('/vms')}>
                {t('snapshots.goToVms')}
              </Button>
            ) : undefined
          }
        />
      )}

      {/* 分页条接在表格下沿，成为同一张卡的页脚（见 .pagination 的样式）。
          出错时不出分页 —— 那会儿没有列表可翻 */}
      {!snapshotsQuery.isError ? <PagerBar pager={snapPage} /> : null}

      {/* ---- 回滚确认 ---- */}
      <ConfirmDialog
        open={Boolean(rollbackTarget)}
        onCancel={() => setRollbackTarget(null)}
        onConfirm={async () => {
          if (!rollbackTarget?.node || !rollbackTarget.vmid) return;
          setBusy(true);
          try {
            /* 容器与虚拟机是两套端点，按行里的类型分派 */
            const isLxcRow = rollbackTarget.vm_type === 'lxc';
            const api = isLxcRow ? lxcApi : vmsApi;
            await runner.run(
              api.rollbackSnapshot(
                rollbackTarget.node,
                rollbackTarget.vmid,
                rollbackTarget.name,
              ),
              {
                title: t('snapshots.taskRollback', {
                  vm: rollbackTarget.vmname || rollbackTarget.vmid,
                  name: rollbackTarget.name,
                }),
                node: rollbackTarget.node,
                invalidate: [
                  ['snapshots'],
                  [isLxcRow ? 'lxc' : 'vm', rollbackTarget.node, rollbackTarget.vmid],
                  [isLxcRow ? 'lxc' : 'vms'],
                ],
                destructive: true,
              },
            );
            setRollbackTarget(null);
            invalidate();
          } finally {
            setBusy(false);
          }
        }}
        title={t('snapshots.rollbackTitle')}
        confirmText={t('snapshots.rollbackConfirm')}
        loading={busy}
        requireText={rollbackTarget?.name}
        message={t('snapshots.rollbackMessage', {
          vm: rollbackTarget?.vmname || rollbackTarget?.vmid || '',
          name: rollbackTarget?.name || '',
          time: formatDateTime(rollbackTarget?.snaptime),
        })}
      />

      {/* ---- 删除确认 ---- */}
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={async () => {
          const target = deleteTarget;
          if (!target?.node || !target.vmid) return;
          const key = snapshotKey(target);
          const isLxcRow = target.vm_type === 'lxc';
          /* 确认即收工：对话框、这一行、缓存里的记录同时撤掉，任务进度交给任务
             提示条。等 PVE 任务跑完才关对话框的话，用户盯着转圈的那几秒恰好看
             不到列表变化，会以为没删掉，于是再点一次。
             另外，任务一提交 runner 就会先刷一次列表（那时快照还在），所以这里
             不只是「隐藏」——缓存里的那一行也一并删掉。 */
          setDeleteTarget(null);
          deletingRef.current.add(key);
          syncDeleting();
          queryClient.setQueryData<Snapshot[]>(['snapshots', 'all'], (rows) =>
            rows ? rows.filter((s) => snapshotKey(s) !== key) : rows,
          );
          setBusy(true);
          try {
            const api = isLxcRow ? lxcApi : vmsApi;
            await runner.run(
              api.deleteSnapshot(target.node, target.vmid, target.name),
              {
                title: t('snapshots.taskDelete', { name: target.name }),
                node: target.node,
                invalidate: [
                  ['snapshots'],
                  [isLxcRow ? 'lxc' : 'vm', target.node, target.vmid],
                ],
                destructive: true,
              },
            );
            /* 等这次真刷新落地再撤掉标记：先撤再刷新的话，中间那一瞬
               会把刚删掉的行闪回列表（看起来还是「没删掉」）。
               这一刷必须绕过服务端 60 秒缓存 —— 缓存里存的是「删除前」的列表，
               命中它就等于把刚删掉的行原样拿回来。 */
            freshRef.current = true;
            await snapshotsQuery.refetch();
            invalidate();
          } finally {
            deletingRef.current.delete(key);
            syncDeleting();
            setBusy(false);
          }
        }}
        title={t('snapshots.deleteTitle')}
        danger
        confirmText={t('common.delete')}
        loading={busy}
        message={t('snapshots.deleteMessage', {
          vm: deleteTarget?.vmname || deleteTarget?.vmid || '',
          name: deleteTarget?.name || '',
        })}
      />

      {/* ---- 批量删除确认 ---- */}
      <ConfirmDialog
        open={bulkDeleteOpen}
        onCancel={() => setBulkDeleteOpen(false)}
        onConfirm={() => {
          /* 确认即收工：对话框先关，逐条结果交给页面上方的结果条与任务队列。
             等整批任务跑完才返回的话，用户会盯着一个关不掉的确认框看几分钟。 */
          setBulkDeleteOpen(false);
          void runBulkDelete(selectedRows);
        }}
        title={t('snapshots.bulkDeleteTitle')}
        danger
        confirmText={t('snapshots.bulkDeleteConfirm', {
          count: selectedRows.length,
        })}
        loading={busy}
        message={t('snapshots.bulkDeleteMessage', {
          count: selectedRows.length,
          vms: new Set(selectedRows.map((s) => `${s.node}/${s.vmid}`)).size,
        })}
      >
        {selectedRows.length > 0 ? (
          <div className="bulk-detail-scroll">
            <table className="bulk-detail-table">
              <caption className="sr-only">
                {t('snapshots.bulkDeleteListCaption')}
              </caption>
              <thead>
                <tr>
                  <th scope="col">{t('snapshots.colName')}</th>
                  <th scope="col">{t('common.createdAt')}</th>
                </tr>
              </thead>
              <tbody>
                {selectedRows.map((s) => (
                  <tr key={snapshotKey(s)}>
                    <td className="fs-sm">
                      <span className="mono">{s.name}</span>
                      <span className="fs-xs text-muted">
                        {' · '}
                        {s.vmname || `VM ${s.vmid}`} ({s.node}/{s.vmid})
                      </span>
                    </td>
                    <td className="mono fs-xs text-muted">
                      {formatDateTime(s.snaptime)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </ConfirmDialog>

    </PageShell>
  );
}
