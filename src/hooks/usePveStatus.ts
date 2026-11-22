/* ==========================================================================
   pvecenter — 面板的 Proxmox 连接状态

   只回答一个问题：**面板填过连接没有**。

   为什么值得单独抽出来：未配置时后端对几乎所有业务接口都返回 428，如果各页面
   各自判断，就是十几处「红色报错 + 英文原文」；统一到这一处之后，全局引导条
   与各页面的引导态读的是同一份状态，不会出现「导航说没配、页面说连不上」。

   数据源用免登录的 `/health`：普通用户没有读连接列表的权限，但同样需要知道
   「现在是系统没配好，不是我点坏了」。
   ========================================================================== */

import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { healthApi } from '../api/endpoints';
import { renewSession } from '../api/client';
import type { HealthStatus } from '../api/types';
import { useRefreshInterval } from './useRefreshInterval';

export interface PveStatus {
  /** 是否已保存可用的连接（false = 首次部署还没填过，该去添加） */
  configured: boolean;
  /** 是否真的连上了 PVE（configured 为 true 时才可能有意义） */
  reachable: boolean;
  /**
   * 兜底读取的那台是否被「暂停接入」。
   *
   * 暂停不是故障：`reachable` 会是 false，但该做的是去连接配置页恢复，而不是
   * 排查地址 / 令牌。凡是要弹「PVE 连不上，请排查」的地方都得先看这一位。
   */
  paused: boolean;
  /** 首次探测还没回来 */
  loading: boolean;
  /**
   * 这份探测结果是否**可信**。
   *
   * 和 `configured` 的区别很关键：探测失败（后端没起来、面板正在重启）或拿到
   * 匿名载荷（access cookie 过期、代理缓存）时 `configured` 会回落到默认的
   * true，但不能据此说「PVE 连不上」—— 那是把面板自己的故障算到 PVE 头上。
   * 判断「配了但连不上」必须要求 hasData 为真。
   */
  hasData: boolean;
  /**
   * **任意一条**连接在线。
   *
   * 与 `reachable`（只看兜底那台）不同：面板的列表 / 统计类接口是遍历全部
   * 未暂停连接的，只要有一条在线，「面板读不到 PVE 数据」就不成立。兜底那台
   * 恰好维护、而其它集群正常时，全局提示条不该报警。
   *
   * 拿不到机队汇总（没有 node.view）时回落到 `reachable`。
   */
  anyReachable: boolean;
}

/** 匿名/旧后端的载荷里没有诊断字段，不能当成「探测到 PVE 连不上」 */
function isProbeResult(data: HealthStatus | undefined): boolean {
  if (!data) return false;
  // 新后端显式标记；旧后端没有这一位，但会带 pve_configured
  if (data.authenticated === false) return false;
  return typeof data.pve_configured === 'boolean';
}

/**
 * 匿名载荷的自愈节流。
 *
 * health 是公开端点，access cookie 过期时后端按匿名回 200（不是 401），拦
 * 截器的「401 自动续期」不会触发。这里主动补一次续期；但探测每 10 秒一次，
 * 会话真失效时不能每 tick 都去打一次 refresh —— 一分钟一次足够。
 */
let lastRenewAt = 0;
const RENEW_COOLDOWN_MS = 60_000;

export function usePveStatus(): PveStatus {
  /* 探测间隔跟「设置 → 面板设置 → 数据刷新间隔」走 */
  const refresh = useRefreshInterval();
  const query = useQuery({
    queryKey: ['health'],
    queryFn: healthApi.check,
    /* 诊断字段缺失时（匿名载荷 / 面板刚重启 / 旧后端）10 秒后再问一次：
       别让状态长时间停在「未知」，也别因为用户选了「不自动刷新」就永远停住。
       拿到可信结果后回归设置里的间隔。 */
    refetchInterval: (q) =>
      isProbeResult(q.state.data) ? refresh : 10_000,
    retry: false,
  });

  const data = query.data;
  const hasData = isProbeResult(data);

  /* 载荷里没有诊断字段（匿名 / 旧后端）说明这枚 access cookie 已经不作数了：
     主动续期一次，成功的话立刻重取，用户不会在状态未知里等下一轮轮询。
     会话真失效时续期会失败 —— 上面的一分钟节流保证不会每个 tick 都打一次。 */
  const refetch = query.refetch;
  useEffect(() => {
    if (!data || hasData) return;
    if (Date.now() - lastRenewAt < RENEW_COOLDOWN_MS) return;
    lastRenewAt = Date.now();
    let cancelled = false;
    void renewSession().then((ok) => {
      if (ok && !cancelled) void refetch();
    });
    return () => {
      cancelled = true;
    };
  }, [data, hasData, refetch]);

  /* 拿不到数据时（首屏加载中、后端刚重启、探测失败）一律当作「已配置」：
     宁可少提示一次，也不能因为一次探测失败就把「还没接入主机」的引导糊到
     所有人脸上 —— 那本身就是另一种「以为平台坏了」。 */
  const configured = hasData ? Boolean(data?.pve_configured) : true;

  const reachable = hasData ? Boolean(data?.pve_connected) : false;
  const paused = hasData ? Boolean(data?.pve_paused) : false;

  /* 机队汇总只有 node.view 才拿得到；缺了就按兜底那台判断 */
  const totals = data?.fleet?.totals;
  const anyReachable = totals ? totals.online > 0 : reachable;

  return {
    configured,
    reachable,
    paused,
    loading: query.isLoading,
    hasData,
    anyReachable,
  };
}

/**
 * 给「依赖 PVE 的轮询」用：未配置连接时返回 false，让 `refetchInterval` 停下。
 *
 * 未配置时后端对每个请求都回 428，继续十秒一次只会刷一屏没人看的报错。配好之后
 * 这个值自动变回 true，轮询自己就接上了 —— 不需要用户刷新页面或重启面板。
 * 探测结果未知（首屏/探测失败）时返回 true：不能因为一次探测失败就把轮询关死。
 */
export function usePveReady(): boolean {
  return usePveStatus().configured;
}
