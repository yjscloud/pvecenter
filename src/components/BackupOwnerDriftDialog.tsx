/* ==========================================================================
   pvecenter — 备份归属对账（只读）

   为什么需要单独的界面：`[owner:x]` 在「机器归属优先」之后已经降级成回落依据，
   但 PVE 的 notes 里那串字不会被改派流程碰 —— 存量归档里必然有一批与现状不一致。
   这些差异平时看不见，只在「前任还能下载别人机器的备份」这种时候才暴露出来。

   这里**只列不改**：存量 notes 是历史事实，自动改写会把唯一还能回答「这台机器
   以前归谁」的线索覆盖掉。要不要动，由人看着清单决定。

   列表布局的几个考虑：
     · 归档 volid 很长（`local:backup/xxx-vzdump-qemu-109-....vma.zst`），直接
       铺在窄列里会把每一行都撑成两行。这里只显示文件名、完整串挂到 title 上，
       并把表格改成固定列宽，长名字用省略号收住；
     · 节点/存储、标记、归属三列内容都很短，固定宽度 + 不换行即可；
     · 「缺创建者标记」时标记列是空的，显示成「未标记」而不是一个孤零零的破折号。
   ========================================================================== */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { errorMessage } from '../api/client';
import { backupsApi } from '../api/endpoints';
import type { BackupOwnerDrift, BackupOwnerDriftReport } from '../api/types';
import { useT, type MessageKey } from '../i18n';
import { Badge } from './ui/Badge';
import { Modal } from './ui/Modal';
import { ErrorState, Notice } from './ui/EmptyState';
import { Spinner } from './ui/Spinner';
import { formatDateTime } from '../utils/format';

/** 差异类型 → 徽标色调与词条（后端给的是机器可读的分类） */
const REASON_META: Record<
  BackupOwnerDrift['reason'],
  { tone: 'warning' | 'danger' | 'info'; key: MessageKey }
> = {
  reassigned: { tone: 'warning', key: 'backups.driftReassigned' },
  'machine-unowned': { tone: 'danger', key: 'backups.driftMachineGone' },
  unmarked: { tone: 'info', key: 'backups.driftUnmarked' },
};

/** 后端将来加了新的差异类型时的兜底，别让整行渲染不出来 */
const FALLBACK_META = {
  tone: 'info' as const,
  key: 'backups.driftUnmarked' as MessageKey,
};

/** `local:backup/xxx.vma.zst` → `xxx.vma.zst`：窄列里只放文件名，完整串见 title */
function archiveName(volid: string): string {
  const slash = volid.lastIndexOf('/');
  return slash >= 0 ? volid.slice(slash + 1) : volid;
}

export function BackupOwnerDriftDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const query = useQuery<BackupOwnerDriftReport>({
    queryKey: ['backups', 'owner-drift'],
    queryFn: () => backupsApi.ownerDrift(),
    /* 打开时才扫：这是全集群存储遍历，不能跟着页面轮询一起跑 */
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  const data = query.data;
  const rows = data?.drift ?? [];

  /* 按差异类型汇总：顶部摘要把分布摆出来，不用逐行去数 */
  const counts = useMemo(() => {
    const map = new Map<BackupOwnerDrift['reason'], number>();
    for (const row of rows) {
      map.set(row.reason, (map.get(row.reason) ?? 0) + 1);
    }
    /* 数量多的排前面：扫一眼先看到主要问题 */
    return [...map.entries()].sort((a, b) => b[1] - a[1]);
  }, [rows]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('backups.driftTitle')}
      description={t('backups.driftDesc')}
      /* 五列 + 一列长 volid，860px 的 lg 会把归档名挤到断行；xl 才有余量
         让「归档」这一列完整铺开 */
      size="xl"
    >
      {query.isLoading ? (
        <div className="flex items-center gap-8">
          <Spinner />
          <span className="fs-sm text-secondary">{t('backups.driftLoading')}</span>
        </div>
      ) : query.isError ? (
        <ErrorState
          title={t('backups.driftFailed')}
          message={errorMessage(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : rows.length === 0 ? (
        <Notice tone="success" title={t('backups.driftEmpty')}>
          {t('backups.driftSummary', { checked: data?.checked ?? 0, total: 0 })}
        </Notice>
      ) : (
        <div className="drift-panel">
          <Notice tone="warning" title={t('backups.driftSummaryTitle')}>
            <div>
              {t('backups.driftSummary', {
                checked: data?.checked ?? 0,
                total: data?.total ?? rows.length,
              })}
              {data?.truncated
                ? ` ${t('backups.driftTruncated', { limit: rows.length })}`
                : ''}
            </div>
            {counts.length > 0 ? (
              <div className="drift-reason-chips">
                {counts.map(([reason, count]) => {
                  const meta = REASON_META[reason] ?? FALLBACK_META;
                  return (
                    <Badge key={reason} variant={meta.tone} size="sm" dot>
                      {t(meta.key)} · {count}
                    </Badge>
                  );
                })}
              </div>
            ) : null}
          </Notice>

          <div className="table-container drift-table-wrap">
            <table className="table table-dense drift-table">
              <colgroup>
                <col className="drift-col-reason" />
                <col />
                <col className="drift-col-node" />
                <col className="drift-col-marker" />
                <col className="drift-col-owner" />
              </colgroup>
              <thead>
                <tr>
                  <th>{t('backups.driftColReason')}</th>
                  <th>{t('backups.driftColArchive')}</th>
                  <th>{t('backups.driftColNode')}</th>
                  <th>{t('backups.driftColMarker')}</th>
                  <th>{t('backups.driftColOwner')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const meta = REASON_META[row.reason] ?? FALLBACK_META;
                  return (
                    <tr key={row.volid}>
                      <td>
                        <Badge variant={meta.tone} size="sm" dot>
                          {t(meta.key)}
                        </Badge>
                      </td>
                      <td>
                        <div className="drift-archive">
                          <span
                            className="drift-archive-name mono"
                            title={row.volid}
                          >
                            {archiveName(row.volid)}
                          </span>
                          <span className="drift-archive-meta">
                            {formatDateTime(row.ctime)}
                          </span>
                        </div>
                      </td>
                      <td className="drift-cell">
                        <span className="mono">{row.node}</span>
                        <span className="text-muted"> / {row.storage}</span>
                        {row.vmid ? (
                          <span className="text-muted"> · {row.vmid}</span>
                        ) : null}
                      </td>
                      <td className="drift-cell">
                        {row.marker || (
                          <span className="text-muted">
                            {t('backups.driftNoMarker')}
                          </span>
                        )}
                      </td>
                      <td className="drift-cell">
                        {row.owner || (
                          <span className="text-danger">
                            {t('backups.driftNoOwner')}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Modal>
  );
}
