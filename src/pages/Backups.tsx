/* ==========================================================================
   pvecenter — 备份
   三个 Tab：备份文件 / 备份计划 / 立即备份
   ========================================================================== */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  backupsApi,
  connectionsApi,
  nodesApi,
  storagesApi,
  vmsApi,
} from '../api/endpoints';
import type { BackupStats } from '../api/types';
import { errorMessage, isNotImplemented } from '../api/client';
import { getGlobalScope } from '../api/scope';
import { PageShell } from '../components/Layout';
import { Card, CardHeader, KpiCard } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button, IconButton } from '../components/ui/Button';
import { Input, Select, Switch, Textarea, Field } from '../components/ui/Input';
import { Table, type Column } from '../components/ui/Table';
import { Modal } from '../components/ui/Modal';
import { ErrorState, Notice, CollapsibleCard } from '../components/ui/EmptyState';
import { PveQueryError } from '../components/SetupRequired';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import { BackupOwnerDriftDialog } from '../components/BackupOwnerDriftDialog';
import {
  IconBackup,
  IconRefresh,
  IconPlus,
  IconSearch,
  IconTrash,
  IconDownload,
  IconClock,
  IconCheck,
  IconShield,
  IconCopy,
  IconActivity,
  IconUpload,
} from '../components/Icons';
import { formatBytes, formatDateTime, formatRelative } from '../utils/format';
import { backupModeOptions, compressOptions } from '../utils/status';
import { PagerBar, usePaged } from '../hooks/usePaged';
import { useTaskRunner, type TaskRunner } from '../hooks/useTaskRunner';
import { useToast } from '../hooks/useToast';
import { useAuth } from '../hooks/useAuth';
import { tStatic, useT, type MessageKey, type TFunc } from '../i18n';
import type {
  BackupItem,
  BackupJob,
  BackupJobInput,
  BackupMode,
  BackupRestoreRequest,
} from '../api/types';

/* ---------------------------------------------------------------------------
   页面
   --------------------------------------------------------------------------- */

type TabKey = 'files' | 'jobs' | 'create';

/**
 * 备份计划行：多台 PVE 的计划合并展示，额外记住它来自哪条连接。
 *
 * 任务号是各主机各自编的（两台都可能有 job_id=1），没有这个字段就无法确定
 * 「删除该发给哪一台」，表格的行键也会撞车。
 */
type JobRow = BackupJob & { connection_id?: string };

/**
 * 保留策略统一成 `keep-daily=7,keep-weekly=4` 属性串。
 *
 * PVE 各版本对 ``prune-backups`` 的返回形状不一致：多数给字符串，有的版本
 * （以及带默认保留策略的新建计划）直接给解析后的对象
 * `{ 'keep-daily': 7, ... }`。对象直接进 JSX 会触发 React #31
 * （Objects are not valid as a React child）整页白屏，编辑框里也只会得到
 * `[object Object]` —— 读出来先收敛成字符串。
 */
function pruneText(value: unknown): string {
  if (typeof value === 'string') return value.trim();
  if (Array.isArray(value)) {
    return value
      .map((v) => pruneText(v))
      .filter(Boolean)
      .join(',');
  }
  if (value && typeof value === 'object') {
    return Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v !== null && v !== undefined && v !== '')
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
      .join(',');
  }
  return value === null || value === undefined ? '' : String(value);
}

/** 保留策略里的人话标签（`keep-daily` → 「日 {n}」） */
const PRUNE_KEY_LABELS: Record<string, MessageKey> = {
  'keep-last': 'backups.keepLast',
  'keep-hourly': 'backups.keepHourly',
  'keep-daily': 'backups.keepDaily',
  'keep-weekly': 'backups.keepWeekly',
  'keep-monthly': 'backups.keepMonthly',
  'keep-yearly': 'backups.keepYearly',
};

/**
 * 解析 ``keep-daily=7,keep-weekly=4``（也接受后端给的字典形状）。
 * 解析不出来返回 null —— 调用方据此回退到原始串，不假装看懂。
 */
function parsePrune(value: unknown): Array<{ key: string; n: number }> | null {
  const text = pruneText(value);
  if (!text) return null;
  const out: Array<{ key: string; n: number }> = [];
  for (const part of text.split(',')) {
    const [rawKey, rawVal] = part.split('=').map((s) => s.trim());
    if (!rawKey) continue;
    if (rawVal === undefined || rawVal === '') {
      /* keep-all 这类没有数值的键 */
      out.push({ key: rawKey, n: Number.NaN });
      continue;
    }
    const n = Number(rawVal);
    if (!Number.isFinite(n)) return null;
    out.push({ key: rawKey, n });
  }
  return out.length ? out : null;
}

/** 保留策略的人话版本：`日 7 · 周 4 · 月 3`；解析失败返回空串 */
function pruneSummary(value: unknown, t: TFunc): string {
  const parsed = parsePrune(value);
  if (!parsed) return '';
  return parsed
    .map(({ key, n }) => {
      if (key === 'keep-all') return t('backups.keepAll');
      const labelKey = PRUNE_KEY_LABELS[key];
      /* 认不出的键（PVE 新加的）原样保留，别把信息吞掉 */
      if (!labelKey) return Number.isNaN(n) ? key : `${key}=${n}`;
      return Number.isNaN(n) ? t(labelKey, { n: 1 }) : t(labelKey, { n });
    })
    .join(' · ');
}

/* PVE schedule：``HH:MM`` 或 ``星期几[,星期几] HH:MM``（``*`` = 每天） */
const WEEKDAY_KEYS: Record<string, MessageKey> = {
  mon: 'backups.wdMon',
  tue: 'backups.wdTue',
  wed: 'backups.wdWed',
  thu: 'backups.wdThu',
  fri: 'backups.wdFri',
  sat: 'backups.wdSat',
  sun: 'backups.wdSun',
};

/** 调度计划格式是否合法（空串交给 required 校验处理） */
function isScheduleValid(raw: string): boolean {
  const text = raw.trim();
  if (!text) return true;
  const timeRe = /^([01]?\d|2[0-3]):[0-5]\d$/;
  if (timeRe.test(text)) return true;
  const match = /^([a-z,*\-\s]+?)\s+(\S+)$/i.exec(text);
  if (!match) return false;
  if (!timeRe.test(match[2])) return false;
  return match[1]
    .split(',')
    .map((day) => day.trim().toLowerCase())
    .every((day) => day === '*' || Boolean(WEEKDAY_KEYS[day]));
}

/**
 * 节点下拉里的「所有节点」。
 *
 * 计划任务**不写 node** 时 PVE 会在集群里每个节点各跑一遍；「立即备份」是一条
 * 节点级 API，选所有节点时由前端拆成每节点一次请求。表单里用一个哨兵值表示
 * 这个状态，不能拿空串兼任 —— 否则「还没选」和「所有节点」会混成同一件事。
 */
const ALL_NODES = '__all__';

/**
 * 目标存储选项（备份计划与「立即备份」共用）。
 *
 * 选「所有节点」时任务会在每个节点各跑一遍，存储必须**每个**节点都有：逐节点取
 * 一遍再取交集。单个节点读不到就跳过它（那台本来也跑不了这个任务）。
 */
function useTargetStorages(
  conn: string,
  target: string,
  nodeNames: string[],
  enabled: boolean,
): Array<{ label: string; value: string }> {
  const query = useQuery({
    queryKey: [
      'storages',
      conn,
      target === ALL_NODES ? `all:${nodeNames.join(',')}` : target,
    ],
    queryFn: async () => {
      if (target !== ALL_NODES) {
        return storagesApi.list(target || undefined, conn);
      }
      const perNode = await Promise.all(
        nodeNames.map(async (name) => {
          try {
            const list = await storagesApi.list(name, conn);
            return list.map((s) => ({ ...s, node: name }));
          } catch {
            return [];
          }
        }),
      );
      return perNode.flat();
    },
    enabled:
      enabled &&
      Boolean(conn) &&
      (target === ALL_NODES ? nodeNames.length > 0 : Boolean(target)),
    staleTime: 30_000,
  });

  return useMemo(() => {
    const usable = (query.data ?? []).filter((s) =>
      (s.content ?? '').split(',').includes('backup'),
    );
    if (target === ALL_NODES) {
      const byNode = new Map<string, Set<string>>();
      for (const s of usable) {
        const key = s.node ?? '';
        if (!byNode.has(key)) byNode.set(key, new Set());
        byNode.get(key)!.add(s.storage);
      }
      const [first, ...rest] = [...byNode.values()];
      if (!first) return [];
      return [...first]
        .filter((name) => rest.every((set) => set.has(name)))
        .map((name) => ({ label: name, value: name }));
    }
    /* 共享存储会在每个节点上各出现一次，按名字去重 */
    const seen = new Set<string>();
    return usable
      .filter((s) => (seen.has(s.storage) ? false : (seen.add(s.storage), true)))
      .map((s) => ({ label: s.storage, value: s.storage }));
  }, [query.data, target]);
}

/**
 * 备份归档行：多台 PVE 的归档合并展示时记住来源连接。
 *
 * 卷标识形如 ``local:backup/vzdump-qemu-100-….vma.zst``，两台 PVE 上完全可能
 * 重名 —— 表格行键与「删除该发给谁」都靠这个字段区分。
 */
type BackupRow = BackupItem & { connection_id?: string };

/**
 * 归档属于虚拟机备份还是容器备份。
 *
 * 两种存储的 volid 形状不同：目录型存储写 ``vzdump-qemu-…`` / ``vzdump-lxc-…``，
 * PBS 按类型分目录写 ``backup/vm/…`` / ``backup/ct/…``。只认 ``vzdump-lxc``
 * 会让 PBS 上的容器备份被当成虚拟机（PVE 回 `got unexpected backup format
 * 'pbs-ct'`），所以这里和 ``backend/app/backup_volid.py`` 用同一套判定。
 */
function archiveGuestKind(
  volid: string,
  format?: string,
): 'qemu' | 'lxc' {
  const text = volid || '';
  if (text.includes('vzdump-lxc')) return 'lxc';
  if (text.includes('vzdump-qemu')) return 'qemu';
  /* ``pbs:backup/ct/104/…``：``backup`` 前面是存储名后的冒号，不是斜杠 */
  if (/(?:^|[:/])backup\/ct\//.test(text)) return 'lxc';
  if (/(?:^|[:/])backup\/vm\//.test(text)) return 'qemu';
  const fmt = (format || '').toLowerCase();
  if (fmt === 'pbs-ct' || fmt.startsWith('tar') || fmt === 'tgz') return 'lxc';
  if (fmt === 'pbs-vm') return 'qemu';
  /* 认不出时按虚拟机处理（与后端一致） */
  return 'qemu';
}

export function Backups() {
  const t = useT();
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const { hasPermission, isAdmin } = useAuth();
  /* 备份能力（立即备份 / 恢复 / 删除卷 / 备份计划）按备份权限判 —— 后端
     backups.py 允许 `vm.backup` **或** `ct.backup`（vzdump 归档里既有虚拟机
     也有容器）。原先这里用 canWrite（按角色名粗判），自定义角色即使勾了
     「备份」也点不动。 */
  const canBackup =
    hasPermission('vm.backup') || hasPermission('ct.backup');
  /** 归属对账：全集群存储遍历 + 列出所有人的标记，只给管理员 */
  const [driftOpen, setDriftOpen] = useState(false);

  const tab = (params.get('tab') as TabKey | null) ?? 'files';

  const setTab = (next: TabKey) => {
    const p = new URLSearchParams(params);
    if (next === 'files') p.delete('tab');
    else p.set('tab', next);
    setParams(p, { replace: true });
  };

  const tabs: Array<{ key: TabKey; labelKey: MessageKey; icon: ReactNode }> = [
    { key: 'files', labelKey: 'backups.tabFiles', icon: <IconBackup size={15} /> },
    { key: 'jobs', labelKey: 'backups.tabJobs', icon: <IconClock size={15} /> },
    {
      key: 'create',
      labelKey: 'backups.tabCreate',
      icon: <IconPlus size={15} />,
    },
  ];

  return (
    <PageShell
      title={
        <>
          <IconBackup size={20} />
          {t('backups.title')}
        </>
      }
      subtitle={t('backups.subtitle')}
      actions={
        <>
          {/* 存量归档里的 [owner:x] 不会随改派更新：这个入口把它们与现状的差异列出来 */}
          {isAdmin ? (
            <Button
              variant="secondary"
              icon={<IconShield size={15} />}
              onClick={() => setDriftOpen(true)}
            >
              {t('backups.driftAction')}
            </Button>
          ) : null}
          <Button
            variant="secondary"
            icon={<IconRefresh size={15} />}
            onClick={() => {
              void queryClient.invalidateQueries({ queryKey: ['backups'] });
            }}
          >
            {t('common.refresh')}
          </Button>
        </>
      }
    >
      <div className="tabs" role="tablist" aria-label={t('backups.tabAria')}>
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            className={`tab ${tab === item.key ? 'is-active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            <span className="tab-icon" aria-hidden="true">
              {item.icon}
            </span>
            {t(item.labelKey)}
          </button>
        ))}
      </div>

      {tab === 'files' ? (
        <BackupFilesTab canBackup={canBackup} onGoCreate={() => setTab('create')} />
      ) : tab === 'jobs' ? (
        <BackupJobsTab canBackup={canBackup} isAdmin={isAdmin} />
      ) : (
        <BackupCreateTab canBackup={canBackup} />
      )}

      <BackupOwnerDriftDialog open={driftOpen} onClose={() => setDriftOpen(false)} />
    </PageShell>
  );
}

/* ---------------------------------------------------------------------------
   Tab：立即备份
   --------------------------------------------------------------------------- */

function BackupCreateTab({ canBackup }: { canBackup: boolean }) {
  const t = useT();
  const toast = useToast();
  const runner = useTaskRunner();

  const [node, setNode] = useState('');
  const [vmid, setVmid] = useState('');
  const [all, setAll] = useState(false);
  const [storage, setStorage] = useState('');
  const [mode, setMode] = useState<BackupMode>('snapshot');
  const [compress, setCompress] = useState('zstd');
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  /** 「备份全部」一次性覆盖整节点的机器，提交前先确认影响面 */
  const [confirmAll, setConfirmAll] = useState(false);
  /* 先选集群再选节点：与计划编辑器同一套交互，避免把请求打到别的主机上 */
  const [conn, setConn] = useState('');

  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    staleTime: 60_000,
  });

  /* 默认集群：优先顶栏作用域，再回落到当前连接 */
  useEffect(() => {
    const conns = connectionsQuery.data ?? [];
    if (!conns.length) return;
    if (conn && conns.some((c) => c.id === conn)) return;
    const target =
      conns.find((c) => c.id === getGlobalScope()) ??
      conns.find((c) => c.active) ??
      conns[0];
    setConn(target?.id ?? '');
  }, [conn, connectionsQuery.data]);

  /* 节点列表只取所选集群下的节点（节点名在每套 PVE 上都是本地的）。
     不带连接时后端会落到「面板当前连接」，在别的主机的节点名上直接报
     `hostname lookup 'pve9' failed`。 */
  const nodesQuery = useQuery({
    queryKey: ['nodes', conn],
    queryFn: () => nodesApi.list(conn),
    enabled: Boolean(conn),
    staleTime: 30_000,
  });

  const vmsQuery = useQuery({
    queryKey: ['vms', 'all', conn],
    queryFn: () => vmsApi.list(undefined, { connectionId: conn }),
    enabled: Boolean(conn),
    staleTime: 30_000,
  });

  const nodeNames = useMemo(
    () =>
      (nodesQuery.data ?? [])
        .filter((n) => !conn || !n.connection_id || n.connection_id === conn)
        .map((n) => n.node),
    [nodesQuery.data, conn],
  );

  /* 默认选中该集群的第一个节点；「所有节点」必须由用户显式选 */
  useEffect(() => {
    if (!node && nodeNames.length > 0) setNode(nodeNames[0]);
  }, [node, nodeNames]);

  const storageOptions = useTargetStorages(conn, node, nodeNames, true);

  useEffect(() => {
    if (!storage && storageOptions.length > 0) {
      setStorage(storageOptions[0].value);
    }
  }, [storage, storageOptions]);

  /* 选「所有节点」时可选整个集群的机器；指定 VMID 时提交会按它所在节点路由 */
  const nodeVms = useMemo(
    () =>
      (vmsQuery.data ?? [])
        .filter((v) => !v.template && (node === ALL_NODES || v.node === node))
        .sort((a, b) => a.vmid - b.vmid),
    [vmsQuery.data, node],
  );

  const nodeOptions = useMemo(
    () => [
      { label: t('backups.allNodes'), value: ALL_NODES },
      ...nodeNames.map((name) => ({ label: name, value: name })),
    ],
    [nodeNames, t],
  );

  /* 立即备份是**节点级**接口：选「所有节点」时拆成每节点一次请求。
     指定 VMID 时只发往它所在的那一台 —— 别的节点上没有这台机器。 */
  const targetNodes = useMemo(() => {
    if (node !== ALL_NODES) return node ? [node] : [];
    if (all) return nodeNames;
    const vm = (vmsQuery.data ?? []).find((v) => v.vmid === Number(vmid));
    return vm?.node ? [vm.node] : [];
  }, [node, all, nodeNames, vmsQuery.data, vmid]);

  const canSubmit =
    canBackup &&
    Boolean(conn) &&
    targetNodes.length > 0 &&
    Boolean(storage) &&
    (all || Boolean(vmid)) &&
    !busy;

  const doSubmit = async () => {
    if (!targetNodes.length) {
      toast.error(t('common.opFailed'), t('backups.vmNodeUnknown'));
      return;
    }
    setBusy(true);
    try {
      /* 每个节点各起一条任务；all=1 时各节点备自己的机器 */
      await Promise.allSettled(
        targetNodes.map((target) =>
          runner.run(
            backupsApi.create(
              {
                node: target,
                vmid: all ? undefined : Number(vmid),
                storage,
                mode,
                compress,
                notes: notes.trim() || undefined,
                all: all || undefined,
              },
              conn,
            ),
            {
              title: all
                ? t('backups.taskAll', { node: target })
                : t('backups.taskOne', { vmid }),
              node: target,
              invalidate: [['backups', 'list'], ['tasks']],
            },
          ),
        ),
      );
    } catch {
      /* 错误已由 runner 提示 */
    } finally {
      setBusy(false);
    }
  };

  /** 全节点备份先确认：一次可能覆盖几十台，stop/suspend 模式下还会停机器 */
  const requestSubmit = () => {
    if (all) {
      setConfirmAll(true);
      return;
    }
    void doSubmit();
  };

  if (!canBackup) {
    return (
      <Notice tone="info" title={t('backups.readOnlyTitle')}>
        {t('backups.readOnlyBody')}
      </Notice>
    );
  }

  return (
    <>
    <div className="grid grid-2">
      <Card>
        <CardHeader
          title={t('backups.createCard')}
          subtitle={t('backups.createCardSub')}
          icon={<IconBackup size={17} />}
        />

        <div className="dyn-list">
          {/* 集群与节点是同一件事的两级（先选哪套 PVE、再选它上面的哪台机器），
              并排放一行；拆成两行会让表单看起来多出一个无关字段 */}
          <div className="dyn-row">
          <Field
            label={t('backups.fieldCluster')}
            required
            hint={t('backups.fieldClusterShortHint')}
          >
            <Select
              value={conn}
              onChange={(e) => {
                /* 换集群 = 换一套 PVE：节点名与存储池都不通用 */
                setConn(e.target.value);
                setNode('');
                setVmid('');
                setStorage('');
              }}
              options={(connectionsQuery.data ?? []).map((c) => ({
                label: `${c.name || c.host || c.id}${
                  c.active ? t('vmCreate.connCurrentTag') : ''
                }`,
                value: c.id,
              }))}
              placeholder={t('backups.clusterPlaceholder')}
            />
          </Field>

          <Field
            label={t('backups.fieldNode')}
            required
            hint={
              node === ALL_NODES
                ? t('backups.allNodesHint')
                : t('backups.nodeFieldHint')
            }
          >
            <Select
              value={node}
              onChange={(e) => {
                const next = e.target.value;
                setNode(next);
                /* 换到具体节点时，原来选的机器未必在那台上；选「所有节点」
                   时保留 —— 它仍然是集群里的一台，稍后按所在节点路由 */
                if (next !== ALL_NODES) setVmid('');
                setStorage('');
              }}
              options={nodeOptions}
              placeholder={
                nodeNames.length === 0
                  ? t('backups.nodeLoading')
                  : t('backups.nodePlaceholder')
              }
              disabled={!conn}
            />
          </Field>
          </div>

          <Field label={t('backups.fieldScope')} required>
            <div
              className="radio-group"
              role="radiogroup"
              aria-label={t('backups.fieldScope')}
            >
              <label className="radio-field">
                <input
                  type="radio"
                  name="backup-scope"
                  className="radio-input"
                  checked={!all}
                  onChange={() => setAll(false)}
                />
                <span className="radio-dot" aria-hidden="true" />
                <span className="radio-content">
                  <span className="radio-label">{t('backups.scopeOne')}</span>
                  <span className="radio-hint">{t('backups.scopeOneHint')}</span>
                </span>
              </label>
              <label className="radio-field">
                <input
                  type="radio"
                  name="backup-scope"
                  className="radio-input"
                  checked={all}
                  onChange={() => {
                    setAll(true);
                    setVmid('');
                  }}
                />
                <span className="radio-dot" aria-hidden="true" />
                <span className="radio-content">
                  <span className="radio-label">{t('backups.scopeAll')}</span>
                  <span className="radio-hint">
                    {t('backups.scopeAllHint', { count: nodeVms.length })}
                  </span>
                </span>
              </label>
            </div>
          </Field>

          {!all ? (
            <Field
              label={t('backups.fieldVm')}
              required
              hint={nodeVms.length === 0 ? t('backups.noVm') : undefined}
            >
              <Select
                value={vmid}
                onChange={(e) => setVmid(e.target.value)}
                placeholder={t('backups.vmPlaceholder')}
                options={nodeVms.map((v) => ({
                  label: `${v.vmid} — ${v.name || t('backups.vmUnnamed')}`,
                  value: String(v.vmid),
                }))}
              />
            </Field>
          ) : null}

          <Field
            label={t('backups.fieldStorage')}
            required
            hint={
              storageOptions.length === 0
                ? node === ALL_NODES
                  ? t('backups.noSharedBackupStorage')
                  : t('backups.noBackupStorage')
                : node === ALL_NODES
                  ? t('backups.storageAllNodesHint')
                  : t('backups.storageHint')
            }
          >
            <Select
              value={storage}
              onChange={(e) => setStorage(e.target.value)}
              options={storageOptions}
              placeholder={t('backups.storagePlaceholder')}
            />
          </Field>

          <Field
            label={t('backups.fieldMode')}
            required
            /* 危险模式的影响写在控件旁边：右侧说明卡离得太远，等于没提示 */
            hint={
              mode === 'stop'
                ? t('backups.modeStopWarn')
                : mode === 'suspend'
                  ? t('backups.modeSuspendWarn')
                  : t('backups.modeSnapshotHint')
            }
          >
            <Select
              value={mode}
              onChange={(e) => setMode(e.target.value as BackupMode)}
              options={backupModeOptions(t).map((o) => ({
                label: o.label,
                value: o.value,
              }))}
            />
          </Field>

          <Field label={t('backups.fieldCompress')} required>
            <Select
              value={compress}
              onChange={(e) => setCompress(e.target.value)}
              options={compressOptions(t).map((o) => ({
                label: o.label,
                value: o.value,
              }))}
            />
          </Field>

          <Field label={t('backups.fieldNotes')} hint={t('backups.fieldNotesHint')}>
            <Textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              placeholder={t('backups.notesPlaceholder')}
            />
          </Field>
        </div>

        <div className="flex items-center gap-8 mt-16">
          <Button
            variant="primary"
            icon={<IconBackup size={15} />}
            onClick={requestSubmit}
            loading={busy}
            disabled={!canSubmit}
          >
            {t('backups.start')}
          </Button>
        </div>
      </Card>

      <div className="flex flex-col gap-16">
        <Card>
          <CardHeader title={t('backups.modeHelp')} icon={<IconShield size={17} />} />
          <div className="desc-list">
            <div className="desc-item">
              <div className="desc-label">Snapshot</div>
              <div className="desc-value">{t('backups.modeSnapshot')}</div>
            </div>
            <div className="desc-item">
              <div className="desc-label">Suspend</div>
              <div className="desc-value">{t('backups.modeSuspend')}</div>
            </div>
            <div className="desc-item">
              <div className="desc-label">Stop</div>
              <div className="desc-value">{t('backups.modeStop')}</div>
            </div>
          </div>
        </Card>

        <CollapsibleCard
          title={t('backups.compressHelp')}
          icon={<IconShield size={15} />}
        >
          <div className="desc-list">
            <div className="desc-item">
              <div className="desc-label">ZSTD</div>
              <div className="desc-value">{t('backups.compressZstd')}</div>
            </div>
            <div className="desc-item">
              <div className="desc-label">LZO</div>
              <div className="desc-value">{t('backups.compressLzo')}</div>
            </div>
            <div className="desc-item">
              <div className="desc-label">GZIP</div>
              <div className="desc-value">{t('backups.compressGzip')}</div>
            </div>
          </div>
        </CollapsibleCard>
      </div>
      </div>

      {/* 全节点备份的影响面确认：台数 + 模式后果，避免一次点下去把整节点机器停掉 */}
      <ConfirmDialog
        open={confirmAll}
        onCancel={() => setConfirmAll(false)}
        onConfirm={() => {
          setConfirmAll(false);
          void doSubmit();
        }}
        title={t('backups.confirmAllTitle')}
        confirmText={t('backups.start')}
        danger={mode === 'stop'}
        message={
          node === ALL_NODES
            ? t('backups.confirmAllBodyAllNodes', {
                nodes: nodeNames.length,
                count: nodeVms.length,
                mode:
                  backupModeOptions(t).find((o) => o.value === mode)?.label ??
                  mode,
              })
            : t('backups.confirmAllBody', {
                node,
                count: nodeVms.length,
                mode:
                  backupModeOptions(t).find((o) => o.value === mode)?.label ??
                  mode,
              })
        }
      />
    </>
  );
}

/* ---------------------------------------------------------------------------
   Tab：备份文件
   --------------------------------------------------------------------------- */

function BackupFilesTab({
  canBackup,
  onGoCreate,
}: {
  canBackup: boolean;
  /** 跳到「立即备份」标签页：空列表时最需要的下一步 */
  onGoCreate: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const runner = useTaskRunner();
  const queryClient = useQueryClient();

  const [node, setNode] = useState('');
  const [storage, setStorage] = useState('');
  const [search, setSearch] = useState('');
  const [restoreTarget, setRestoreTarget] = useState<BackupRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<BackupRow | null>(null);
  const [busy, setBusy] = useState(false);
  /** 近期失败明细：成功率卡点开 */
  const [failuresOpen, setFailuresOpen] = useState(false);

  const nodesQuery = useQuery({
    queryKey: ['nodes'],
    queryFn: () => nodesApi.list(),
    staleTime: 30_000,
  });

  /* 节点属于哪台 PVE（/api/nodes 跨主机聚合，每条带 connection_id）：
     选中别的 PVE 上的节点时，不带连接的请求会被送到「面板当前连接」，
     在那边找这个节点名 —— PVE 回 hostname lookup 失败，列表就是空的/报错。 */
  const connOfNode = (name: string) =>
    (nodesQuery.data ?? []).find((n) => n.node === name)?.connection_id;
  const conn = node ? connOfNode(node) : undefined;

  /* 「全部节点」必须跨所有 PVE 取：归档各存各家，只查当前连接会让别的主机的备份
     凭空消失（而页头的 KPI 是跨主机汇总的，两边会自相矛盾）。每条归档记住来源
     连接 —— 卷标识（local:backup/xxx）在不同主机上可能重名，删除要知道发给谁。 */
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    staleTime: 60_000,
  });
  const connectionIds = (connectionsQuery.data ?? []).map((c) => c.id).join(',');

  const listQuery = useQuery({
    queryKey: ['backups', 'list', conn, node, storage, connectionIds],
    queryFn: async () => {
      if (node) {
        // 选了具体节点：只问它所在的那台主机
        return backupsApi.list(
          { node, storage: storage || undefined },
          conn,
        );
      }
      const conns = connectionsQuery.data ?? [];
      const results = await Promise.all(
        conns.map(async (c) => {
          try {
            const list = await backupsApi.list(
              { storage: storage || undefined },
              c.id,
            );
            return list.map((b) => ({ ...b, connection_id: c.id }));
          } catch {
            // 单台主机读失败只跳过它自己，不影响其它主机的归档
            return [] as BackupRow[];
          }
        }),
      );
      return results.flat();
    },
    enabled: Boolean(node) || connectionsQuery.isSuccess,
    staleTime: 20_000,
  });

  /* 备份失败最怕「事前不知道」：把最近的成功率摆在页面上，
     和归档列表放在一起，跑没跑、成没成一目了然 */
  const statsQuery = useQuery({
    queryKey: ['backups', 'stats'],
    queryFn: () => backupsApi.stats(),
    staleTime: 60_000,
    retry: false,
  });
  const backupStats: BackupStats | undefined = statsQuery.data;

  const nodeOptions = useMemo(
    () => [
      { label: t('backups.filterAllNodes'), value: '' },
      ...(nodesQuery.data ?? []).map((n) => ({ label: n.node, value: n.node })),
    ],
    [nodesQuery.data],
  );

  /* 从已加载数据中提取存储名，避免额外请求 */
  const storageOptions = useMemo(() => {
    const set = new Set(
      (listQuery.data ?? [])
        .map((b) => b.storage)
        .filter((s): s is string => Boolean(s)),
    );
    return [
      { label: t('backups.filterAllStorage'), value: '' },
      ...[...set].sort().map((s) => ({ label: s, value: s })),
    ];
  }, [listQuery.data]);

  const items = useMemo(() => {
    const list = listQuery.data ?? [];
    const q = search.trim().toLowerCase();
    const filtered = q
      ? list.filter(
          (b) =>
            b.volid.toLowerCase().includes(q) ||
            String(b.vmid).includes(q) ||
            (b.notes ?? '').toLowerCase().includes(q),
        )
      : list;
    return [...filtered].sort((a, b) => (b.ctime ?? 0) - (a.ctime ?? 0));
  }, [listQuery.data, search]);

  /* 分页交给统一的分页 hook（原先在这一段手写页码 / 每页条数 + 手动切片）：
     它一并处理「筛选条件换了回第 1 页」与「删到最后一页时越界夹取」，
     并带出「每页 N 条」下拉与「跳至 _ 页」，与其它列表的分页条完全一致。
     切片对象是上面那份**已按备份时间倒序**的 items —— 先排序再分页，
     否则排的只是当前这一页。 */
  const pager = usePaged(items, `${search}|${node}|${storage}`);

  const stats = useMemo(() => {
    const list = listQuery.data ?? [];
    const totalSize = list.reduce((acc, b) => acc + (b.size ?? 0), 0);
    const vms = new Set(list.map((b) => b.vmid)).size;
    return { count: list.length, totalSize, vms };
  }, [listQuery.data]);

  /* 下载：新标签页会自动带上 HttpOnly Cookie，不再把令牌拼进 URL */
  const download = (item: BackupRow) => {
    // 归档在宿主机上的路径由「存储路径 + 归档名」拼出，两个字段缺一不可
    if (!item.node || !item.storage) {
      toast.error(
        t('backups.downloadFailed'),
        t('backups.downloadFailedHint'),
      );
      return;
    }
    window.open(
      backupsApi.downloadUrl({
        node: item.node,
        storage: item.storage,
        volid: item.volid,
        connectionId: item.connection_id,
      }),
      '_blank',
    );
  };

  const copyVolid = async (volid: string) => {
    try {
      await navigator.clipboard.writeText(volid);
      toast.success(t('backups.volidCopied'));
    } catch {
      toast.error(t('backups.copyFailed'), t('backups.copyFailedHint'));
    }
  };

  const columns: Array<Column<BackupRow>> = [
    {
      key: 'vmid',
      header: t('backups.colVm'),
      width: 110,
      render: (b) => (
        <div className="vm-name-cell">
          <span className="fw-500">
            {archiveGuestKind(b.volid, b.format) === 'lxc' ? 'CT' : 'VM'} {b.vmid}
          </span>
          <span className="fs-xs text-muted mono">{b.node ?? '—'}</span>
        </div>
      ),
      sortable: true,
      sortValue: (b) => b.vmid,
    },
    {
      key: 'volid',
      header: t('backups.colVolid'),
      render: (b) => (
        <div className="vm-name-cell">
          <span className="mono fs-sm truncate" title={b.volid}>
            {b.volid}
          </span>
          <span className="fs-xs text-muted truncate">
            {b.format || t('backups.unknownFormat')}
            {b.notes ? ` · ${b.notes}` : ''}
          </span>
        </div>
      ),
    },
    {
      key: 'ctime',
      header: t('vmDetail.snapColCreated'),
      width: 180,
      render: (b) => (
        <div className="vm-name-cell">
          <span className="mono fs-sm">{formatDateTime(b.ctime)}</span>
          <span className="fs-xs text-muted">{formatRelative(b.ctime)}</span>
        </div>
      ),
      sortable: true,
      sortValue: (b) => b.ctime ?? 0,
    },
    {
      key: 'size',
      header: t('vmDetail.backupColSize'),
      width: 110,
      align: 'right',
      render: (b) => <span className="mono fs-sm">{formatBytes(b.size)}</span>,
      sortable: true,
      sortValue: (b) => b.size ?? 0,
    },
    {
      key: 'owner',
      header: t('backups.colCreator'),
      width: 90,
      render: (b) => (
        <span className="fs-sm text-secondary">{b.owner || '—'}</span>
      ),
      sortable: true,
      sortValue: (b) => b.owner ?? '',
    },
    {
      key: 'actions',
      header: t('backups.colActions'),
      /* 四个图标 + 删除前的分隔（见 .row-actions-split），再加单元格左右各 14px
         内边距：170px 会把删除按钮挤出格子裁掉，这里留够实际占用 */
      width: 192,
      align: 'right',
      /* 操作列不参与拖拽：拖窄了就会把删除按钮裁掉 */
      resizable: false,
      render: (b) => (
        <span className="row-actions">
          <IconButton
            label={t('backups.restoreAria', { volid: b.volid })}
            variant="primary"
            disabled={!canBackup}
            onClick={(e) => {
              e.stopPropagation();
              setRestoreTarget(b);
            }}
          >
            <IconUpload size={15} />
          </IconButton>
          <IconButton
            label={t('backups.downloadAria', { volid: b.volid })}
            onClick={(e) => {
              e.stopPropagation();
              download(b);
            }}
          >
            <IconDownload size={15} />
          </IconButton>
          <IconButton
            label={t('backups.copyVolid')}
            onClick={(e) => {
              e.stopPropagation();
              void copyVolid(b.volid);
            }}
          >
            <IconCopy size={15} />
          </IconButton>
          {/* 删除与前面几个按钮之间加一道分隔：四个图标挨在一起时很容易点错 */}
          <span className="row-actions-split">
            <IconButton
              label={t('backups.deleteAria', { volid: b.volid })}
              variant="danger"
              disabled={!canBackup}
              onClick={(e) => {
                e.stopPropagation();
                setDeleteTarget(b);
              }}
            >
              <IconTrash size={15} />
            </IconButton>
          </span>
        </span>
      ),
    },
  ];

  if (listQuery.isError && isNotImplemented(listQuery.error)) {
    return (
      <ErrorState
        notImplemented
        title={t('backups.notImplTitle')}
        message={t('backups.notImplMessage')}
        onRetry={() => void listQuery.refetch()}
      />
    );
  }

  return (
    <>
      <div className="grid grid-4">
        <KpiCard
          label={t('backups.kpiFiles')}
          value={stats.count}
          icon={<IconBackup size={18} />}
          tone="accent"
          loading={listQuery.isLoading}
        />
        <KpiCard
          label={t('backups.kpiSpace')}
          value={formatBytes(stats.totalSize)}
          icon={<IconShield size={18} />}
          tone="neutral"
          loading={listQuery.isLoading}
        />
        <KpiCard
          label={t('backups.kpiVms')}
          value={stats.vms}
          icon={<IconCheck size={18} />}
          tone="success"
          loading={listQuery.isLoading}
        />
        <KpiCard
          label={t('backups.kpiSuccess')}
          value={
            backupStats?.success_rate === null ||
            backupStats?.success_rate === undefined
              ? '—'
              : `${backupStats.success_rate}%`
          }
          hint={
            backupStats
              ? t('backups.kpiRecent', {
                  days: backupStats.days,
                  ok: backupStats.ok,
                  failed: backupStats.failed,
                })
              : undefined
          }
          icon={<IconActivity size={18} />}
          tone={
            backupStats?.failed ? 'danger' : backupStats?.success_rate === 100 ? 'success' : 'neutral'
          }
          loading={statsQuery.isLoading}
          /* 有失败才可点：把「几天里失败了几次」变成「哪几次、哪台机器」 */
          onClick={
            backupStats?.failed
              ? () => setFailuresOpen(true)
              : undefined
          }
          title={backupStats?.failed ? t('backups.failuresOpen') : undefined}
        />
      </div>

      {/* 正在跑的备份不在这张列表里（归档要等任务结束才出现），单独给一条提示，
          否则用户会以为「点了没反应」 */}
      {backupStats?.running ? (
        <Notice tone="info" title={t('backups.runningTitle', { n: backupStats.running })}>
          {t('backups.runningBody')}{' '}
          <Link to="/tasks">{t('backups.goTaskQueue')}</Link>
        </Notice>
      ) : null}

      <div className="toolbar">
        <div className="toolbar-left">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('backups.searchPlaceholder')}
            prefix={<IconSearch size={15} />}
            block={false}
            aria-label={t('backups.searchAria')}
          />
          <Select
            value={node}
            onChange={(e) => {
              setNode(e.target.value);
              setStorage('');
            }}
            options={nodeOptions}
            aria-label={t('backups.filterNodeAria')}
          />
          <Select
            value={storage}
            onChange={(e) => setStorage(e.target.value)}
            options={storageOptions}
            aria-label={t('backups.filterStorageAria')}
          />
        </div>
        <div className="toolbar-right">
          <span className="fs-sm text-muted">
            {t('backups.totalItems', { count: items.length })}
          </span>
          <Button
            variant="secondary"
            size="sm"
            icon={<IconRefresh size={14} />}
            onClick={() => void listQuery.refetch()}
            loading={listQuery.isFetching && !listQuery.isLoading}
          >
            {t('common.refresh')}
          </Button>
        </div>
      </div>

      {listQuery.isError && !isNotImplemented(listQuery.error) ? (
        <PveQueryError
          title={t('backups.loadFailed')}
          error={listQuery.error}
          onRetry={() => void listQuery.refetch()}
        />
      ) : (
        <>
          <Table<BackupRow>
            columns={columns}
            rows={pager.rows}
            /* 卷标识在不同 PVE 上可能重名，行键必须带上来源连接 */
            rowKey={(b) => `${b.connection_id ?? ''}:${b.volid}`}
            loading={listQuery.isLoading}
            caption={t('backups.tableCaption')}
            emptyTitle={
              search ? t('backups.emptySearch') : t('backups.empty')
            }
            emptyDescription={
              search
                ? t('backups.emptySearchDesc')
                : t('backups.emptyDesc')
            }
            emptyAction={
              canBackup && !search ? (
                <Button
                  variant="primary"
                  icon={<IconPlus size={15} />}
                  onClick={onGoCreate}
                >
                  {t('backups.goCreate')}
                </Button>
              ) : undefined
            }
            rowTitle={(b) => b.volid}
          />
          {/* 分页条接在表格下沿，成为同一张卡的页脚（见 .pagination 的样式） */}
          <PagerBar pager={pager} />
        </>
      )}

      {/* ---- 近期失败明细 ---- */}
      <BackupFailuresDialog
        open={failuresOpen}
        onClose={() => setFailuresOpen(false)}
        stats={backupStats}
      />

      {/* ---- 恢复 ---- */}
      <RestoreDialog
        item={restoreTarget}
        onClose={() => setRestoreTarget(null)}
        onDone={() => {
          setRestoreTarget(null);
          void queryClient.invalidateQueries({ queryKey: ['backups'] });
          void queryClient.invalidateQueries({ queryKey: ['vms'] });
        }}
        runner={runner}
      />

      {/* ---- 删除 ---- */}
      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={async () => {
          if (!deleteTarget) return;
          const nodeName = deleteTarget.node ?? node;
          const storageName = deleteTarget.storage ?? storage;
          if (!nodeName || !storageName) {
            toast.error(t('backups.deleteFailed'), t('backups.deleteFailedHint'));
            return;
          }
          setBusy(true);
          try {
            // 走备份专用接口（vm.backup + 归属校验）。不能用 storages 的
            // 通用卷删除：那条路要求 storage.manage，普通用户角色会被 403。
            await runner.run(
              backupsApi.remove(
                {
                  node: nodeName,
                  storage: storageName,
                  volid: deleteTarget.volid,
                },
                // 归档来自哪台 PVE 就以它为准（列表跨主机合并，卷标识可能重名），
                // 退一步再按节点名定位连接
                deleteTarget.connection_id ?? connOfNode(nodeName),
              ),
              {
                title: t('backups.deleteTask', { volid: deleteTarget.volid }),
                node: nodeName,
                invalidate: [['backups', 'list']],
                destructive: true,
              },
            );
            setDeleteTarget(null);
          } catch {
            /* 已提示 */
          } finally {
            setBusy(false);
          }
        }}
        title={t('backups.deleteTitle')}
        danger
        confirmText={t('common.delete')}
        loading={busy}
        requireText={deleteTarget?.volid}
        message={
          <>
            {t('backups.deleteBodyPre')}
            <strong className="mono">{deleteTarget?.volid}</strong>（
            {formatBytes(deleteTarget?.size)}）
            {t('backups.deleteBodyTail')}
          </>
        }
      />
    </>
  );
}

/* ---------------------------------------------------------------------------
   Tab：备份计划
   --------------------------------------------------------------------------- */

function BackupJobsTab({
  canBackup,
  isAdmin,
}: {
  canBackup: boolean;
  isAdmin: boolean;
}) {
  const t = useT();
  const toast = useToast();
  const runner = useTaskRunner();
  const queryClient = useQueryClient();

  const [editorOpen, setEditorOpen] = useState(false);
  const [editTarget, setEditTarget] = useState<JobRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<JobRow | null>(null);
  const [busy, setBusy] = useState(false);

  /* 备份计划是**每台 PVE 各存一份**（standalone 时互不相通）。只查「面板当前
     连接」的话，给别的 PVE 建完计划在列表里根本看不见 —— 于是这里跨所有连接
     合并，每条计划带上来源连接，删除时才知道该发给哪一台。 */
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    staleTime: 60_000,
  });

  const jobsQuery = useQuery({
    queryKey: [
      'backups',
      'jobs',
      (connectionsQuery.data ?? []).map((c) => c.id).join(','),
    ],
    queryFn: async () => {
      const conns = connectionsQuery.data ?? [];
      const results = await Promise.all(
        conns.map(async (c) => {
          try {
            const jobs = await backupsApi.jobs(c.id);
            return jobs.map((j) => ({ ...j, connection_id: c.id }));
          } catch {
            // 单台主机读失败只跳过它自己，不影响其它主机的计划
            return [] as JobRow[];
          }
        }),
      );
      return results.flat();
    },
    enabled: connectionsQuery.isSuccess,
    staleTime: 30_000,
  });

  /* 计划本身没有「执行结果」字段，只能拿 vzdump 任务统计反查：这个计划覆盖的
     机器最近失败过没有。只报失败（成功与否看归档列表更直接），不编造「成功」。 */
  const statsQuery = useQuery({
    queryKey: ['backups', 'stats'],
    queryFn: () => backupsApi.stats(),
    staleTime: 60_000,
    retry: false,
  });

  const failureOf = (job: JobRow) =>
    (statsQuery.data?.failures ?? []).find((f) => {
      if (
        f.connection_id &&
        job.connection_id &&
        f.connection_id !== job.connection_id
      ) {
        return false;
      }
      /* 集群级计划（没有 node）：任何节点上的失败都算它的 */
      if (!job.node) {
        return !f.connection_id || f.connection_id === job.connection_id;
      }
      if (String(f.node ?? '') !== String(job.node ?? '')) return false;
      /* 整节点计划：该节点上任何一台失败都算它 */
      if (job.all) return true;
      return String(job.vmid ?? '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
        .includes(String(f.vmid ?? ''));
    });

  /* 计划存在哪套 PVE 上，比 PVE 内部的 job id 更有意义：多集群下同名节点
     的两条计划，光看 id 分不清是谁的 */
  const connLabel = (id?: string) => {
    const match = (connectionsQuery.data ?? []).find((c) => c.id === id);
    return match?.name || match?.host || id || '—';
  };

  const columns: Array<Column<JobRow>> = [
    {
      key: 'job_id',
      header: t('backups.colJob'),
      width: 160,
      render: (j) => (
        <div className="vm-name-cell">
          <span className="fs-sm">{connLabel(j.connection_id)}</span>
          <span className="mono fs-xs text-muted">#{j.job_id}</span>
        </div>
      ),
    },
    {
      key: 'schedule',
      header: t('backups.colSchedule'),
      render: (j) => (
        <div className="vm-name-cell">
          <span className="mono fs-sm">{j.schedule || '—'}</span>
          <span className="fs-xs text-muted">{describeSchedule(j.schedule)}</span>
        </div>
      ),
    },
    {
      key: 'scope',
      header: t('backups.colScope'),
      render: (j) => (
        <div className="vm-name-cell">
          <span className="fs-sm">
            {j.vmid ? `VMID: ${j.vmid}` : t('backups.scopeAllNode')}
          </span>
          <span className="fs-xs text-muted">
            {j.node
              ? t('backups.nodePrefix', { node: j.node })
              : `${t('backups.allNodes')} → `}
            {j.storage}
          </span>
        </div>
      ),
    },
    {
      key: 'mode',
      header: t('backups.colMode'),
      width: 120,
      render: (j) => (
        <Badge variant="neutral" size="sm">
          {j.mode} / {j.compress}
        </Badge>
      ),
    },
    {
      key: 'recent',
      header: t('backups.colRecentFailure'),
      width: 110,
      align: 'center',
      render: (j) => {
        const failure = failureOf(j);
        return failure ? (
          <Badge variant="danger" size="sm" dot>
            {t('backups.recentFailed')}
          </Badge>
        ) : (
          <span className="fs-sm text-muted">—</span>
        );
      },
    },
    {
      key: 'retention',
      header: t('backups.colRetention'),
      width: 170,
      render: (j) => {
        /* 表格里给人话（日 7 · 周 4 · 月 3），原始属性串放进 title 供对照 */
        const raw = pruneText(j.prune_backups);
        const prune =
          pruneSummary(j.prune_backups, t) ||
          [
            j.keep_daily ? t('backups.keepDaily', { n: j.keep_daily }) : '',
            j.keep_weekly ? t('backups.keepWeekly', { n: j.keep_weekly }) : '',
            j.keep_monthly ? t('backups.keepMonthly', { n: j.keep_monthly }) : '',
          ]
            .filter(Boolean)
            .join(' / ');
        return (
          <span className="fs-sm text-secondary truncate" title={raw || undefined}>
            {prune || '—'}
          </span>
        );
      },
    },
    {
      key: 'enabled',
      header: t('common.status'),
      width: 100,
      align: 'center',
      render: (j) => (
        <Badge variant={j.enabled ? 'success' : 'neutral'} dot size="sm">
          {j.enabled ? t('backups.jobEnabled') : t('backups.jobDisabled')}
        </Badge>
      ),
    },
    {
      key: 'next_run',
      header: t('backups.colNextRun'),
      width: 180,
      render: (j) => (
        <div className="vm-name-cell">
          <span className="fs-sm">
            {j.next_run ? formatDateTime(j.next_run) : '—'}
          </span>
          <span className="fs-xs text-muted">
            {j.last_run
              ? t('backups.lastRun', { time: formatRelative(j.last_run) })
              : t('backups.neverRun')}
          </span>
        </div>
      ),
    },
    {
      key: 'owner',
      header: t('backups.colCreator'),
      width: 90,
      render: (j) => (
        <span className="fs-sm text-secondary">{j.owner || '—'}</span>
      ),
      sortable: true,
      sortValue: (j) => j.owner ?? '',
    },
    {
      key: 'actions',
      header: t('common.actions'),
      /* 两个图标 + 删除前的分隔 + 单元格内边距：100px 会把删除按钮挤出格子裁掉 */
      width: 120,
      align: 'right',
      /* 操作列不参与拖拽：拖窄了就会把删除按钮裁掉 */
      resizable: false,
      render: (j) => (
        <span className="row-actions">
          <IconButton
            label={t('backups.editPlanAria', { id: j.job_id })}
            disabled={!canBackup}
            onClick={(e) => {
              e.stopPropagation();
              setEditTarget(j);
              setEditorOpen(true);
            }}
          >
            <IconClock size={15} />
          </IconButton>
          <span className="row-actions-split">
            <IconButton
              label={t('backups.deletePlanAria', { id: j.job_id })}
              variant="danger"
              disabled={!isAdmin}
              onClick={(e) => {
                e.stopPropagation();
                setDeleteTarget(j);
              }}
            >
              <IconTrash size={15} />
            </IconButton>
          </span>
        </span>
      ),
    },
  ];

  /* 备份计划同样分页：计划数随机器增多，一屏也铺不完。
     这一栏没有搜索 / 筛选，resetKey 给个固定值就够 —— 删掉计划导致页数变少时，
     越界仍由 hook 夹回最后一页。 */
  const jobPage = usePaged(jobsQuery.data ?? [], 'jobs');

  return (
    <>
      {!isAdmin ? (
        <Notice tone="info" title={t('backups.permTitle')}>
          {t('backups.permBody')}
        </Notice>
      ) : null}

      <div className="toolbar">
        <div className="toolbar-left">
          <span className="fs-sm text-secondary">
            {t('backups.jobsSummary', { n: jobsQuery.data?.length ?? 0 })}
          </span>
        </div>
        <div className="toolbar-right">
          <Button
            variant="secondary"
            size="sm"
            icon={<IconRefresh size={14} />}
            onClick={() => void jobsQuery.refetch()}
            loading={jobsQuery.isFetching && !jobsQuery.isLoading}
          >
            {t('common.refresh')}
          </Button>
          <Button
            variant="primary"
            icon={<IconPlus size={15} />}
            onClick={() => {
              setEditTarget(null);
              setEditorOpen(true);
            }}
            disabled={!canBackup}
          >
            {t('backups.newPlan')}
          </Button>
        </div>
      </div>

      {jobsQuery.isError && isNotImplemented(jobsQuery.error) ? (
        <ErrorState
          notImplemented
          title={t('backups.jobsNotImplTitle')}
          message={t('backups.jobsNotImplMsg')}
          onRetry={() => void jobsQuery.refetch()}
        />
      ) : jobsQuery.isError ? (
        <PveQueryError
          title={t('backups.jobsLoadFailed')}
          error={jobsQuery.error}
          onRetry={() => void jobsQuery.refetch()}
        />
      ) : (
        <>
          <Table<JobRow>
            columns={columns}
            rows={jobPage.rows}
            /* 任务号是各主机各自编的，两台 PVE 都可能有 job_id=1 —— 键必须带上连接 */
            rowKey={(j) => `${j.connection_id ?? ''}:${j.job_id}`}
            loading={jobsQuery.isLoading}
            caption={t('backups.jobsCaption')}
            emptyTitle={t('backups.jobsEmpty')}
            emptyDescription={t('backups.jobsEmptyDesc')}
            emptyAction={
              canBackup ? (
                <Button
                  variant="primary"
                  icon={<IconPlus size={15} />}
                  onClick={() => {
                    setEditTarget(null);
                    setEditorOpen(true);
                  }}
                >
                  {t('backups.newPlan')}
                </Button>
              ) : undefined
            }
          />
          {/* 分页条接在表格下沿，成为同一张卡的页脚（见 .pagination 的样式） */}
          <PagerBar pager={jobPage} />
        </>
      )}

      <JobEditor
        open={editorOpen}
        job={editTarget}
        onClose={() => setEditorOpen(false)}
        onDone={() => {
          setEditorOpen(false);
          void queryClient.invalidateQueries({ queryKey: ['backups', 'jobs'] });
        }}
        runner={runner}
      />

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={async () => {
          if (!deleteTarget) return;
          setBusy(true);
          try {
            await runner.run(
              backupsApi.deleteJob(deleteTarget.job_id, deleteTarget.connection_id),
              {
              title: t('backups.deleteJobTask', { id: deleteTarget.job_id }),
              node: deleteTarget.node ?? '',
              invalidate: [['backups', 'jobs']],
            });
            setDeleteTarget(null);
          } catch (err) {
            toast.error(t('backups.deleteJobFailed'), errorMessage(err));
          } finally {
            setBusy(false);
          }
        }}
        title={t('backups.deletePlanTitle')}
        danger
        confirmText={t('backups.deletePlanConfirm')}
        loading={busy}
        message={t('backups.deletePlanMessage', {
          id: deleteTarget?.job_id ?? '',
          schedule: deleteTarget?.schedule ?? '',
        })}
      />
    </>
  );
}

/* ---------------------------------------------------------------------------
   恢复对话框
   --------------------------------------------------------------------------- */

function RestoreDialog({
  item,
  onClose,
  onDone,
  runner,
}: {
  /* 需要 connection_id：归档只存在于它那台 PVE 上 */
  item: BackupRow | null;
  onClose: () => void;
  onDone: () => void;
  runner: TaskRunner;
}) {
  const t = useT();
  const toast = useToast();

  /* 先选集群再选节点：与计划编辑器同一套交互，避免把恢复打到别的主机上 */
  const [conn, setConn] = useState('');
  const [node, setNode] = useState('');
  const [vmid, setVmid] = useState('');
  const [storage, setStorage] = useState('');
  const [force, setForce] = useState(false);
  const [start, setStart] = useState(false);
  const [busy, setBusy] = useState(false);
  /** 恢复会覆盖目标 VMID —— 要求逐字输入一遍，和删除归档同一档门槛 */
  const [confirmVmid, setConfirmVmid] = useState('');

  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    enabled: Boolean(item),
    staleTime: 60_000,
  });

  /* 打开时用备份自带信息预填；集群跟着归档走 */
  useEffect(() => {
    if (!item) return;
    setConn(item.connection_id ?? '');
    setNode(item.node ?? '');
    setVmid(String(item.vmid ?? ''));
    setStorage(item.storage ?? '');
    setForce(false);
    setStart(false);
    setConfirmVmid('');
  }, [item]);

  /* 归档没带连接（老数据）或那条连接已被删时，回落到作用域 / 默认连接 */
  useEffect(() => {
    if (!item) return;
    const conns = connectionsQuery.data ?? [];
    if (!conns.length) return;
    if (conn && conns.some((c) => c.id === conn)) return;
    const target =
      conns.find((c) => c.id === getGlobalScope()) ??
      conns.find((c) => c.active) ??
      conns[0];
    setConn(target?.id ?? '');
  }, [item, conn, connectionsQuery.data]);

  const nodesQuery = useQuery({
    queryKey: ['nodes', conn],
    queryFn: () => nodesApi.list(conn),
    enabled: Boolean(item) && Boolean(conn),
    staleTime: 30_000,
  });

  const storagesQuery = useQuery({
    queryKey: ['storages', conn, node],
    queryFn: () => storagesApi.list(node || undefined, conn),
    enabled: Boolean(conn && node),
    staleTime: 30_000,
  });

  /* 目标 VMID 已被占用？PVE 会拒绝（除非 force）：提前说清楚，
     别让用户点了才读到英文报错。普通用户只拿得到自己名下的机器，
     别人的 VMID 仍由后端兜底拦截。 */
  const existingVmsQuery = useQuery({
    queryKey: ['vms', 'restore-check', conn, node],
    queryFn: () => vmsApi.list(node, { connectionId: conn, type: 'all' }),
    enabled: Boolean(item) && Boolean(conn && node),
    staleTime: 15_000,
  });

  const vmidNum = Number(vmid);
  /* 归档是虚拟机备份还是容器备份：既决定恢复出来的是什么，也决定撞号能否覆盖。
     PBS 归档（``pbs-ct``）的 volid 里没有 vzdump 前缀，必须带上 format 一起判。 */
  const archiveKind: 'qemu' | 'lxc' = archiveGuestKind(
    item?.volid ?? '',
    item?.format,
  );
  const archiveKindLabel =
    archiveKind === 'lxc' ? t('backups.guestKindCt') : t('backups.guestKindVm');
  const existingGuest = (existingVmsQuery.data ?? []).find(
    (v) => v.vmid === vmidNum,
  );
  const conflict = Boolean(existingGuest);
  /* PVE 里虚拟机和容器共用一套 VMID 编号：撞上**另一种**类型时 force 也覆盖不了
     （PVE 只会回英文的 "CT 103 already exists on node ..."），所以这一档连
     「强制覆盖」都不放行，只能换 VMID 或先删掉占用的机器。 */
  const crossTypeConflict = Boolean(
    existingGuest?.type && existingGuest.type !== archiveKind,
  );
  const conflictKindLabel = existingGuest?.type === 'lxc' ? 'CT' : 'VM';
  const crossTypeDetail = crossTypeConflict
    ? t('backups.restoreConflictCrossType', {
        kind: conflictKindLabel,
        vmid: String(vmidNum),
      })
    : undefined;
  /* 目标机器还开着：PVE 会先把盘拷一遍再拒绝（"vm is running"），留下
     "state is NOT cleaned up" 的半成品。同类型才叫「覆盖」，跨类型上一条已拦。 */
  const runningConflict = Boolean(
    existingGuest?.type === archiveKind && existingGuest.status === 'running',
  );
  const runningDetail = runningConflict
    ? t('backups.restoreConflictRunning', {
        kind: conflictKindLabel,
        vmid: String(vmidNum),
      })
    : undefined;
  const confirmOk =
    confirmVmid.trim().length > 0 && confirmVmid.trim() === vmid.trim();
  const canRestore =
    Boolean(conn) &&
    Boolean(node) &&
    Boolean(storage) &&
    Number.isFinite(vmidNum) &&
    vmidNum > 0 &&
    confirmOk &&
    !crossTypeConflict &&
    !runningConflict &&
    (!conflict || force);

  /* 恢复的**目标**存储和备份所在的存储是两回事：PBS / 纯备份存储只放归档，
     落不了 rootfs / 磁盘。容器要 rootdir，虚拟机要 images —— 把不能落地的
     选项直接滤掉，否则用户选中 PBS 存储后只会吃到 PVE 的 "storage does not
     support content type" 报错。 */
  const storageOptions = useMemo(() => {
    const all = storagesQuery.data ?? [];
    const needed = archiveKind === 'lxc' ? 'rootdir' : 'images';
    const capable = all.filter((s) =>
      (s.content || '')
        .split(',')
        .map((c) => c.trim())
        .includes(needed),
    );
    /* content 字段缺失时（精简响应）不过滤，免得把选项清空 */
    return (capable.length ? capable : all).map((s) => ({
      label: s.storage,
      value: s.storage,
    }));
  }, [storagesQuery.data, archiveKind]);

  /* 预选值来自「备份所在的存储」，不一定能当恢复目标 —— 不合规时换成第一个
     可落地的存储。 */
  useEffect(() => {
    if (!item) return;
    const values = storageOptions.map((o) => o.value);
    if (!values.length || values.includes(storage)) return;
    setStorage(values[0]);
  }, [item, storage, storageOptions]);

  const nodeOptions = useMemo(
    () =>
      (nodesQuery.data ?? [])
        .filter((n) => !conn || !n.connection_id || n.connection_id === conn)
        .map((n) => ({ label: n.node, value: n.node })),
    [nodesQuery.data, conn],
  );

  const submit = async () => {
    if (!item) return;
    const targetVmid = Number(vmid);
    if (!node || !storage || !Number.isFinite(targetVmid) || targetVmid <= 0) {
      toast.error(t('common.opFailed'), t('backups.restoreMissing'));
      return;
    }
    if (crossTypeConflict) {
      toast.error(t('common.opFailed'), crossTypeDetail);
      return;
    }
    if (runningConflict) {
      toast.error(t('common.opFailed'), runningDetail);
      return;
    }
    if (conflict && !force) {
      toast.error(t('common.opFailed'), t('backups.restoreConflictNoForce'));
      return;
    }
    if (!confirmOk) {
      toast.error(t('common.opFailed'), t('backups.restoreConfirmHint'));
      return;
    }
    setBusy(true);
    try {
      const body: BackupRestoreRequest = {
        node,
        storage,
        volid: item.volid,
        vmid: targetVmid,
        /* 显式下发：早前写成 `force || undefined`，后端默认值是 True，
           于是「强制覆盖」关着也照样 force=1 —— 开关形同虚设。 */
        force,
        /* 带上 format：PBS 归档的 volid 里没有 vzdump 前缀，后端靠它兜底判类型 */
        format: item.format || undefined,
        start: start || undefined,
      };
      await runner.run(backupsApi.restore(body, conn), {
        title: t('backups.restoreTask', { vmid: targetVmid }),
        node,
        invalidate: [['backups'], ['vms'], ['tasks']],
      });
      onDone();
    } catch {
      /* 已提示 */
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={Boolean(item)}
      onClose={onClose}
      title={t('backups.restoreTitle')}
      description={t('backups.restoreDesc', { kind: archiveKindLabel })}
      size="md"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="danger"
            onClick={() => void submit()}
            loading={busy}
            disabled={busy || !canRestore}
          >
            {t('backups.restoreConfirm')}
          </Button>
        </>
      }
    >
      <Notice tone="danger" title={t('backups.restoreWarnTitle')}>
        {t('backups.restoreWarnBody')}
      </Notice>

      <div className="dyn-list mt-16">
        <Field label={t('backups.fieldBackupFile')}>
          <div className="mono fs-sm truncate" title={item?.volid}>
            {item?.volid}
          </div>
        </Field>

        <div className="dyn-row">
          <Field label={t('backups.fieldCluster')} required>
            <Select
              value={conn}
              onChange={(e) => {
                setConn(e.target.value);
                setNode('');
                setStorage('');
              }}
              options={(connectionsQuery.data ?? []).map((c) => ({
                label: c.name || c.host || c.id,
                value: c.id,
              }))}
              placeholder={t('backups.clusterPlaceholder')}
            />
          </Field>

          <Field label={t('backups.fieldNode')} required>
            <Select
              value={node}
              onChange={(e) => {
                setNode(e.target.value);
                setStorage('');
              }}
              options={nodeOptions}
              placeholder={t('backups.nodePlaceholder')}
              disabled={!conn}
            />
          </Field>

          <Field label={t('backups.fieldTargetStorage')} required>
            <Select
              value={storage}
              onChange={(e) => setStorage(e.target.value)}
              options={storageOptions}
              placeholder={t('backups.storagePlaceholder')}
            />
          </Field>
        </div>

        <Field
          label={t('backups.fieldTargetVmid')}
          required
          error={
            crossTypeDetail ??
            runningDetail ??
            (conflict && !force ? t('backups.restoreConflict') : undefined)
          }
          hint={t('backups.targetVmidHint')}
        >
          <Input
            type="number"
            min={1}
            value={vmid}
            onChange={(e) => {
              setVmid(e.target.value);
              /* 换了目标 ID，之前输入过的确认文本就失效了 */
              setConfirmVmid('');
            }}
            className="mono"
          />
        </Field>

        <Switch
          checked={force}
          onChange={setForce}
          label={t('backups.forceOverwrite')}
          hint={
            crossTypeConflict
              ? t('backups.forceOverwriteCrossType')
              : t('backups.forceOverwriteHint')
          }
          /* 撞上另一种类型时 force 在 PVE 侧无效，别让用户以为打开就能过 */
          disabled={crossTypeConflict}
        />

        <Switch
          checked={start}
          onChange={setStart}
          label={t('backups.startAfterRestore')}
          hint={t('backups.startAfterRestoreHint')}
        />

        {/* 破坏性确认：与删除归档同一档门槛（删除要输 volid，恢复要输目标 VMID） */}
        <Field
          label={t('backups.restoreConfirmLabel', { vmid })}
          required
          hint={t('backups.restoreConfirmHint')}
        >
          <Input
            value={confirmVmid}
            onChange={(e) => setConfirmVmid(e.target.value)}
            placeholder={vmid}
            className="mono"
          />
        </Field>
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   近期失败明细（成功率 KPI 点开）
   --------------------------------------------------------------------------- */

function BackupFailuresDialog({
  open,
  onClose,
  stats,
}: {
  open: boolean;
  onClose: () => void;
  stats?: BackupStats;
}) {
  const t = useT();
  const failures = stats?.failures ?? [];
  const days = stats?.days ?? 0;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('backups.failuresTitle')}
      description={t('backups.failuresDesc', { days })}
      size="md"
    >
      {failures.length === 0 ? (
        <Notice tone="success" title={t('backups.failuresEmpty')}>
          {t('backups.failuresEmptyBody')}
        </Notice>
      ) : (
        <div className="desc-list">
          {failures.map((f) => (
            <div className="desc-item" key={`${f.connection_id}:${f.upid}`}>
              <div className="desc-label">
                <Badge variant="danger" size="sm" dot>
                  {t('backups.recentFailed')}
                </Badge>
              </div>
              <div className="desc-value">
                <span className="mono fs-sm">
                  {f.node} · VM {f.vmid ?? '—'}
                </span>
                <span className="fs-xs text-muted">
                  {' · '}
                  {formatDateTime(f.endtime)} · {f.status}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-16">
        <Link to="/tasks" onClick={onClose}>
          {t('backups.goTaskQueue')}
        </Link>
      </div>
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   备份计划编辑器
   --------------------------------------------------------------------------- */

const EMPTY_JOB: BackupJobInput = {
  schedule: '02:00',
  storage: '',
  mode: 'snapshot',
  compress: 'zstd',
  vmid: '',
  /* 默认「指定虚拟机」：整节点备份是更大范围的动作，不该是默认选项 */
  all: false,
  node: '',
  enabled: true,
  notes: '',
  prune_backups: 'keep-daily=7,keep-weekly=4,keep-monthly=3',
};



function JobEditor({
  open,
  job,
  onClose,
  onDone,
  runner,
}: {
  open: boolean;
  /* 编辑时带上 connection_id：计划属于某一套 PVE，默认集群要落在它原来那套上 */
  job: JobRow | null;
  onClose: () => void;
  onDone: () => void;
  runner: TaskRunner;
}) {
  const t = useT();
  const toast = useToast();

  const [form, setForm] = useState<BackupJobInput>(EMPTY_JOB);
  const [busy, setBusy] = useState(false);
  const [startNow, setStartNow] = useState(false);
  /* 计划落在哪套 PVE 上（= 一条连接）。节点名在每台 PVE 上都是本地的，先选集群
     再选节点，才不会拿 A 集群的节点名去 B 集群建计划 —— 那种请求会被 PVE 回一句
     `hostname lookup failed`，看着像 DNS 坏了，其实是打错了机器。 */
  const [conn, setConn] = useState('');

  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    enabled: open,
    staleTime: 60_000,
  });

  /* 打开时先落到计划原来那套集群；新建时留空，交给下面的 effect 挑默认值 */
  useEffect(() => {
    if (!open) return;
    setConn(job?.connection_id ?? '');
  }, [open, job]);

  /* 默认集群：优先顶栏「集群作用域」选中的那一套（刚把作用域切到 B 却默认在 A，
     多半不是用户想要的），再回落到面板的当前连接。 */
  useEffect(() => {
    if (!open) return;
    const conns = connectionsQuery.data ?? [];
    if (!conns.length) return;
    if (conn && conns.some((c) => c.id === conn)) return;
    const target =
      conns.find((c) => c.id === getGlobalScope()) ??
      conns.find((c) => c.active) ??
      conns[0];
    setConn(target?.id ?? '');
  }, [open, conn, connectionsQuery.data]);

  useEffect(() => {
    if (!open) return;
    if (job) {
      setForm({
        schedule: job.schedule ?? '02:00',
        storage: job.storage ?? '',
        mode: job.mode ?? 'snapshot',
        compress: job.compress ?? 'zstd',
        vmid: job.vmid ?? '',
        all: job.all ?? false,
        /* 计划里没有 node = 集群所有节点 */
        node: job.node || ALL_NODES,
        enabled: job.enabled ?? true,
        notes: job.notes ?? job.comment ?? '',
        prune_backups: pruneText(job.prune_backups),
      });
    } else {
      setForm(EMPTY_JOB);
    }
    setStartNow(false);
  }, [open, job]);

  /* 节点列表只取所选集群下的节点：混进别的主机的节点名，选中后计划就会被写到
     错的那套 PVE 上。 */
  const nodesQuery = useQuery({
    queryKey: ['nodes', conn],
    queryFn: () => nodesApi.list(conn),
    enabled: open && Boolean(conn),
    staleTime: 30_000,
  });

  const nodeNames = useMemo(
    () =>
      (nodesQuery.data ?? [])
        /* 兜一道连接过滤：缓存里的旧响应可能来自「未指定连接 → 合并全部集群」
           的那一版接口，直接信它会又混进别的主机的节点 */
        .filter((n) => !conn || !n.connection_id || n.connection_id === conn)
        .map((n) => n.node),
    [nodesQuery.data, conn],
  );

  /* 新建计划要先落在一个具体节点上；「所有节点」必须由用户显式选，
     否则默认范围会悄悄放大到整个集群 */
  useEffect(() => {
    if (!open || job || form.node) return;
    const first = nodeNames[0];
    if (first) setForm((prev) => (prev.node ? prev : { ...prev, node: first }));
  }, [open, job, form.node, nodeNames]);

  /* 存储：选「所有节点」时只列每个节点都有的那些（见 useTargetStorages） */
  const storageOptions = useTargetStorages(conn, form.node ?? '', nodeNames, open);

  const nodeOptions = useMemo(
    () => [
      { label: t('backups.allNodes'), value: ALL_NODES },
      ...nodeNames.map((name) => ({ label: name, value: name })),
    ],
    [nodeNames, t],
  );

  const connectionOptions = useMemo(
    () =>
      (connectionsQuery.data ?? []).map((c) => ({
        /* 与创建向导同一口径：普通用户拿到的是精简连接（没有地址），
           标签退回到名称 / id 即可 */
        label: `${c.name || c.host || c.id}${
          c.active ? t('vmCreate.connCurrentTag') : ''
        }`,
        value: c.id,
      })),
    [connectionsQuery.data, t],
  );

  /* 换集群 = 换一套 PVE：节点名与存储池都不通用，必须一起清掉 */
  const chooseConn = (next: string) => {
    if (next === conn) return;
    setConn(next);
    setForm((prev) => ({ ...prev, node: '', storage: '' }));
  };

  const patch = <K extends keyof BackupJobInput>(
    key: K,
    value: BackupJobInput[K],
  ) => setForm((prev) => ({ ...prev, [key]: value }));

  /** 备份范围：全节点，或至少填一个 VMID —— 两者都空时后端会直接 400 */
  const scopeOk = Boolean(form.all) || Boolean(form.vmid?.trim());
  const scheduleOk = isScheduleValid(form.schedule);
  const vmids = (form.vmid ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const valid =
    Boolean(conn) &&
    scheduleOk &&
    Boolean(form.storage) &&
    Boolean(form.node) &&
    scopeOk;

  const submit = async () => {
    if (!valid) {
      toast.error(t('common.opFailed'), t('backups.jobMissing'));
      return;
    }
    setBusy(true);
    try {
      /* 「所有节点」= 不向 PVE 发 node（它在集群里每个节点各跑一遍） */
      const nodeValue = form.node === ALL_NODES ? undefined : form.node;
      const pruneValue = form.prune_backups?.trim() || undefined;
      const removals = [
        ...(form.all && job?.vmid ? ['vmid'] : []),
        /* 从「某个节点」改成「所有节点」：PVE 的更新是合并语义，不删掉旧的
           node 就会继续只在原来那台上跑 */
        ...(nodeValue === undefined && job?.node ? ['node'] : []),
        /* 清空保留策略同理：不发 prune-backups 只是「保持原样」，
           想清掉必须显式删 */
        ...(!pruneValue && job && pruneText(job.prune_backups)
          ? ['prune-backups']
          : []),
      ];
      const body: BackupJobInput = {
        ...form,
        node: nodeValue,
        /* all 与 vmid 互斥：整节点计划不能带上残留的 VMID 列表 */
        vmid: form.all ? undefined : form.vmid?.trim() || undefined,
        notes: form.notes?.trim() || undefined,
        prune_backups: pruneValue,
      };
      await runner.run(
        job
          ? /* 编辑必须走 PUT：用 POST 会另建一条同名计划（看起来「改了没生效」） */
            backupsApi.updateJob(
              job.job_id,
              {
                ...body,
                delete: removals.length ? removals : undefined,
              },
              conn,
            )
          : backupsApi.createJob(body, conn),
        {
          title: job ? t('backups.jobUpdateTask') : t('backups.jobCreateTask'),
          node: nodeValue,
          invalidate: [['backups', 'jobs']],
        },
      );
      if (startNow) {
        const singleVmid =
          !form.all && vmids.length === 1 && Number.isFinite(Number(vmids[0]))
            ? Number(vmids[0])
            : undefined;
        if (!nodeValue) {
          /* 立即备份必须落在某一台上：整集群的计划只能等调度执行 */
          toast.info(
            t('backups.startNowSkipped'),
            t('backups.startNowNeedsNode'),
          );
        } else if (!form.all && singleVmid === undefined) {
          /* 立即备份接口只接受单台或整节点：多 VMID 计划不做「猜一台」，
             宁可跳过并说清楚，也不偷偷扩大/缩小备份范围 */
          toast.info(
            t('backups.startNowSkipped'),
            t('backups.startNowSkippedHint'),
          );
        } else {
          await runner.run(
            backupsApi.create(
              {
                node: nodeValue,
                vmid: singleVmid,
                storage: form.storage,
                mode: form.mode,
                compress: form.compress,
                notes: form.notes?.trim() || undefined,
                all: form.all || undefined,
              },
              conn,
            ),
            {
              title: t('backups.jobRunNowTask'),
              node: nodeValue,
              invalidate: [['backups', 'list']],
            },
          );
        }
      }
      onDone();
    } catch {
      /* 已提示 */
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={
        job ? t('backups.jobEditTitle', { id: job.job_id }) : t('backups.jobNewTitle')
      }
      description={t('backups.jobDesc')}
      size="lg"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            loading={busy}
            disabled={!valid}
          >
            {job ? t('backups.saveChanges') : t('backups.createPlan')}
          </Button>
        </>
      }
    >
      <div className="dyn-list">
        <div className="dyn-row">
          <Field
            label={t('backups.fieldCluster')}
            required
            hint={
              job
                ? t('backups.fieldClusterEditHint')
                : t('backups.fieldClusterHint')
            }
          >
            <Select
              value={conn}
              onChange={(e) => chooseConn(e.target.value)}
              options={connectionOptions}
              placeholder={t('backups.clusterPlaceholder')}
              /* 计划只存在于它所在的那套 PVE 上：改到另一套等于把 PUT 发给
                 一台没有这个 job_id 的机器，只会回「计划不存在」 */
              disabled={Boolean(job)}
            />
          </Field>

          <Field
            label={t('backups.fieldNode')}
            required
            hint={
              form.node === ALL_NODES
                ? t('backups.allNodesHint')
                : t('backups.nodeFieldHint')
            }
          >
            <Select
              value={form.node ?? ''}
              onChange={(e) => {
                const next = e.target.value;
                setForm((prev) => ({ ...prev, node: next, storage: '' }));
                /* 立即执行需要一个具体节点：整集群计划没法「马上跑一次」 */
                if (next === ALL_NODES) setStartNow(false);
              }}
              options={nodeOptions}
              placeholder={t('backups.nodePlaceholder')}
              /* 先选集群：节点名只在所属 PVE 内有意义 */
              disabled={!conn}
            />
          </Field>

          <Field
            label={t('backups.colSchedule')}
            required
            /* 填错不要等 PVE 用英文报错：就地指出格式 */
            error={scheduleOk ? undefined : t('backups.scheduleInvalid')}
            hint={t('backups.scheduleHint')}
          >
            <Input
              value={form.schedule}
              onChange={(e) => patch('schedule', e.target.value)}
              placeholder="02:00"
              className="mono"
            />
          </Field>
        </div>

        <div className="dyn-row">
          <Field
            label={t('backups.fieldStorage')}
            required
            hint={
              form.node === ALL_NODES
                ? t('backups.storageAllNodesHint')
                : undefined
            }
          >
            <Select
              value={form.storage}
              onChange={(e) => patch('storage', e.target.value)}
              options={storageOptions}
              placeholder={t('backups.storagePlaceholder')}
            />
          </Field>

          <Field
            label={t('backups.fieldMode')}
            required
            hint={
              form.mode === 'stop'
                ? t('backups.modeStopWarn')
                : form.mode === 'suspend'
                  ? t('backups.modeSuspendWarn')
                  : t('backups.modeSnapshotHint')
            }
          >
            <Select
              value={form.mode}
              onChange={(e) => patch('mode', e.target.value as BackupMode)}
              options={backupModeOptions(t).map((o) => ({
                label: o.label,
                value: o.value,
              }))}
            />
          </Field>

          <Field label={t('backups.fieldCompress')} required>
            <Select
              value={form.compress}
              onChange={(e) => patch('compress', e.target.value)}
              options={compressOptions(t).map((o) => ({
                label: o.label,
                value: o.value,
              }))}
            />
          </Field>
        </div>

        {/* 备份范围：整节点（PVE 的 all=1）与指定 VMID 互斥。
            缺了这一项，用户只能靠「留空」表达整节点，而后端会直接 400。 */}
        <Field label={t('backups.fieldScope')} required>
          <div
            className="radio-group"
            role="radiogroup"
            aria-label={t('backups.fieldScope')}
          >
            <label className="radio-field">
              <input
                type="radio"
                name="plan-scope"
                className="radio-input"
                checked={!form.all}
                onChange={() =>
                  setForm((prev) => ({ ...prev, all: false }))
                }
              />
              <span className="radio-dot" aria-hidden="true" />
              <span className="radio-content">
                <span className="radio-label">{t('backups.planScopeOne')}</span>
                <span className="radio-hint">{t('backups.planScopeOneHint')}</span>
              </span>
            </label>
            <label className="radio-field">
              <input
                type="radio"
                name="plan-scope"
                className="radio-input"
                checked={Boolean(form.all)}
                onChange={() =>
                  /* 切到整节点：vmid 与 all 互斥，留着只会让 PVE 那边两套配置打架 */
                  setForm((prev) => ({ ...prev, all: true, vmid: '' }))
                }
              />
              <span className="radio-dot" aria-hidden="true" />
              <span className="radio-content">
                <span className="radio-label">{t('backups.planScopeAll')}</span>
                <span className="radio-hint">{t('backups.planScopeAllHint')}</span>
              </span>
            </label>
          </div>
        </Field>

        {form.all ? null : (
          <Field label={t('backups.vmidsLabel')} required hint={t('backups.vmidsHint')}>
            <Input
              value={form.vmid ?? ''}
              onChange={(e) => patch('vmid', e.target.value)}
              placeholder="100,101,102"
              className="mono"
            />
          </Field>
        )}

        <Field
          label={t('backups.colRetention')}
          hint={
            pruneSummary(form.prune_backups, t)
              ? t('backups.retentionPreview', {
                  text: pruneSummary(form.prune_backups, t),
                })
              : t('backups.retentionHint')
          }
        >
          <Input
            value={form.prune_backups ?? ''}
            onChange={(e) => patch('prune_backups', e.target.value)}
            placeholder="keep-daily=7,keep-weekly=4,keep-monthly=3"
            className="mono"
          />
        </Field>

        <Field label={t('backups.fieldNotes')}>
          <Textarea
            value={form.notes ?? ''}
            onChange={(e) => patch('notes', e.target.value)}
            rows={2}
          />
        </Field>

        <Switch
          checked={form.enabled ?? true}
          onChange={(v) => patch('enabled', v)}
          label={t('backups.enablePlan')}
          hint={t('backups.enablePlanHint')}
        />

        <Switch
          checked={startNow}
          onChange={setStartNow}
          label={t('backups.startNow')}
          hint={
            form.node === ALL_NODES
              ? t('backups.startNowNeedsNode')
              : t('backups.startNowHint')
          }
          disabled={form.node === ALL_NODES}
        />
      </div>

      <Notice tone="info" title={t('backups.retentionExample')}>
        <div className="desc-list">
          <div className="desc-item">
            <div className="desc-label mono">keep-daily=7</div>
            <div className="desc-value">{t('backups.keepDailyValue')}</div>
          </div>
          <div className="desc-item">
            <div className="desc-label mono">keep-weekly=4</div>
            <div className="desc-value">{t('backups.keepWeeklyValue')}</div>
          </div>
          <div className="desc-item">
            <div className="desc-label mono">keep-last=3</div>
            <div className="desc-value">{t('backups.keepLastValue')}</div>
          </div>
        </div>
      </Notice>
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   调度描述（把 Proxmox 的 schedule 字符串转成中文说明）
   --------------------------------------------------------------------------- */

function describeSchedule(schedule?: string | null): string {
  if (!schedule) return tStatic('backups.schedNone');
  const raw = schedule.trim();

  if (/^\d{1,2}:\d{2}$/.test(raw)) return tStatic('backups.schedDaily', { time: raw });

  const m = /^([a-z,*-]+)\s+(\d{1,2}:\d{2})$/.exec(raw.toLowerCase());
  if (m) {
    const dayPart = m[1];
    const time = m[2];
    if (dayPart === '*') return tStatic('backups.schedDaily', { time });
    const days = dayPart
      .split(',')
      .map((d) => (WEEKDAY_KEYS[d] ? tStatic(WEEKDAY_KEYS[d]) : d))
      .filter(Boolean);
    return tStatic('backups.schedWeekdays', { days: days.join('、') });
  }

  return raw;
}
