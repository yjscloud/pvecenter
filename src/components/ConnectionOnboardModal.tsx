/* ==========================================================================
   pvecenter — PVE「一键接入」：签发链接 → 节点上粘贴一条命令即可
   ==========================================================================

   要解决的问题：机房多、集群多，一台台在浏览器里填表单不现实，而 PVE 节点上的
   脚本**没法走**新建连接那条路 —— 它要求 step-up 二次确认，step-up 对 API Token
   一律 403（令牌是无人值守凭据，见 app/security.require_step_up）。

   于是把「管理员在浏览器里的一次授权」换成一张**短期、一次性**的注册码：脚本
   拿着它只能建**一条**连接，用完即废。本弹窗负责两件事：

     1. 签发 / 吊销注册码（明文只在这里出现一次）；
     2. 给出**一条命令**：``bash -c "$(curl -fsSL '<面板>/api/connections/onboard.sh?code=...')"``

   命令里的脚本由后端渲染（见 app/onboard.py），不再在前端拼 bash：两处维护同一
   段脚本迟早会漂移成「网页上那套能跑、链接下载那套不能跑」。前端只负责展示与
   复制，脚本里的面板地址也由后端按「设置里的对外地址」决定，而不是浏览器当前
   的 origin —— 管理员从内网访问面板时，节点那边往往连不上那个地址。
   ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { connectionsApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Input, Select } from './ui/Input';
import { Modal } from './ui/Modal';
import { Notice } from './ui/EmptyState';
import { IconCheck, IconCopy, IconInfo, IconLink, IconTerminal } from './Icons';
import { useToast } from '../hooks/useToast';
import { useT, type TFunc } from '../i18n';
import type { RegisterCodeIssued, RegisterCodeRow } from '../api/types';

export interface ConnectionOnboardModalProps {
  open: boolean;
  onClose: () => void;
  /** 注册成功 / 注册码被用掉后调用：让连接列表去重新拉一遍 */
  onChanged?: () => void;
}

/* 有效期选项：批量接入通常几分钟就跑完，给到 4 小时是「中途去吃饭」的余量；
   再长就等于一张长期有效的「建连接许可证」，不如直接发 API Token。 */
const TTL_OPTIONS = [15, 30, 60, 240];

/* 可用次数：1 是一次性（旧行为），0 表示不限。中间几档覆盖「一个机房几台 / 十几台」
   的常见规模；再往上就等于长期凭据了，不如发 API Token。 */
const USES_OPTIONS = [1, 3, 5, 10, 20, 0];

/** 注册码状态 → 徽章样式 */
const STATUS_VARIANT = {
  active: 'success',
  used: 'info',
  exhausted: 'warning',
  expired: 'neutral',
  revoked: 'neutral',
} as const;

/**
 * 命令的兜底拼法。
 *
 * 正常情况下面板会直接回 ``command``；这里兜的是「前端比后端新」的场景（面板刚升级、
 * 后端还没重启）。不兜的话命令块会渲染成一片空白 —— 用户以为功能坏了，而实际只是
 * 少了一个字段。
 */
function fallbackCommand(issued: RegisterCodeIssued): string {
  const url =
    issued.script_url ||
    `${window.location.origin}/api/connections/onboard.sh?code=${encodeURIComponent(
      issued.code,
    )}`;
  return `bash -c "$(curl -fsSL '${url}')"`;
}

/* 状态文案用 switch 而不是 `t('...' + status)`：MessageKey 是字面量联合，
   拼出来的字符串过不了类型检查，拼错也只有在运行时才会暴露成「显示成 key」。 */
function statusLabel(t: TFunc, status: RegisterCodeRow['status']): string {
  switch (status) {
    case 'used':
      return t('conn.onboardStatus.used');
    case 'exhausted':
      return t('conn.onboardStatus.exhausted');
    case 'expired':
      return t('conn.onboardStatus.expired');
    case 'revoked':
      return t('conn.onboardStatus.revoked');
    default:
      return t('conn.onboardStatus.active');
  }
}

export function ConnectionOnboardModal({
  open,
  onClose,
  onChanged,
}: ConnectionOnboardModalProps) {
  const t = useT();
  const toast = useToast();

  const [region, setRegion] = useState('');
  const [note, setNote] = useState('');
  const [ttl, setTtl] = useState('30');
  const [maxUses, setMaxUses] = useState('1');
  const [issuing, setIssuing] = useState(false);
  const [issued, setIssued] = useState<RegisterCodeIssued | null>(null);
  /* 签发失败就地把原因贴在表单上方：只弹 toast 的话，用户点完按钮一抬眼就找不到
     错在哪了，尤其是 TTL / 次数被后端参数校验拦下的时候。 */
  const [issueError, setIssueError] = useState<string | null>(null);
  const [codes, setCodes] = useState<RegisterCodeRow[]>([]);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  /* 删除是二次确认，但不用弹窗：点一次变成「确认删除」，再点才真删。
     一屏能清好几条时，比一路弹窗快得多，也不会误触。 */
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  /* 「已复制」不能常亮：用户过一会儿还要再复制一次，按钮一直写着「已复制」会
     让人以为点不动。2.5 秒后回到常态。 */
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2500);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const loadCodes = useCallback(async () => {
    try {
      setCodes(await connectionsApi.registerCodes());
    } catch {
      /* 列表只是辅助信息：拿不到不拦签发流程 */
    }
  }, []);

  useEffect(() => {
    if (open) void loadCodes();
  }, [open, loadCodes]);

  const issue = async () => {
    setIssuing(true);
    setIssueError(null);
    try {
      const data = await connectionsApi.issueRegisterCode({
        region: region.trim(),
        note: note.trim(),
        ttl_minutes: Number(ttl),
        max_uses: Number(maxUses),
      });
      setIssued(data);
      setCopied(false);
      void loadCodes();
      /* 拿到链接后用户的下一个动作一定是「粘贴到终端」，顺手复制一次：
         少了「再来点一下复制」这一步。浏览器拒绝写剪贴板就静默跳过，
         按钮还在，不会卡住流程。 */
      try {
        await navigator.clipboard.writeText(data.command || fallbackCommand(data));
        setCopied(true);
        toast.success(t('conn.onboardAutoCopied'));
      } catch {
        /* 交给「复制命令」按钮 */
      }
    } catch (err) {
      setIssueError(errorMessage(err));
      toast.error(t('conn.onboardIssueFailed'), errorMessage(err));
    } finally {
      setIssuing(false);
    }
  };

  const copyCommand = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      toast.success(t('conn.onboardCopied'));
    } catch {
      toast.error(t('conn.onboardCopyFailed'));
    }
  };

  const revoke = async (id: string) => {
    setRevoking(id);
    try {
      await connectionsApi.revokeRegisterCode(id);
      toast.success(t('conn.onboardRevoked'));
      await loadCodes();
      if (issued?.id === id) setIssued(null);
    } catch (err) {
      toast.error(t('conn.onboardRevokeFailed'), errorMessage(err));
    } finally {
      setRevoking(null);
    }
  };

  const remove = async (id: string) => {
    if (confirmId !== id) {
      setConfirmId(id);
      return;
    }
    setDeleting(id);
    try {
      await connectionsApi.deleteRegisterCode(id);
      toast.success(t('conn.onboardDeleted'));
      setConfirmId(null);
      await loadCodes();
    } catch (err) {
      /* 后端只允许删已失效的码：真删不掉时把原因原样说出来 */
      toast.error(t('conn.onboardDeleteFailed'), errorMessage(err));
    } finally {
      setDeleting(null);
    }
  };

  const close = () => {
    /* 关掉就丢掉明文：库里只有哈希，再打开也拿不回来 */
    setIssued(null);
    setCopied(false);
    setConfirmId(null);
    setIssueError(null);
    onChanged?.();
    onClose();
  };

  /* 命令取后端返回值；后端还没升级时用 code 现拼一条，别让用户对着空框发呆 */
  const command = issued ? issued.command || fallbackCommand(issued) : '';

  const uses = Number(maxUses);
  /* 参数摘要：让「30 分钟 / 3 次」这种决定一眼可见，不用回头读两个下拉框 */
  const summary = `${t('conn.onboardTtlOption', { n: Number(ttl) })} · ${
    uses === 0
      ? t('conn.onboardUsesUnlimitedOption')
      : t('conn.onboardUsesOption', { n: uses })
  }`;
  const activeCount = codes.filter((row) => row.status === 'active').length;

  /* 步骤条跟着状态走：签发完自动复制，于是直接落到「节点执行」这一步 */
  const activeStep = issued ? (copied ? 3 : 2) : 1;
  const steps = [
    t('conn.onboardStep1'),
    t('conn.onboardStep2'),
    t('conn.onboardStep3'),
  ];

  return (
    <Modal
      open={open}
      onClose={close}
      title={t('conn.onboardTitle')}
      description={t('conn.onboardDesc')}
      size="lg"
      className="onboard-modal"
      footer={
        <div className="wizard-footer">
          <span className="wizard-footer-step onboard-footer-hint">
            {issued ? t('conn.onboardDoneHint') : t('conn.onboardOnceHint')}
          </span>
          <div className="wizard-footer-actions">
            {issued ? (
              <Button
                variant="secondary"
                onClick={() => {
                  setIssued(null);
                  setCopied(false);
                }}
              >
                {t('conn.onboardAgain')}
              </Button>
            ) : (
              <Button
                variant="primary"
                icon={<IconTerminal size={15} />}
                loading={issuing}
                onClick={() => void issue()}
              >
                {t('conn.onboardIssue')}
              </Button>
            )}
            <Button variant="ghost" onClick={close}>
              {t('common.close')}
            </Button>
          </div>
        </div>
      }
    >
      <ol className="onboard-steps" aria-label={t('conn.onboardStepsAria')}>
        {steps.map((label, index) => {
          const num = index + 1;
          const state =
            num < activeStep ? 'done' : num === activeStep ? 'current' : 'todo';
          return (
            <li
              key={label}
              className={`onboard-step is-${state}`}
              aria-current={state === 'current' ? 'step' : undefined}
            >
              <span className="onboard-step-num">
                {state === 'done' ? <IconCheck size={11} /> : num}
              </span>
              <span className="onboard-step-label">{label}</span>
            </li>
          );
        })}
      </ol>

      {!issued ? (
        <section
          className={`onboard-card${issuing ? ' is-busy' : ''}`}
          aria-busy={issuing || undefined}
        >
          <header className="onboard-card-head">
            <span className="onboard-card-icon">
              <IconTerminal size={16} />
            </span>
            <div className="onboard-card-text">
              <h3 className="onboard-card-title">{t('conn.onboardConfigTitle')}</h3>
              <p className="onboard-card-desc">{t('conn.onboardConfigDesc')}</p>
            </div>
            <span className="onboard-summary" aria-hidden="true">
              {summary}
            </span>
          </header>

          {issueError ? (
            <Notice tone="danger" title={t('conn.onboardIssueFailed')}>
              {issueError}
            </Notice>
          ) : null}

          <div className="onboard-grid">
            <Input
              label={t('conn.fieldRegion')}
              hint={t('conn.onboardRegionHint')}
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              placeholder={t('conn.regionPlaceholder')}
              autoComplete="off"
            />
            <Input
              label={t('conn.onboardNote')}
              hint={t('conn.onboardNoteHint')}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('conn.onboardNotePlaceholder')}
              autoComplete="off"
            />
            <Select
              label={t('conn.onboardTtl')}
              hint={t('conn.onboardTtlHint')}
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              options={TTL_OPTIONS.map((m) => ({
                value: String(m),
                label: t('conn.onboardTtlOption', { n: m }),
              }))}
            />
            <Select
              label={t('conn.onboardMaxUses')}
              hint={t('conn.onboardMaxUsesHint')}
              value={maxUses}
              onChange={(e) => setMaxUses(e.target.value)}
              options={USES_OPTIONS.map((n) => ({
                value: String(n),
                label:
                  n === 0
                    ? t('conn.onboardUsesUnlimitedOption')
                    : t('conn.onboardUsesOption', { n }),
              }))}
            />
          </div>

          {/* 原理说明是「想确认一下」时才读的内容，收起来交给一行摘要，
              让表单与主按钮回到首屏第一眼的位置 */}
          <details className="onboard-how">
            <summary>
              <IconInfo size={14} />
              <span>{t('conn.onboardHowTitle')}</span>
              <svg
                className="onboard-how-chevron"
                viewBox="0 0 24 24"
                width="14"
                height="14"
                aria-hidden="true"
              >
                <path
                  d="m6 9 6 6 6-6"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </summary>
            <p>{t('conn.onboardHowBody')}</p>
          </details>
        </section>
      ) : (
        <>
          <section className="onboard-result" aria-live="polite">
            <span className="onboard-result-icon">
              <IconCheck size={18} />
            </span>
            <div className="onboard-result-text">
              <h3 className="onboard-result-title">{t('conn.onboardIssuedTitle')}</h3>
              <p className="onboard-result-body">
                {t('conn.onboardIssuedBody', {
                  minutes: issued.ttl_minutes,
                  time: new Date(issued.expires * 1000).toLocaleTimeString(),
                })}
              </p>
              {/* 面板进程比前端旧时没有 max_uses 字段：宁可不显示，也不显示
                  「还剩 undefined 次」 */}
              {typeof issued.max_uses === 'number' ? (
                <p className="onboard-result-uses">
                  {issued.max_uses === 0
                    ? t('conn.onboardUsesUnlimited')
                    : t('conn.onboardUsesLeft', {
                        left: issued.remaining ?? issued.max_uses,
                        total: issued.max_uses,
                      })}
                </p>
              ) : null}
            </div>
          </section>

          <section className="onboard-command">
            <div className="onboard-command-head">
              <span className="onboard-command-label">
                <IconTerminal size={14} />
                {t('conn.onboardCommand')}
              </span>
              <div className="onboard-actions">
                <Button
                  size="sm"
                  variant="primary"
                  icon={copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
                  onClick={() => void copyCommand()}
                >
                  {copied ? t('conn.onboardCopiedShort') : t('conn.onboardCopyCommand')}
                </Button>
                {issued.script_url ? (
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={<IconLink size={14} />}
                    onClick={() =>
                      window.open(issued.script_url, '_blank', 'noopener,noreferrer')
                    }
                  >
                    {t('conn.onboardViewScript')}
                  </Button>
                ) : null}
              </div>
            </div>
            {/* 整块可点即复制：命令只有一行，鼠标总会在上面划一下再找按钮 */}
            <pre
              className={`console-command onboard-script is-clickable${
                copied ? ' is-copied' : ''
              }`}
              role="button"
              tabIndex={0}
              title={t('conn.onboardClickToCopy')}
              onClick={() => void copyCommand()}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  void copyCommand();
                }
              }}
            >
              {command}
            </pre>
            <p className="onboard-hint">{t('conn.onboardCommandHint')}</p>
            <p className="onboard-hint">{t('conn.onboardAutoNote')}</p>
          </section>
        </>
      )}

      {/* ---- 最近的注册码：回答「这张码为什么用不了」，并允许清理已失效的 ---- */}
      <section className="onboard-card onboard-recent">
        <header className="onboard-card-head">
          <span className="onboard-card-icon">
            <IconLink size={16} />
          </span>
          <div className="onboard-card-text">
            <h3 className="onboard-card-title">{t('conn.onboardRecent')}</h3>
            <p className="onboard-card-desc">{t('conn.onboardRecentDesc')}</p>
          </div>
          <span className="onboard-summary">
            {t('conn.onboardRecentCount', { n: codes.length })}
            {activeCount > 0 ? (
              <span className="onboard-summary-active">
                {t('conn.onboardActiveCount', { n: activeCount })}
              </span>
            ) : null}
          </span>
        </header>

        {codes.length === 0 ? (
          <div className="onboard-empty-state">
            <IconLink size={20} />
            <p>{t('conn.onboardNoCodes')}</p>
          </div>
        ) : null}

        {codes.slice(0, 8).map((row) => (
          <div key={row.id} className="onboard-code-row" data-status={row.status}>
            <span className="onboard-code-status">
              <Badge variant={STATUS_VARIANT[row.status] ?? 'neutral'} size="sm" dot>
                {statusLabel(t, row.status)}
              </Badge>
            </span>
            <span className="onboard-code-uses">
              {typeof row.max_uses !== 'number'
                ? '' /* 后端还没升级：不显示次数，别渲染出 undefined */
                : row.max_uses === 0
                  ? t('conn.onboardUsesChipUnlimited', { used: row.used_count })
                  : t('conn.onboardUsesChip', {
                      used: row.used_count,
                      total: row.max_uses,
                    })}
            </span>
            <span className="onboard-code-label mono fs-xs">
              {row.region || t('conn.onboardNoRegion')}
              {row.note ? ` · ${row.note}` : ''}
            </span>
            {/* 时间写在「谁用的」后面：一列时间戳读起来最碍事，放右边次要位置 */}
            <span className="onboard-code-meta onboard-code-by fs-xs text-muted">
              {row.status === 'used' && row.used_by
                ? row.used_by
                : t('conn.onboardBy', { name: row.created_by || '-' })}
            </span>
            <span className="onboard-code-meta onboard-code-time fs-xs text-muted">
              {row.status === 'used'
                ? t('conn.onboardUsedAt', {
                    time: new Date(row.used * 1000).toLocaleString(),
                  })
                : t('conn.onboardExpiresAt', {
                    time: new Date(row.expires * 1000).toLocaleString(),
                  })}
            </span>
            <div className="onboard-code-actions">
              {row.status === 'active' ? (
                <Button
                  size="sm"
                  variant="ghost"
                  loading={revoking === row.id}
                  onClick={() => void revoke(row.id)}
                >
                  {t('conn.onboardRevoke')}
                </Button>
              ) : (
                /* 已失效的码只留历史价值：撤销 / 用过 / 过期之后就能删 */
                <Button
                  size="sm"
                  variant={confirmId === row.id ? 'danger' : 'ghost'}
                  loading={deleting === row.id}
                  onClick={() => void remove(row.id)}
                >
                  {confirmId === row.id
                    ? t('conn.onboardDeleteConfirm')
                    : t('conn.onboardDelete')}
                </Button>
              )}
            </div>
          </div>
        ))}

        {codes.length > 8 ? (
          <p className="onboard-empty">{t('conn.onboardMoreCodes', { n: codes.length - 8 })}</p>
        ) : null}
      </section>
    </Modal>
  );
}
