/* ==========================================================================
   pvecenter — 集群实时指标流（节点 + 虚拟机 / 容器）
   后端把逐页的轮询集中到一条 WebSocket 上：页面只要挂着这个 hook，
   指标就按服务端的节奏（3 秒一帧）自动更新，不需要自己再 setInterval。

   为什么做成「一个标签页一条连接」的共享 store
   ---------------------------------------------
   这条流现在有两个消费者：仪表盘（节点读数）与全站的客户机状态监听
   （见 components/GuestLiveWatcher）。各开一条连接意味着同一份帧在浏览器里
   被解析两遍、走两条队列，而它们的内容**完全一样**。所以连接与状态放在模块
   层，组件只订阅快照（useSyncExternalStore）：谁先挂载谁建连，最后一个卸载
   时断开。

   数据口径
   --------
   * ``nodes``：节点读数（cpu / mem / 状态 / 运行时长），仪表盘 KPI 与节点行用；
   * ``guests``：客户机读数（状态 / lock / cpu / mem / 运行时长），列表页与
     详情页用。``guests`` 只有持有 ``vm.view`` / ``ct.view`` 的账号才收得到，
     且服务端已按归属裁过，前端不再做可见性判断。
   ========================================================================== */

import { useCallback, useSyncExternalStore } from 'react';
import { metricsWsUrl } from '../api/endpoints';
import { useDocumentVisible } from './useDocumentVisible';

export interface NodeMetric {
  node: string;
  status?: string;
  cpu?: number;
  maxcpu?: number;
  mem?: number;
  maxmem?: number;
  loadavg?: number | string | Array<number | string>;
  uptime?: number;
}

export interface GuestMetric {
  node: string;
  vmid: number;
  /** qemu / lxc */
  type?: string;
  status?: string;
  cpu?: number;
  maxcpu?: number;
  mem?: number;
  maxmem?: number;
  disk?: number;
  maxdisk?: number;
  uptime?: number;
  /** PVE 正挂着的任务（shutdown / migrate / backup…）；null = 没有锁 */
  lock?: string | null;
  template?: number | boolean;
  /**
   * 这条读数来自哪条 PVE 连接。
   *
   * 帧本身只描述**一条**连接（后端采集的是当前生效的那台），而客户机列表可能
   * 跨多条连接合并展示 —— 不带它，两台 PVE 上同名节点 + 同号机器会互相覆盖。
   */
  connection_id: string;
}

export interface MetricsSnapshot {
  /** 服务端出帧的时刻（Unix 秒） */
  ts: number;
  /** ``node`` -> 节点读数 */
  nodes: Record<string, NodeMetric>;
  /** ``${connection}:${node}:${vmid}`` -> 客户机读数 */
  guests: Record<string, GuestMetric>;
  /** WebSocket 是否在线。断线时快照保留最后一帧，调用方应停止用实时值覆盖轮询值 */
  connected: boolean;
}

/** 客户机读数在 ``guests`` 里的键：连接 + 节点 + VMID，两侧拼法必须一致 */
export function guestLiveKey(
  connectionId: string | null | undefined,
  node: string,
  vmid: number | string,
): string {
  return `${connectionId || ''}:${node}:${vmid}`;
}

const EMPTY: MetricsSnapshot = { ts: 0, nodes: {}, guests: {}, connected: false };

/* 重连退避：5s 起，逐次翻倍，5 分钟封顶。
   固定 5 秒重试的问题在于「连不上」和「没凭据」长得一样 —— 浏览器只给一个
   code 1006，分不出是对端拒绝还是网络抖动。而会话失效后这个状态会持续很久
   （要等到某次 HTTP 请求触发续期），固定间隔就会在这段时间里一直空转刷服务端
   日志。指数退避把长期失败的成本压到每分钟一次量级，同时又不影响短暂抖动后的
   快速恢复 —— 连上就把计数清零。 */
const BASE_DELAY_MS = 5_000;
const MAX_DELAY_MS = 300_000;

/* 节点指标里会变的字段。比较这几个就够了：任何一个变了才替换那一项，
   其余节点保持原对象引用 —— 图表的 memo / useMemo 才能真的命中。 */
const VOLATILE_FIELDS = [
  'status',
  'cpu',
  'maxcpu',
  'mem',
  'maxmem',
  'loadavg',
  'uptime',
] as const;

/* 客户机指标里会变的字段（状态与锁的变化最要紧：它决定按钮能不能点） */
const GUEST_FIELDS = [
  'status',
  'lock',
  'cpu',
  'maxcpu',
  'mem',
  'maxmem',
  'disk',
  'maxdisk',
  'uptime',
  'template',
] as const;

/** loadavg 可能是数字、字符串或三元数组 —— 按值比较，别拿引用比。 */
function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => String(v) === String(b[i]));
  }
  return a === b;
}

function sameNode(a: NodeMetric | undefined, b: NodeMetric): boolean {
  if (!a) return false;
  return VOLATILE_FIELDS.every((key) => sameValue(a[key], b[key]));
}

function sameGuest(a: GuestMetric | undefined, b: GuestMetric): boolean {
  if (!a) return false;
  return GUEST_FIELDS.every((key) => sameValue(a[key], b[key]));
}

/**
 * 把一帧指标合并进上一份状态，只替换**真的变了**的那几项。
 *
 * 服务端每 3 秒推一次全量，而实际上每次只有一两个节点的 cpu/mem 在动。
 * 原来每次都 `setMetrics(全新对象)`：所有 `metrics[name]` 的引用都变了，
 * 挂着这个 hook 的卡片、迷你曲线会跟着全量重渲染 —— 节点越多越像卡顿。
 * 没有任何变化时直接返回原对象，React 会跳过这次渲染。
 */
function mergeNodes(
  prev: Record<string, NodeMetric>,
  incoming: NodeMetric[],
): Record<string, NodeMetric> {
  let changed = false;
  const next: Record<string, NodeMetric> = { ...prev };

  for (const item of incoming) {
    if (!item?.node) continue;
    if (sameNode(prev[item.node], item)) continue;
    next[item.node] = item;
    changed = true;
  }

  /* 节点被移出集群：从状态里剔掉，避免旧读数一直挂在界面上 */
  if (Object.keys(prev).length > 0) {
    const alive = new Set(incoming.map((item) => item?.node).filter(Boolean));
    for (const name of Object.keys(prev)) {
      if (!alive.has(name)) {
        delete next[name];
        changed = true;
      }
    }
  }

  return changed ? next : prev;
}

function mergeGuests(
  prev: Record<string, GuestMetric>,
  incoming: GuestMetric[],
  connection: string,
): Record<string, GuestMetric> {
  let changed = false;
  const next: Record<string, GuestMetric> = {};

  for (const item of incoming) {
    /* vmid 统一成数字：PVE 各版本偶尔回字符串，而下游要拿它拼 React Query 的
       键（``['vm', node, vmid]``）—— 类型对不上，失效与匹配就全落空。 */
    const vmid = Number(item?.vmid);
    if (!item?.node || !Number.isFinite(vmid)) continue;
    const key = guestLiveKey(connection, item.node, vmid);
    const merged: GuestMetric = { ...item, vmid, connection_id: connection };
    const old = prev[key];
    if (old && sameGuest(old, merged)) {
      next[key] = old;
      continue;
    }
    next[key] = merged;
    changed = true;
  }

  /* 客户机被删除（或不在自己的可见范围内了）：不要留一份「幽灵读数」 */
  if (Object.keys(prev).length !== Object.keys(next).length) changed = true;

  return changed ? next : prev;
}

/* -------------------------------------------------------------------------
   模块级连接与快照
   ------------------------------------------------------------------------- */

let snapshot: MetricsSnapshot = EMPTY;
let socket: WebSocket | null = null;
let retryTimer: number | undefined;
let attempt = 0;
let refs = 0;
let visibilityBound = false;

const listeners = new Set<() => void>();

function emit(next: MetricsSnapshot): void {
  snapshot = next;
  for (const listener of listeners) listener();
}

function setConnected(connected: boolean): void {
  if (snapshot.connected === connected) return;
  emit({ ...snapshot, connected });
}

function scheduleRetry(): void {
  // 没有消费者 / 页面在后台时不排重连：后台标签页拿不到续期，重连只会被拒
  if (refs <= 0 || document.hidden || retryTimer !== undefined) return;
  const delay = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** Math.min(attempt, 6));
  attempt += 1;
  retryTimer = window.setTimeout(() => {
    retryTimer = undefined;
    open();
  }, delay);
}

function open(): void {
  if (socket || refs <= 0 || document.hidden) return;

  const ws = new WebSocket(metricsWsUrl());
  socket = ws;

  ws.onopen = () => {
    attempt = 0; // 连上了就把退避清零，下一次断线仍能快速恢复
    setConnected(true);
  };

  ws.onmessage = (event) => {
    let data: {
      type?: string;
      ts?: number;
      connection?: string;
      nodes?: NodeMetric[];
      guests?: GuestMetric[];
    };
    try {
      data = JSON.parse(event.data as string);
    } catch {
      return; // 忽略无法解析的帧
    }
    if (!data || data.type !== 'metrics') return;

    const connection = typeof data.connection === 'string' ? data.connection : '';
    /* 字段缺席 ≠ 空：后端对「没有 node.view」的订阅者根本不发 nodes，
       对「没有 vm.view / ct.view」的不发 guests。缺席时保持上一份，
       否则只读账号会看到读数被自己清空。 */
    const nodes = Array.isArray(data.nodes)
      ? mergeNodes(snapshot.nodes, data.nodes)
      : snapshot.nodes;
    const guests = Array.isArray(data.guests)
      ? mergeGuests(snapshot.guests, data.guests, connection)
      : snapshot.guests;

    if (nodes === snapshot.nodes && guests === snapshot.guests) return;
    emit({
      ts: Number(data.ts) || Math.floor(Date.now() / 1000),
      nodes,
      guests,
      connected: true,
    });
  };

  ws.onclose = () => {
    if (socket === ws) socket = null;
    setConnected(false);
    scheduleRetry();
  };

  ws.onerror = () => {
    try {
      ws.close();
    } catch {
      /* 忽略 */
    }
  };
}

function close(): void {
  if (retryTimer !== undefined) {
    window.clearTimeout(retryTimer);
    retryTimer = undefined;
  }
  const ws = socket;
  socket = null;
  if (ws) {
    ws.onopen = null;
    ws.onmessage = null;
    ws.onclose = null;
    ws.onerror = null;
    try {
      ws.close();
    } catch {
      /* 忽略 */
    }
  }
  setConnected(false);
}

function onVisibilityChange(): void {
  if (document.hidden) {
    close();
  } else if (refs > 0) {
    // 切回前台：立刻补一次连接，不必等退避计时器
    attempt = 0;
    if (retryTimer !== undefined) {
      window.clearTimeout(retryTimer);
      retryTimer = undefined;
    }
    open();
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  refs += 1;
  if (refs === 1) {
    if (!visibilityBound) {
      document.addEventListener('visibilitychange', onVisibilityChange);
      visibilityBound = true;
    }
    attempt = 0;
    open();
  }
  return () => {
    listeners.delete(listener);
    refs -= 1;
    if (refs === 0) {
      close();
      /* 最后一个订阅者走了（退出登录、切页到没有实时数据的界面）：把快照也清掉。
         不清的话下一个人登录后、在第一帧到达之前会读到上一个人的读数 —— 虽然
         `connected` 是 false、各处都会回落到轮询值，但没有任何理由留着它。 */
      snapshot = EMPTY;
    }
  };
}

/**
 * 订阅实时指标流。
 *
 * ``enabled`` 为 false（未登录、没配 PVE 连接、没有相关权限）时既不建连也
 * 不返回数据 —— 后端会把这类连接直接拒掉，而 hook 是按指数退避「断线重连」
 * 的（它分不出拒绝与网络抖动），连上只会空转握手、把服务端日志刷满 403。
 */
export function useMetricsStream(enabled: boolean): MetricsSnapshot {
  const visible = useDocumentVisible();
  const active = enabled && visible;

  const subscribeActive = useCallback(
    (listener: () => void) => (active ? subscribe(listener) : () => undefined),
    [active],
  );
  const readActive = useCallback(
    () => (active ? snapshot : EMPTY),
    [active],
  );

  return useSyncExternalStore(subscribeActive, readActive, readActive);
}
