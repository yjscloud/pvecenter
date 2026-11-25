/* ==========================================================================
   pvecenter — 证书工作流工作台（嵌入证书管理页）

   证书管理页的「工作流」标签同时承载流程实例与流程定义：
     · 流程实例：发起、审批、逐步流转，是日常操作入口；
     · 流程定义：模板 / 空白 / 导入三种起点，最终都落到同一个可视化编辑器。
   编辑器以全屏浮层打开，不再单独占一个页面；顶部返回即回到本工作台。
   ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  certWorkflowsApi,
  type CertWorkflowInput,
} from '../api/endpoints';
import { ApiError } from '../api/client';
import type {
  CertSite,
  CertWorkflow,
  CertWorkflowIssue,
  CertWorkflowNodeSpec,
  CertWorkflowSummary,
  CertWorkflowTemplate,
} from '../api/types';
import { Button } from '../components/ui/Button';
import { Modal } from '../components/ui/Modal';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import { Spinner } from '../components/ui/Spinner';
import { EmptyState, ErrorState } from '../components/ui/EmptyState';
import { Badge } from '../components/ui/Badge';
import {
  IconCertificate,
  IconChevronDown,
  IconClose,
  IconEdit,
  IconFlow,
  IconPlus,
  IconSearch,
  IconTrash,
  IconUpload,
} from '../components/Icons';
import { nodeLabel, nodeVisual, originLabel } from '../components/workflow/nodeMeta';
import {
  CertWorkflowRuns,
  WorkflowLaunchDialog,
} from '../components/workflow/CertWorkflowRuns';
import { CertWorkflowEditor } from './CertWorkflowEditor';
import { useToast } from '../hooks/useToast';
import { formatDateTime } from '../utils/format';
import { useT } from '../i18n';

function errText(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

function cloneNodes(nodes: CertWorkflowTemplate['nodes']): CertWorkflowInput['nodes'] {
  return JSON.parse(JSON.stringify(nodes)) as CertWorkflowInput['nodes'];
}

function downloadText(name: string, text: string, format: 'yaml' | 'json') {
  const blob = new Blob([text], {
    type: format === 'yaml' ? 'application/x-yaml;charset=utf-8' : 'application/json;charset=utf-8',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const safe = (name || 'workflow').replace(/[\\/:*?"<>|]+/g, '-');
  link.href = url;
  link.download = `${safe}.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

const ORIGIN_VARIANT: Record<string, 'info' | 'neutral' | 'success' | 'accent'> = {
  template: 'info',
  blank: 'neutral',
  import: 'success',
  editor: 'accent',
};

/* ---------------------------------------------------------------------------
   导入对话框
   --------------------------------------------------------------------------- */

function ImportDialog({
  open,
  onClose,
  onImported,
  sample,
}: {
  open: boolean;
  onClose: () => void;
  onImported: (workflow: CertWorkflow) => void;
  sample?: CertWorkflowTemplate;
}) {
  const t = useT();
  const toast = useToast();
  const inputRef = useRef<HTMLInputElement>(null);
  const [filename, setFilename] = useState('');
  const [content, setContent] = useState('');
  const [size, setSize] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [errors, setErrors] = useState<CertWorkflowIssue[]>([]);
  const [warnings, setWarnings] = useState<CertWorkflowIssue[]>([]);
  const [busy, setBusy] = useState(false);

  const reset = () => {
    setFilename('');
    setContent('');
    setSize(0);
    setErrors([]);
    setWarnings([]);
    setDragging(false);
    setBusy(false);
  };

  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };

  const acceptFile = async (file: File) => {
    setErrors([]);
    setWarnings([]);
    if (!/\.(ya?ml|json)$/i.test(file.name)) {
      setErrors([{ message: t('workflow.import.invalidExt'), path: '' }]);
      return;
    }
    if (file.size > 1024 * 1024) {
      setErrors([{ message: t('workflow.import.tooLarge'), path: '' }]);
      return;
    }
    try {
      const text = await file.text();
      setFilename(file.name);
      setSize(file.size);
      setContent(text);
    } catch (err) {
      setErrors([{ message: errText(err), path: '' }]);
    }
  };

  const submit = async () => {
    if (!filename || !content || busy) return;
    setBusy(true);
    setErrors([]);
    setWarnings([]);
    try {
      const result = await certWorkflowsApi.importFile(filename, content);
      setWarnings(result.warnings ?? []);
      if (!result.ok || !result.workflow) {
        setErrors(result.errors ?? []);
        return;
      }
      toast.success(
        t('workflow.import.success'),
        result.renamed_from
          ? `${result.renamed_from} → ${result.workflow.name}`
          : result.workflow.name,
      );
      reset();
      onImported(result.workflow);
    } catch (err) {
      setErrors([{ message: errText(err), path: '' }]);
    } finally {
      setBusy(false);
    }
  };

  const formatLabel = filename
    ? /\.json$/i.test(filename)
      ? 'JSON'
      : 'YAML'
    : '';

  return (
    <Modal
      open={open}
      onClose={close}
      title={t('workflow.import.title')}
      description={t('workflow.import.desc')}
      size="lg"
      closeOnOverlay={!busy}
      closeOnEsc={!busy}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            icon={<IconUpload size={16} />}
            loading={busy}
            disabled={!filename || !content}
            onClick={() => void submit()}
          >
            {busy ? t('workflow.import.importing') : t('workflow.import.action')}
          </Button>
        </>
      }
    >
      <input
        ref={inputRef}
        type="file"
        accept=".yaml,.yml,.json,application/json,text/yaml,application/x-yaml"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void acceptFile(file);
          event.target.value = '';
        }}
      />

      <div
        className={`wf-drop ${dragging ? 'is-dragging' : ''} ${filename ? 'has-file' : ''}`}
        role="button"
        tabIndex={0}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') inputRef.current?.click();
        }}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          const file = event.dataTransfer.files?.[0];
          if (file) void acceptFile(file);
        }}
      >
        <div className="wf-drop-icon" aria-hidden="true">
          <IconUpload size={26} />
        </div>
        {filename ? (
          <>
            <div className="wf-drop-file">{filename}</div>
            <div className="text-secondary">
              {formatLabel} · {t('workflow.import.fileSize', { size: Math.max(1, Math.round(size / 1024)) })}
            </div>
          </>
        ) : (
          <>
            <div className="wf-drop-title">{t('workflow.import.dropHint')}</div>
            <div className="text-secondary">{t('workflow.import.accepted')}</div>
          </>
        )}
      </div>

      <div className="wf-drop-actions">
        {filename ? (
          <Button size="sm" variant="ghost" onClick={reset} disabled={busy}>
            {t('workflow.import.removeFile')}
          </Button>
        ) : null}
        {sample ? (
          <Button
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() =>
              downloadText(
                t('workflow.defaultName'),
                JSON.stringify(
                  {
                    name: t('workflow.defaultName'),
                    description: '',
                    nodes: sample.nodes,
                  },
                  null,
                  2,
                ),
                'json',
              )
            }
          >
            {t('workflow.import.sample')}
          </Button>
        ) : null}
      </div>

      {errors.length ? (
        <div className="wf-issue-box is-error" role="alert">
          <div className="wf-issue-title">{t('workflow.import.errors')}</div>
          <ul className="wf-issue-list">
            {errors.map((issue, index) => (
              <li key={`${issue.path}-${index}`}>
                <span>{issue.message}</span>
                {issue.path ? <code>{issue.path}</code> : null}
                {issue.line ? (
                  <span className="text-secondary">
                    {t('workflow.import.errorAt', { line: issue.line })}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {warnings.length ? (
        <div className="wf-issue-box is-warning">
          <div className="wf-issue-title">{t('workflow.import.warnings')}</div>
          <ul className="wf-issue-list">
            {warnings.map((issue, index) => (
              <li key={`${issue.path}-${index}`}>
                <span>{issue.message}</span>
                {issue.path ? <code>{issue.path}</code> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {content ? (
        <div className="wf-preview">
          <div className="wf-preview-head">{t('workflow.import.preview')}</div>
          <pre className="wf-preview-body">{content.slice(0, 1600)}</pre>
        </div>
      ) : null}
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   模板选择对话框
   --------------------------------------------------------------------------- */

function TemplateDialog({
  open,
  onClose,
  templates,
  specs,
  creating,
  error,
  onUse,
}: {
  open: boolean;
  onClose: () => void;
  templates: CertWorkflowTemplate[];
  specs: Map<string, CertWorkflowNodeSpec>;
  creating: string;
  error: string;
  onUse: (template: CertWorkflowTemplate) => void;
}) {
  const t = useT();
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('workflow.templateTitle')}
      description={t('workflow.templateHint')}
      size="lg"
      footer={
        <Button variant="ghost" onClick={onClose}>
          {t('common.cancel')}
        </Button>
      }
    >
      {error ? (
        <div className="wf-issue-box is-error">{error}</div>
      ) : templates.length === 0 ? (
        <div className="wf-loading">
          <Spinner size={24} label={t('common.loading')} />
        </div>
      ) : null}
      <div className="wf-template-grid">
        {templates.map((template) => {
          const icon = nodeVisual(template.nodes[0]?.type ?? '');
          return (
            <button
              key={template.key}
              type="button"
              className="wf-template-card"
              onClick={() => onUse(template)}
              disabled={Boolean(creating)}
            >
              <span className={`wf-node-icon tone-${icon.tone}`}>
                {icon.icon(22)}
              </span>
              <span className="wf-template-name">{template.name}</span>
              <span className="wf-template-desc">{template.description}</span>
              <span className="wf-template-flow">
                {template.nodes
                  .map((node) => nodeLabel(node, t, specs))
                  .join(' → ')}
              </span>
              <span className="wf-template-cta">
                {creating === template.key
                  ? t('workflow.creating')
                  : t('workflow.useTemplate')}
              </span>
            </button>
          );
        })}
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   页面
   --------------------------------------------------------------------------- */

export function CertWorkflowWorkspace({
  sites,
  canManage,
  launchSite,
  onLaunchConsumed,
  toolbarSlot,
}: {
  sites: CertSite[];
  canManage: boolean;
  /** 从证书行「发起流程」进入时，预选该站点并自动打开发起弹窗 */
  launchSite?: CertSite | null;
  onLaunchConsumed?: () => void;
  /**
   * 工具栏挂载点：嵌在证书页里时由证书页提供，切换器与主操作渲染到那条工具栏上，
   * 与站点、清单两个子视图保持同一结构。传 undefined 表示独立使用，工具栏留在本组件顶部。
   */
  toolbarSlot?: HTMLElement | null;
}) {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [section, setSection] = useState<'runs' | 'definitions'>('runs');
  const [search, setSearch] = useState('');
  const [templateOpen, setTemplateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [creating, setCreating] = useState('');
  const [editingId, setEditingId] = useState('');
  const [launchOpen, setLaunchOpen] = useState(false);
  const [launchSiteId, setLaunchSiteId] = useState('');
  const [launchDefinitionId, setLaunchDefinitionId] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<CertWorkflowSummary | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [exporting, setExporting] = useState('');

  const listQuery = useQuery({
    queryKey: ['cert-workflows'],
    queryFn: certWorkflowsApi.list,
  });
  const templateQuery = useQuery({
    queryKey: ['cert-workflow-templates'],
    queryFn: certWorkflowsApi.templates,
    staleTime: 10 * 60_000,
  });
  const specQuery = useQuery({
    queryKey: ['cert-workflow-node-types'],
    queryFn: certWorkflowsApi.nodeTypes,
    staleTime: Infinity,
  });

  const items = listQuery.data?.items ?? [];
  const templates = templateQuery.data?.templates ?? [];
  const specs = useMemo(
    () => new Map((specQuery.data?.types ?? []).map((spec) => [spec.type, spec])),
    [specQuery.data],
  );
  const filtered = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    if (!keyword) return items;
    return items.filter(
      (item) =>
        item.name.toLowerCase().includes(keyword) ||
        (item.description || '').toLowerCase().includes(keyword),
    );
  }, [items, search]);

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['cert-workflows'] });

  const ensureUniqueName = (base: string) => {
    const names = new Set(items.map((item) => item.name));
    if (!names.has(base)) return base;
    for (let index = 2; index < 1000; index += 1) {
      const candidate = `${base} ${index}`;
      if (!names.has(candidate)) return candidate;
    }
    return base;
  };

  const createWorkflow = async (body: CertWorkflowInput) => {
    const result = await certWorkflowsApi.create(body);
    toast.success(t('workflow.created'), result.workflow.name);
    /* 建完之后先把「选模板」这类浮层收掉再进编辑器：不然编辑器叠在模板弹窗
       上面，从编辑器返回时底下那个已经用完的选模板窗口还在，得手动再关一次 */
    setTemplateOpen(false);
    setImportOpen(false);
    await refresh();
    setSection('definitions');
    setEditingId(result.workflow.id);
  };

  const openLaunch = (definition?: CertWorkflowSummary) => {
    setLaunchDefinitionId(definition?.id ?? '');
    setLaunchSiteId('');
    setLaunchOpen(true);
  };

  /* 证书行点了「发起流程」：自动切到工作流视图，并带着站点打开发起弹窗 */
  useEffect(() => {
    if (!launchSite) return;
    setSection('runs');
    setLaunchDefinitionId('');
    setLaunchSiteId(launchSite.id);
    setLaunchOpen(true);
    onLaunchConsumed?.();
    // 只对父组件传入的那一次请求作出反应
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [launchSite]);

  const useTemplate = async (template: CertWorkflowTemplate) => {
    if (creating) return;
    setCreating(template.key);
    try {
      await createWorkflow({
        name: template.name,
        description: template.description,
        nodes: cloneNodes(template.nodes),
        origin: 'template',
      });
    } catch (err) {
      toast.error(t('workflow.saveFailed'), errText(err));
    } finally {
      setCreating('');
    }
  };

  const createBlank = async () => {
    if (creating) return;
    setCreating('blank');
    try {
      await createWorkflow({
        name: ensureUniqueName(t('workflow.defaultName')),
        description: '',
        nodes: [],
        origin: 'blank',
      });
    } catch (err) {
      toast.error(t('workflow.saveFailed'), errText(err));
    } finally {
      setCreating('');
    }
  };

  const remove = async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await certWorkflowsApi.remove(deleteTarget.id);
      toast.success(t('workflow.deleted'), deleteTarget.name);
      setDeleteTarget(null);
      await refresh();
      /* 后端同时解绑了引用它的证书站点、清掉了它的审批实例：
         这两处缓存不跟着作废，切过去还会显示「已绑定 / 有实例」。 */
      await queryClient.invalidateQueries({ queryKey: ['cert-workflow-runs'] });
      await queryClient.invalidateQueries({ queryKey: ['certs'] });
    } catch (err) {
      toast.error(t('workflow.deleteFailed'), errText(err));
    } finally {
      setDeleting(false);
    }
  };

  const exportWorkflow = async (workflow: CertWorkflowSummary, format: 'yaml' | 'json') => {
    setExporting(workflow.id);
    try {
      const text = await certWorkflowsApi.exportText(workflow.id, format);
      downloadText(workflow.name, text, format);
    } catch (err) {
      toast.error(t('workflow.exportFailed'), errText(err));
    } finally {
      setExporting('');
    }
  };

  /* 工具栏：二级切换 + 主操作。嵌在证书页时 portal 到证书页那条工具栏上，和站点/
     清单的搜索、筛选并排；独立使用时留在本组件顶部 */
  const toolbar = (
    <div className="wf-workspace-head">
      <div className="cert-filters" role="tablist" aria-label={t('workflow.title')}>
        <button
          type="button"
          role="tab"
          aria-selected={section === 'runs'}
          className={`cert-filter${section === 'runs' ? ' is-active' : ''}`}
          onClick={() => setSection('runs')}
        >
          {t('workflow.tabRuns')}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={section === 'definitions'}
          className={`cert-filter${section === 'definitions' ? ' is-active' : ''}`}
          onClick={() => setSection('definitions')}
        >
          {t('workflow.tabDefinitions')}
          <span className="cert-filter-count">{items.length}</span>
        </button>
      </div>

      <Button
        variant="primary"
        size="sm"
        icon={<IconFlow size={14} />}
        disabled={!canManage || items.length === 0}
        onClick={() => openLaunch()}
      >
        {t('workflow.run.new')}
      </Button>
    </div>
  );

  return (
    <div className="wf-workspace">
      {toolbarSlot === undefined ? toolbar : toolbarSlot ? createPortal(toolbar, toolbarSlot) : null}

      {section === 'runs' ? (
        <CertWorkflowRuns
          definitions={items}
          canManage={canManage}
          onLaunch={(definition) => openLaunch(definition)}
          onGoDefinitions={() => setSection('definitions')}
        />
      ) : listQuery.isError ? (
        <ErrorState
          message={errText(listQuery.error)}
          onRetry={() => void listQuery.refetch()}
        />
      ) : (
        <>
          <div className="wf-def-toolbar">
            <div className="cert-search">
              <IconSearch size={14} />
              <input
                value={search}
                placeholder={t('workflow.searchPlaceholder')}
                onChange={(event) => setSearch(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Escape' && search) {
                    event.preventDefault();
                    setSearch('');
                  }
                }}
                aria-label={t('workflow.searchPlaceholder')}
              />
              {search ? (
                <button
                  type="button"
                  className="cert-search-clear"
                  aria-label={t('cert.clearSearch')}
                  title={t('cert.clearSearch')}
                  onClick={() => setSearch('')}
                >
                  <IconClose size={12} />
                </button>
              ) : null}
            </div>
            <span className="text-secondary">
              {t('workflow.count', { n: filtered.length })}
            </span>
            {/* 新建类操作跟着它作用的卡片走，主操作（发起流程）留在页面工具栏 */}
            <div className="wf-def-toolbar-actions">
              <Button
                size="sm"
                icon={<IconPlus size={14} />}
                onClick={() => setTemplateOpen(true)}
                disabled={Boolean(creating)}
              >
                {t('workflow.createFromTemplate')}
              </Button>
              <Button
                size="sm"
                icon={<IconEdit size={14} />}
                onClick={() => void createBlank()}
                loading={creating === 'blank'}
                disabled={Boolean(creating) && creating !== 'blank'}
              >
                {t('workflow.createBlank')}
              </Button>
              <Button
                size="sm"
                icon={<IconUpload size={14} />}
                onClick={() => setImportOpen(true)}
              >
                {t('workflow.importFile')}
              </Button>
            </div>
          </div>

          {listQuery.isLoading ? (
            <div className="wf-loading">
              <Spinner size={24} label={t('common.loading')} />
            </div>
          ) : items.length === 0 ? (
            <EmptyState
              icon={<IconCertificate size={30} />}
              title={t('workflow.emptyTitle')}
              description={t('workflow.emptyHint')}
              action={
                <div className="wf-empty-actions">
                  <Button variant="primary" onClick={() => setTemplateOpen(true)}>
                    {t('workflow.createFromTemplate')}
                  </Button>
                  <Button onClick={() => setImportOpen(true)}>
                    {t('workflow.importFile')}
                  </Button>
                  <Button onClick={() => void createBlank()}>
                    {t('workflow.createBlank')}
                  </Button>
                </div>
              }
            />
          ) : filtered.length === 0 ? (
            <div className="table-container table-container-empty">
              {t('workflow.searchEmpty')}
            </div>
          ) : (
            <div className="wf-def-grid">
              {filtered.map((workflow) => (
                <article key={workflow.id} className="wf-def-card">
                  <div className="wf-def-head">
                    <span className="wf-node-icon tone-blue">
                      <IconCertificate size={18} />
                    </span>
                    <div className="wf-def-title-wrap">
                      <div className="wf-def-title">
                        {workflow.name}
                        <Badge variant="neutral" size="sm">
                          {t('workflow.version', { n: workflow.version })}
                        </Badge>
                      </div>
                      <div className="wf-def-desc">
                        {workflow.description || t('workflow.noDescription')}
                      </div>
                    </div>
                  </div>
                  <div className="wf-def-meta">
                    <Badge
                      variant={ORIGIN_VARIANT[workflow.origin] ?? 'neutral'}
                      size="sm"
                    >
                      {originLabel(workflow.origin, t)}
                    </Badge>
                    <span>{t('workflow.nodeCount', { n: workflow.node_count })}</span>
                    <span>
                      {workflow.updated_at
                        ? t('workflow.updatedAt', {
                            time: formatDateTime(workflow.updated_at),
                          })
                        : '—'}
                    </span>
                  </div>
                  <div className="wf-def-actions">
                    <Button
                      size="sm"
                      variant="primary"
                      icon={<IconFlow size={14} />}
                      disabled={!canManage || workflow.node_count === 0}
                      onClick={() => openLaunch(workflow)}
                    >
                      {t('workflow.run.new')}
                    </Button>
                    <Button
                      size="sm"
                      icon={<IconEdit size={14} />}
                      onClick={() => setEditingId(workflow.id)}
                    >
                      {t('workflow.edit')}
                    </Button>
                    <span
                      className={`wf-export${exporting === workflow.id ? ' is-busy' : ''}`}
                    >
                      <span className="wf-export-label">{t('workflow.export')}</span>
                      <IconChevronDown size={12} className="wf-export-caret" />
                      <select
                        className="wf-export-native"
                        aria-label={t('workflow.export')}
                        value=""
                        disabled={exporting === workflow.id}
                        onChange={(event) => {
                          const format = event.target.value as 'yaml' | 'json';
                          if (format) void exportWorkflow(workflow, format);
                          event.target.value = '';
                        }}
                      >
                        <option value="">{t('workflow.export')}</option>
                        <option value="json">{t('workflow.exportJson')}</option>
                        <option value="yaml">{t('workflow.exportYaml')}</option>
                      </select>
                    </span>
                    {/* 删除用带文字的按钮，不再只是角落里的图标：
                        之前是纯图标 + 藏在「定义」子视图里，用户根本找不到 */}
                    <Button
                      size="sm"
                      variant="ghost"
                      className="cert-danger-text"
                      icon={<IconTrash size={14} />}
                      disabled={!canManage}
                      onClick={() => setDeleteTarget(workflow)}
                    >
                      {t('workflow.delete')}
                    </Button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}

      <WorkflowLaunchDialog
        open={launchOpen}
        definitions={items}
        sites={sites}
        canManage={canManage}
        initialSiteId={launchSiteId}
        initialWorkflowId={launchDefinitionId}
        onClose={() => setLaunchOpen(false)}
        onCreated={() => {
          void queryClient.invalidateQueries({ queryKey: ['cert-workflow-runs'] });
          setSection('runs');
        }}
        onGoDefinitions={() => {
          setLaunchOpen(false);
          setSection('definitions');
        }}
      />

      <TemplateDialog
        open={templateOpen}
        onClose={() => setTemplateOpen(false)}
        templates={templates}
        specs={specs}
        creating={creating}
        error={templateQuery.isError ? errText(templateQuery.error) : ''}
        onUse={(template) => void useTemplate(template)}
      />

      <ImportDialog
        open={importOpen}
        onClose={() => setImportOpen(false)}
        sample={templates[0]}
        onImported={(workflow) => {
          setImportOpen(false);
          void refresh();
          setSection('definitions');
          setEditingId(workflow.id);
        }}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title={t('workflow.deleteTitle')}
        message={t('workflow.deleteMessage', { name: deleteTarget?.name ?? '' })}
        confirmText={t('workflow.delete')}
        danger
        loading={deleting}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => void remove()}
      />

      {editingId ? (
        <CertWorkflowEditor
          embedded
          workflowId={editingId}
          onClose={() => setEditingId('')}
          onSaved={() => void refresh()}
        />
      ) : null}
    </div>
  );
}
