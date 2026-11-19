/* ==========================================================================
   pvecenter — 证书工作流编辑器

   画布是自绘的（绝对定位节点 + SVG 贝塞尔连线），没有引入图形库：
   节点类型与参数表单都由后端契约驱动，这里只需要处理「摆位置、连依赖、
   改参数」三件事，自己画比接一套 React Flow 更轻、也更好控样式。

   数据流始终是「一份 nodes 数组」：
     · 拖动只改 position；
     · 连线只改 needs；
     · 参数面板只改 options；
   保存前统一交给后端校验（节点类型、必填参数、域名格式、依赖成环），
   校验通过的归一化结果再写回本地，避免前后端对「有效值」的理解漂移。
   ========================================================================== */

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { useNavigate, useParams } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { certWorkflowsApi, type CertWorkflowInput } from '../api/endpoints';
import { ApiError } from '../api/client';
import type {
  CertWorkflowIssue,
  CertWorkflowNode,
  CertWorkflowNodeSpec,
} from '../api/types';
import { PageShell } from '../components/Layout';
import { Button, IconButton } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import { ErrorState } from '../components/ui/EmptyState';
import { Spinner } from '../components/ui/Spinner';
import { Badge } from '../components/ui/Badge';
import {
  IconCheck,
  IconChevronLeft,
  IconClose,
  IconCopy,
  IconFlow,
  IconPlus,
  IconRefresh,
  IconSave,
  IconTrash,
} from '../components/Icons';
import { NodeOptionsForm } from '../components/workflow/NodeOptionsForm';
import { nodeLabel, nodeSummary, nodeVisual } from '../components/workflow/nodeMeta';
import { useToast } from '../hooks/useToast';
import { useAuth } from '../hooks/useAuth';
import { useT, type MessageKey } from '../i18n';

const NODE_WIDTH = 236;
const NODE_HEIGHT = 96;
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 1.5;

function errText(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function emptyValue(spec: CertWorkflowNodeSpec['fields'][number]): unknown {
  if (spec.default !== undefined) return clone(spec.default);
  if (spec.type === 'switch') return false;
  if (spec.type === 'stringlist' || spec.type === 'multiselect') return [];
  if (spec.type === 'number') return '';
  return '';
}

function defaultsFor(spec: CertWorkflowNodeSpec): Record<string, unknown> {
  const options: Record<string, unknown> = {};
  for (const field of spec.fields) {
    options[field.key] = emptyValue(field);
  }
  return options;
}

/** 节点 ID 输入要等失焦再提交，否则每敲一个字符都会改一次依赖表。 */
function NodeIdField({
  value,
  onCommit,
}: {
  value: string;
  onCommit: (next: string) => boolean;
}) {
  const t = useT();
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <Input
      label={t('workflow.form.nodeId')}
      hint={t('workflow.form.nodeIdHint')}
      value={draft}
      className="mono"
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        const next = draft.trim();
        if (next && next !== value) {
          if (!onCommit(next)) setDraft(value);
        } else {
          setDraft(value);
        }
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') (event.target as HTMLInputElement).blur();
      }}
    />
  );
}

export function CertWorkflowEditor({
  workflowId,
  embedded = false,
  onClose,
  onSaved,
}: {
  /** 嵌入证书管理页时由外层指定；独立路由则从 URL 取 */
  workflowId?: string;
  embedded?: boolean;
  onClose?: () => void;
  onSaved?: () => void;
} = {}) {
  const t = useT();
  const navigate = useNavigate();
  const { id: routeId = '' } = useParams();
  const id = workflowId ?? routeId;
  const toast = useToast();
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();
  const canManage = hasPermission('cert.manage');

  /* 嵌入证书页时锁定背景滚动，避免拖动画布时整页跟着滚 */
  useEffect(() => {
    if (!embedded) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.style.overflow = previous;
    };
  }, [embedded]);

  const workflowQuery = useQuery({
    queryKey: ['cert-workflow', id],
    queryFn: () => certWorkflowsApi.get(id),
    enabled: Boolean(id),
    retry: false,
  });
  const specQuery = useQuery({
    queryKey: ['cert-workflow-node-types'],
    queryFn: certWorkflowsApi.nodeTypes,
    staleTime: Infinity,
  });

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [nodes, setNodes] = useState<CertWorkflowNode[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [issues, setIssues] = useState<CertWorkflowIssue[]>([]);
  const [warnings, setWarnings] = useState<CertWorkflowIssue[]>([]);
  const [zoom, setZoom] = useState(1);
  const [deleteNode, setDeleteNode] = useState<CertWorkflowNode | null>(null);
  const [leaveOpen, setLeaveOpen] = useState(false);

  const loadedRef = useRef('');
  const fittedRef = useRef('');
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    id: string;
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);

  const workflow = workflowQuery.data?.workflow;

  useEffect(() => {
    if (!workflow || loadedRef.current === workflow.id) return;
    loadedRef.current = workflow.id;
    setName(workflow.name);
    setDescription(workflow.description);
    setNodes(workflow.nodes);
    setSelectedId(null);
    setIssues([]);
    setWarnings([]);
    setDirty(false);
  }, [workflow]);

  useEffect(() => {
    if (!dirty) return;
    const handler = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [dirty]);

  const specs = useMemo(
    () => new Map((specQuery.data?.types ?? []).map((spec) => [spec.type, spec])),
    [specQuery.data],
  );
  const specList = specQuery.data?.types ?? [];
  const maxNodes = specQuery.data?.max_nodes ?? 64;

  const selected = nodes.find((node) => node.id === selectedId) ?? null;
  const selectedSpec = selected ? specs.get(selected.type) : undefined;

  /* ------------------------------------------------------------ 几何 / 连线 */
  const canvasWidth = Math.max(
    900,
    ...nodes.map((node) => node.position.x + NODE_WIDTH + 80),
  );
  const canvasHeight = Math.max(
    520,
    ...nodes.map((node) => node.position.y + NODE_HEIGHT + 100),
  );

  /**
   * 把整条流程缩放到视口内。导入的模板通常是横向 4 个节点，在窄屏下 100%
   * 只能看到前两个；首次打开时自动适应一次，之后由用户自己控制缩放。
   */
  const zoomToFit = (floor: number) => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const widthFit = (viewport.clientWidth - 32) / canvasWidth;
    const heightFit = (viewport.clientHeight - 32) / canvasHeight;
    const next = Math.min(1, Math.max(floor, Math.min(widthFit, heightFit)));
    setZoom(Math.max(floor, Math.round(next * 100) / 100));
    viewport.scrollTo({ left: 0, top: 0 });
  };

  const fitToView = () => zoomToFit(MIN_ZOOM);

  useEffect(() => {
    if (!workflow || nodes.length === 0 || fittedRef.current === workflow.id) return;
    const frame = requestAnimationFrame(() => {
      // 首次打开至少保留 75%：4 节点模板在窄屏上全部塞进视口会小到看不清，
      // 宁可横向滚动一点，也让节点标题与参数摘要可读。
      zoomToFit(0.75);
      fittedRef.current = workflow.id;
    });
    return () => cancelAnimationFrame(frame);
    // 只在首次加载该工作流时自适应；后续增删节点不打断用户的缩放
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workflow, nodes.length]);

  const byId = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const edges = useMemo(
    () =>
      nodes.flatMap((target) =>
        (target.needs ?? []).flatMap((dep) => {
          const from = byId.get(dep);
          if (!from) return [];
          return [{ from, to: target, key: `${dep}->${target.id}` }];
        }),
      ),
    [nodes, byId],
  );

  const markDirty = () => setDirty(true);

  const updateNode = (nodeId: string, patch: Partial<CertWorkflowNode>) => {
    setNodes((prev) =>
      prev.map((node) => (node.id === nodeId ? { ...node, ...patch } : node)),
    );
    markDirty();
  };

  const updateOption = (nodeId: string, key: string, value: unknown) => {
    setNodes((prev) =>
      prev.map((node) =>
        node.id === nodeId
          ? { ...node, options: { ...node.options, [key]: value } }
          : node,
      ),
    );
    markDirty();
  };

  const addNode = (spec: CertWorkflowNodeSpec) => {
    if (nodes.length >= maxNodes) {
      toast.error(t('workflow.maxNodes', { n: maxNodes }));
      return;
    }
    let index = 1;
    let nodeId = `${spec.type}_${index}`;
    while (nodes.some((node) => node.id === nodeId)) {
      index += 1;
      nodeId = `${spec.type}_${index}`;
    }
    const anchor = selected;
    const count = nodes.length;
    const node: CertWorkflowNode = {
      id: nodeId,
      type: spec.type,
      title: '',
      options: defaultsFor(spec),
      needs: anchor ? [anchor.id] : count ? [nodes[count - 1].id] : [],
      position: anchor
        ? { x: anchor.position.x + NODE_WIDTH + 72, y: anchor.position.y }
        : {
            x: 40 + (count % 3) * (NODE_WIDTH + 72),
            y: 80 + Math.floor(count / 3) * 156,
          },
    };
    setNodes((prev) => [...prev, node]);
    setSelectedId(node.id);
    markDirty();
  };

  const duplicateNode = (node: CertWorkflowNode) => {
    if (nodes.length >= maxNodes) {
      toast.error(t('workflow.maxNodes', { n: maxNodes }));
      return;
    }
    let index = 2;
    let nodeId = `${node.id}_${index}`;
    while (nodes.some((item) => item.id === nodeId)) {
      index += 1;
      nodeId = `${node.id}_${index}`;
    }
    const copyNode: CertWorkflowNode = {
      ...clone(node),
      id: nodeId,
      position: { x: node.position.x + 36, y: node.position.y + 36 },
    };
    setNodes((prev) => [...prev, copyNode]);
    setSelectedId(nodeId);
    markDirty();
    toast.success(t('workflow.nodeAdded'), nodeLabel(copyNode, t, specs));
  };

  const removeNode = (node: CertWorkflowNode) => {
    setNodes((prev) =>
      prev
        .filter((item) => item.id !== node.id)
        .map((item) => ({
          ...item,
          needs: (item.needs ?? []).filter((dep) => dep !== node.id),
        })),
    );
    if (selectedId === node.id) setSelectedId(null);
    markDirty();
  };

  const renameNode = (node: CertWorkflowNode, next: string): boolean => {
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/.test(next)) {
      toast.error(t('workflow.nodeIdInvalid'), next);
      return false;
    }
    if (nodes.some((item) => item.id === next)) {
      toast.error(t('workflow.nodeIdDuplicate'), next);
      return false;
    }
    setNodes((prev) =>
      prev.map((item) => {
        if (item.id === node.id) return { ...item, id: next };
        return {
          ...item,
          needs: (item.needs ?? []).map((dep) => (dep === node.id ? next : dep)),
        };
      }),
    );
    setSelectedId(next);
    markDirty();
    return true;
  };

  const descendantsOf = (nodeId: string): Set<string> => {
    const result = new Set<string>();
    let changed = true;
    while (changed) {
      changed = false;
      for (const node of nodes) {
        if (result.has(node.id)) continue;
        if ((node.needs ?? []).some((dep) => dep === nodeId || result.has(dep))) {
          result.add(node.id);
          changed = true;
        }
      }
    }
    return result;
  };

  const tidyLayout = () => {
    if (nodes.length === 0) return;
    const depth = new Map<string, number>();
    const visiting = new Set<string>();
    const depthOf = (nodeId: string): number => {
      if (depth.has(nodeId)) return depth.get(nodeId) as number;
      if (visiting.has(nodeId)) return 0;
      visiting.add(nodeId);
      const node = byId.get(nodeId);
      const deps = (node?.needs ?? []).filter((dep) => byId.has(dep));
      const value = deps.length
        ? Math.max(...deps.map((dep) => depthOf(dep))) + 1
        : 0;
      visiting.delete(nodeId);
      depth.set(nodeId, value);
      return value;
    };
    for (const node of nodes) depthOf(node.id);
    const rows = new Map<number, number>();
    setNodes((prev) =>
      prev.map((node) => {
        const level = depth.get(node.id) ?? 0;
        const row = rows.get(level) ?? 0;
        rows.set(level, row + 1);
        return {
          ...node,
          position: {
            x: 40 + level * (NODE_WIDTH + 84),
            y: 80 + row * 156,
          },
        };
      }),
    );
    markDirty();
  };

  /* ------------------------------------------------------------ 拖动 */
  const onPointerDown = (
    event: ReactPointerEvent<HTMLDivElement>,
    node: CertWorkflowNode,
  ) => {
    if ((event.target as HTMLElement).closest('button,input,select,textarea')) return;
    event.preventDefault();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    dragRef.current = {
      id: node.id,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: node.position.x,
      originY: node.position.y,
    };
    setSelectedId(node.id);
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const dx = (event.clientX - drag.startX) / zoom;
    const dy = (event.clientY - drag.startY) / zoom;
    setNodes((prev) =>
      prev.map((node) =>
        node.id === drag.id
          ? {
              ...node,
              position: {
                x: Math.round(drag.originX + dx),
                y: Math.round(drag.originY + dy),
              },
            }
          : node,
      ),
    );
  };

  const onPointerUp = () => {
    if (dragRef.current) {
      dragRef.current = null;
      markDirty();
    }
  };

  /* ------------------------------------------------------------ 校验 / 保存 */
  const payload = (): CertWorkflowInput => ({
    name: name.trim(),
    description: description.trim(),
    nodes,
    origin: 'editor',
  });

  const selectIssueNode = (issue: CertWorkflowIssue) => {
    const match = /^nodes\[(\d+)\]/.exec(issue.path || '');
    if (!match) return;
    const node = nodes[Number(match[1])];
    if (node) setSelectedId(node.id);
  };

  const validate = async () => {
    setValidating(true);
    try {
      const result = await certWorkflowsApi.validate(payload());
      setIssues(result.errors ?? []);
      setWarnings(result.warnings ?? []);
      if (result.valid) {
        toast.success(t('workflow.validation.ok'));
        return result;
      }
      toast.error(
        t('workflow.validation.failed', { n: (result.errors ?? []).length }),
        result.errors?.[0]?.message,
      );
      if (result.errors?.length) selectIssueNode(result.errors[0]);
      return result;
    } catch (err) {
      toast.error(t('workflow.validation.failed', { n: 1 }), errText(err));
      return null;
    } finally {
      setValidating(false);
    }
  };

  const save = async () => {
    if (!id || saving || !canManage) return;
    setSaving(true);
    try {
      const result = await certWorkflowsApi.validate(payload());
      setIssues(result.errors ?? []);
      setWarnings(result.warnings ?? []);
      if (!result.valid || !result.definition) {
        toast.error(
          t('workflow.validation.failed', { n: (result.errors ?? []).length }),
          result.errors?.[0]?.message,
        );
        if (result.errors?.length) selectIssueNode(result.errors[0]);
        return;
      }
      const normalized = result.definition;
      setName(normalized.name);
      setDescription(normalized.description);
      setNodes(normalized.nodes);
      const updated = await certWorkflowsApi.update(id, {
        ...normalized,
        origin: 'editor',
      });
      queryClient.setQueryData(['cert-workflow', id], updated);
      await queryClient.invalidateQueries({ queryKey: ['cert-workflows'] });
      setDirty(false);
      onSaved?.();
      toast.success(t('workflow.saved'), updated.workflow.name);
    } catch (err) {
      toast.error(t('workflow.saveFailed'), errText(err));
    } finally {
      setSaving(false);
    }
  };

  const exitEditor = () => {
    if (embedded) {
      onClose?.();
      return;
    }
    navigate('/certificates');
  };

  const goBack = () => {
    if (dirty) setLeaveOpen(true);
    else exitEditor();
  };

  /* ------------------------------------------------------------ 渲染 */
  if (workflowQuery.isLoading) {
    if (embedded) {
      return (
        <div className="wf-embed-overlay" role="dialog" aria-modal="true">
          <div className="wf-embed-dialog is-message">
            <Spinner size={28} label={t('common.loading')} />
          </div>
        </div>
      );
    }
    return (
      <PageShell title={t('workflow.title')}>
        <div className="wf-loading">
          <Spinner size={28} label={t('common.loading')} />
        </div>
      </PageShell>
    );
  }

  if (workflowQuery.isError || !workflow) {
    if (embedded) {
      return (
        <div className="wf-embed-overlay" role="dialog" aria-modal="true">
          <div className="wf-embed-dialog is-message">
            <ErrorState
              title={t('workflow.notFound')}
              message={
                workflowQuery.error ? errText(workflowQuery.error) : t('workflow.notFound')
              }
              onRetry={() => void workflowQuery.refetch()}
              action={<Button onClick={exitEditor}>{t('workflow.editor.back')}</Button>}
            />
          </div>
        </div>
      );
    }
    return (
      <PageShell title={t('workflow.title')}>
        <ErrorState
          title={t('workflow.notFound')}
          message={
            workflowQuery.error ? errText(workflowQuery.error) : t('workflow.notFound')
          }
          onRetry={() => void workflowQuery.refetch()}
          action={
            <Button onClick={exitEditor}>
              {t('workflow.editor.back')}
            </Button>
          }
        />
      </PageShell>
    );
  }

  const descendantIds = selected ? descendantsOf(selected.id) : new Set<string>();

  const editorActions = (
    <div className="wf-page-actions">
      <Button icon={<IconChevronLeft size={16} />} onClick={goBack}>
        {t('workflow.editor.back')}
      </Button>
      <Button
        icon={<IconCheck size={16} />}
        loading={validating}
        onClick={() => void validate()}
      >
        {validating ? t('workflow.validation.validating') : t('workflow.validation.validate')}
      </Button>
      <Button
        variant="primary"
        icon={<IconSave size={16} />}
        loading={saving}
        disabled={!canManage}
        onClick={() => void save()}
      >
        {saving ? t('workflow.editor.saving') : t('workflow.editor.save')}
      </Button>
    </div>
  );

  const body = (
    <>
      <div className="wf-meta">
        <Input
          label={t('workflow.form.name')}
          required
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            markDirty();
          }}
        />
        <Input
          label={t('workflow.form.description')}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
            markDirty();
          }}
        />
        <div className="wf-meta-badges">
          <Badge variant="neutral">{t('workflow.nodeCount', { n: nodes.length })}</Badge>
          <Badge variant="info">{t('workflow.version', { n: workflow.version })}</Badge>
          {dirty ? (
            <Badge variant="warning" dot>
              {t('workflow.editor.unsaved')}
            </Badge>
          ) : null}
        </div>
      </div>

      <div className="wf-editor">
        <aside className="wf-palette" aria-label={t('workflow.editor.palette')}>
          <div className="wf-panel-head">
            <span>{t('workflow.editor.palette')}</span>
          </div>
          <div className="wf-panel-hint">{t('workflow.editor.paletteHint')}</div>
          {specList.map((spec) => {
            const visual = nodeVisual(spec.type);
            return (
              <button
                key={spec.type}
                type="button"
                className="wf-palette-item"
                onClick={() => addNode(spec)}
                disabled={nodes.length >= maxNodes}
              >
                <span className={`wf-node-icon tone-${visual.tone}`}>
                  {visual.icon(17)}
                </span>
                <span className="wf-palette-text">
                  <span className="wf-palette-name">
                    {t(spec.label_key as MessageKey)}
                  </span>
                  <span className="wf-palette-desc">
                    {t(spec.desc_key as MessageKey)}
                  </span>
                </span>
                <IconPlus size={14} />
              </button>
            );
          })}
        </aside>

        <section className="wf-canvas-panel">
          <div className="wf-canvas-toolbar">
            <span className="wf-canvas-title">{t('workflow.editor.canvas')}</span>
            <div className="wf-canvas-tools">
              <IconButton
                label={t('workflow.editor.zoomOut')}
                onClick={() => setZoom((value) => Math.max(MIN_ZOOM, Math.round((value - 0.1) * 10) / 10))}
              >
                <span aria-hidden="true">−</span>
              </IconButton>
              <button
                type="button"
                className="wf-zoom-value"
                onClick={() => setZoom(1)}
                title={t('workflow.editor.resetZoom')}
              >
                {Math.round(zoom * 100)}%
              </button>
              <IconButton
                label={t('workflow.editor.zoomIn')}
                onClick={() => setZoom((value) => Math.min(MAX_ZOOM, Math.round((value + 0.1) * 10) / 10))}
              >
                <span aria-hidden="true">+</span>
              </IconButton>
              <Button size="sm" onClick={fitToView}>
                {t('workflow.editor.fit')}
              </Button>
              <Button size="sm" icon={<IconRefresh size={14} />} onClick={tidyLayout}>
                {t('workflow.editor.tidy')}
              </Button>
            </div>
          </div>

          <div className="wf-canvas-viewport" ref={viewportRef}>
            {nodes.length === 0 ? (
              <div className="wf-canvas-empty">
                <div className="wf-empty-title">{t('workflow.editor.emptyCanvas')}</div>
                <div className="text-secondary">
                  {t('workflow.editor.emptyCanvasHint')}
                </div>
                {specList[0] ? (
                  <Button
                    variant="primary"
                    icon={<IconPlus size={15} />}
                    onClick={() => addNode(specList[0])}
                  >
                    {t(specList[0].label_key as MessageKey)}
                  </Button>
                ) : null}
              </div>
            ) : (
              <div
                className="wf-canvas-space"
                style={{ width: canvasWidth * zoom, height: canvasHeight * zoom }}
              >
                <div
                  className="wf-canvas"
                  style={{
                    width: canvasWidth,
                    height: canvasHeight,
                    transform: `scale(${zoom})`,
                  }}
                  onPointerDown={(event) => {
                    if (event.target === event.currentTarget) setSelectedId(null);
                  }}
                >
                  <svg
                    className="wf-edges"
                    width={canvasWidth}
                    height={canvasHeight}
                    aria-hidden="true"
                  >
                    <defs>
                      <marker
                        id="wf-arrow"
                        viewBox="0 0 10 10"
                        refX="9"
                        refY="5"
                        markerWidth="7"
                        markerHeight="7"
                        orient="auto-start-reverse"
                      >
                        <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
                      </marker>
                    </defs>
                    {edges.map((edge) => {
                      const x1 = edge.from.position.x + NODE_WIDTH;
                      const y1 = edge.from.position.y + 42;
                      const x2 = edge.to.position.x;
                      const y2 = edge.to.position.y + 42;
                      const bend = Math.max(40, Math.abs(x2 - x1) / 2);
                      const active =
                        selectedId === edge.to.id || selectedId === edge.from.id;
                      return (
                        <path
                          key={edge.key}
                          className={`wf-edge ${active ? 'is-active' : ''}`}
                          d={`M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`}
                          markerEnd="url(#wf-arrow)"
                        />
                      );
                    })}
                  </svg>

                  {nodes.map((node) => {
                    const spec = specs.get(node.type);
                    const visual = nodeVisual(node.type);
                    const active = node.id === selectedId;
                    return (
                      <div
                        key={node.id}
                        className={`wf-node ${active ? 'is-selected' : ''} tone-${visual.tone}`}
                        style={{ left: node.position.x, top: node.position.y }}
                        role="button"
                        tabIndex={0}
                        aria-label={nodeLabel(node, t, specs)}
                        onPointerDown={(event) => onPointerDown(event, node)}
                        onPointerMove={onPointerMove}
                        onPointerUp={onPointerUp}
                        onPointerCancel={onPointerUp}
                        onClick={() => setSelectedId(node.id)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            setSelectedId(node.id);
                          }
                          if (event.key === 'Delete' || event.key === 'Backspace') {
                            event.preventDefault();
                            setDeleteNode(node);
                          }
                        }}
                      >
                        <div className="wf-node-head">
                          <span className={`wf-node-icon tone-${visual.tone}`}>
                            {visual.icon(16)}
                          </span>
                          <span className="wf-node-title">
                            {nodeLabel(node, t, specs)}
                          </span>
                          {node.title ? (
                            <span className="wf-node-type">
                              {spec ? t(spec.label_key as MessageKey) : node.type}
                            </span>
                          ) : null}
                        </div>
                        <div className="wf-node-summary">
                          {nodeSummary(node, t, specs) || t('workflow.form.optionsEmpty')}
                        </div>
                        <div className="wf-node-foot">
                          <span className="wf-node-deps">
                            {(node.needs ?? []).length
                              ? `↳ ${(node.needs ?? [])
                                  .map((dep) => {
                                    const upstream = byId.get(dep);
                                    return upstream ? nodeLabel(upstream, t, specs) : dep;
                                  })
                                  .join(', ')}`
                              : t('workflow.form.noNeeds')}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </section>

        <aside className="wf-inspector" aria-label={t('workflow.editor.inspector')}>
          {selected && selectedSpec ? (
            <>
              <div className="wf-panel-head wf-inspector-head">
                <span>
                  {nodeLabel(selected, t, specs)}
                  {selected.title ? (
                    <span className="wf-inspector-type">
                      {t(selectedSpec.label_key as MessageKey)}
                    </span>
                  ) : null}
                </span>
                <IconButton
                  label={t('common.close')}
                  onClick={() => setSelectedId(null)}
                >
                  <IconClose size={15} />
                </IconButton>
              </div>
              <div className="wf-inspector-body">
                <Input
                  label={t('workflow.form.nodeTitle')}
                  hint={t('workflow.form.nodeTitleHint')}
                  value={selected.title}
                  placeholder={t(selectedSpec.label_key as MessageKey)}
                  onChange={(event) =>
                    updateNode(selected.id, { title: event.target.value })
                  }
                />
                <NodeIdField
                  key={selected.id}
                  value={selected.id}
                  onCommit={(next) => renameNode(selected, next)}
                />

                <div className="wf-field-block">
                  <div className="wf-field-label">{t('workflow.form.needs')}</div>
                  <div className="wf-field-hint">{t('workflow.form.needsHint')}</div>
                  <div className="wf-needs">
                    {nodes.length <= 1 ? (
                      <span className="text-secondary">{t('workflow.form.noNeeds')}</span>
                    ) : (
                      nodes
                        .filter((node) => node.id !== selected.id)
                        .map((node) => {
                          const checked = (selected.needs ?? []).includes(node.id);
                          const blocked = descendantIds.has(node.id);
                          return (
                            <label
                              key={node.id}
                              className={`wf-check ${checked ? 'is-on' : ''} ${blocked ? 'is-disabled' : ''}`}
                              title={blocked ? t('workflow.form.needsCycle') : undefined}
                            >
                              <input
                                type="checkbox"
                                checked={checked}
                                disabled={blocked}
                                onChange={() =>
                                  updateNode(selected.id, {
                                    needs: checked
                                      ? (selected.needs ?? []).filter((dep) => dep !== node.id)
                                      : [...(selected.needs ?? []), node.id],
                                  })
                                }
                              />
                              <span>{nodeLabel(node, t, specs)}</span>
                            </label>
                          );
                        })
                    )}
                  </div>
                </div>

                <div className="wf-section-title">{t('workflow.form.options')}</div>
                <NodeOptionsForm
                  spec={selectedSpec}
                  nodeId={selected.id}
                  options={selected.options}
                  disabled={!canManage}
                  onChange={(key, value) => updateOption(selected.id, key, value)}
                />

                <div className="wf-inspector-actions">
                  <Button
                    size="sm"
                    icon={<IconCopy size={14} />}
                    onClick={() => duplicateNode(selected)}
                  >
                    {t('workflow.duplicateNode')}
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    icon={<IconTrash size={14} />}
                    onClick={() => setDeleteNode(selected)}
                  >
                    {t('workflow.removeNode')}
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <div className="wf-inspector-body">
              <div className="wf-panel-head">
                <span>{t('workflow.editor.workflowInfo')}</span>
              </div>
              <p className="wf-inspector-help">{t('workflow.editor.inspectorEmpty')}</p>
              <div className="wf-inspector-help">{t('workflow.editor.canvasHint')}</div>
            </div>
          )}

          {issues.length || warnings.length ? (
            <div className="wf-issues">
              <div className="wf-section-title">{t('workflow.issuesTitle')}</div>
              {issues.map((issue, index) => (
                <button
                  key={`e-${issue.path}-${index}`}
                  type="button"
                  className="wf-issue-row is-error"
                  onClick={() => selectIssueNode(issue)}
                >
                  <span>{issue.message}</span>
                  {issue.path ? <code>{issue.path}</code> : null}
                </button>
              ))}
              {warnings.map((issue, index) => (
                <button
                  key={`w-${issue.path}-${index}`}
                  type="button"
                  className="wf-issue-row is-warning"
                  onClick={() => selectIssueNode(issue)}
                >
                  <span>{issue.message}</span>
                  {issue.path ? <code>{issue.path}</code> : null}
                </button>
              ))}
            </div>
          ) : null}
        </aside>
      </div>

      <ConfirmDialog
        open={Boolean(deleteNode)}
        title={t('workflow.deleteNodeTitle')}
        message={t('workflow.deleteNodeMessage', {
          name: deleteNode ? nodeLabel(deleteNode, t, specs) : '',
        })}
        confirmText={t('workflow.deleteNode')}
        danger
        onCancel={() => setDeleteNode(null)}
        onConfirm={() => {
          if (deleteNode) removeNode(deleteNode);
          setDeleteNode(null);
        }}
      />

      <ConfirmDialog
        open={leaveOpen}
        title={t('workflow.editor.leaveTitle')}
        message={t('workflow.editor.leaveMessage')}
        confirmText={t('workflow.editor.leave')}
        cancelText={t('workflow.editor.stay')}
        danger
        onCancel={() => setLeaveOpen(false)}
        onConfirm={exitEditor}
      />
    </>
  );

  if (embedded) {
    return (
      <div className="wf-embed-overlay" role="dialog" aria-modal="true">
        <div className="wf-embed-dialog">
          <div className="wf-embed-bar">
            <div className="wf-embed-heading">
              <span className="wf-embed-icon">
                <IconFlow size={18} />
              </span>
              <div>
                <div className="wf-embed-title">{name || t('workflow.title')}</div>
                <div className="wf-embed-sub">{t('workflow.editor.embeddedHint')}</div>
              </div>
            </div>
            {editorActions}
          </div>
          <div className="wf-embed-body">{body}</div>
        </div>
      </div>
    );
  }

  return (
    <PageShell
      title={t('workflow.title')}
      subtitle={name || t('workflow.editor.canvasHint')}
      actions={editorActions}
    >
      {body}
    </PageShell>
  );
}
