/* ==========================================================================
   pvecenter — AI 助手授权状态选择器

   页面只从这一份派生结果渲染，不再让「主机开关」和「虚拟机票据」各说各话。
   这里保持纯函数：输入页面上真实存在的几块状态，输出「现在是什么状态、
   下一步该做什么」。不在这里发请求，也不在这里改状态。
   ========================================================================== */

import type { VmAiLease, VmAiTarget } from '../api/endpoints';

export type AiAuthLevel = 'read' | 'write' | 'ops';

export type AiAuthState =
  | 'none'
  | 'host_only'
  | 'vm_only'
  | 'active'
  | 'expiring'
  | 'vm_expired'
  | 'blocked';

export type AiAuthPrimary = 'grant' | 'complete' | 'renew' | 'adjust';

export interface AiAuthorizationView {
  state: AiAuthState;
  /** 两条通道中较高的能力；用于总状态文案 */
  level: AiAuthLevel;
  primary: AiAuthPrimary;
  host: {
    available: boolean;
    /** 生效中的主机授权（手动开通，或被虚拟机运维授权覆盖） */
    authorized: boolean;
    /** 用户是否手动开通过主机命令（区别于被运维授权覆盖） */
    manual: boolean;
    /** 由当前目标的「运维」级虚拟机授权自动覆盖 */
    coveredByLease: boolean;
    /** 主机命令是否允许提议写操作 */
    writeAllowed: boolean;
    reason: string;
  };
  vm: {
    available: boolean;
    lease: VmAiLease | null;
    active: boolean;
    expired: boolean;
    remaining: number;
    level: AiAuthLevel;
    reason: string;
  };
  /** 总状态是否允许点击主按钮 */
  actionable: boolean;
  /** 快速只读授权是否可用 */
  canQuickGrant: boolean;
  blockedReason: string;
}

function isExpired(lease: VmAiLease, now: number): boolean {
  return Number(lease.expires_at || 0) <= now;
}

function sameTarget(lease: VmAiLease, target: VmAiTarget | null): boolean {
  if (!target) return false;
  if (Number(lease.vmid || 0) !== Number(target.vmid || 0)) return false;
  if (String(lease.node || '') !== String(target.node || '')) return false;
  // connection_id 缺失时退回 node/vmid：旧接口没有这个字段，不能因此把票判丢。
  if (target.connection_id && lease.connection_id) {
    return String(lease.connection_id) === String(target.connection_id);
  }
  return true;
}

/** 当前目标上仍生效的租约；页面选中票与 active 列表共同参与判定。 */
export function leaseForTarget(
  lease: VmAiLease | null,
  activeLeases: VmAiLease[],
  target: VmAiTarget | null,
): VmAiLease | null {
  if (lease && sameTarget(lease, target)) return lease;
  return activeLeases.find((item) => sameTarget(item, target)) || null;
}

export function buildAuthorizationView(input: {
  allowExec: boolean;
  execAccess: 'read' | 'write';
  lease: VmAiLease | null;
  activeLeases: VmAiLease[];
  target: VmAiTarget | null;
  canExec: boolean;
  canVmRead: boolean;
  canVmWrite: boolean;
  now?: number;
}): AiAuthorizationView {
  const now = input.now ?? Date.now() / 1000;
  const effective = leaseForTarget(input.lease, input.activeLeases, input.target);
  const expired = Boolean(effective) && isExpired(effective as VmAiLease, now);
  const active =
    Boolean(effective) && !expired && effective?.status === 'active';
  const remaining = effective
    ? Math.max(0, Math.floor(Number(effective.expires_at || 0) - now))
    : 0;
  const leaseLevel = (effective?.access || 'read') as AiAuthLevel;

  const hostAvailable = input.canExec;
  const vmAvailable = Boolean(input.target) && input.canVmRead;
  const vmLevel: AiAuthLevel = input.canVmWrite ? leaseLevel : 'read';

  /**
   * 一次授权、全程生效：
   * 当前目标已有「运维」级虚拟机授权时，主机命令视为已授权，不再要求
   * 用户单独开通或重复申请。只在用户确实有 ``ai.exec`` 权限时生效 ——
   * 权限位本身不能被一次业务授权绕过。
   */
  const coveredByLease = Boolean(
    active &&
      effective?.access === 'ops' &&
      input.canVmWrite &&
      hostAvailable,
  );
  const hostAuthorized = input.allowExec || coveredByLease;
  const hostWrite =
    coveredByLease || (input.allowExec && input.execAccess === 'write');

  const level: AiAuthLevel =
    effective?.access === 'ops' && input.canVmWrite
      ? 'ops'
      : effective?.access === 'write' && input.canVmWrite
        ? 'write'
        : hostWrite
          ? 'write'
          : 'read';

  const host = {
    available: hostAvailable,
    authorized: hostAuthorized,
    manual: input.allowExec,
    coveredByLease,
    writeAllowed: hostWrite,
    reason: hostAvailable ? '' : 'ai.exec',
  };
  const vm = {
    available: vmAvailable,
    lease: effective,
    active,
    expired,
    remaining,
    level: vmLevel,
    reason: !input.target
      ? 'ai.auth.noVm'
      : !input.canVmRead
        ? 'ai.vm.read'
        : '',
  };

  let state: AiAuthState = 'none';
  // 用「生效中的授权」判定，而不是只看手动开关：运维票据覆盖主机命令后，
  // 页面不该再显示「仅虚拟机」这种半授权状态。
  if (!hostAuthorized && !active) state = 'none';
  else if (hostAuthorized && !effective) state = 'host_only';
  else if (!hostAuthorized && active) state = 'vm_only';
  else if (hostAuthorized && expired) state = 'vm_expired';
  else if (hostAuthorized && active && remaining > 0 && remaining <= 300) {
    state = 'expiring';
  } else if (hostAuthorized && active) state = 'active';
  else if (hostAuthorized) state = 'host_only';

  const noChannel = !hostAvailable && !vmAvailable;
  if (noChannel) state = 'blocked';

  let primary: AiAuthPrimary = 'grant';
  if (state === 'host_only' || state === 'vm_only') primary = 'complete';
  else if (state === 'active') primary = 'adjust';
  else if (state === 'expiring' || state === 'vm_expired') primary = 'renew';
  else if (state === 'blocked') primary = 'grant';

  return {
    state,
    level,
    primary,
    host,
    vm,
    actionable: !noChannel,
    canQuickGrant:
      state === 'none' && hostAvailable && vmAvailable && !expired,
    blockedReason: noChannel ? 'ai.auth.noChannel' : '',
  };
}

/** 统一授权面板里把「级别」映射成主机命令的 exec_access。 */
export function hostAccessForLevel(level: AiAuthLevel): 'read' | 'write' {
  return level === 'read' ? 'read' : 'write';
}
