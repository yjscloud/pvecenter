/* ==========================================================================
   pvecenter — AI 助手统一授权卡

   合并前的两块授权（主机命令开关 + 虚拟机票据）各自有状态、各自有入口：
   用户要开两条通道就得点两次，而且「主机已授权、虚拟机未授权」时页面没有一句
   话能说清现在到底能做什么。这里用一个总状态 + 一个主入口把它们收在一起：

   * 总状态决定「现在能做什么、下一步该点哪个按钮」；
   * 两条通道各自保留生命周期（主机=本次会话，虚拟机=限时票据）；
   * 一次确认同时写两边的服务端状态，不再要求用户重复授权。
   ========================================================================== */

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import {
  aiApi,
  type AiAuthorizationGrantResult,
  type VmAiLease,
  type VmAiPolicy,
  type VmAiTarget,
} from '../api/endpoints';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Field, Input, Select, Switch } from './ui/Input';
import { ConfirmDialog } from './ui/ConfirmDialog';
import { Modal } from './ui/Modal';
import { Notice } from './ui/EmptyState';
import { VmAiLeaseCard } from './VmAiLeaseCard';
import { useToast } from '../hooks/useToast';
import { useT, type MessageKey } from '../i18n';
import {
  buildAuthorizationView,
  hostAccessForLevel,
  type AiAuthLevel,
  type AiAuthorizationView,
} from '../pages/aiAuthorization';

const WRITABLE_DIRS = ['/tmp', '/var/tmp', '/var/log', '/etc', '/opt', '/srv'];

type RevokedChannel = 'host' | 'vm';

/** 撤销成功后留在卡片上的确认信息：哪几条通道、什么时候撤的 */
interface RevokedNotice {
  channels: RevokedChannel[];
  at: number;
  /** 授权撤了、但客户机里那条命令没停下来：确认条要把这层说清楚 */
  killFailed?: boolean;
}

/** 撤销失败时留下的重试参数：错误条旁边的按钮照原样再打一次 */
interface RevokeRetry {
  channels: { host?: boolean; vm?: boolean };
  lease: VmAiLease | null;
}

/** 标题直接点明撤的是哪条通道：「授权已撤销」在只撤了一条时会被读成「全撤了」 */
function revokedTitleKey(channels: RevokedChannel[]): MessageKey {
  if (channels.length > 1) return 'ai.auth.revokedTitleBoth';
  return channels[0] === 'vm' ? 'ai.auth.revokedTitleVm' : 'ai.auth.revokedTitleHost';
}

function revokedHintKey(
  channels: RevokedChannel[],
  view: AiAuthorizationView,
): MessageKey {
  if (channels.length > 1) return 'ai.auth.revokedHintBoth';
  /* 只剩一条通道被撤、另一条还在生效时，文案必须说清「哪条停了、哪条还能用」：
     主机撤了但虚拟机票据还活着时说「AI 已停止登录这台机器」是假话，用户也会
     把它读成「我刚才的授权被撤销了」。 */
  if (channels[0] === 'vm') {
    return view.host.authorized ? 'ai.auth.revokedHintVmHostOn' : 'ai.auth.revokedHintVm';
  }
  return view.vm.active ? 'ai.auth.revokedHintHostVmOn' : 'ai.auth.revokedHintHost';
}

const MINUTE_KEYS: Record<number, MessageKey> = {
  15: 'ai.lease.min15',
  30: 'ai.lease.min30',
  60: 'ai.lease.min60',
  120: 'ai.lease.min120',
};

export interface AiAuthorizationPatch {
  conversationId?: string;
  allowExec?: boolean;
  execAccess?: 'read' | 'write';
  lease?: VmAiLease | null;
  /** 本次操作新开了主机通道：页面据此显示「继续执行」 */
  justGrantedHost?: boolean;
  /** 本次操作新签了运维票（会覆盖主机命令）：同样允许「继续执行」 */
  justGrantedLease?: boolean;
}

type LevelOption = {
  value: AiAuthLevel;
  label: string;
  hint: string;
  disabled: boolean;
  reason: string;
};

function formatLeft(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

/**
 * 票据剩余时间：**自己管自己的秒表**。
 *
 * 放在父组件里（每秒 setState）会让整张 44KB 的授权卡跟着重绘 —— 对话框、策略、
 * 授权项、撤销确认全都要重新 reconcile 一遍；而真正需要每秒变的只有这一行字。
 * 这里 memo + 内部 interval，父组件重渲染时只要 ``expiresAt`` 没变就直接命中缓存。
 */
const LeaseCountdown = memo(function LeaseCountdown({
  expiresAt,
}: {
  expiresAt: number;
}) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!(expiresAt > Date.now() / 1000)) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [expiresAt]);
  return <>{formatLeft(Math.max(0, Math.floor(expiresAt - now)))}</>;
});

function statusLabelKey(view: AiAuthorizationView): MessageKey {
  switch (view.state) {
    case 'host_only':
      return 'ai.auth.stateHostOnly';
    case 'vm_only':
      return 'ai.auth.stateVmOnly';
    case 'active':
      return 'ai.auth.stateActive';
    case 'expiring':
      return 'ai.auth.stateExpiring';
    case 'vm_expired':
      return 'ai.auth.stateExpired';
    case 'blocked':
      return 'ai.auth.stateBlocked';
    default:
      return 'ai.auth.stateNone';
  }
}

function statusDescKey(view: AiAuthorizationView): MessageKey {
  // 被运维授权覆盖时，不能说「主机和虚拟机都已授权」——那会让人以为另外开过一次
  if (view.host.coveredByLease && (view.state === 'active' || view.state === 'expiring')) {
    return 'ai.auth.descActiveCovered';
  }
  switch (view.state) {
    case 'host_only':
      return 'ai.auth.descHostOnly';
    case 'vm_only':
      return 'ai.auth.descVmOnly';
    case 'active':
      return 'ai.auth.descActive';
    case 'expiring':
      return 'ai.auth.descExpiring';
    case 'vm_expired':
      return 'ai.auth.descExpired';
    case 'blocked':
      return 'ai.auth.descBlocked';
    default:
      return 'ai.auth.descNone';
  }
}

function primaryLabelKey(view: AiAuthorizationView): MessageKey {
  switch (view.primary) {
    case 'complete':
      return 'ai.auth.primaryComplete';
    case 'renew':
      return 'ai.auth.primaryRenew';
    case 'adjust':
      return 'ai.auth.primaryAdjust';
    default:
      return 'ai.auth.primaryGrant';
  }
}

function levelLabelKey(level: string): MessageKey {
  if (level === 'ops') return 'ai.auth.levelOps';
  if (level === 'write') return 'ai.auth.levelWrite';
  return 'ai.auth.levelRead';
}

export function AiAuthorizationCard({
  conversationId,
  hostId,
  hostName,
  allowExec,
  execAccess,
  lease,
  target,
  canExec,
  canVmRead,
  canVmWrite,
  onPatch,
}: {
  conversationId: string;
  hostId: string;
  hostName: string;
  allowExec: boolean;
  execAccess: 'read' | 'write';
  lease: VmAiLease | null;
  target: VmAiTarget | null;
  canExec: boolean;
  canVmRead: boolean;
  canVmWrite: boolean;
  onPatch: (patch: AiAuthorizationPatch) => void;
}) {
  const t = useT();
  const toast = useToast();
  const [activeLeases, setActiveLeases] = useState<VmAiLease[]>([]);
  const [policy, setPolicy] = useState<VmAiPolicy | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [now, setNow] = useState(() => Date.now() / 1000);
  /* 撤销之后「刚刚发生了什么」需要在卡片里留痕：toast 一闪而过，状态又从
     「已授权」跳回「未授权」，用户很容易以为没撤成功。 */
  const [revoked, setRevoked] = useState<RevokedNotice | null>(null);
  /** 上一次撤销失败留下的重试参数；成功或换动作后清空 */
  const [retryRevoke, setRetryRevoke] = useState<RevokeRetry | null>(null);

  /* 这张卡有对话框、策略、授权项一堆结构（渲染一次几十毫秒），**不该为了倒计时
     每秒整卡重绘**：它常驻在 AI 页，流式对话期间会和打字机抢主线程。
     现在的做法是分工：
       * 秒级倒计时交给 :func:`LeaseCountdown` 子组件（只重绘那几个字）；
       * 这里只在「状态真的会变」的时刻醒一次 —— 票据进入 5 分钟警戒（还有
         ``-300s``）与真正到期（``expires_at``），两者按最近的排一个延时器。
     这样静止时一次都不会重绘，票据到期/临近过期的文案也不会迟到。 */
  useEffect(() => {
    const real = Date.now() / 1000;
    // 页面长时间静止后 now 会落后于真实时间：先对齐一次再排边界，
    // 否则「票据已过期」要等到一个按旧时钟算出来的很久以后
    if (real - now > 5) {
      setNow(real);
      return;
    }
    const expires = [lease?.expires_at, ...activeLeases.map((item) => item.expires_at)]
      .map((value) => Number(value || 0))
      .filter((value) => value > real);
    if (!expires.length) return;
    const next = expires
      .flatMap((value) => [value - 300, value])
      .filter((value) => value > real + 0.5)
      .sort((a, b) => a - b)[0];
    if (!next) return;
    const timer = window.setTimeout(
      () => setNow(Date.now() / 1000),
      (next - real) * 1000 + 20,
    );
    return () => window.clearTimeout(timer);
  }, [lease, activeLeases, now]);

  /** 请求序号：撤销后紧跟的轮询可能带回更早发出的旧列表，旧结果不许覆盖界面 */
  const pullSeq = useRef(0);

  /** 拉一次服务端生效列表；返回 null 表示这次没拿到可信结果（调用方据此跳过判断） */
  const pullLeases = useCallback(async (): Promise<VmAiLease[] | null> => {
    const seq = ++pullSeq.current;
    try {
      const data = await aiApi.vmLeases();
      const ts = Date.now() / 1000;
      const items = data.items.filter(
        (item) => item.status === 'active' && item.expires_at > ts,
      );
      if (seq !== pullSeq.current) return null;
      setActiveLeases(items);
      return items;
    } catch {
      /* 列表失败不阻塞状态卡：签发和续期本身会给出真正的错误 */
      return null;
    }
  }, []);

  const refreshLeases = useCallback(async () => {
    await pullLeases();
  }, [pullLeases]);

  /** 轮询回调要读最新值，又不想让定时器随每次渲染重建，这里拿一份镜像 */
  const leaseRef = useRef(lease);
  leaseRef.current = lease;
  const patchRef = useRef(onPatch);
  patchRef.current = onPatch;

  useEffect(() => {
    void refreshLeases();
    aiApi
      .vmPolicy()
      .then((data) => setPolicy(data.policy))
      .catch(() => setPolicy(null));
  }, [refreshLeases]);

  /* 授权状态不只被这张卡改变：另一个标签页撤销、票据到期、AI 那边主动收尾，
     服务端都会先变。只拉一次的话，卡片会长期停在「已授权」，用户没有任何
     线索发现状态其实已经变了。 */
  useEffect(() => {
    const pull = async () => {
      if (document.hidden) return;
      const fetchedAt = Date.now() / 1000;
      const items = await pullLeases();
      const held = leaseRef.current;
      if (!items || !held) return;
      // 刚签发的票可能比列表快一步：给服务端写入留一点余量，别自己把自己撤了
      if (held.created && fetchedAt - held.created < 10) return;
      /* 自然到期交给倒计时的既有逻辑处理（还要保留「已过期」这个形态），
         这里只认「票没过期、服务端却已经不认」这种被动撤销。 */
      if (Number(held.expires_at || 0) <= fetchedAt) return;
      if (items.some((item) => item.id === held.id)) return;
      /* 服务端已经不认这张票（别处撤销 / 到期）：会话里那个租约 id 必须立刻
         清掉，否则卡片会一直显示「生效中」，下一轮对话还会带着失效 id 去请求。 */
      patchRef.current({ lease: null });
      setRevoked((prev) => prev ?? { channels: ['vm'], at: Date.now() });
    };
    const timer = window.setInterval(() => void pull(), 30_000);
    // 切回前台先补一次：后台被节流时定时器可能很久才醒，回来时页面必须是新的
    const onFocus = () => void pull();
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
    };
  }, [pullLeases]);

  const view = useMemo(
    () =>
      buildAuthorizationView({
        allowExec,
        execAccess,
        lease,
        activeLeases,
        target,
        canExec,
        canVmRead,
        canVmWrite,
        now,
      }),
    [allowExec, execAccess, lease, activeLeases, target, canExec, canVmRead, canVmWrite, now],
  );

  /* 把「当前目标上生效的租约」同步给父组件：刷新后父组件手里只有 null，
     请求就带不上 lease_id，运维授权会被当成没签过。只在 id 变化时同步。 */
  useEffect(() => {
    if (!view.vm.active || !view.vm.lease) return;
    if (leaseRef.current?.id === view.vm.lease.id) return;
    patchRef.current({ lease: view.vm.lease });
  }, [view.vm.active, view.vm.lease]);

  const applyResult = (
    result: AiAuthorizationGrantResult,
    hostJustGranted: boolean,
    leaseJustGranted = false,
  ) => {
    onPatch({
      conversationId: result.conversation?.id || conversationId,
      allowExec: result.host.authorized,
      execAccess: result.host.access === 'read' ? 'read' : 'write',
      lease: result.lease,
      justGrantedHost: hostJustGranted,
      justGrantedLease: leaseJustGranted,
    });
  };

  const submitGrant = async (options: {
    level: AiAuthLevel;
    host: boolean;
    vm: boolean;
    minutes: number;
    fsRw: string[];
    units: string[];
    reason: string;
    replaceExisting: boolean;
  }) => {
    setBusy(true);
    setError('');
    try {
      const result = await aiApi.authorizationGrant({
        conversation_id: conversationId || undefined,
        host_id: hostId || undefined,
        host_name: hostName || undefined,
        host: options.host,
        exec_access: hostAccessForLevel(options.level),
        vm: options.vm,
        vm_grant: target
          ? {
              node: target.node,
              vmid: target.vmid,
              connection_id: target.connection_id,
              vm_name: target.name,
              guest_type: target.guest_type,
              access: options.level,
              minutes: options.minutes,
              fs_rw: options.level === 'write' ? options.fsRw : [],
              units: options.level === 'write' ? options.units : [],
              reason: options.reason,
            }
          : undefined,
        replace_existing: options.replaceExisting,
      });
      applyResult(
        result,
        options.host && !allowExec,
        // 只有「运维」票才会覆盖主机命令，读/写档不该弹「继续执行」
        Boolean(options.vm && result.lease?.status === 'active' && result.lease.access === 'ops'),
      );
      /* 刚完成一次授权，屏幕上不该还挂着上一次的「已撤销」——那两句并排出现，
         用户会以为这次授权又被撤了。 */
      setRevoked(null);
      for (const warning of result.warnings || []) {
        toast.warning(t('ai.auth.revokedKillFailed'), warning);
      }
      toast.success(
        t('ai.auth.granted', {
          text: [
            options.host ? t('ai.auth.hostGranted') : '',
            options.vm ? t('ai.auth.vmGranted') : '',
          ]
            .filter(Boolean)
            .join(' + '),
        }),
      );
      setDialogOpen(false);
      await refreshLeases();
    } catch (err) {
      setError(errorMessage(err));
      await refreshLeases();
    } finally {
      setBusy(false);
    }
  };

  const renew = async () => {
    const current = view.vm.lease;
    if (!current || view.vm.expired) {
      setDialogOpen(true);
      return;
    }
    setBusy(true);
    setError('');
    try {
      const data = await aiApi.authorizationRenew({
        lease_id: current.id,
        minutes: current.minutes || 15,
      });
      onPatch({ lease: data.lease });
      setRevoked(null);
      toast.success(
        t('ai.auth.renewed', {
          time: formatLeft(
            Math.max(0, Math.floor(data.lease.expires_at - Date.now() / 1000)),
          ),
        }),
      );
      await refreshLeases();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (
    channels: { host?: boolean; vm?: boolean },
    /* 显式传票：列表里「其他授权」的撤销针对的不是当前选中那张。
       ``undefined`` 表示沿用当前选中票，``null`` 表示当前确实没有票。 */
    leaseTarget?: VmAiLease | null,
  ) => {
    const current = leaseTarget !== undefined ? leaseTarget : view.vm.lease;
    setBusy(true);
    setError('');
    setRetryRevoke(null);
    let failed = false;
    try {
      const result = await aiApi.authorizationRevoke({
        conversation_id: conversationId || undefined,
        host: Boolean(channels.host),
        vm: Boolean(channels.vm),
        lease_id: channels.vm ? current?.id : undefined,
        vm_grant:
          channels.vm && target
            ? {
                connection_id: target.connection_id,
                node: target.node,
                vmid: target.vmid,
              }
            : undefined,
        reason: t('ai.auth.revokedReason'),
      });
      const done: RevokedChannel[] = [];
      if (channels.host) {
        /* host_updated=false 表示服务端没有真正关掉（会话已被删之类）：
           这时绝不能把界面改成「未授权」，否则用户会以为撤销生效了。 */
        if (result.host_updated) {
          onPatch({ allowExec: false, execAccess: 'read' });
          done.push('host');
        } else {
          setError(t('ai.auth.revokeHostFailed'));
          failed = true;
        }
      }
      if (channels.vm) {
        /* 返回的票仍是 active，说明这次撤销没有真正落库：界面若照样改成
           「未授权」，用户会带着「已经撤掉了」的错觉离开这一页。 */
        if (result.lease?.status === 'active') {
          setError(t('ai.auth.revokeVmFailed'));
          failed = true;
        } else {
          /* 只有撤的是页面当前持有的那张，才需要清掉会话里的租约 id；
             撤「其他授权」时当前选中的票没变，清空会让对话莫名失去授权。 */
          if (lease?.id === current?.id) onPatch({ lease: null });
          /* activeLeases 里这份票还在的话，下一个 tick 的 view 会把它重新算成
             「生效中」—— 撤销后必须立刻从本地列表摘掉，不能等下一次拉取。 */
          if (current?.id) {
            setActiveLeases((prev) => prev.filter((item) => item.id !== current.id));
          }
          done.push('vm');
        }
      }
      if (done.length) {
        setRevoked({
          channels: done,
          at: Date.now(),
          killFailed: Boolean(result.kill && result.kill.output && !result.kill.ok),
        });
      }
      if (failed) setRetryRevoke({ channels, lease: current });
      if (result.kill && result.kill.output && !result.kill.ok) {
        toast.warning(t('ai.auth.revokedKillFailed'), result.kill.output);
      } else if (done.length) {
        toast.success(t('ai.auth.revoked'));
      }
      // 服务端才是权威：成功与否都拉一次，界面跟着真实状态走
      await refreshLeases();
    } catch (err) {
      setError(errorMessage(err));
      setRetryRevoke({ channels, lease: current });
      // 失败更要拉一次：票可能已经被别处撤销，界面不能停在旧状态上
      await refreshLeases();
    } finally {
      setBusy(false);
      setConfirmRevoke(false);
    }
  };

  /** 按上次失败的通道与票原样重试；参数从 state 取，不必重新推导 */
  const retryRevokeAction = () => {
    if (!retryRevoke) return;
    void revoke(retryRevoke.channels, retryRevoke.lease);
  };

  const openPrimary = () => {
    if (view.primary === 'renew') {
      void renew();
      return;
    }
    setError('');
    setDialogOpen(true);
  };

  const quickGrant = () => {
    void submitGrant({
      level: 'read',
      host: canExec,
      vm: Boolean(target && canVmRead),
      minutes: 15,
      fsRw: [],
      units: [],
      reason: '',
      replaceExisting: false,
    });
  };

  const matching = view.vm.lease;

  /* 只把「现在确实已经不生效」的通道算进撤销提示：撤销后马上重新授权时，
     提示应当自己消失，不能留一条与状态矛盾的「已撤销」。 */
  const revokedChannels: RevokedChannel[] = revoked
    ? revoked.channels.filter((channel) =>
        channel === 'host' ? !view.host.authorized : !view.vm.active,
      )
    : [];
  const showRevoked = revokedChannels.length > 0;
  const hostRevoked = revokedChannels.includes('host');
  const vmRevoked = revokedChannels.includes('vm');
  /* 两条通道都空着时总状态显示「已撤销」而不是「未授权」：用户刚点完，
     需要的是「你的操作生效了」，而不是一个看不出前因的默认态。 */
  const showRevokedState = showRevoked && view.state === 'none';

  const otherLeases = activeLeases.filter(
    (item) =>
      !target ||
      item.node !== target.node ||
      Number(item.vmid) !== Number(target.vmid) ||
      (item.connection_id &&
        target.connection_id &&
        item.connection_id !== target.connection_id),
  );

  return (
    <div className={`ai-auth-card${showRevoked ? ' is-revoked' : ''}`}>
      <div className="ai-auth-head">
        <div className="ai-auth-head-main">
          <div className="ai-auth-title-row">
            <span className="ai-auth-title">{t('ai.auth.title')}</span>
            <Badge
              variant={
                showRevokedState
                  ? 'warning'
                  : view.state === 'active'
                    ? 'success'
                    : view.state === 'blocked' || view.state === 'vm_expired'
                      ? 'warning'
                      : view.state === 'none'
                        ? 'neutral'
                        : 'warning'
              }
              size="sm"
              dot
            >
              {showRevokedState
                ? t('ai.auth.stateRevoked')
                : t(statusLabelKey(view))}
            </Badge>
            {view.state === 'active' || view.state === 'expiring' ? (
              <Badge variant="neutral" size="sm">
                {t(levelLabelKey(view.level))}
              </Badge>
            ) : null}
          </div>
          <p className="ai-auth-desc">{t(statusDescKey(view))}</p>
        </div>
        <div className="ai-auth-head-actions">
          <Button
            variant="primary"
            size="sm"
            onClick={openPrimary}
            disabled={!view.actionable || busy}
            loading={busy}
          >
            {t(primaryLabelKey(view))}
          </Button>
          {view.canQuickGrant ? (
            <Button variant="secondary" size="sm" onClick={quickGrant} disabled={busy}>
              {t('ai.auth.quickGrant')}
            </Button>
          ) : null}
          {view.host.manual || matching ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setConfirmRevoke(true)}
              disabled={busy}
            >
              {t('ai.auth.revokeAll')}
            </Button>
          ) : null}
        </div>
      </div>

      {/* 撤销后的原地确认条：说清「撤了哪条、现在还能做什么、下一步点哪」。
          不做弹窗，避免打断正在看的排查过程；点 × 就收起来。 */}
      {showRevoked ? (
        <div
          key={revoked?.at}
          className="ai-auth-revoked"
          role="status"
          aria-live="polite"
        >
          <span className="ai-auth-revoked-mark" aria-hidden="true">
            <svg viewBox="0 0 16 16" width="12" height="12" fill="none">
              <path
                d="M3.5 8.5l3 3 6-6.5"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div className="ai-auth-revoked-body">
            <span className="ai-auth-revoked-title">
              {t(revokedTitleKey(revokedChannels))}
            </span>
            <span className="ai-auth-revoked-hint">
              {t(revokedHintKey(revokedChannels, view))}
            </span>
            {/* 撤销成功 ≠ 客户机里那条命令停了：这句必须留在屏上，不能只闪一下 toast */}
            {revoked?.killFailed ? (
              <span className="ai-auth-revoked-hint">{t('ai.auth.revokedKillHint')}</span>
            ) : null}
          </div>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => {
              setError('');
              setDialogOpen(true);
            }}
            disabled={busy}
          >
            {t('ai.auth.revokedRegrant')}
          </Button>
          <button
            type="button"
            className="ai-auth-revoked-close"
            onClick={() => setRevoked(null)}
            aria-label={t('ai.auth.revokedDismiss')}
            title={t('ai.auth.revokedDismiss')}
          >
            ×
          </button>
        </div>
      ) : null}

      {error ? (
        <div className="login-error" role="alert">
          <span>{error}</span>
          {retryRevoke ? (
            <Button
              variant="secondary"
              size="sm"
              style={{ marginLeft: 'auto', flex: 'none' }}
              onClick={retryRevokeAction}
              disabled={busy}
            >
              {t('ai.auth.revokeRetry')}
            </Button>
          ) : null}
        </div>
      ) : null}

      <div className="ai-auth-channels">
        <div className={`ai-auth-channel${hostRevoked ? ' is-revoked' : ''}`}>
          <div className="ai-auth-channel-main">
            <span className="ai-auth-channel-name">{t('ai.auth.hostChannel')}</span>
            <Badge
              variant={
                view.host.authorized ? 'success' : hostRevoked ? 'warning' : 'neutral'
              }
              size="sm"
              dot
            >
              {view.host.coveredByLease
                ? t('ai.auth.hostCovered')
                : view.host.authorized
                  ? t('ai.auth.hostOn')
                  : hostRevoked
                    ? t('ai.auth.stateRevoked')
                    : view.host.available
                      ? t('ai.auth.hostOff')
                      : t('ai.auth.hostUnavailable')}
            </Badge>
          </div>
          {/* 未授权/无权限时徽标已经说清，不再补一句同样的话 */}
          {view.host.coveredByLease ? (
            <span className="ai-auth-channel-hint">
              {t('ai.auth.hostCoveredHint')}
            </span>
          ) : view.host.authorized ? (
            <span className="ai-auth-channel-hint">
              {view.host.writeAllowed ? t('ai.auth.hostWrite') : t('ai.auth.hostRead')}
            </span>
          ) : null}
          {/* 手动开过就保留这个撤销入口：撤掉的是「手动授权」本身。若同时被
              运维票覆盖，撤销后主机命令仍可用，页面上的覆盖徽标会如实说明。 */}
          {view.host.manual ? (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void revoke({ host: true })}
              disabled={busy}
            >
              {t('ai.auth.revokeHost')}
            </Button>
          ) : null}
        </div>

        <div className={`ai-auth-channel${vmRevoked ? ' is-revoked' : ''}`}>
          <div className="ai-auth-channel-main">
            <span className="ai-auth-channel-name">{t('ai.auth.vmChannel')}</span>
            <Badge
              variant={
                view.vm.active
                  ? 'success'
                  : vmRevoked
                    ? 'warning'
                    : view.vm.expired
                      ? 'warning'
                      : 'neutral'
              }
              size="sm"
              dot
            >
              {view.vm.active ? (
                <>
                  {t(levelLabelKey(view.vm.level))}
                  {' · '}
                  {/* 秒级倒计时自成一个组件：只有它每秒重绘，整张卡不动 */}
                  <LeaseCountdown
                    expiresAt={Number(view.vm.lease?.expires_at || 0)}
                  />
                </>
              ) : vmRevoked ? (
                t('ai.auth.stateRevoked')
              ) : view.vm.expired ? (
                t('ai.auth.vmExpired')
              ) : view.vm.available ? (
                t('ai.auth.vmOff')
              ) : (
                t('ai.auth.vmUnavailable')
              )}
            </Badge>
          </div>
          {/* 只补徽标说不出来的信息：剩余动作数、过期后的下一步 */}
          {view.vm.active ? (
            <span className="ai-auth-channel-hint">
              {t('ai.auth.vmQuota', {
                used: matching?.actions_used ?? 0,
                max: matching?.actions_max ?? 0,
              })}
            </span>
          ) : view.vm.expired ? (
            <span className="ai-auth-channel-hint">{t('ai.auth.expiredHint')}</span>
          ) : null}
          <div className="ai-auth-channel-actions">
            {view.vm.active ? (
              <Button variant="ghost" size="sm" onClick={() => void renew()} disabled={busy}>
                {t('ai.auth.primaryRenew')}
              </Button>
            ) : null}
            {view.vm.lease ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void revoke({ vm: true })}
                disabled={busy}
              >
                {t('ai.auth.revokeVm')}
              </Button>
            ) : null}
            {view.vm.expired ? (
              <Button variant="ghost" size="sm" onClick={() => setDialogOpen(true)}>
                {t('ai.auth.primaryGrant')}
              </Button>
            ) : null}
          </div>
        </div>
      </div>

      {matching ? (
        <div className="ai-auth-details">
          <VmAiLeaseCard
            embedded
            lease={matching}
            target={target}
            onLeaseChange={(next) => {
              // 过期时子组件会尝试清空；这里保留元数据，页面才能显示「已过期」
              if (next === null && view.vm.expired) return;
              onPatch({ lease: next });
            }}
            onRevoked={(revokedLease, info) => {
              /* 子卡撤销成功后必须把这张票从本卡的「生效中列表」里摘掉：
                 否则下面用 activeLeases 重算 view 时又会把它找回来，界面
                 继续显示「运维授权 13:46」，看起来像撤销没生效。 */
              setActiveLeases((prev) =>
                prev.filter((item) => item.id !== revokedLease.id),
              );
              if (lease?.id === revokedLease.id) onPatch({ lease: null });
              setRevoked({
                channels: ['vm'],
                at: Date.now(),
                killFailed: info.killFailed,
              });
              void refreshLeases();
            }}
          />
        </div>
      ) : null}

      {otherLeases.length ? (
        <div className="ai-auth-other">
          <span className="ai-auth-other-title">{t('ai.auth.otherLeases')}</span>
          {otherLeases.map((item) => (
            <div key={item.id} className="ai-auth-other-row">
              <Badge variant="neutral" size="sm">
                {t(levelLabelKey(item.access))}
              </Badge>
              <code>
                {item.vm_name || `VM ${item.vmid}`} · {item.node}/{item.vmid}
              </code>
              <span className="fs-xs text-muted">
                <LeaseCountdown expiresAt={Number(item.expires_at || 0)} />
              </span>
              {(!target ||
                item.node === target.node &&
                  Number(item.vmid) === Number(target.vmid)) &&
              item.username ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => onPatch({ lease: item })}
                >
                  {t('ai.auth.useThis')}
                </Button>
              ) : null}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => void revoke({ vm: true }, item)}
                disabled={busy}
              >
                {t('ai.auth.revokeVm')}
              </Button>
            </div>
          ))}
        </div>
      ) : null}

      <AuthorizationDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        target={target}
        view={view}
        policy={policy}
        canExec={canExec}
        canVmRead={canVmRead}
        canVmWrite={canVmWrite}
        busy={busy}
        error={error}
        allowExec={allowExec}
        execAccess={execAccess}
        onSubmit={submitGrant}
      />

      <ConfirmDialog
        open={confirmRevoke}
        onCancel={() => setConfirmRevoke(false)}
        onConfirm={() => revoke({ host: true, vm: Boolean(matching) })}
        title={t('ai.auth.revokeConfirmTitle')}
        message={t('ai.auth.revokeConfirmMsg')}
        confirmText={t('ai.auth.confirmRevoke')}
        cancelText={t('ai.auth.cancel')}
        danger
        irreversible
        loading={busy}
      />
    </div>
  );
}

function AuthorizationDialog({
  open,
  onClose,
  target,
  view,
  policy,
  canExec,
  canVmRead,
  canVmWrite,
  busy,
  error,
  allowExec,
  execAccess,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  target: VmAiTarget | null;
  view: AiAuthorizationView;
  policy: VmAiPolicy | null;
  canExec: boolean;
  canVmRead: boolean;
  canVmWrite: boolean;
  busy: boolean;
  error: string;
  allowExec: boolean;
  execAccess: 'read' | 'write';
  onSubmit: (options: {
    level: AiAuthLevel;
    host: boolean;
    vm: boolean;
    minutes: number;
    fsRw: string[];
    units: string[];
    reason: string;
    replaceExisting: boolean;
  }) => Promise<void>;
}) {
  const t = useT();
  const [level, setLevel] = useState<AiAuthLevel>('read');
  const [host, setHost] = useState(true);
  const [vm, setVm] = useState(true);
  const [minutes, setMinutes] = useState('15');
  const [writable, setWritable] = useState<string[]>([]);
  const [units, setUnits] = useState('');
  const [reason, setReason] = useState('');
  const [replace, setReplace] = useState(false);
  // 运维授权是风险最高的一档：多一道显式确认，避免误点「按一下就走」
  const [opsConfirmed, setOpsConfirmed] = useState(false);
  const [agent, setAgent] = useState<'idle' | 'checking' | 'ready' | 'unavailable'>('idle');

  useEffect(() => {
    if (!open) return;
    const current = view.vm.lease;
    setLevel((current?.access as AiAuthLevel) || (allowExec && execAccess === 'write' ? 'write' : 'read'));
    // 默认只反映「用户手动开过的主机授权」。以前这里用了 ``|| canExec``，
    // 只要用户有权限就把开关预勾上，等于每打开一次面板就重新授权一次。
    setHost(allowExec);
    setVm(Boolean(target) && canVmRead);
    setMinutes(String(current?.minutes || 15));
    setWritable(current?.fs_rw || []);
    setUnits((current?.units || []).join(', '));
    setReason('');
    setReplace(false);
    setOpsConfirmed(false);
    if (!target) {
      setAgent('idle');
      return;
    }
    let alive = true;
    setAgent('checking');
    aiApi
      .vmAgent(target.node, target.vmid)
      .then((data) => {
        if (alive) setAgent(data.ok ? 'ready' : 'unavailable');
      })
      .catch(() => {
        if (alive) setAgent('idle');
      });
    return () => {
      alive = false;
    };
  }, [open, target, view.vm.lease, allowExec, execAccess, canExec, canVmRead]);

  const maxMinutes = Math.max(1, Number(policy?.max_minutes || 120));
  const minuteOptions = [15, 30, 60, 120]
    .filter((value) => value <= maxMinutes)
    .map((value) => ({ value: String(value), label: t(MINUTE_KEYS[value]) }));
  if (!minuteOptions.length) {
    minuteOptions.push({
      value: String(maxMinutes),
      label: `${maxMinutes} ${t('ai.auth.minutesUnit')}`,
    });
  }

  const vmSelectable = Boolean(target) && canVmRead;
  const opsAllowed = Boolean(policy?.allow_ops) && vm && canVmWrite;
  const writeAllowed = !vm || canVmWrite;
  const levelOptions: LevelOption[] = [
    {
      value: 'read',
      label: t('ai.auth.levelRead'),
      hint: t('ai.auth.levelReadHint'),
      disabled: false,
      reason: '',
    },
    {
      value: 'write',
      label: t('ai.auth.levelWrite'),
      hint: t('ai.auth.levelWriteHint'),
      disabled: !writeAllowed,
      reason: t('ai.auth.levelsLockedWrite'),
    },
    {
      value: 'ops',
      label: t('ai.auth.levelOps'),
      hint: t('ai.auth.levelOpsHint'),
      disabled: !opsAllowed,
      reason: !policy?.allow_ops
        ? t('ai.auth.levelsLockedOps')
        : t('ai.auth.levelOpsNeedsVm'),
    },
  ];
  // 通道或权限变化后，原级别可能不再可选；渲染时收敛到最近的可选档，
  // 不能在 render 里 setState（会让输入框每次按键都失焦）。
  const effectiveLevel: AiAuthLevel =
    level === 'ops' && !opsAllowed
      ? writeAllowed
        ? 'write'
        : 'read'
      : level === 'write' && !writeAllowed
        ? 'read'
        : level;

  const scopeChanged =
    vm &&
    Boolean(view.vm.lease) &&
    (view.vm.lease?.access !== effectiveLevel ||
      setOf(view.vm.lease?.fs_rw).size !== setOf(writable).size ||
      [...setOf(view.vm.lease?.fs_rw)].some((item) => !setOf(writable).has(item)) ||
      setOf(view.vm.lease?.units).size !== setOf(unitsList(units)).size ||
      [...setOf(view.vm.lease?.units)].some((item) => !setOf(unitsList(units)).has(item)));

  /** 主机命令是否已由当前运维授权覆盖（覆盖时无需再勾主机通道） */
  const hostCovered = view.host.coveredByLease;
  // 关掉虚拟机通道 = 撤掉这张票，覆盖也随之消失；此时要把主机开关交还给用户
  const hostCoveredNow = hostCovered && vm;
  const hostShown = hostCoveredNow || host;

  const canSubmit =
    !busy &&
    (hostShown || vm) &&
    (host ? canExec : true) &&
    (vm ? vmSelectable && agent !== 'unavailable' : true) &&
    (!scopeChanged || replace) &&
    // 运维授权必须显式勾选风险确认，否则确认按钮不可用
    (effectiveLevel !== 'ops' || opsConfirmed);

  const preview = useMemo(() => {
    const parts: string[] = [];
    if (hostCovered) {
      parts.push(`${t('ai.auth.hostChannel')}（${t('ai.auth.hostCovered')}）`);
    } else if (host) {
      parts.push(`${t('ai.auth.hostChannel')}（${t('ai.auth.sessionScope')}，${t(levelLabelKey(level))}）`);
    }
    if (vm && target) {
      parts.push(
        `${t('ai.auth.vmChannel')}「${t(levelLabelKey(effectiveLevel))}」（${minutes} ${t('ai.auth.minutesUnit')}）`,
      );
    }
    return parts.join(' + ');
  }, [host, hostCoveredNow, vm, target, effectiveLevel, minutes, t]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('ai.auth.panelTitle')}
      description={t('ai.auth.panelDesc')}
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('ai.auth.cancel')}
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!canSubmit}
            onClick={() =>
              void onSubmit({
                level: effectiveLevel,
                // 覆盖是派生的，不凭这次提交把主机授权写死；用户此前手动开过
                // 就保留原值，租约到期后仍按他的手动选择生效
                host: hostCovered ? allowExec : host,
                vm,
                minutes: Number(minutes) || 15,
                fsRw: effectiveLevel === 'write' ? writable : [],
                units:
                  effectiveLevel === 'write'
                    ? unitsList(units)
                    : [],
                reason,
                replaceExisting: replace,
              })
            }
          >
            {view.vm.expired ? t('ai.auth.primaryGrant') : t('ai.auth.confirm')}
          </Button>
        </>
      }
    >
      <div className="ai-auth-form">
        <div className="ai-auth-section">
          <span className="ai-auth-section-title">{t('ai.auth.level')}</span>
          <div className="ai-auth-levels">
            {levelOptions.map((option) => (
              <button
                key={option.value}
                type="button"
                className={`ai-auth-level${effectiveLevel === option.value ? ' is-on' : ''}`}
                disabled={option.disabled}
                onClick={() => {
                  setLevel(option.value);
                  if (option.value !== 'ops') setOpsConfirmed(false);
                }}
              >
                <span className="ai-auth-level-name">{option.label}</span>
                <span className="ai-auth-level-hint">
                  {option.disabled ? option.reason : option.hint}
                </span>
              </button>
            ))}
          </div>
          {effectiveLevel === 'ops' ? (
            <Notice tone="warning" title={t('ai.auth.levelOpsRisk')}>
              <Switch
                checked={opsConfirmed}
                onChange={setOpsConfirmed}
                label={t('ai.auth.opsConfirm')}
              />
            </Notice>
          ) : null}
        </div>

        <div className="ai-auth-section">
          <span className="ai-auth-section-title">{t('ai.auth.channels')}</span>
          <div className="ai-auth-switches">
            <Switch
              checked={hostShown}
              onChange={setHost}
              disabled={!canExec || hostCoveredNow}
              label={t('ai.auth.hostChannel')}
              hint={
                hostCoveredNow
                  ? t('ai.auth.hostCoveredHint')
                  : canExec
                    ? t('ai.auth.channelHostHint')
                    : t('ai.auth.hostUnavailable')
              }
            />
            <Switch
              checked={vm}
              onChange={setVm}
              disabled={!vmSelectable}
              label={t('ai.auth.vmChannel')}
              hint={
                !target
                  ? t('ai.auth.noVm')
                  : !canVmRead
                    ? t('ai.auth.vmUnavailable')
                    : agent === 'checking'
                      ? t('ai.auth.agentChecking')
                      : agent === 'unavailable'
                        ? t('ai.auth.agentMissing')
                        : t('ai.auth.channelVmHint')
              }
            />
          </div>
          {target && agent === 'unavailable' ? (
            <Notice tone="warning" title={t('ai.auth.agentMissing')}>
              {t('ai.lease.agentMissingHint')}
            </Notice>
          ) : null}
          {/* 取消虚拟机通道不是「这次不签」，而是「把这张票撤掉」（见 grant 接口
              的 elif vm_payload 分支）。不提前说清，用户想只开主机命令时会顺手
              关掉它，结果正在跑的排查被撤了授权。 */}
          {view.vm.lease && !vm ? (
            <Notice tone="warning" title={t('ai.auth.dropVmTitle')}>
              {t('ai.auth.dropVmHint')}
            </Notice>
          ) : null}
        </div>

        {vm ? (
          <div className="ai-auth-section">
            <span className="ai-auth-section-title">{t('ai.auth.vmScope')}</span>
            <div className="ai-lease-grid">
              <Field label={t('ai.auth.minutes')} hint={t('ai.lease.minutesHint')}>
                <Select
                  value={minutes}
                  onChange={(event) => setMinutes(event.target.value)}
                  options={minuteOptions}
                />
              </Field>
              {effectiveLevel === 'write' ? (
                <>
                  <Field label={t('ai.auth.scopeWritable')} hint={t('ai.lease.writableHint')}>
                    <div className="ai-lease-scope">
                      {WRITABLE_DIRS.map((dir) => {
                        const on = writable.includes(dir);
                        return (
                          <button
                            key={dir}
                            type="button"
                            className={`os-chip${on ? ' is-on' : ''}`}
                            aria-pressed={on}
                            onClick={() =>
                              setWritable((prev) =>
                                on ? prev.filter((item) => item !== dir) : [...prev, dir],
                              )
                            }
                          >
                            {dir}
                          </button>
                        );
                      })}
                    </div>
                  </Field>
                  <Field label={t('ai.auth.scopeUnits')} hint={t('ai.lease.unitsHint')}>
                    <Input
                      value={units}
                      onChange={(event) => setUnits(event.target.value)}
                      placeholder="nginx, php8.2-fpm"
                      mono
                    />
                  </Field>
                </>
              ) : null}
            </div>
            {effectiveLevel === 'read' ? (
              <Notice tone="info">{t('ai.auth.scopeReadOnly')}</Notice>
            ) : null}
          </div>
        ) : null}

        <Field label={t('ai.auth.reason')} hint={t('ai.lease.reasonHint')}>
          <Input
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder={t('ai.auth.reasonPlaceholder')}
          />
        </Field>

        {scopeChanged ? (
          <Notice tone="warning" title={t('ai.auth.conflict')}>
            <div>{t('ai.auth.conflictHint')}</div>
            <Switch
              checked={replace}
              onChange={setReplace}
              label={t('ai.auth.replace')}
            />
          </Notice>
        ) : null}

        <Notice tone="info" title={t('ai.auth.preview')}>
          {preview || t('ai.auth.noChannel')}
        </Notice>

        {error ? (
          <div className="login-error" role="alert">
            {error}
          </div>
        ) : null}
      </div>
    </Modal>
  );
}

function unitsList(value: string): string[] {
  return value
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function setOf(values: string[] | undefined): Set<string> {
  return new Set(values || []);
}
