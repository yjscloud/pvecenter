/* ==========================================================================
   pvecenter — 批量下发进度（方案 A）
   ==========================================================================

   后端批量接口是「同步受理 + 逐台 PVE 任务」：返回时每台只有一个 UPID，
   真正的开机关机 / 克隆 / 建盘还在 PVE 任务队列里跑。这里按每台的 task 并行
   轮询 `tasksApi.detail`，把「已受理」推进到最终结果。

   结果判定不能只看 `ok`：
   * `ok` 只表示**创建请求已被受理**，不代表 PVE 任务已经完成；
   * PVE 的终态是 `stopped` + `exitstatus`，其中 `WARNINGS: n` 是「完成但有
     警告」（LXC 创建很常见，容器通常已经建好并在运行），不是失败；
   * 轮询超时 / 网络中断 / 任务记录被清理时，任务可能仍在执行，只能标成
     「待确认」，不能标成失败。

   因此这里区分 6 种行状态：下发中 / 成功 / 有警告 / 失败 / 已跳过 / 待确认。
   顶部汇总再按「全部成功 / 部分成功 / 全部失败」三档给出结论。
   ========================================================================== */
import { useEffect, useState } from 'react';
import type {
  BadgeVariant,
  BatchCreateItemResult,
  BatchCreateResponse,
} from '../api/types';
import { errorMessage } from '../api/client';
import { TaskWaitError, waitForTask } from '../hooks/useTaskRunner';
import { useT, type MessageKey } from '../i18n';
import { classifyTaskExit, taskFailureHint } from '../utils/status';
import { Notice } from './ui/EmptyState';
import { Button } from './ui/Button';
import { Badge } from './ui/Badge';
import { ProgressBar } from './ui/ProgressBar';

type Phase = 'running' | 'done' | 'warning' | 'failed' | 'skipped' | 'unknown';

interface Row extends BatchCreateItemResult {
  phase: Phase;
  note?: string;
}

const PHASE_META: Record<Phase, { variant: BadgeVariant; key: MessageKey }> = {
  running: { variant: 'info', key: 'batchCreate.runningOne' },
  done: { variant: 'success', key: 'batchCreate.doneOne' },
  warning: { variant: 'warning', key: 'batchCreate.warnOne' },
  failed: { variant: 'danger', key: 'batchCreate.failedOne' },
  skipped: { variant: 'neutral', key: 'batchCreate.skippedOne' },
  unknown: { variant: 'warning', key: 'batchCreate.unknownOne' },
};

export interface BatchCreateProgressProps {
  response: BatchCreateResponse;
  /** 用户点「完成」：父组件负责关闭向导 / 刷新列表 */
  onClose: () => void;
}

export function BatchCreateProgress({ response, onClose }: BatchCreateProgressProps) {
  const t = useT();
  const [rows, setRows] = useState<Row[]>(() =>
    response.results.map((item) => {
      /* 跳过 ≠ 失败：前序失败后 stop_on_error 只是「没执行」，不能混进失败数 */
      if (item.skipped) {
        return {
          ...item,
          phase: 'skipped',
          note: item.error || t('batchCreate.skippedNote'),
        };
      }
      if (!item.ok) {
        return {
          ...item,
          phase: 'failed',
          note: item.error || t('bulk.unknownReason'),
        };
      }
      return { ...item, phase: item.task ? 'running' : 'done' };
    }),
  );

  useEffect(() => {
    let cancelled = false;
    const patch = (index: number, next: Partial<Row>) => {
      if (cancelled) return;
      setRows((prev) =>
        prev.map((row) => (row.index === index ? { ...row, ...next } : row)),
      );
    };

    const pending = response.results.filter(
      (item) => item.ok && item.task && !item.skipped,
    );
    for (const item of pending) {
      void (async () => {
        try {
          const task = await waitForTask(String(item.task), item.node, {
            interval: 2_000,
            timeout: 600_000,
          });
          const exit = task.exitstatus ?? '';
          const outcome = classifyTaskExit(task.status, task.exitstatus);

          if (outcome === 'ok') {
            patch(item.index, { phase: 'done', note: undefined });
          } else if (outcome === 'warning') {
            /* WARNINGS: n —— 任务完成、机器通常已就绪，只是 PVE 报了警告 */
            patch(item.index, {
              phase: 'warning',
              note: [exit, t('batchCreate.warnNote')].filter(Boolean).join(' · '),
            });
          } else if (outcome === 'failed') {
            const hint = taskFailureHint(exit, t);
            patch(item.index, {
              phase: 'failed',
              note: [exit || t('task.interrupted'), hint]
                .filter(Boolean)
                .join(' · '),
            });
          } else {
            /* 没拿到可判定的终态：任务可能还在跑，不能标失败 */
            patch(item.index, {
              phase: 'unknown',
              note: t('batchCreate.unknownNote'),
            });
          }
        } catch (err) {
          if (err instanceof TaskWaitError) {
            const key =
              err.kind === 'timeout'
                ? 'batchCreate.timeoutNote'
                : err.kind === 'missing'
                  ? 'batchCreate.missingNote'
                  : 'batchCreate.unreachableNote';
            patch(item.index, { phase: 'unknown', note: t(key) });
          } else {
            /* 认证 / 其它异常同样只代表「读不到终态」，不代表机器没建出来 */
            patch(item.index, {
              phase: 'unknown',
              note: errorMessage(err),
            });
          }
        }
      })();
    }

    return () => {
      cancelled = true;
    };
  }, [response, t]);

  const countOf = (phase: Phase) =>
    rows.filter((row) => row.phase === phase).length;
  const done = countOf('done');
  const warned = countOf('warning');
  const failed = countOf('failed');
  const skipped = countOf('skipped');
  const unknown = countOf('unknown');
  const running = countOf('running');
  const finished = rows.length - running;
  const success = done + warned;
  const allDone = running === 0;

  /* 只有「全部硬失败」才是 danger。全为「待确认」时任务可能仍在跑，
     不能直接把整批标成失败。 */
  const tone: 'info' | 'success' | 'warning' | 'danger' = !allDone
    ? 'info'
    : success === rows.length
      ? 'success'
      : success === 0 && failed > 0 && unknown === 0
        ? 'danger'
        : 'warning';

  let title: string;
  if (!allDone) {
    title = t('batchCreate.summary', {
      done: finished,
      total: rows.length,
      ok: success,
      failed: failed + skipped + unknown,
    });
  } else if (success === rows.length) {
    title =
      warned > 0
        ? t('batchCreate.summaryAllOkWarn', { ok: success, warn: warned })
        : t('batchCreate.summaryAllOk', { ok: success });
  } else if (success === 0 && failed > 0 && unknown === 0) {
    title =
      skipped > 0
        ? t('batchCreate.summaryFailedSkipped', { failed, skipped })
        : t('batchCreate.summaryFailed', { failed });
  } else if (success === 0 && failed === 0) {
    title = t('batchCreate.summaryUnknown', { unknown });
  } else if (success === 0) {
    title = t('batchCreate.summaryFailedUnknown', { failed, unknown });
  } else {
    title = t('batchCreate.summaryPartial', { ok: success, failed });
  }

  const pct = rows.length ? (finished / rows.length) * 100 : 100;

  return (
    <div className="wizard-body">
      <Notice
        tone={tone}
        title={title}
        action={
          <Button size="sm" variant="secondary" onClick={onClose}>
            {t('batchCreate.close')}
          </Button>
        }
      >
        <div className="flex flex-col gap-8">
          <ProgressBar value={pct} label={t('batchCreate.progress')} showValue />
          {allDone ? (
            <div className="fs-sm text-secondary">
              {t('batchCreate.counts', {
                ok: success,
                warn: warned,
                failed,
                skipped,
                unknown,
              })}
            </div>
          ) : (
            <div className="fs-sm text-secondary">
              {t('batchCreate.pendingHint')}
            </div>
          )}
        </div>
      </Notice>

      <div className="bulk-detail-scroll">
        <table className="bulk-detail-table">
          <caption className="sr-only">{t('batchCreate.title')}</caption>
          <thead>
            <tr>
              <th scope="col">{t('bulk.colTarget')}</th>
              <th scope="col">{t('bulk.colName')}</th>
              <th scope="col">{t('bulk.colResult')}</th>
              <th scope="col">{t('bulk.colNote')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const meta = PHASE_META[row.phase];
              return (
                <tr key={`${row.node}-${row.vmid}-${row.index}`}>
                  <td className="mono fs-sm">
                    {row.node}/{row.vmid || '—'}
                  </td>
                  <td className="fs-sm">
                    <span className="truncate bulk-detail-name">
                      {row.name || '—'}
                    </span>
                  </td>
                  <td>
                    <Badge variant={meta.variant} size="sm" dot>
                      {t(meta.key)}
                    </Badge>
                  </td>
                  <td className="fs-xs text-muted bulk-detail-error">
                    {row.note || row.disk_note || '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
