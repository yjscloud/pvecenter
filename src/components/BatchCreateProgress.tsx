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

   **「读不到任务」不等于「不知道结果」**：机器建没建成，问机器本身就行。任务记录
   被 PVE 清理、连接指错、普通用户的归属判定对不上时，这里回到 ``/vms`` / ``/lxc``
   列表按 vmid 确认一次 —— 在，就是建成（绿色）；确认不在，就是失败（红色）。只有
   连列表都读不到时才保留「待确认」。这条兜底来自一个真实投诉：普通用户批量建了
   两台机器，机器都在列表里跑着，结果页却报「待确认」并配了一条红色进度条。
    ========================================================================== */
import { useEffect, useState } from 'react';
import type {
  BadgeVariant,
  BatchCreateItemResult,
  BatchCreateResponse,
  VmSummary,
} from '../api/types';
import { errorMessage } from '../api/client';
import { lxcApi, vmsApi } from '../api/endpoints';
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

   /** 回查客户机是否真的存在：在 / 不在 / 问不到（读列表本身失败） */
   type Presence = 'present' | 'absent' | 'unknown';

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
  /**
   * 这批任务是在哪条 PVE 连接上发起的。
   *
   * 归属记录带连接前缀，轮询时不带就可能落到「当前生效连接」上 —— 多连接部署里
   * 两者不一致时，普通用户会拿到 404（管理员不受归属限制，所以只有普通用户中招）。
   */
  connectionId?: string;
  /** 用户点「完成」：父组件负责关闭向导 / 刷新列表 */
  onClose: () => void;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 回查客户机是否真的存在。
 *
 * 为什么不能只信任务：``/tasks/{upid}`` 走归属判定，跨节点克隆（任务在源节点、
 * 归属记在目标节点）与多连接部署都可能回 404，而机器其实已经建好。这里改问
 * 列表 ——「这台机器在不在」，比「那条任务记录读不读得到」更接近用户关心的问题。
 *
 * 返回 ``unknown``（列表本身读不到）与 ``absent`` 必须分开：前者不能判失败。
 */
async function confirmPresence(
  item: BatchCreateItemResult,
  connectionId: string | undefined,
): Promise<Presence> {
  /* 克隆刚结束的那几秒，cluster/resources 可能还没刷新出这台机器，多问两轮 */
  const ATTEMPTS = 3;
  for (let attempt = 0; attempt < ATTEMPTS; attempt += 1) {
    if (attempt > 0) await sleep(1_500);
    try {
      const guests = await loadGuests(item, connectionId);
      if (guests.some((guest) => Number(guest.vmid) === Number(item.vmid))) {
        return 'present';
      }
    } catch {
      return 'unknown';
    }
  }
  return 'absent';
}

/**
 * 列表请求的短时缓存：一批 20 台全「读不到任务」时不应该打 60 次列表接口。
 *
 * TTL 比重试间隔（1.5s）短，所以每一轮重试仍会拿到新数据 —— 这里只想合并
 * **同一瞬间**的并发查询，不想掩盖「刚建好的机器还没出现在列表里」。
 */
const GUESTS_TTL_MS = 1_200;
const guestsCache = new Map<string, { at: number; promise: Promise<VmSummary[]> }>();

function loadGuests(
  item: BatchCreateItemResult,
  connectionId: string | undefined,
): Promise<VmSummary[]> {
  const key = `${item.type}|${item.node}|${connectionId ?? ''}`;
  const now = Date.now();
  const hit = guestsCache.get(key);
  if (hit && now - hit.at < GUESTS_TTL_MS) return hit.promise;
  const promise =
    item.type === 'lxc'
      ? lxcApi.list(item.node, { connectionId })
      : vmsApi.list(item.node, { type: 'qemu', connectionId });
  guestsCache.set(key, { at: now, promise });
  return promise;
}

export function BatchCreateProgress({
  response,
  connectionId,
  onClose,
}: BatchCreateProgressProps) {
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

    /**
     * 读不到任务结果时，用「机器在不在」收尾。
     *
     * ``fallbackNote`` 是三种都问不到时的说明（任务被清理 / 连接读不到 / 其它异常），
     * 它必须保留 —— 那时确实只能让人去列表里看。
     */
    const settleByPresence = async (
      item: BatchCreateItemResult,
      fallbackNote: string,
    ) => {
      const presence = await confirmPresence(item, connectionId);
      if (cancelled) return;
      if (presence === 'present') {
        /* 机器在 → 就是建成了。任务记录读不到只是「过程查不到」，不是结果不明 */
        patch(item.index, { phase: 'done', note: t('batchCreate.verifiedNote') });
      } else if (presence === 'absent') {
        patch(item.index, { phase: 'failed', note: t('batchCreate.absentNote') });
      } else {
        patch(item.index, { phase: 'unknown', note: fallbackNote });
      }
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
            connectionId,
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
            await settleByPresence(item, t('batchCreate.unknownNote'));
          }
        } catch (err) {
          if (err instanceof TaskWaitError) {
            /* 超时说明任务还在跑 —— 这时候「机器已出现」不代表克隆做完了，
               保持「待确认」，别把中途状态报成成功 */
            if (err.kind === 'timeout') {
              patch(item.index, {
                phase: 'unknown',
                note: t('batchCreate.timeoutNote'),
              });
              return;
            }
            await settleByPresence(
              item,
              t(
                err.kind === 'missing'
                  ? 'batchCreate.missingNote'
                  : 'batchCreate.unreachableNote',
              ),
            );
          } else {
            /* 认证 / 其它异常同样只代表「读不到终态」，不代表机器没建出来 */
            await settleByPresence(item, errorMessage(err));
          }
        }
      })();
    }

    return () => {
      cancelled = true;
    };
  }, [response, connectionId, t]);

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

  /* 结论的色调只看**结果**，不看比例：
       有失败（含被跳过的）→ 红；只剩「问不到」→ 黄；否则绿。
     全为「待确认」时任务可能仍在跑，不能反过来标成失败（流程还在继续）。 */
  const tone: 'info' | 'success' | 'warning' | 'danger' = !allDone
    ? 'info'
    : failed + skipped > 0
      ? 'danger'
      : unknown > 0
        ? 'warning'
        : 'success';

  /* 进度条颜色同理：它是「这批下发成不成」的结论条，不是容量计量条。
     ProgressBar 默认按百分比取色（usageColor），100% 恰好落在红色档 ——
     一批**全部成功**的机器也会顶着一条红条，正是这里要避免的。
     所以显式给色，且与上面那张结论卡片同一套语义，两处不打架。 */
  const barColor: string =
    tone === 'success'
      ? 'var(--success)'
      : tone === 'danger'
        ? 'var(--danger)'
        : tone === 'warning'
          ? 'var(--warning)'
          : 'var(--accent)';

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
          <ProgressBar
            value={pct}
            color={barColor}
            label={t('batchCreate.progress')}
            showValue
          />
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
