/* ==========================================================================
   pvecenter — AI 写命令的内联审批卡

   与「弹一个模态框」的做法不同：这张卡**内嵌在对话流里**。
   「授权」本身就是对话中的一个动作 —— 用户在右边看着 AI 的请求就地拍板，
   不必被一个模态框打断，也不用在日志与弹窗之间来回跳。

   倒计时仍然必要：后端等待有上限（APPROVAL_TIMEOUT），超时即按拒绝处理 ——
   界面上先把这件事说清楚，比让用户点了按钮才发现「请求已失效」好得多。
   ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api/client';
import { aiApi } from '../api/endpoints';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { useT } from '../i18n';

/** 一条待用户拍板的写命令（与 SSE 的 approval 事件同构） */
export interface ApprovalRequest {
  approval_id: string;
  command: string;
  purpose: string;
  timeout: number;
  /** ``vm_action``：虚拟机内的写操作，多一个 diff 预览与风险级别 */
  kind?: string;
  risk?: number;
  risk_label?: string;
  detail?: string;
  /** 需要多少人批准（高危动作是 2）。缺省按 1 处理 */
  required?: number;
}

/** 逐行给 diff 上色：只认 ``+`` / ``-`` / ``@@`` 三个前缀，够用且不会误判 */
function DiffPreview({ text }: { text: string }) {
  return (
    <pre className="ai-diff">
      {text.split('\n').map((line, index) => {
        const cls =
          line.startsWith('+++') || line.startsWith('---')
            ? 'ai-diff-line ai-diff-file'
            : line.startsWith('+')
              ? 'ai-diff-line ai-diff-add'
              : line.startsWith('-')
                ? 'ai-diff-line ai-diff-del'
                : line.startsWith('@@')
                  ? 'ai-diff-line ai-diff-hunk'
                  : 'ai-diff-line';
        return (
          <span key={index} className={cls}>
            {line || ' '}
          </span>
        );
      })}
    </pre>
  );
}

export function ApprovalCard({
  request,
  onDone,
}: {
  request: ApprovalRequest;
  /** 决策完成后回调（是否批准），调用方据此把卡片从对话流里摘掉 */
  onDone: (approved: boolean) => void;
}) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [left, setLeft] = useState(Math.max(0, Math.floor(request.timeout)));
  /** 自己这一票已投出，但还没凑够人数（双人审批） */
  const [voted, setVoted] = useState(false);
  const required = Math.max(1, Number(request.required) || 1);
  /* 一旦做出决定（含超时）就不再重复提交 —— 倒计时与手动点击必须只生效一次 */
  const settled = useRef(false);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setLeft((prev) => {
        if (prev <= 1) {
          window.clearInterval(timer);
          if (!settled.current) {
            settled.current = true;
            onDone(false);
          }
          return 0;
        }
        return prev - 1;
      });
    }, 1000);
    return () => window.clearInterval(timer);
  }, [onDone]);

  const decide = async (approved: boolean) => {
    if (settled.current) return;
    settled.current = true;
    setBusy(true);
    setError('');
    try {
      const result = await aiApi.approve(request.approval_id, approved);
      // 后端用 ok=false 表示「这个 approval_id 已经不在等待队列里」（超时，
      // 或已被另一处拍板）。这时不能把卡片一收了之 —— 那正是「点了批准却
      // 什么都没发生」的来源：用户以为批过了，后端其实从没收到这一票。
      if (!result.ok) {
        settled.current = false;
        setBusy(false);
        setError(t('ai.approval.stale'));
        return;
      }
      /* 双人审批：``done=false`` 说明这一票只是记账，后端还挂在等待里。
         这时候收掉卡片最要命 —— 用户以为批完了，实际上面板会一直等到超时
         按「拒绝」收口，表现就是「批准了，AI 却停着不动」。 */
      if (result.done === false) {
        setBusy(false);
        setVoted(true);
        return;
      }
      onDone(approved);
    } catch (err) {
      // 提交失败就把决定「撤销」，让用户能重试（否则卡片卡死在已决定状态）
      settled.current = false;
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <div
      role="group"
      aria-label={t('ai.approval.title')}
      style={{
        border: '1px solid var(--warning)',
        background: 'var(--warning-dim)',
        borderRadius: 8,
        padding: '10px 12px',
      }}
    >
      <div className="flex items-center gap-8 flex-wrap">
        <Badge variant="warning" size="sm">
          {request.kind === 'vm_action'
            ? t('ai.approval.vmBadge')
            : t('ai.approval.badge')}
        </Badge>
        {/* 虚拟机内动作额外标出风险级别：改配置与写临时文件不该长成一个样子 */}
        {request.risk_label ? (
          <Badge variant={request.risk && request.risk >= 2 ? 'danger' : 'neutral'} size="sm">
            {request.risk_label}
          </Badge>
        ) : null}
        {/* 需要两人批准时先讲清楚：不然用户批完一票后只会看到界面静止 */}
        {required > 1 ? (
          <Badge variant="danger" size="sm">
            {t('ai.approval.requiredTwo', { n: required })}
          </Badge>
        ) : null}
        {voted ? (
          <Badge variant="success" size="sm" dot>
            {t('ai.approval.voted', { n: required })}
          </Badge>
        ) : null}
        <span className="fs-xs text-muted">
          {t('ai.approval.countdown', { s: left })}
        </span>
      </div>

      {error ? (
        <div className="login-error" role="alert" style={{ marginTop: 8 }}>
          {error}
        </div>
      ) : null}

      <div className="fs-xs text-muted" style={{ marginTop: 8 }}>
        {t('ai.approval.purpose')}
      </div>
      <div className="fs-sm" style={{ marginBottom: 8, lineHeight: 1.7 }}>
        {request.purpose}
      </div>

      <div className="fs-xs text-muted">
        {request.kind === 'vm_action'
          ? t('ai.approval.target')
          : t('ai.approval.command')}
      </div>
      <div
        className="mono fs-xs"
        style={{
          marginTop: 4,
          padding: '8px 10px',
          background: 'var(--bg-surface)',
          border: '1px solid var(--border-muted)',
          borderRadius: 6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
          lineHeight: 1.6,
          maxHeight: 160,
          overflow: 'auto',
        }}
      >
        {request.command}
      </div>

      {/* 写文件时把 diff 摆在按钮**之前**：批准的人要先看清改了什么，
          而不是点完再回头找「到底改哪一行」 */}
      {request.detail ? (
        <>
          <div className="fs-xs text-muted" style={{ marginTop: 8 }}>
            {t('ai.approval.diff')}
          </div>
          <DiffPreview text={request.detail} />
        </>
      ) : null}

      <div className="flex items-center gap-8" style={{ marginTop: 10 }}>
        {voted ? (
          /* 自己已经投过票，还差别人：按钮收起来，避免重复点击把票算成两个人 */
          <span className="fs-xs text-muted">{t('ai.approval.waitSecond')}</span>
        ) : (
          <>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => void decide(false)}
              disabled={busy}
            >
              {t('ai.approval.reject')}
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => void decide(true)}
              loading={busy}
            >
              {t('ai.approval.approve')}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
