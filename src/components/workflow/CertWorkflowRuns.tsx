/* ==========================================================================
   pvecenter — 证书工作流运行面板

   与「流程定义」放在同一张卡的两个子视图里：定义回答「流程长什么样」，
   实例回答「现在走到哪一步」。运行实例由操作者发起并审批，然后逐步确认完成；
   真正的证书签发 / 部署仍走证书站点上的动作，避免把审批和外部副作用绑死。
   ========================================================================== */

import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { certWorkflowRunsApi } from '../../api/endpoints';
import { ApiError } from '../../api/client';
import type {
  CertSite,
  CertWorkflowRun,
  CertWorkflowRunAction,
  CertWorkflowRunStatus,
  CertWorkflowSummary,
} from '../../api/types';
import { Badge } from '../ui/Badge';
import { Button } from '../ui/Button';
import { Select, Textarea } from '../ui/Input';
import { Modal } from '../ui/Modal';
import { EmptyState, ErrorState, Notice } from '../ui/EmptyState';
import { Spinner } from '../ui/Spinner';
import { IconCheck, IconClock, IconFlow, IconRefresh } from '../Icons';
import { nodeVisual } from './nodeMeta';
import { useRefreshInterval } from '../../hooks/useRefreshInterval';
import { useToast } from '../../hooks/useToast';
import { useT, type MessageKey } from '../../i18n';
import { formatDateTime, formatRelative } from '../../utils/format';

const STATUS_VARIANT: Record<
  CertWorkflowRunStatus,
  'warning' | 'info' | 'success' | 'danger' | 'neutral'
> = {
  pending: 'warning',
  running: 'info',
  succeeded: 'success',
  rejected: 'danger',
  cancelled: 'neutral',
};

const STATUS_KEY: Record<CertWorkflowRunStatus, MessageKey> = {
  pending: 'workflow.run.status.pending',
  running: 'workflow.run.status.running',
  succeeded: 'workflow.run.status.succeeded',
  rejected: 'workflow.run.status.rejected',
  cancelled: 'workflow.run.status.cancelled',
};

const STEP_STATUS_KEY: Record<string, MessageKey> = {
  pending: 'workflow.run.stepStatus.pending',
  running: 'workflow.run.stepStatus.running',
  done: 'workflow.run.stepStatus.done',
  skipped: 'workflow.run.stepStatus.skipped',
};

const ACTION_KEY: Record<CertWorkflowRunAction, MessageKey> = {
  approve: 'workflow.run.action.approve',
  reject: 'workflow.run.action.reject',
  advance: 'workflow.run.action.advance',
  cancel: 'workflow.run.action.cancel',
};

const TYPE_KEY: Record<string, MessageKey> = {
  domain_monitor: 'workflow.node.domain_monitor',
  cert_apply: 'workflow.node.cert_apply',
  deploy_upload: 'workflow.node.deploy_upload',
  notification: 'workflow.node.notification',
};

function errText(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

function stepLabel(
  step: CertWorkflowRun['steps'][number],
  t: ReturnType<typeof useT>,
): string {
  if (step.title) return step.title;
  const key = TYPE_KEY[step.type];
  return key ? t(key) : step.type || '—';
}

function runProgress(run: CertWorkflowRun): { done: number; total: number; pct: number } {
  const total = run.steps.length;
  const done = run.steps.filter((step) => step.status === 'done' || step.status === 'skipped').length;
  return { done, total, pct: total ? Math.round((done / total) * 100) : 0 };
}

function currentStepText(
  run: CertWorkflowRun,
  t: ReturnType<typeof useT>,
): string {
  if (run.status === 'pending') return t('workflow.run.waitingApproval');
  if (run.status === 'succeeded') return t('workflow.run.status.succeeded');
  if (run.status === 'rejected') return t('workflow.run.status.rejected');
  if (run.status === 'cancelled') return t('workflow.run.status.cancelled');
  const step = run.steps[run.current_index];
  return step ? stepLabel(step, t) : '—';
}

/* ---------------------------------------------------------------------------
   发起流程
   --------------------------------------------------------------------------- */

export function WorkflowLaunchDialog({
  open,
  definitions,
  sites,
  canManage,
  initialSiteId,
  initialWorkflowId,
  onClose,
  onCreated,
  onGoDefinitions,
}: {
  open: boolean;
  definitions: CertWorkflowSummary[];
  sites: CertSite[];
  canManage: boolean;
  initialSiteId?: string;
  initialWorkflowId?: string;
  onClose: () => void;
  onCreated: (run: CertWorkflowRun) => void;
  onGoDefinitions: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const usable = useMemo(
    () => definitions.filter((item) => item.node_count > 0),
    [definitions],
  );
  const [workflowId, setWorkflowId] = useState('');
  const [siteId, setSiteId] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setWorkflowId(
      initialWorkflowId && usable.some((item) => item.id === initialWorkflowId)
        ? initialWorkflowId
        : usable[0]?.id ?? '',
    );
    setSiteId(initialSiteId ?? '');
    setNote('');
    setBusy(false);
    // 只在打开时初始化；用户正在选择时不应被父组件重渲染打断
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialSiteId, initialWorkflowId]);

  /* 从证书行发起时，定义列表可能还在加载：数据到位后补上默认选项 */
  useEffect(() => {
    if (!open || workflowId || usable.length === 0) return;
    const preferred = initialWorkflowId
      ? usable.find((item) => item.id === initialWorkflowId)
      : null;
    setWorkflowId(preferred?.id ?? usable[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, usable, workflowId]);

  const selected = usable.find((item) => item.id === workflowId);
  const canSubmit = Boolean(workflowId) && canManage && !busy;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    try {
      const result = await certWorkflowRunsApi.create({
        workflow_id: workflowId,
        site_id: siteId || undefined,
        note: note.trim() || undefined,
      });
      toast.success(t('workflow.run.createSuccess'), result.run.title);
      onCreated(result.run);
      onClose();
    } catch (err) {
      toast.error(t('workflow.run.createFailed'), errText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      title={t('workflow.run.launchTitle')}
      description={t('workflow.run.launchDesc')}
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            icon={<IconFlow size={15} />}
            loading={busy}
            disabled={!canSubmit}
            onClick={() => void submit()}
          >
            {busy ? t('workflow.run.creating') : t('workflow.run.create')}
          </Button>
        </>
      }
    >
      <div className="wf-launch-form">
        {usable.length === 0 ? (
          <Notice tone="warning">
            <div className="wf-launch-empty">
              <span>{t('workflow.run.noUsableDefinition')}</span>
              <Button size="sm" onClick={onGoDefinitions}>
                {t('workflow.tabDefinitions')}
              </Button>
            </div>
          </Notice>
        ) : null}

        <Select
          label={t('workflow.run.chooseWorkflow')}
          value={workflowId}
          onChange={(event) => setWorkflowId(event.target.value)}
          options={usable.map((item) => ({
            value: item.id,
            label: `${item.name} · ${t('workflow.nodeCount', { n: item.node_count })}`,
          }))}
          placeholder={t('workflow.run.chooseWorkflowPlaceholder')}
        />

        {selected ? (
          <div className="wf-launch-preview">
            <div className="wf-launch-preview-head">
              <span className="wf-launch-preview-name">{selected.name}</span>
              <Badge variant="neutral" size="sm">
                {t('workflow.version', { n: selected.version })}
              </Badge>
            </div>
            {selected.description ? (
              <p>{selected.description}</p>
            ) : null}
            <span className="wf-launch-preview-flow">
              {t('workflow.nodeCount', { n: selected.node_count })}
            </span>
          </div>
        ) : null}

        <Select
          label={t('workflow.run.site')}
          hint={t('workflow.run.siteHint')}
          value={siteId}
          onChange={(event) => setSiteId(event.target.value)}
          options={[
            { value: '', label: t('workflow.run.noSiteOption') },
            ...sites.map((site) => ({
              value: site.id,
              label: `${site.name || site.domain} · ${site.domain}`,
            })),
          ]}
        />

        <Textarea
          label={t('workflow.run.note')}
          rows={3}
          value={note}
          placeholder={t('workflow.run.notePlaceholder')}
          onChange={(event) => setNote(event.target.value)}
        />

        {!canManage ? <Notice tone="info">{t('workflow.run.readonly')}</Notice> : null}
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   列表
   --------------------------------------------------------------------------- */

export function CertWorkflowRuns({
  definitions,
  canManage,
  onLaunch,
  onGoDefinitions,
}: {
  definitions: CertWorkflowSummary[];
  canManage: boolean;
  onLaunch: (definition?: CertWorkflowSummary) => void;
  onGoDefinitions: () => void;
}) {
  const t = useT();
  const qc = useQueryClient();
  const refresh = useRefreshInterval();
  const [filter, setFilter] = useState<'' | CertWorkflowRunStatus>('');
  const [runId, setRunId] = useState('');
  const [note, setNote] = useState('');
  const [busyAction, setBusyAction] = useState<CertWorkflowRunAction | ''>('');
  const toast = useToast();

  const listQuery = useQuery({
    queryKey: ['cert-workflow-runs'],
    queryFn: () => certWorkflowRunsApi.list({ limit: 100 }),
    refetchInterval: refresh,
  });
  const runs = listQuery.data?.items ?? [];
  const stats = listQuery.data?.stats;

  const detailQuery = useQuery({
    queryKey: ['cert-workflow-run', runId],
    queryFn: () => certWorkflowRunsApi.get(runId),
    enabled: Boolean(runId),
    refetchInterval: runId ? refresh : false,
  });
  const selectedRun =
    detailQuery.data?.run ?? runs.find((item) => item.id === runId) ?? null;

  const filtered = filter ? runs.filter((run) => run.status === filter) : runs;

  const filters: Array<{ key: '' | CertWorkflowRunStatus; label: MessageKey; count: number }> = [
    { key: '', label: 'workflow.run.filterAll', count: stats?.total ?? runs.length },
    { key: 'pending', label: 'workflow.run.status.pending', count: stats?.pending ?? 0 },
    { key: 'running', label: 'workflow.run.status.running', count: stats?.running ?? 0 },
    { key: 'succeeded', label: 'workflow.run.status.succeeded', count: stats?.succeeded ?? 0 },
    { key: 'rejected', label: 'workflow.run.status.rejected', count: stats?.rejected ?? 0 },
    /* 后端 RUN_STATUSES 有五种，取消掉的运行原来没有任何一档能落到，
       只能在「全部」里翻 —— 补上它，五档之和才等于「全部」 */
    { key: 'cancelled', label: 'workflow.run.status.cancelled', count: stats?.cancelled ?? 0 },
  ];

  useEffect(() => {
    setNote('');
  }, [runId]);

  const openDetail = (id: string) => {
    setRunId(id);
  };

  const act = async (action: CertWorkflowRunAction) => {
    if (!selectedRun || busyAction) return;
    setBusyAction(action);
    try {
      const result = await certWorkflowRunsApi.transition(
        selectedRun.id,
        action,
        note.trim(),
      );
      qc.setQueryData(['cert-workflow-run', selectedRun.id], result);
      await qc.invalidateQueries({ queryKey: ['cert-workflow-runs'] });
      setNote('');
      toast.success(
        t('workflow.run.actionDone'),
        t(STATUS_KEY[result.run.status]),
      );
    } catch (err) {
      toast.error(t('workflow.run.actionFailed'), errText(err));
    } finally {
      setBusyAction('');
    }
  };

  if (listQuery.isError) {
    return (
      <ErrorState
        message={errText(listQuery.error)}
        onRetry={() => void listQuery.refetch()}
      />
    );
  }

  return (
    <div className="wf-runs">
      <div className="wf-runs-toolbar">
        {/* 与证书清单第二行的统计胶囊同一套样式：数字在前、标签在后、胶囊描边，
            选中的一档用 is-strong 的强调色，保持一致 */}
        <div className="cert-inline-stats wf-run-filters" role="tablist" aria-label={t('workflow.tabRuns')}>
          {filters.map((item) => (
            <button
              key={item.key || 'all'}
              type="button"
              role="tab"
              aria-selected={filter === item.key}
              className={`cert-inline-stat wf-run-filter${filter === item.key ? ' is-strong' : ''}`}
              onClick={() => setFilter(item.key)}
            >
              <b>{item.count}</b>
              {t(item.label)}
            </button>
          ))}
        </div>
      </div>

      {listQuery.isLoading ? (
        <div className="wf-loading">
          <Spinner size={24} label={t('common.loading')} />
        </div>
      ) : runs.length === 0 ? (
        <EmptyState
          icon={<IconFlow size={30} />}
          title={t('workflow.run.emptyTitle')}
          description={t('workflow.run.emptyHint')}
          action={
            <div className="wf-empty-actions">
              <Button
                variant="primary"
                disabled={!canManage || definitions.length === 0}
                onClick={() => onLaunch()}
              >
                {t('workflow.run.new')}
              </Button>
              <Button onClick={onGoDefinitions}>{t('workflow.tabDefinitions')}</Button>
            </div>
          }
        />
      ) : filtered.length === 0 ? (
        <div className="table-container table-container-empty">
          {t('workflow.run.emptyFilter')}
        </div>
      ) : (
        <div className="wf-run-list">
          {filtered.map((run) => {
            const progress = runProgress(run);
            const domain = run.site_domain;
            return (
              <article key={run.id} className="wf-run-card">
                <div className="wf-run-main">
                  <div className="wf-run-head">
                    <span className={`wf-run-icon is-${run.status}`}>
                      <IconFlow size={18} />
                    </span>
                    <div className="wf-run-title-wrap">
                      <div className="wf-run-title">
                        {run.title || run.workflow_name}
                        <Badge variant={STATUS_VARIANT[run.status]} size="sm" dot>
                          {t(STATUS_KEY[run.status])}
                        </Badge>
                      </div>
                      <div className="wf-run-meta">
                        <span>{run.workflow_name}</span>
                        <span className="mono">
                          {domain || t('workflow.run.noSite')}
                        </span>
                        <span>{formatRelative(run.created_at)}</span>
                      </div>
                    </div>
                  </div>

                  <div className="wf-run-progress">
                    <div className="wf-run-progress-head">
                      <span>{currentStepText(run, t)}</span>
                      <span>
                        {t('workflow.run.stepProgress', {
                          done: progress.done,
                          total: progress.total,
                        })}
                      </span>
                    </div>
                    <div className="wf-run-bar" aria-hidden="true">
                      <span
                        className={`is-${run.status}`}
                        style={{ width: `${progress.pct}%` }}
                      />
                    </div>
                  </div>
                </div>

                <div className="wf-run-side">
                  <div className="wf-run-approval">
                    {run.status === 'pending' ? (
                      <>
                        <IconClock size={14} />
                        <span>{t('workflow.run.approvalPendingHint')}</span>
                      </>
                    ) : run.approver ? (
                      <>
                        <IconCheck size={14} />
                        <span>
                          {t('workflow.run.approvedBy', {
                            name: run.approver,
                            time: formatRelative(run.updated_at),
                          })}
                        </span>
                      </>
                    ) : (
                      <span>{t('workflow.run.noApproval')}</span>
                    )}
                  </div>
                  <Button size="sm" onClick={() => openDetail(run.id)}>
                    {t('workflow.run.viewDetail')}
                  </Button>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <Modal
        open={Boolean(runId)}
        onClose={() => {
          if (!busyAction) setRunId('');
        }}
        title={t('workflow.run.detailTitle')}
        description={selectedRun?.title}
        size="lg"
        footer={
          selectedRun ? (
            <>
              <Button
                variant="ghost"
                onClick={() => setRunId('')}
                disabled={Boolean(busyAction)}
              >
                {t('common.close')}
              </Button>
              {selectedRun.actions.includes('cancel') && canManage ? (
                <Button
                  variant="ghost"
                  className="wf-danger-text"
                  loading={busyAction === 'cancel'}
                  disabled={Boolean(busyAction)}
                  onClick={() => void act('cancel')}
                >
                  {t(ACTION_KEY.cancel)}
                </Button>
              ) : null}
              {selectedRun.actions.includes('reject') && canManage ? (
                <Button
                  variant="secondary"
                  loading={busyAction === 'reject'}
                  disabled={Boolean(busyAction)}
                  onClick={() => void act('reject')}
                >
                  {t(ACTION_KEY.reject)}
                </Button>
              ) : null}
              {selectedRun.actions.includes('approve') && canManage ? (
                <Button
                  variant="primary"
                  icon={<IconCheck size={15} />}
                  loading={busyAction === 'approve'}
                  disabled={Boolean(busyAction)}
                  onClick={() => void act('approve')}
                >
                  {t(ACTION_KEY.approve)}
                </Button>
              ) : null}
              {selectedRun.actions.includes('advance') && canManage ? (
                <Button
                  variant="primary"
                  icon={<IconRefresh size={15} />}
                  loading={busyAction === 'advance'}
                  disabled={Boolean(busyAction)}
                  onClick={() => void act('advance')}
                >
                  {t(ACTION_KEY.advance)}
                </Button>
              ) : null}
            </>
          ) : null
        }
      >
        {!selectedRun ? (
          <div className="wf-loading">
            <Spinner size={24} label={t('common.loading')} />
          </div>
        ) : (
          <div className="wf-run-detail">
            <div className="wf-run-detail-head">
              <div>
                <div className="wf-run-detail-title">
                  {selectedRun.title || selectedRun.workflow_name}
                  <Badge variant={STATUS_VARIANT[selectedRun.status]} size="sm" dot>
                    {t(STATUS_KEY[selectedRun.status])}
                  </Badge>
                </div>
                <div className="wf-run-meta">
                  <span>{selectedRun.workflow_name}</span>
                  {selectedRun.site_domain ? (
                    <span className="mono">{selectedRun.site_domain}</span>
                  ) : null}
                  <span>
                    {t('workflow.run.requestedBy', { name: selectedRun.requester })}
                  </span>
                  <span>{formatDateTime(selectedRun.created_at)}</span>
                </div>
              </div>
              <div className="wf-run-detail-progress">
                <b>
                  {runProgress(selectedRun).done}/{runProgress(selectedRun).total}
                </b>
                <span>{t('workflow.run.steps')}</span>
              </div>
            </div>

            <div className={`wf-run-approval-box is-${selectedRun.status}`}>
              <div className="wf-run-approval-title">
                {selectedRun.status === 'pending' ? (
                  <>
                    <IconClock size={16} />
                    {t('workflow.run.approvalPending')}
                  </>
                ) : selectedRun.approver ? (
                  <>
                    <IconCheck size={16} />
                    {selectedRun.status === 'rejected'
                      ? t('workflow.run.approvalRejected')
                      : t('workflow.run.approvalApproved')}
                  </>
                ) : (
                  <>
                    <IconFlow size={16} />
                    {t('workflow.run.noApproval')}
                  </>
                )}
              </div>
              <div className="wf-run-approval-meta">
                {selectedRun.request_note ? (
                  <span>
                    {t('workflow.run.requestNote')}：{selectedRun.request_note}
                  </span>
                ) : null}
                {selectedRun.approver ? (
                  <span>
                    {selectedRun.approver}
                    {selectedRun.approval_note
                      ? ` · ${selectedRun.approval_note}`
                      : ''}
                  </span>
                ) : null}
              </div>
            </div>

            <div className="wf-run-steps">
              <div className="wf-section-title">{t('workflow.run.steps')}</div>
              <ol>
                {selectedRun.steps.map((step, index) => {
                  const visual = nodeVisual(step.type);
                  const active = selectedRun.current_index === index && selectedRun.status === 'running';
                  return (
                    <li
                      key={`${step.node_id}-${index}`}
                      className={`wf-run-step is-${step.status}${active ? ' is-active' : ''}`}
                    >
                      <span className={`wf-run-step-dot tone-${visual.tone}`}>
                        {visual.icon(15)}
                      </span>
                      <div className="wf-run-step-body">
                        <div className="wf-run-step-head">
                          <span className="wf-run-step-name">{stepLabel(step, t)}</span>
                          <span className={`wf-run-step-state is-${step.status}`}>
                            {t(STEP_STATUS_KEY[step.status] ?? 'workflow.run.stepStatus.pending')}
                          </span>
                        </div>
                        <div className="wf-run-step-meta">
                          <span>{t('workflow.run.stepIndex', { n: index + 1 })}</span>
                          {step.started_at ? (
                            <span>{formatDateTime(step.started_at)}</span>
                          ) : null}
                          {step.note ? <span>{step.note}</span> : null}
                        </div>
                      </div>
                    </li>
                  );
                })}
              </ol>
            </div>

            {selectedRun.actions.length > 0 && canManage ? (
              <Textarea
                label={t('workflow.run.actionNote')}
                rows={2}
                value={note}
                placeholder={t('workflow.run.actionNotePlaceholder')}
                onChange={(event) => setNote(event.target.value)}
              />
            ) : !canManage ? (
              <Notice tone="info">{t('workflow.run.readonly')}</Notice>
            ) : null}
          </div>
        )}
      </Modal>
    </div>
  );
}
