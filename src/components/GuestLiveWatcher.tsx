/* ==========================================================================
   pvecenter — 客户机状态实时监听（没有界面）

   解决的是「点了启动 / 重装完，页面不动，得手动刷新一下」：

   客户机的 running/stopped 与锁原先只有 HTTP 轮询一条路，默认 30 秒一次，
   用户还可能把「数据刷新间隔」整个关掉；从别的会话、或者后台作业（重装、
   批量任务）改出来的状态，本地更是完全感知不到 —— 列表与详情页会一直挂着
   几分钟前的读数。

   这里订阅全局实时指标流（与仪表盘共用同一条 WebSocket，见
   hooks/useMetricsStream），比对每一帧里客户机的 `status` / `lock`：
   一旦有变化，就把**相关的那几份查询**作废，让 React Query 立刻重取。
   不直接改缓存里的数据是因为状态往往伴随别的东西一起变（锁、卷、IP、
   任务列表），重取整份比逐字段猜更可靠。

   为什么挂在应用层：用户可能停在任意页面（详情页、任务页、仪表盘），
   变化必须在那里就能被看见，而不是等下次进入列表页才发现。
   ========================================================================== */

import { useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { useMetricsStream } from '../hooks/useMetricsStream';
import { usePveReady } from '../hooks/usePveStatus';
import { useAuth } from '../hooks/useAuth';

/** 两次批量失效之间的最小间隔（毫秒）。
 *
 * 批量启停十几台时，每一帧都会有好几台同时变。逐个失效会让同一份列表在一秒内
 * 被重取很多次（React Query 虽然会合并，但每条 invalidate 都是一次遍历 + 一次
 * 调度）；这里统一攒一下，最多两秒对一次账，观感上仍然是「立刻就变了」。 */
const MIN_FLUSH_MS = 2_000;

interface GuestState {
  node: string;
  vmid: number;
  status?: string;
  lock: string | null;
}

export function GuestLiveWatcher() {
  const queryClient = useQueryClient();
  const pveReady = usePveReady();
  const { hasPermission } = useAuth();

  /* 只关心客户机：两类权限一个都没有就别连（这条 WS 带不来要用的东西，
     而它会常驻一整个会话）。只有 ``node.view`` 的账号由仪表盘自己那条连接
     负责节点读数，这里不重复占一条。 */
  const allowed = hasPermission('vm.view') || hasPermission('ct.view');

  const { guests, connected } = useMetricsStream(pveReady && allowed);

  /** 上一帧各客户机的状态；键是「连接:节点:VMID」 */
  const previous = useRef<Map<string, GuestState>>(new Map());
  /** 是否已经用第一帧建立基线：首帧只是「当前长什么样」，不是「变了」 */
  const seeded = useRef(false);
  const lastFlush = useRef(0);
  const timer = useRef<number | null>(null);
  /** 攒下来等待失效的具体客户机（详情页按 node/vmid 精确失效） */
  const dirty = useRef<Map<string, { node: string; vmid: number }>>(new Map());
  /** 是否有客户机出现 / 消失 —— 这一种列表自己补不出来，必须重取整份清单 */
  const rosterChanged = useRef(false);

  useEffect(() => {
    return () => {
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  useEffect(() => {
    /* 断线：清掉基线。重连后的第一帧要重新「认一遍现状」，
       否则会把断线期间的所有变化当成一整批变更，白白重取一遍。 */
    if (!connected) {
      previous.current = new Map();
      seeded.current = false;
      dirty.current.clear();
      rosterChanged.current = false;
      return;
    }

    const next = new Map<string, GuestState>();
    let changed = false;

    for (const [key, guest] of Object.entries(guests)) {
      const state: GuestState = {
        node: guest.node,
        vmid: guest.vmid,
        status: guest.status,
        lock: guest.lock ?? null,
      };
      next.set(key, state);

      const old = previous.current.get(key);
      if (!old) {
        /* 首帧之后冒出来的新键 = 新机器 / 刚进入自己的可见范围 */
        if (seeded.current) {
          changed = true;
          rosterChanged.current = true;
          dirty.current.set(key, { node: state.node, vmid: state.vmid });
        }
        continue;
      }
      if (old.status !== state.status || old.lock !== state.lock) {
        changed = true;
        dirty.current.set(key, { node: state.node, vmid: state.vmid });
      }
    }

    /* 上一帧有、这一帧没有 = 被删除（或不再自己的可见范围内）。
       没有 node/vmid 之外的信息可用，只作废列表与汇总。 */
    if (seeded.current) {
      for (const key of previous.current.keys()) {
        if (!next.has(key)) {
          changed = true;
          rosterChanged.current = true;
          break;
        }
      }
    }

    previous.current = next;
    if (!seeded.current) {
      seeded.current = true;
      return;
    }
    if (!changed) return;

    const flush = () => {
      timer.current = null;
      lastFlush.current = Date.now();

      const client = queryClient;

      /* 状态 / 锁的变化**不必**重取列表：列表页自己会用同一帧里的实时值覆盖
         状态、CPU、内存与锁（见 pages/GuestList.tsx）。这里只重取列表补不出来的
         两件事 —— 台数变了（新建 / 删除），以及**详情页**的整份数据（状态一变，
         卷、网卡、任务这些往往也跟着变）。

         这一点很关键：批量关五十台时，如果每次变化都作废整份清单，列表页会
         被反复重取；而列表页的实时覆盖已经能把这些变化画出来了。 */
      if (rosterChanged.current) {
        void client.invalidateQueries({ queryKey: ['vms'] });
        void client.invalidateQueries({ queryKey: ['lxc'] });
        rosterChanged.current = false;
      }
      /* 仪表盘的台数 / 运行中计数。只有一条轻查询，代价远低于数字对不上的困惑。 */
      void client.invalidateQueries({ queryKey: ['dashboard'] });
      /* 任务列表：状态变化几乎总对应一条新任务（启动 / 关机 / 重装），
         详情页与任务页都在看这份。 */
      void client.invalidateQueries({ queryKey: ['cluster', 'tasks'] });

      for (const target of dirty.current.values()) {
        for (const kind of ['vm', 'lxc']) {
          void client.invalidateQueries({
            queryKey: [kind, target.node, target.vmid],
          });
        }
      }
      dirty.current.clear();
    };

    const wait = MIN_FLUSH_MS - (Date.now() - lastFlush.current);
    if (wait <= 0) {
      flush();
      return;
    }
    if (timer.current === null) {
      timer.current = window.setTimeout(flush, wait);
    }
  }, [guests, connected, queryClient]);

  return null;
}
