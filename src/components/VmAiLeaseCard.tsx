/* ==========================================================================
   pvecenter — AI 的「虚拟机内取证」授权卡

   为什么不是「一个开关」
   --------------------
   「允许 AI 进虚拟机」是个含糊的授权：进哪台、看什么、能不能改、改多久，
   每一项的后果都不一样。这里把它拆成一张**票据**：目标机器、可读目录、
   可写目录、可重启的服务、时长，一次填清楚。

   写能力默认不勾：勾了要有 ``ai.vm.write`` 权限，而且每次写都要在对话里
   单独批准（带 diff），所以这里不需要再用「确认弹窗」拦一次 —— 拦在最该拦
   的地方（具体那次改动）比拦在授权时有用得多。

   倒计时按 ``expires_at`` 本地推算，不用 ``remaining`` 每秒减一：
   标签页在后台被节流时，递减会失准，而时间戳不会。
   ========================================================================== */

import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import {
  aiApi,
  type VmAiAction,
  type VmAiLease,
  type VmAiTarget,
} from '../api/endpoints';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Field, Input, Select } from './ui/Input';
import { Notice } from './ui/EmptyState';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../hooks/useToast';
import { useT } from '../i18n';

/** 可申请的写目录：与后端 ``aivm.WRITABLE_DIRS`` 同一份口径 */
const WRITABLE_DIRS = ['/tmp', '/var/tmp', '/var/log', '/etc', '/opt', '/srv'];

/** 动作流水的刷新间隔：够近能看着它动，又不至于每 5 秒打一次接口 */
const REFRESH_MS = 15000;

function useCountdown(expiresAt: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return Math.max(0, Math.floor(expiresAt - now / 1000));
}

function formatLeft(seconds: number) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

export function VmAiLeaseCard({
  lease,
  onLeaseChange,
  target,
}: {
  /** 当前生效的授权；为空时显示「申请」入口 */
  lease: VmAiLease | null;
  /** 签发 / 撤销 / 到期后回调，父组件据此更新传给对话的租约 id */
  onLeaseChange: (lease: VmAiLease | null) => void;
  /**
   * 目标虚拟机由**页面顶部统一选择**后传进来。
   *
   * 这里不再放第二个「目标虚拟机」下拉：同一台机器选两次既啰嗦，也容易选岔
   * （上面选 101、下面选 103，用户往往看不出自己授权的是哪台）。
   */
  target: VmAiTarget | null;
}) {
  const t = useT();
  const toast = useToast();
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [actions, setActions] = useState<VmAiAction[]>([]);
  /**
   * 服务端仍然生效的授权。
   *
   * 为什么需要单独拉一份：``lease`` 只是**本次页面会话**选中的那张票，刷新页面后
   * 就丢了；而票在服务端还活着。没有这份列表，用户会遇到「提示已有一张不同级别的
   * 授权，请先撤销」却翻遍界面找不到撤销入口 —— 那张票根本不在当前状态里。
   */
  const [activeLeases, setActiveLeases] = useState<VmAiLease[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  /** diagd 安装脚本：按需拉取，展开一次就够，不占默认视野 */
  const [diagd, setDiagd] = useState<{ version: string; script: string; hint: string } | null>(
    null,
  );

  /**
   * 选中机器后的纳管状态。没有 Guest Agent 的机器连不进去，不该让它走到
   * 「签发」那一步再失败 —— 这是「只给被安全管控的虚拟机授权」的落点。
   */
  const [agentCheck, setAgentCheck] = useState<
    'idle' | 'checking' | 'ready' | 'unavailable'
  >('idle');
  /** 授权级别：默认「受限变更」，与改造前的行为一致；运维授权必须显式选择 */
  const [access, setAccess] = useState<'read' | 'write' | 'ops'>('write');
  const [writable, setWritable] = useState<string[]>([]);
  const [units, setUnits] = useState('');
  const [minutes, setMinutes] = useState('15');
  const [reason, setReason] = useState('');

  const left = useCountdown(lease?.expires_at ?? 0);
  const expired = Boolean(lease) && left <= 0;

  const refreshLeases = useCallback(async () => {
    try {
      const data = await aiApi.vmLeases();
      const now = Date.now() / 1000;
      setActiveLeases(
        data.items.filter((item) => item.status === 'active' && item.expires_at > now),
      );
    } catch {
      /* 拉列表失败不弹错：签发本身会给出真正的错误，这里失败只是少一个入口 */
    }
  }, []);

  /* 过期就通知父组件把租约摘掉：否则后续每条消息都会带着一个已失效的租约 id
     去请求，后端直接 403，用户看到的却是「发送失败」而不是「授权到期了」。 */
  const notified = useRef(false);
  useEffect(() => {
    if (!lease) {
      notified.current = false;
      return;
    }
    if (expired && !notified.current) {
      notified.current = true;
      onLeaseChange(null);
      // 过期后重新拉列表：清单里可能还留着这条已失效的记录
      void refreshLeases();
    }
  }, [expired, lease, onLeaseChange, refreshLeases]);

  // 挂载就拉一次：刷新页面后，服务端仍生效的票必须能在这里被看见、被撤销
  useEffect(() => {
    void refreshLeases();
  }, [refreshLeases]);

  useEffect(() => {
    if (open) void refreshLeases();
  }, [open, refreshLeases]);

  const accessLabel = (value: string) =>
    value === 'ops'
      ? t('ai.lease.accessOpsShort')
      : value === 'read'
        ? t('ai.lease.accessReadShort')
        : t('ai.lease.accessWriteShort');

  /* 动作流水：授权存续期间每 15 秒刷一次，撤销后停。
     它是这张卡最值钱的部分 —— 用户签完授权最想知道的就是「AI 刚才干了什么」。 */
  useEffect(() => {
    if (!lease || expired) {
      setActions([]);
      return;
    }
    let alive = true;
    const pull = async () => {
      try {
        const data = await aiApi.vmLease(lease.id);
        if (alive) setActions(data.actions);
      } catch {
        /* 刷新失败不弹错：授权本身还有效，等下个周期再试 */
      }
    };
    void pull();
    const timer = window.setInterval(() => void pull(), REFRESH_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, [lease, expired]);

  /* 选中就探一次 Guest Agent：列表只能保证「运行中」，agent 在不在线必须问一次。
     探测本身 5 秒封顶，不阻塞列表；接口不可用时放行，由签发时的服务端校验兜底。 */
  useEffect(() => {
    if (!open || !target) {
      setAgentCheck('idle');
      return;
    }
    let alive = true;
    setAgentCheck('checking');
    aiApi
      .vmAgent(target.node, target.vmid)
      .then((data) => {
        if (alive) setAgentCheck(data.ok ? 'ready' : 'unavailable');
      })
      .catch(() => {
        if (alive) setAgentCheck('idle');
      });
    return () => {
      alive = false;
    };
  }, [open, target]);

  const grant = async () => {
    if (!target) {
      setError(t('ai.lease.pickRequired'));
      return;
    }
    if (agentCheck === 'unavailable') {
      setError(t('ai.lease.agentMissingHint'));
      return;
    }
    setBusy(true);
    setError('');
    try {
      const data = await aiApi.vmGrant({
        node: target.node,
        vmid: target.vmid,
        vm_name: target.name,
        guest_type: target.guest_type,
        // 运维授权不提交目录/服务：后端整机放行，具体每一步走审批。
        fs_rw: access === 'write' ? writable : [],
        units:
          access === 'write'
            ? units
                .split(/[,，\s]+/)
                .map((item) => item.trim())
                .filter(Boolean)
            : [],
        access,
        minutes: Number(minutes) || 15,
        reason,
      });
      onLeaseChange(data.lease);
      setOpen(false);
      setWritable([]);
      setUnits('');
      setReason('');
      // 级别回到最保守的一档：上一次选了运维授权，不该让下一次「顺手」也是整机权限
      setAccess('write');
    } catch (err) {
      setError(errorMessage(err));
      // 409「这台机器已有一张不同级别的授权」：立刻刷新列表，用户就能就地撤销
      void refreshLeases();
    } finally {
      setBusy(false);
    }
  };

  /**
   * 撤销一张票。传的是票本身而不是读 ``lease`` state —— 列表里的票可能不属于
   * 当前会话（刷新后重新拉到的），那种情况下 ``lease`` 是 null。
   */
  const revokeLease = async (target: VmAiLease) => {
    setBusy(true);
    setError('');
    try {
      const data = await aiApi.vmRevoke(target.id, t('ai.lease.revokedByUser'));
      // 撤销成功 ≠ 客户机里那条命令停了：兼容模式没有终止开关，必须如实说清楚，
      // 否则用户会以为「撤了授权就安全了」，而日志还在被 tail。
      if (data.kill && data.kill.ok) {
        toast.success(t('ai.lease.revoked'), data.kill.output);
      } else if (data.kill && data.kill.output) {
        toast.warning(t('ai.lease.revokedKillFailed'), data.kill.output);
      } else {
        toast.success(t('ai.lease.revoked'));
      }
      // 只有撤销的是当前选中的那张，才需要把对话里的租约 id 清掉
      if (lease && lease.id === target.id) onLeaseChange(null);
      await refreshLeases();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    if (!lease) return;
    await revokeLease(lease);
  };

  /** 只终止运行中的命令，不动授权本身：排查到一半想停的往往只是某一条 */
  const kill = async () => {
    if (!lease) return;
    setBusy(true);
    setError('');
    try {
      const result = await aiApi.vmKill(lease.id);
      if (result.ok) toast.success(t('ai.lease.killOk'), result.output);
      else toast.warning(t('ai.lease.killFailed'), result.output);
    } catch (err) {
      toast.error(t('ai.lease.killFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  /** 兼容模式没有沙箱：把 diagd 安装脚本直接给到用户，别让他去翻文档找版本 */
  const loadDiagd = async () => {
    if (diagd) {
      setDiagd(null);
      return;
    }
    setBusy(true);
    try {
      const data = await aiApi.vmDiagd();
      setDiagd(data);
    } catch (err) {
      toast.error(t('ai.lease.diagdLoadFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const copyDiagd = async () => {
    if (!diagd) return;
    try {
      await navigator.clipboard.writeText(diagd.script);
      toast.success(t('ai.lease.diagdCopied'));
    } catch {
      toast.error(t('ai.lease.diagdLoadFailed'));
    }
  };

  const rollback = async (payload: { action_id?: string; mode?: 'file' | 'snapshot' }) => {
    if (!lease) return;
    setBusy(true);
    setError('');
    try {
      const result = await aiApi.vmRollback(lease.id, payload);
      if (!result.ok) setError(result.output);
      else {
        const data = await aiApi.vmLease(lease.id);
        setActions(data.actions);
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  /* ------------------------------------------------------------- 无授权：申请 */
  if (!lease) {
    return (
      <div className="ai-lease">
        <div className="ai-lease-head">
          {/* 标题由外层「02 授权范围」提供，这里只留状态与入口，避免同一段文字出现两遍 */}
          <Badge variant={activeLeases.length ? 'warning' : 'neutral'} size="sm">
            {activeLeases.length
              ? t('ai.lease.activeCount', { n: activeLeases.length })
              : t('ai.lease.noneBadge')}
          </Badge>
          <span className="fs-xs text-muted" style={{ marginLeft: 'auto' }}>
            {activeLeases.length
              ? t('ai.lease.activeListHint')
              : t('ai.lease.noneHint')}
          </span>
          <Button size="sm" variant="ghost" onClick={() => setOpen((prev) => !prev)}>
            {open ? t('ai.lease.collapse') : t('ai.lease.open')}
          </Button>
        </div>

        {error ? (
          <div className="login-error" role="alert" style={{ marginTop: 8 }}>
            {error}
          </div>
        ) : null}

        {/* 生效中的授权常驻显示，不折叠：撤销入口必须比签发入口更容易找到，
            否则「请先撤销再签发」就成了一条走不通的死路。 */}
        {activeLeases.length ? (
          <div className="ai-lease-actions-log" style={{ marginTop: 10 }}>
            <div className="ai-lease-sub" style={{ marginBottom: 6 }}>
              {t('ai.lease.activeList')}
            </div>
            {activeLeases.map((item) => (
              <div key={item.id} className="ai-lease-action-row">
                <Badge
                  variant={item.access === 'ops' ? 'warning' : 'neutral'}
                  size="sm"
                >
                  {accessLabel(item.access)}
                </Badge>
                <code>
                  {item.vm_name || `VM ${item.vmid}`} · {item.node}/{item.vmid}
                </code>
                <span className="fs-xs text-muted">
                  {t('ai.lease.left', {
                    time: formatLeft(
                      Math.max(0, Math.floor(item.expires_at - Date.now() / 1000)),
                    ),
                  })}
                </span>
                {item.username === user?.username ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={busy}
                    onClick={() => {
                      onLeaseChange(item);
                      setOpen(false);
                    }}
                  >
                    {t('ai.lease.useThis')}
                  </Button>
                ) : (
                  <span className="fs-xs text-muted">{item.username}</span>
                )}
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void revokeLease(item)}
                >
                  {t('ai.lease.revokeThis')}
                </Button>
              </div>
            ))}
          </div>
        ) : null}

        {open ? (
          <>
            {/* 目标虚拟机由页面顶部统一选择，这里只回显「授权给哪台」：
                同一屏放两个选择器，很容易出现上面选 101、下面选 103，而用户
                看不出自己到底授权了哪台机器。 */}
            {target ? (
              <div className="ai-lease-agent" style={{ marginTop: 8 }}>
                <span className="fs-xs text-muted">{t('ai.lease.target')}</span>
                <code>
                  {target.name || `VM ${target.vmid}`} · {target.node}/{target.vmid}
                </code>
                {agentCheck === 'checking' ? (
                  <span className="fs-xs text-muted">{t('ai.lease.agentChecking')}</span>
                ) : agentCheck === 'ready' ? (
                  <Badge variant="success" size="sm" dot>
                    {t('ai.lease.agentReady')}
                  </Badge>
                ) : agentCheck === 'unavailable' ? (
                  <Badge variant="warning" size="sm" dot>
                    {t('ai.lease.agentMissing')}
                  </Badge>
                ) : null}
              </div>
            ) : (
              <Notice tone="info">{t('ai.lease.pickRequired')}</Notice>
            )}
            {/* 未响应 agent 时把话说完：用户通常以为「运行中就能授权」，
                实际还差安装 / 启动 qemu-guest-agent 这一步 */}
            {target && agentCheck === 'unavailable' ? (
              <Notice tone="warning" title={t('ai.lease.agentMissing')}>
                {t('ai.lease.agentMissingHint')}
              </Notice>
            ) : null}
            <div className="ai-lease-grid">
              <Field label={t('ai.lease.access')} hint={t('ai.lease.accessHint')}>
                <Select
                  value={access}
                  onChange={(event) => {
                    const next = event.target.value as 'read' | 'write' | 'ops';
                    setAccess(next);
                    // 切到运维授权后原来勾的目录/服务不再有意义，清掉避免票面上
                    // 留下「授了整机又写着只改 /tmp」这种自相矛盾的记录
                    if (next !== 'write') {
                      setWritable([]);
                      setUnits('');
                    }
                  }}
                  options={[
                    { label: t('ai.lease.accessRead'), value: 'read' },
                    { label: t('ai.lease.accessWrite'), value: 'write' },
                    { label: t('ai.lease.accessOps'), value: 'ops' },
                  ]}
                />
              </Field>
              <Field label={t('ai.lease.minutes')} hint={t('ai.lease.minutesHint')}>
                <Select
                  value={minutes}
                  onChange={(event) => setMinutes(event.target.value)}
                  options={[
                    { label: t('ai.lease.min15'), value: '15' },
                    { label: t('ai.lease.min30'), value: '30' },
                    { label: t('ai.lease.min60'), value: '60' },
                    { label: t('ai.lease.min120'), value: '120' },
                  ]}
                />
              </Field>
            </div>

            {access === 'write' ? (
              <div className="ai-lease-grid">
                <Field label={t('ai.lease.writable')} hint={t('ai.lease.writableHint')}>
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
                <Field label={t('ai.lease.units')} hint={t('ai.lease.unitsHint')}>
                  <Input
                    value={units}
                    onChange={(event) => setUnits(event.target.value)}
                    placeholder="nginx, php8.2-fpm"
                    mono
                  />
                </Field>
              </div>
            ) : null}

            {access === 'ops' ? (
              <Notice tone="warning" title={t('ai.lease.accessOps')}>
                {t('ai.lease.accessOpsWarn')}
              </Notice>
            ) : null}

            {access === 'read' ? (
              <Notice tone="info">{t('ai.lease.readOnlyHint')}</Notice>
            ) : null}

            <Field label={t('ai.lease.reason')} hint={t('ai.lease.reasonHint')}>
              <Input
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder={t('ai.lease.reasonPlaceholder')}
              />
            </Field>

            <div className="ai-lease-actions">
              <Button
                variant="primary"
                size="sm"
                onClick={() => void grant()}
                loading={busy}
                disabled={agentCheck === 'checking' || agentCheck === 'unavailable'}
              >
                {t('ai.lease.grant')}
              </Button>
              <span className="fs-xs text-muted">
                {access === 'ops'
                  ? t('ai.lease.accessOpsShortWarn')
                  : access === 'write' && (writable.length || units.trim())
                    ? t('ai.lease.writeWarn')
                    : t('ai.lease.readOnlyHint')}
              </span>
            </div>
          </>
        ) : null}
      </div>
    );
  }

  /* ------------------------------------------------------------- 有授权：状态 */
  return (
    <div className="ai-lease">
      <div className="ai-lease-head">
        <span className="ai-lease-title">
          {lease.vm_name || `VM ${lease.vmid}`}
        </span>
        <span className="ai-lease-sub">
          {lease.node} / {lease.vmid}
        </span>
        <Badge variant={expired ? 'neutral' : 'success'} size="sm">
          {expired ? t('ai.lease.expired') : t('ai.lease.active')}
        </Badge>
        {/* 系统与隔离等级是解释「为什么有些操作做不了」的前提：
            Windows 的路径语义不同，兼容模式下也没有可终止的开关。 */}
        <Badge variant="neutral" size="sm">
          {lease.os_type === 'windows'
            ? t('ai.lease.osWindows')
            : t('ai.lease.osLinux')}
        </Badge>
        <Badge
          variant={lease.mode === 'standard' ? 'success' : 'warning'}
          size="sm"
          title={
            lease.mode === 'standard'
              ? t('ai.lease.modeSandboxHint')
              : t('ai.lease.modeCompatHint')
          }
        >
          {lease.mode === 'standard'
            ? t('ai.lease.modeSandbox')
            : t('ai.lease.modeCompat')}
        </Badge>
        {/* 级别是这张票最该被看见的一行：它决定了 AI 能不能装软件 */}
        <Badge
          variant={lease.access === 'ops' ? 'warning' : 'neutral'}
          size="sm"
          title={t('ai.lease.accessHint')}
        >
          {lease.access === 'ops'
            ? t('ai.lease.accessOpsShort')
            : lease.access === 'read'
              ? t('ai.lease.accessReadShort')
              : t('ai.lease.accessWriteShort')}
        </Badge>
        {lease.double_approve ? (
          <Badge variant="warning" size="sm" title={t('ai.lease.doubleApproveHint')}>
            {t('ai.lease.doubleApprove')}
          </Badge>
        ) : null}
        <span className="ai-lease-sub" style={{ marginLeft: 'auto' }}>
          {t('ai.lease.left', { time: formatLeft(left) })} ·{' '}
          {t('ai.lease.quota', {
            used: lease.actions_used,
            max: lease.actions_max,
          })}
        </span>
      </div>

      {error ? (
        <div className="login-error" role="alert" style={{ marginTop: 8 }}>
          {error}
        </div>
      ) : null}

      <div className="ai-lease-grid">
        <div className="ai-lease-field">
          <span className="ai-lease-label">{t('ai.lease.fsRo')}</span>
          <div className="ai-lease-scope">
            {lease.fs_ro.map((dir) => (
              <span key={dir} className="ai-lease-chip">
                {dir}
              </span>
            ))}
          </div>
        </div>
        <div className="ai-lease-field">
          <span className="ai-lease-label">{t('ai.lease.fsRw')}</span>
          {lease.fs_rw.length ? (
            <div className="ai-lease-scope">
              {lease.fs_rw.map((dir) => (
                <span key={dir} className="ai-lease-chip">
                  {dir}
                </span>
              ))}
            </div>
          ) : (
            <span className="fs-xs text-muted">{t('ai.lease.readOnly')}</span>
          )}
        </div>
        <div className="ai-lease-field">
          <span className="ai-lease-label">{t('ai.lease.unitsLabel')}</span>
          {lease.units.length ? (
            <div className="ai-lease-scope">
              {lease.units.map((unit) => (
                <span key={unit} className="ai-lease-chip">
                  {unit}
                </span>
              ))}
            </div>
          ) : (
            <span className="fs-xs text-muted">{t('ai.lease.none')}</span>
          )}
        </div>
        <div className="ai-lease-field">
          <span className="ai-lease-label">{t('ai.lease.snapshot')}</span>
          <span className="ai-lease-sub">
            {lease.snapshot || t('ai.lease.snapshotNone')}
          </span>
        </div>
      </div>

      {/* 标签是策略的输入：prod 机器为什么禁写，看这一行比看错误提示更直接 */}
      <div className="flex items-center gap-6 flex-wrap" style={{ marginTop: 8 }}>
        <span className="ai-lease-label">{t('ai.lease.tags')}</span>
        {lease.tags.length ? (
          lease.tags.map((tag) => (
            <span key={tag} className="ai-lease-chip">
              {tag}
            </span>
          ))
        ) : (
          <span className="fs-xs text-muted">{t('ai.lease.noTags')}</span>
        )}
      </div>

      {lease.policy_note ? (
        <Notice tone="warning" title={t('ai.lease.policyBlocked')}>
          {lease.policy_note}
        </Notice>
      ) : null}

      {lease.access === 'ops' ? (
        <Notice tone="warning" title={t('ai.lease.accessOpsShort')}>
          {t('ai.lease.accessOpsActive')}
        </Notice>
      ) : null}

      {/* 兼容模式：说清楚隔离靠什么、差在哪，并给出升级路径。
          不写这一段，用户会默认「授权 = 沙箱」，出事时才发现没有资源上限。 */}
      {lease.mode === 'standard' ? null : (
        <Notice tone="info" title={t('ai.lease.modeCompat')}>
          <div style={{ marginBottom: 6 }}>{t('ai.lease.modeCompatHint')}</div>
          <Button size="sm" variant="ghost" loading={busy} onClick={() => void loadDiagd()}>
            {diagd ? t('ai.lease.diagdCollapse') : t('ai.lease.diagd')}
          </Button>
          {diagd ? (
            <div style={{ marginTop: 8 }}>
              <div className="fs-xs text-muted">
                {t('ai.lease.diagdVersion', { version: diagd.version })}
                {diagd.hint ? ` · ${diagd.hint}` : ''}
              </div>
              <pre
                className="ai-lease-script"
                style={{
                  maxHeight: 220,
                  overflow: 'auto',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                }}
              >
                {diagd.script}
              </pre>
              <Button size="sm" variant="secondary" onClick={() => void copyDiagd()}>
                {t('ai.lease.diagdCopy')}
              </Button>
            </div>
          ) : null}
        </Notice>
      )}

      {actions.length ? (
        <div className="ai-lease-actions-log">
          {actions.map((action) => (
            <div key={action.id} className="ai-lease-action-row">
              <Badge
                variant={action.ok ? 'neutral' : 'danger'}
                size="sm"
              >
                {action.verdict === 'deny' ? t('ai.lease.denied') : action.risk_label}
              </Badge>
              <code title={action.target}>
                {action.tool} {action.target}
              </code>
              {action.tool === 'vm_write_file' && action.verdict === 'allow' ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() => void rollback({ action_id: action.id, mode: 'file' })}
                >
                  {t('ai.lease.rollbackFile')}
                </Button>
              ) : null}
            </div>
          ))}
        </div>
      ) : (
        <div className="fs-xs text-muted" style={{ marginTop: 8 }}>
          {t('ai.lease.noActions')}
        </div>
      )}

      <div className="ai-lease-actions">
        <Button
          size="sm"
          variant="primary"
          onClick={() => void revoke()}
          loading={busy}
          disabled={expired}
        >
          {t('ai.lease.revoke')}
        </Button>
        {/* 只停命令、不撤授权：一条 tail -f 不该逼用户把整张票作废 */}
        {lease.mode === 'standard' ? (
          <Button
            size="sm"
            variant="secondary"
            disabled={busy || expired}
            onClick={() => void kill()}
            title={t('ai.lease.killHint')}
          >
            {t('ai.lease.kill')}
          </Button>
        ) : null}
        {lease.snapshot ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => void rollback({ mode: 'snapshot' })}
          >
            {t('ai.lease.rollbackSnapshot')}
          </Button>
        ) : null}
        <span className="fs-xs text-muted" style={{ marginLeft: 'auto' }}>
          {t('ai.lease.inChatHint')}
        </span>
      </div>
    </div>
  );
}
