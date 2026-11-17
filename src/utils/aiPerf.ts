/* ==========================================================================
   pvecenter — AI 一轮对话的前端秒表

   后端那套 ``aiperf`` 说的是「服务端各自花了多久」，但用户感知到的「慢」有一半
   在浏览器里：整页重渲染、长任务抢占主线程、收尾全量回拉。这些只有前端自己量。

   三个时间点足够定位绝大多数问题：

   * ``send → first-token``  首字。它慢时先怀疑排队与 prompt 体积，不是渲染；
   * ``send → done``         整轮。与首字之差就是「生成 + 工具 + 收尾」；
   * ``longtask``            一轮里超过 50ms 的主线程任务数与最长值。它不为 0，
                             用户就会看到打字机「一顿一顿」—— 这条与后端无关。

   不往控制台打日志：正常使用的人不需要看到这些。结果留在 ``window.__aiPerf()``
   里（最近 20 轮），排查时在控制台敲一下就能看；接 RUM 时把这个返回值上报即可。
   ========================================================================== */

export type AiTurnOutcome = 'done' | 'error' | 'aborted';

/** 一轮的度量结果（毫秒）。缺的点用 0 表示，不做假数据。 */
export interface AiTurnPerf {
  mode: string;
  /** 发送 → 首个 token */
  ttfbMs: number;
  /** 发送 → done */
  totalMs: number;
  /** 一轮里 >50ms 的长任务数 */
  longTasks: number;
  /** 最长的一次长任务 */
  longestTaskMs: number;
  outcome: AiTurnOutcome | 'running';
}

interface ActiveTurn {
  mode: string;
  startedAt: number;
  firstTokenAt: number;
  longTasks: number;
  longestTaskMs: number;
}

/** 只留最近这么多轮：它是排查用的环，不是统计报表。 */
const KEEP = 20;
const turns: AiTurnPerf[] = [];
let active: ActiveTurn | null = null;
let observer: PerformanceObserver | null = null;

function ensureObserver(): void {
  if (observer || typeof PerformanceObserver === 'undefined') return;
  try {
    observer = new PerformanceObserver((list) => {
      if (!active) return;
      for (const entry of list.getEntries()) {
        active.longTasks += 1;
        active.longestTaskMs = Math.max(active.longestTaskMs, entry.duration);
      }
    });
    // longtask 只有 Chromium 系支持；不支持就只留时间点，属于预期内降级
    observer.observe({ entryTypes: ['longtask'] });
  } catch {
    observer = null;
  }
}

/** 开始一轮：在发请求**之前**调用。上一轮还没收口时按 aborted 收掉。 */
export function startTurn(mode: string): void {
  finishTurn('aborted');
  ensureObserver();
  active = {
    mode,
    startedAt: performance.now(),
    firstTokenAt: 0,
    longTasks: 0,
    longestTaskMs: 0,
  };
}

/** 首个 token 到达。只在第一次生效：后面的增量不该改写首字。 */
export function firstToken(): void {
  if (!active || active.firstTokenAt) return;
  active.firstTokenAt = performance.now();
}

/** 收口。失败与终止也照收 —— 慢在哪一步与结果无关。 */
export function finishTurn(outcome: AiTurnOutcome): void {
  const turn = active;
  if (!turn) return;
  active = null;
  const finishedAt = performance.now();
  turns.push({
    mode: turn.mode,
    ttfbMs: turn.firstTokenAt
      ? Math.round(turn.firstTokenAt - turn.startedAt)
      : 0,
    totalMs: Math.round(finishedAt - turn.startedAt),
    longTasks: turn.longTasks,
    longestTaskMs: Math.round(turn.longestTaskMs),
    outcome,
  });
  if (turns.length > KEEP) turns.splice(0, turns.length - KEEP);
}

/** 最近若干轮的只读快照。 */
export function recentTurns(): AiTurnPerf[] {
  return turns.map((item) => ({ ...item }));
}

/** 正在跑的那一轮（还没收口）；没有就返回 null。 */
export function currentTurn(): AiTurnPerf | null {
  if (!active) return null;
  return {
    mode: active.mode,
    ttfbMs: active.firstTokenAt
      ? Math.round(active.firstTokenAt - active.startedAt)
      : 0,
    totalMs: Math.round(performance.now() - active.startedAt),
    longTasks: active.longTasks,
    longestTaskMs: Math.round(active.longestTaskMs),
    outcome: 'running',
  };
}

declare global {
  interface Window {
    /** 控制台排查入口：``__aiPerf()`` 看最近 20 轮的首字/整轮/长任务 */
    __aiPerf?: () => AiTurnPerf[];
  }
}

if (typeof window !== 'undefined') {
  window.__aiPerf = recentTurns;
}
