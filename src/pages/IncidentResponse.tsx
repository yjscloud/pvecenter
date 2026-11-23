/* ==========================================================================
   pvecenter — 应急响应

   两块内容，都是「出事时用的」：

   1. **可疑虚拟机隔离**：一键完成「取证快照 → 断网 → 关机 → 加保护」，以及解除
      隔离。首页大字写明**处置能力的边界**（快照不是内存取证、link_down 不覆盖
      PCI 直通网卡、回滚快照会覆盖现场），不做任何超出实际能力的承诺。
   2. **备份防护（防删）**：把关键备份登记为受保护 —— 面板层禁止删除 + 定期核对
      是否被删除 / 改动。同时如实说明：真正的不可变（WORM）要靠 PBS immutability
      或 S3 Object Lock，只靠 PVE API 做不到。
   ========================================================================== */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useSearchParams } from 'react-router';
import {
  backupsApi,
  isolationApi,
  nodesApi,
  protectedBackupsApi,
  storagesApi,
  vmsApi,
  type QuarantineInput,
} from '../api/endpoints';
import { errorMessage } from '../api/client';
import { PageShell } from '../components/Layout';
import { Card, CardHeader, KpiCard } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Field, Input, Select, Switch, Textarea } from '../components/ui/Input';
import { Table, type Column } from '../components/ui/Table';
import { ConfirmDialog } from '../components/ui/ConfirmDialog';
import {
  CollapsibleCard,
  EmptyState,
  ErrorState,
  Notice,
} from '../components/ui/EmptyState';
import {
  IconAlert,
  IconCheck,
  IconInfo,
  IconLock,
  IconRefresh,
  IconShield,
  IconStorage,
} from '../components/Icons';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../hooks/useToast';
import { useT, type MessageKey } from '../i18n';
import { formatBytes, formatDateTime } from '../utils/format';
import type {
  ProtectedBackup,
  ProtectedBackupState,
  QuarantineRecord,
  QuarantineResult,
} from '../api/types';

const STATE_META: Record<
  ProtectedBackupState,
  { label: MessageKey; variant: 'success' | 'danger' | 'warning' | 'neutral' }
> = {
  ok: { label: 'incident.state.ok', variant: 'success' },
  missing: { label: 'incident.state.missing', variant: 'danger' },
  changed: { label: 'incident.state.changed', variant: 'warning' },
  unknown: { label: 'incident.state.unknown', variant: 'neutral' },
};

/** 处置步骤的文案键 */
const STEP_LABEL: Record<string, MessageKey> = {
  snapshot: 'incident.step.snapshot',
  network: 'incident.step.network',
  power: 'incident.step.power',
  protect: 'incident.step.protect',
};

function StepList({ result }: { result: QuarantineResult }) {
  const t = useT();
  return (
    <div className="baseline-todo">
      {result.steps.map((step) => (
        <div
          key={step.step}
          className={`baseline-item is-${step.ok ? 'pass' : 'fail'}`}
        >
          <span className="baseline-item-icon" aria-hidden="true">
            {step.ok ? <IconCheck size={15} /> : <IconAlert size={15} />}
          </span>
          <div className="baseline-item-body">
            <div className="baseline-item-head">
              <span className="baseline-item-title">
                {STEP_LABEL[step.step] ? t(STEP_LABEL[step.step]) : step.step}
              </span>
              <Badge variant={step.ok ? 'success' : 'danger'} size="sm">
                {step.ok ? t('status.audit.success') : t('status.audit.failed')}
              </Badge>
            </div>
            <div className="fs-xs text-muted">{step.detail}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function IncidentResponse() {
  const t = useT();
  const { hasPermission } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const canBackup = hasPermission('vm.backup');
  const canIsolate = hasPermission('vm.isolate');

  /* ---- 隔离表单 ---- */
  const [target, setTarget] = useState('');
  const [snapshot, setSnapshot] = useState(true);
  const [cutNetwork, setCutNetwork] = useState(true);
  const [powerAction, setPowerAction] = useState<'none' | 'shutdown' | 'stop'>('shutdown');
  const [protect, setProtect] = useState(true);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [lastResult, setLastResult] = useState<QuarantineResult | null>(null);
  const [powerOnConfirm, setPowerOnConfirm] = useState(false);

  /* ---- 备份防护表单 ---- */
  const [volid, setVolid] = useState('');
  const [bNode, setBNode] = useState('');
  const [bStorage, setBStorage] = useState('');
  const [bNote, setBNote] = useState('');
  /* 整条记录（含 connection）：同一个 volid 可能在两套集群各有一条 */
  const [unprotectTarget, setUnprotectTarget] = useState<ProtectedBackup | null>(null);

  const vmsQuery = useQuery({
    queryKey: ['incident', 'vms'],
    queryFn: () => vmsApi.list(),
    staleTime: 120_000,
    retry: false,
  });
  const protectedQuery = useQuery({
    queryKey: ['incident', 'protected'],
    queryFn: () => protectedBackupsApi.list(false),
    retry: false,
  });

  /* ---- 受保护备份表单的选项来源：节点 → 该节点的备份存储 → 该存储下的归档 ----

     逐级 enabled：上一级没选就不发请求。节点作用域接口必须带 connection_id ——
     /api/nodes 是跨 PVE 聚合的，只拿节点名去问「当前连接」，名字属于别的主机时
     存储列表就是空的（PVE 回 hostname lookup failed）。 */
  const nodesQuery = useQuery({
    queryKey: ['incident', 'nodes'],
    queryFn: () => nodesApi.list(),
    staleTime: 60_000,
    retry: false,
  });

  const bConn = useMemo(
    () => (nodesQuery.data ?? []).find((n) => n.node === bNode)?.connection_id,
    [nodesQuery.data, bNode],
  );

  const bStoragesQuery = useQuery({
    queryKey: ['incident', 'storages', bConn ?? '', bNode],
    queryFn: () => storagesApi.list(bNode, bConn),
    enabled: Boolean(bNode),
    staleTime: 30_000,
    retry: false,
  });

  const bArchivesQuery = useQuery({
    queryKey: ['incident', 'archives', bConn ?? '', bNode, bStorage],
    queryFn: () => backupsApi.list({ node: bNode, storage: bStorage }, bConn),
    enabled: Boolean(bNode && bStorage),
    staleTime: 30_000,
    retry: false,
  });

  const [searchParams, setSearchParams] = useSearchParams();
  /* 从虚拟机列表跳过来时可能带 ``?connection=``：列表还没回来、或旧链接里没有
     连接信息时，用它兜住处置目标的连接。 */
  const connectionParam = searchParams.get('connection') ?? '';

  const parsed = useMemo(() => {
    const [node, vmidText] = target.split('/');
    const vmid = Number(vmidText);
    return node && Number.isFinite(vmid) && vmid > 0 ? { node, vmid } : null;
  }, [target]);

  /* 处置目标真正属于哪条 PVE 连接：虚拟机列表的每一行都带 connection_id。
     隔离状态 / 隔离 / 解除一律**硬指定**它 —— 只靠顶栏作用域的话，在另一套
     集群上处置会打到「当前连接」，那台 PVE 把节点名当主机名解析，回
     hostname lookup 失败。 */
  const targetConn = useMemo(() => {
    if (!parsed) return connectionParam;
    const hit = (vmsQuery.data ?? []).find(
      (vm) => vm.node === parsed.node && vm.vmid === parsed.vmid,
    );
    return hit?.connection_id ?? connectionParam;
  }, [parsed, vmsQuery.data, connectionParam]);

  const statusQuery = useQuery({
    /* 连接进 key：切换顶栏作用域 / 换机器时不能命中上一次的读数缓存 */
    queryKey: ['incident', 'quarantine', targetConn, target],
    queryFn: () => isolationApi.status(parsed!.node, parsed!.vmid, targetConn),
    enabled: Boolean(parsed),
    retry: false,
  });

  /* 面板登记过的隔离清单。页面不再只依赖「上一次操作返回的对象」：
     刷新之后仍然能看到现在有哪些机器被隔离、谁在什么时候做的。 */
  const isolationQuery = useQuery({
    queryKey: ['incident', 'isolation-list'],
    queryFn: () => isolationApi.list(),
    enabled: hasPermission('vm.view'),
    staleTime: 15_000,
    retry: false,
  });
  const isolationItems = isolationQuery.data?.items ?? [];

  /* 从虚拟机列表的「请先解除隔离」跳过来时带 ``?target=节点/VMID``：
     直接选中那台机器，不用在几十台的下拉里再找一遍。 */
  useEffect(() => {
    const wanted = searchParams.get('target');
    if (!wanted || target) return;
    if (!/^[^/]+\/\d+$/.test(wanted)) return;
    setTarget(wanted);
    const next = new URLSearchParams(searchParams);
    next.delete('target');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams, target]);

  const vmOptions = useMemo(
    () =>
      /* 模板不是可处置对象：混在清单里既选不中正确的目标，也容易误把模板当
         机器隔离。这里直接不列出来（与「快照 / 告警」的选择器同一口径）。 */
      (vmsQuery.data ?? [])
        .filter((vm) => !vm.template)
        .map((vm) => ({
          label: `${vm.name || `VM ${vm.vmid}`}（${vm.vmid} · ${vm.node} · ${vm.status}）`,
          value: `${vm.node}/${vm.vmid}`,
        })),
    [vmsQuery.data],
  );

  /* 隔离清单里的节点可能已经过期（机器在面板外迁移过）：以当前虚拟机清单为准。
     否则「解除隔离」会打到一台已经不在那个节点上的机器，只能收到一句
     「找不到这台虚拟机」。 */
  const liveNodeOf = useCallback(
    (row: QuarantineRecord) => {
      if (vmOptions.some((o) => o.value === `${row.node}/${row.vmid}`)) {
        return row.node;
      }
      const moved = vmOptions.find((o) => o.value.endsWith(`/${row.vmid}`));
      return moved
        ? moved.value.slice(0, moved.value.lastIndexOf('/'))
        : row.node;
    },
    [vmOptions],
  );

  /* 节点下拉的 value 就是节点名（与「备份」页保持一致，登记时后端只认这个）。
     多台 PVE 上出现同名节点时只取第一条：这种集群请分别用各自的面板连接操作。 */
  const bNodeOptions = useMemo(() => {
    const seen = new Set<string>();
    const options: Array<{ label: string; value: string }> = [];
    for (const n of nodesQuery.data ?? []) {
      if (seen.has(n.node)) continue;
      seen.add(n.node);
      options.push({ label: n.node, value: n.node });
    }
    return options;
  }, [nodesQuery.data]);

  /* 只留启用了 backup 内容的存储：images / rootdir 这类存储放不下 vzdump 归档，
     列出来只会让人选中后吃到 PVE 的 "storage does not support content type"。 */
  const bStorageOptions = useMemo(() => {
    const all = bStoragesQuery.data ?? [];
    const canBackup = all.filter((s) =>
      (s.content || '')
        .split(',')
        .map((c) => c.trim())
        .includes('backup'),
    );
    /* content 字段缺失时（精简响应）不过滤，免得把选项清空 */
    const usable = canBackup.length ? canBackup : all;
    return [...new Set(usable.map((s) => s.storage))]
      .sort((a, b) => a.localeCompare(b))
      .map((name) => ({ label: name, value: name }));
  }, [bStoragesQuery.data]);

  /* 归档按创建时间倒序：最近的那份通常就是要保护的那份 */
  const bArchiveOptions = useMemo(
    () =>
      [...(bArchivesQuery.data ?? [])]
        .sort((a, b) => (b.ctime ?? 0) - (a.ctime ?? 0))
        .map((a) => ({ label: a.volid, value: a.volid })),
    [bArchivesQuery.data],
  );

  const bSelectedArchive = useMemo(
    () => (bArchivesQuery.data ?? []).find((a) => a.volid === volid),
    [bArchivesQuery.data, volid],
  );

  /* 列表刷新后原先选中的存储 / 归档可能已经不在了：自动退回未选状态，
     否则提交的是一个后端根本找不到的卷名 */
  useEffect(() => {
    if (!bStorage || bStoragesQuery.isLoading) return;
    if (!bStorageOptions.some((o) => o.value === bStorage)) setBStorage('');
  }, [bStorage, bStorageOptions, bStoragesQuery.isLoading]);

  useEffect(() => {
    if (!volid || bArchivesQuery.isLoading) return;
    if (!bArchiveOptions.some((o) => o.value === volid)) setVolid('');
  }, [volid, bArchiveOptions, bArchivesQuery.isLoading]);

  /* ---- 三个下拉的占位与说明：只在「还不能选」或「选不了」时给出文字 ---- */
  const bNodeHint = nodesQuery.isLoading
    ? t('backups.nodeLoading')
    : nodesQuery.isError
      ? errorMessage(nodesQuery.error)
      : bNodeOptions.length === 0
        ? t('incident.noNodes')
        : undefined;

  const bNodePlaceholder =
    bNodeOptions.length === 0
      ? nodesQuery.isLoading
        ? t('backups.nodeLoading')
        : t('incident.noNodes')
      : t('backups.nodePlaceholder');

  const bStorageHint = !bNode
    ? t('incident.pickNodeFirst')
    : bStoragesQuery.isLoading
      ? t('backups.nodeLoading')
      : bStoragesQuery.isError
        ? errorMessage(bStoragesQuery.error)
        : bStorageOptions.length === 0
          ? t('backups.noBackupStorage')
          : undefined;

  const bVolidHint = !bStorage
    ? t('incident.pickStorageFirst')
    : bArchivesQuery.isLoading
      ? t('backups.nodeLoading')
      : bArchivesQuery.isError
        ? errorMessage(bArchivesQuery.error)
        : bArchiveOptions.length === 0
          ? t('incident.noVolids')
          : bSelectedArchive
            ? t('incident.volidDetail', {
                size: formatBytes(bSelectedArchive.size),
                time: formatDateTime(bSelectedArchive.ctime),
              })
            : t('incident.volidSelectHint');

  const refreshProtected = async () => {
    await queryClient.invalidateQueries({ queryKey: ['incident', 'protected'] });
  };

  const runQuarantine = async () => {
    if (!parsed) {
      toast.error(t('incident.selectVmFirst'));
      return;
    }
    const body: QuarantineInput = {
      snapshot,
      cut_network: cutNetwork,
      power_action: powerAction,
      protect,
      note: note.trim(),
    };
    setBusy(true);
    try {
      const result = await isolationApi.quarantine(
        parsed.node,
        parsed.vmid,
        body,
        targetConn,
      );
      setLastResult(result);
      if (result.ok) {
        /* 隔离会断网并关机，属于不可撤销的处置动作：成功也要让人一眼看到 */
        toast.destructive(t('incident.quarantineDone'), result.summary);
      } else {
        toast.warning(t('incident.quarantinePartial'), result.summary);
      }
      await queryClient.invalidateQueries({ queryKey: ['incident'] });
      await queryClient.invalidateQueries({ queryKey: ['vms'] });
      /* 隔离会把系统信息采集进备注条目（见 app.guest_osinfo）：列表的「操作系统」
         徽标与备注弹窗都读它，不失效就还停在旧的那份上。 */
      await queryClient.invalidateQueries({ queryKey: ['vm-meta'] });
    } catch (err) {
      toast.error(t('incident.quarantineFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  /* ``row`` 有值 = 从清单里直接解除某一条；没有 = 用上方选中的机器。
     清单行里记着登记时的连接（conn_id），优先用它：那才是这台机器真正所在的
     PVE，解除动作不能按当前作用域去猜。 */
  const runRelease = async (powerOn: boolean, row?: QuarantineRecord) => {
    const node = row?.node ?? parsed?.node;
    const vmid = row?.vmid ?? parsed?.vmid;
    const conn = row?.conn_id || targetConn;
    if (!node || !vmid) return;
    setBusy(true);
    try {
      const result = await isolationApi.release(
        node,
        vmid,
        {
          restore_network: true,
          unprotect: true,
          power_on: powerOn,
          note: '',
        },
        conn,
      );
      setLastResult(result);
      toast.success(t('incident.released'), result.summary);
      await queryClient.invalidateQueries({ queryKey: ['incident'] });
      await queryClient.invalidateQueries({ queryKey: ['vms'] });
      /* 解除隔离会重新采集一次系统信息并补回缺失的 os 家族：备注缓存必须一起失效，
         否则「解除后图标回来了」在界面上要等下一次整页刷新才看得见。 */
      await queryClient.invalidateQueries({ queryKey: ['vm-meta'] });
    } catch (err) {
      toast.error(t('incident.releaseFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const protectBackup = async () => {
    if (!volid.trim() || !bNode.trim() || !bStorage.trim()) {
      toast.error(t('incident.fillFields'));
      return;
    }
    setBusy(true);
    try {
      const result = await protectedBackupsApi.protect({
        node: bNode.trim(),
        storage: bStorage.trim(),
        volid: volid.trim(),
        note: bNote.trim(),
      }, bConn);
      toast.success(t('incident.protectDone'), result.detail);
      setVolid('');
      setBNote('');
      await refreshProtected();
    } catch (err) {
      toast.error(t('incident.protectFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const verifyNow = async () => {
    setBusy(true);
    try {
      const result = await protectedBackupsApi.verify(true);
      /* 普通用户拿到的是「只统计自己那份」的结果，也不会给他推告警 ——
         文案要说清，否则「已推送告警」会让人以为全站都收到了通知 */
      const scopeHint = result.scoped ? t('incident.scopeHint') : '';
      if (result.missing || result.changed) {
        toast.error(
          t('incident.verifyProblem'),
          t('incident.verifyProblemDetail', {
            missing: result.missing,
            changed: result.changed,
            tail: result.scoped ? scopeHint : t('incident.verifyAlertsPushed'),
          }),
        );
      } else {
        toast.success(
          t('incident.verifyDone'),
          t('incident.verifyDoneDetail', { n: result.checked, scopeHint }),
        );
      }
      await refreshProtected();
    } catch (err) {
      toast.error(t('incident.verifyFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const doUnprotect = async () => {
    if (!unprotectTarget) return;
    try {
      await protectedBackupsApi.unprotect(
        unprotectTarget.volid,
        unprotectTarget.connection,
      );
      toast.success(t('incident.unprotectDone'), t('incident.unprotectDoneDetail'));
      await refreshProtected();
    } catch (err) {
      toast.error(t('incident.unprotectFailed'), errorMessage(err));
    } finally {
      setUnprotectTarget(null);
    }
  };

  const items = protectedQuery.data?.items ?? [];
  const stats = useMemo(
    () => ({
      total: items.length,
      missing: items.filter((item) => item.state === 'missing').length,
      changed: items.filter((item) => item.state === 'changed').length,
      pveProtected: items.filter((item) => item.pve_protected).length,
    }),
    [items],
  );

  /* 按当前勾选实时排出的动作清单：让「立即隔离」按下去之前就能看清会发生什么，
     而不是点完再从结果里读。 */
  const plannedSteps: Array<{ on: boolean; key: MessageKey; offKey: MessageKey }> = [
    { on: snapshot, key: 'incident.plan.snapshot', offKey: 'incident.planOff.snapshot' },
    { on: cutNetwork, key: 'incident.plan.network', offKey: 'incident.planOff.network' },
    {
      on: powerAction !== 'none',
      key: powerAction === 'stop' ? 'incident.plan.stop' : 'incident.plan.shutdown',
      offKey:
        powerAction === 'stop' ? 'incident.planOff.stop' : 'incident.planOff.shutdown',
    },
    { on: protect, key: 'incident.plan.protect', offKey: 'incident.planOff.protect' },
  ];

  /* 隔离清单的列：只放「处置时要知道的」——哪台机器、哪台宿主机、什么时候、
     断了几张网卡、取证快照叫什么、谁做的 + 备注。 */
  const isolationColumns: Array<Column<QuarantineRecord>> = [
    {
      key: 'guest',
      header: t('incident.isoColGuest'),
      render: (row) => (
        <span>
          <span className="fs-sm">{row.name || `VM ${row.vmid}`}</span>
          <span className="mono fs-xs text-muted"> · {row.vmid}</span>
          {row.state !== 'ok' ? (
            <Badge variant="warning" size="sm">
              {t('incident.isoStatePartial')}
            </Badge>
          ) : null}
        </span>
      ),
    },
    {
      key: 'node',
      header: t('common.node'),
      width: 120,
      render: (row) => <span className="mono fs-xs">{liveNodeOf(row)}</span>,
    },
    {
      key: 'at',
      header: t('incident.isoColAt'),
      width: 165,
      render: (row) => <span className="fs-xs">{formatDateTime(row.started_at)}</span>,
    },
    {
      key: 'nics',
      header: t('incident.isoColNics'),
      width: 90,
      render: (row) => <span className="fs-xs">{row.interfaces.length}</span>,
    },
    {
      key: 'snapshot',
      header: t('incident.isoColSnapshot'),
      render: (row) => <span className="mono fs-xs">{row.snapshot || '—'}</span>,
    },
    {
      key: 'detail',
      header: t('incident.colDetail'),
      render: (row) => (
        <span className="fs-xs text-muted">
          {[row.recorded_by, row.note].filter(Boolean).join(' · ') || '—'}
        </span>
      ),
    },
    {
      key: 'actions',
      header: t('common.actions'),
      width: 110,
      render: (row) =>
        canIsolate ? (
          <Button
            size="sm"
            variant="secondary"
            loading={busy}
            onClick={() => {
              /* 顺手把上方「处置目标」切到这一台：解除之后的状态横幅、
                 证据快照列表都跟着这台走，不会停在上一台的读数上。
                 节点取实时清单里的那个（清单里的可能是迁移前的旧节点）。 */
              const node = liveNodeOf(row);
              setTarget(`${node}/${row.vmid}`);
              void runRelease(false, { ...row, node });
            }}
          >
            {t('incident.release')}
          </Button>
        ) : null,
    },
  ];

  const columns: Array<Column<ProtectedBackup>> = [
    {
      key: 'volid',
      header: t('incident.colVolid'),
      render: (row) => <span className="mono fs-xs">{row.volid}</span>,
    },
    {
      key: 'where',
      header: t('incident.colStorage'),
      width: 180,
      render: (row) => (
        <span className="fs-xs">
          {row.node}/{row.storage}
          {row.vmid ? <span className="text-muted"> · VM {row.vmid}</span> : null}
        </span>
      ),
    },
    {
      key: 'size',
      header: t('incident.colSize'),
      width: 100,
      render: (row) => <span className="fs-xs">{formatBytes(row.size)}</span>,
    },
    {
      key: 'state',
      header: t('incident.colState'),
      width: 200,
      render: (row) => (
        <span className="form-row" style={{ gap: 4 }}>
          <Badge variant={STATE_META[row.state].variant} size="sm">
            {t(STATE_META[row.state].label)}
          </Badge>
          {row.pve_protected ? (
            <Badge variant="accent" size="sm" title={t('incident.pveProtectedTitle')}>
              {t('incident.pveProtectedBadge')}
            </Badge>
          ) : null}
        </span>
      ),
    },
    {
      key: 'detail',
      header: t('incident.colDetail'),
      render: (row) => (
        <span className="fs-xs text-muted">{row.state_detail || row.note || '—'}</span>
      ),
    },
    {
      key: 'actions',
      header: t('common.actions'),
      width: 90,
      render: (row) =>
        canBackup ? (
          <Button size="sm" variant="ghost" onClick={() => setUnprotectTarget(row)}>
            {t('incident.remove')}
          </Button>
        ) : null,
    },
  ];

  return (
    <PageShell
      title={t('incident.title')}
      subtitle={t('incident.subtitle')}
      actions={
        <Button
          size="sm"
          variant="ghost"
          icon={<IconRefresh size={14} />}
          loading={protectedQuery.isFetching}
          onClick={() => {
            /* 按前缀失效：「待处理 / 受保护」与隔离表单里的机器清单是两路查询，
               只刷前者会让目标下拉停在旧值 */
            void queryClient.invalidateQueries({ queryKey: ['incident'] });
          }}
        >
          {t('common.refresh')}
        </Button>
      }
    >
      {/* ============================================== 虚拟机隔离 */}
      <div className="section-block">
        <div className="section-title">
          <IconLock size={15} />
          <span className="section-name">{t('incident.isoSection')}</span>
          <span className="section-hint">{t('incident.isoSectionHint')}</span>
        </div>

        <Card collapsible={false}>
          <CardHeader
            title={t('incident.targetTitle')}
            subtitle={t('incident.targetSubtitle')}
            icon={<IconAlert size={16} />}
          />

          {!canIsolate ? (
            <Notice tone="info" title={t('incident.needPermTitle')}>
              {t('incident.needPermPre')}
              <span className="mono">vm.isolate</span>
              {t('incident.needPermMid')}
            </Notice>
          ) : (
            <>
              {/* 状态横幅：一进页面就知道这台机器现在是死是活 */}
              {statusQuery.isError ? (
                <Notice tone="warning" title={t('incident.statusReadError')}>
                  {errorMessage(statusQuery.error)}
                </Notice>
              ) : null}

              {statusQuery.data ? (
                <Notice
                  tone={statusQuery.data.isolated ? 'warning' : 'success'}
                  title={
                    statusQuery.data.isolated
                      ? t('incident.isolatedTitle', {
                          n: statusQuery.data.cut_interfaces.length,
                          protectSuffix: statusQuery.data.protected
                            ? t('incident.isolatedProtectSuffix')
                            : '',
                        })
                      : t('incident.notIsolated')
                  }
                >
                  {statusQuery.data.evidence_snapshots.length ? (
                    <>
                      {t('incident.evidenceLabel')}
                      {statusQuery.data.evidence_snapshots
                        .map((item) =>
                          t('incident.evidenceItem', {
                            name: item.name,
                            time: formatDateTime(item.snaptime ?? 0),
                          }),
                        )
                        .join(t('incident.evidenceSeparator'))}
                    </>
                  ) : (
                    t('incident.noEvidence')
                  )}
                </Notice>
              ) : null}

              <div className="split-panel is-even">
                {/* 左：控制台 */}
                <div className="inc-col">
                  <div className="dyn-row">
                    <Select
                      label={t('incident.selectVmLabel')}
                      placeholder={t('incident.selectVmPlaceholder')}
                      value={target}
                      onChange={(event) => setTarget(event.target.value)}
                      options={vmOptions}
                      searchable
                    />
                    <Select
                      label={t('incident.step.power')}
                      value={powerAction}
                      onChange={(event) =>
                        setPowerAction(event.target.value as 'none' | 'shutdown' | 'stop')
                      }
                      options={[
                        { label: t('incident.powerShutdown'), value: 'shutdown' },
                        { label: t('incident.powerNone'), value: 'none' },
                        { label: t('incident.powerStop'), value: 'stop' },
                      ]}
                    />
                  </div>

                  <div className="dyn-row">
                    <Switch
                      checked={snapshot}
                      onChange={setSnapshot}
                      label={t('incident.snapSwitch')}
                      hint={t('incident.snapSwitchHint')}
                    />
                    <Switch
                      checked={cutNetwork}
                      onChange={setCutNetwork}
                      label={t('incident.netSwitch')}
                      hint={t('incident.netSwitchHint')}
                    />
                    <Switch
                      checked={protect}
                      onChange={setProtect}
                      label={t('incident.protectSwitch')}
                      hint={t('incident.protectSwitchHint')}
                    />
                  </div>

                  <Textarea
                    label={t('incident.noteLabel')}
                    hint={t('incident.noteHint')}
                    rows={2}
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                  />

                  <div className="inc-actions">
                    <Button
                      variant="danger"
                      icon={<IconLock size={15} />}
                      loading={busy}
                      disabled={!parsed}
                      onClick={() => void runQuarantine()}
                    >
                      {t('incident.isolateNow')}
                    </Button>
                    <Button
                      variant="secondary"
                      loading={busy}
                      disabled={!parsed || !statusQuery.data?.isolated}
                      onClick={() => void runRelease(false)}
                    >
                      {t('incident.release')}
                    </Button>
                    <Button
                      variant="secondary"
                      loading={busy}
                      disabled={!parsed || !statusQuery.data?.isolated}
                      onClick={() => setPowerOnConfirm(true)}
                    >
                      {t('incident.releasePowerOn')}
                    </Button>
                  </div>
                  <div className="inc-hint">
                    {t('incident.actionsHint')}
                  </div>
                </div>

                {/* 右：动作预览 / 上次结果 —— 让危险操作在按下之前就能看清 */}
                <div className="inc-col">
                  <div className="inc-side-title">
                    {lastResult ? t('incident.lastResult') : t('incident.previewTitle')}
                  </div>

                  {lastResult ? (
                    <>
                      <div className="fs-sm">{lastResult.summary}</div>
                      <StepList result={lastResult} />
                      {lastResult.caveats?.length ? (
                        <Notice tone="info" title={t('incident.caveatsTitle')}>
                          <ul style={{ paddingLeft: 18, margin: 0 }}>
                            {lastResult.caveats.map((text) => (
                              <li key={text} className="fs-xs">
                                {text}
                              </li>
                            ))}
                          </ul>
                        </Notice>
                      ) : null}
                    </>
                  ) : (
                    <div className="inc-explain">
                      {plannedSteps.map((step, index) => (
                        <div
                          key={step.key}
                          className={`inc-explain-item ${step.on ? '' : 'is-off'}`}
                        >
                          <span className="inc-explain-index" aria-hidden="true">
                            {index + 1}
                          </span>
                          <span>{step.on ? t(step.key) : t(step.offKey)}</span>
                        </div>
                      ))}
                      {!parsed ? (
                        <div className="inc-hint">{t('incident.pickVmHint')}</div>
                      ) : null}
                    </div>
                  )}
                </div>
              </div>
            </>
          )}
        </Card>
      </div>

      {/* ============================================== 隔离清单（面板登记） */}
      <div className="section-block">
        <div className="section-title">
          <IconLock size={15} />
          <span className="section-name">{t('incident.isoListSection')}</span>
          <span className="section-hint">{t('incident.isoListSectionHint')}</span>
        </div>

        <Card collapsible={false}>
          <CardHeader
            title={t('incident.isoListTitle', { n: isolationItems.length })}
            subtitle={t('incident.isoListSubtitle')}
            icon={<IconLock size={16} />}
          />

          {isolationQuery.isError ? (
            <ErrorState
              title={t('incident.isoListLoadFailed')}
              message={errorMessage(isolationQuery.error)}
              onRetry={() => void isolationQuery.refetch()}
            />
          ) : isolationItems.length ? (
            <Table
              columns={isolationColumns}
              rows={isolationItems}
              rowKey={(row) => `${row.conn_id}:${row.node}/${row.vmid}`}
              caption={t('incident.isoListCaption')}
              dense
              emptyTitle={t('incident.isoListEmptyTitle')}
            />
          ) : (
            <EmptyState
              title={t('incident.isoListEmptyTitle')}
              description={t('incident.isoListEmptyDesc')}
              icon={<IconLock size={26} />}
            />
          )}
        </Card>
      </div>

      {/* ============================================== 备份防删 */}
      <div className="section-block">
        <div className="section-title">
          <IconShield size={15} />
          <span className="section-name">{t('incident.backupSection')}</span>
          <span className="section-hint">{t('incident.backupSectionHint')}</span>
        </div>

        <div className="grid grid-4">
        <KpiCard
          label={t('incident.kpi.total')}
          value={stats.total}
          icon={<IconStorage size={16} />}
          tone="accent"
          hint={t('incident.kpi.totalHint')}
        />
        <KpiCard
          label={t('incident.state.missing')}
          value={stats.missing}
          icon={<IconAlert size={16} />}
          tone={stats.missing ? 'danger' : 'success'}
          hint={stats.missing ? t('incident.kpi.missingHint') : t('incident.kpi.noneMissing')}
        />
        <KpiCard
          label={t('incident.state.changed')}
          value={stats.changed}
          icon={<IconInfo size={16} />}
          tone={stats.changed ? 'warning' : 'success'}
          hint={stats.changed ? t('incident.kpi.changedHint') : t('incident.kpi.metaOk')}
        />
        <KpiCard
          label={t('incident.kpi.pveProtected')}
          value={stats.pveProtected}
          icon={<IconShield size={16} />}
          tone="neutral"
          hint={t('incident.kpi.pveProtectedHint')}
        />
      </div>

        <CollapsibleCard
          title={t('incident.immutableTitle')}
          icon={<IconInfo size={15} />}
        >
          <div className="inc-note">
            <p>
              {t('incident.worm1a')}<b>{t('incident.worm1b')}</b>{t('incident.worm1c')}
            </p>
            <p>
              {t('incident.worm2a')}<b>{t('incident.worm2b')}</b>{t('incident.worm2c')}
              <span className="mono"> protected</span>{t('incident.worm2d')}
            </p>
            <p>
              {t('incident.worm3a')}<b>{t('incident.worm3b')}</b>{t('incident.worm3c')}
              <b>{t('incident.worm3d')}</b>{t('incident.worm3e')}
            </p>
          </div>
        </CollapsibleCard>

      <Card collapsible={false}>
        <CardHeader
          title={t('incident.protectedTitle', { n: items.length })}
          subtitle={t('incident.protectedSubtitle')}
          icon={<IconShield size={16} />}
          actions={
            canBackup ? (
              <Button
                size="sm"
                variant="secondary"
                icon={<IconShield size={14} />}
                loading={busy}
                onClick={() => void verifyNow()}
              >
                {t('incident.verifyNow')}
              </Button>
            ) : null
          }
        />

        {canBackup ? (
          <div className="create-bar">
            <div className="field-row">
              <Field
                label={t('common.node')}
                required
                className="field-narrow"
                hint={bNodeHint}
              >
                <Select
                  value={bNode}
                  onChange={(event) => {
                    /* 换节点 = 换一台主机：存储与卷都不一定在那儿，一并清掉，
                       免得拿着上一台的旧值提交 */
                    setBNode(event.target.value);
                    setBStorage('');
                    setVolid('');
                  }}
                  options={bNodeOptions}
                  placeholder={bNodePlaceholder}
                  disabled={nodesQuery.isLoading || bNodeOptions.length === 0}
                />
              </Field>
              <Field
                label={t('incident.fieldStorage')}
                required
                className="field-narrow"
                hint={bStorageHint}
              >
                <Select
                  value={bStorage}
                  onChange={(event) => {
                    setBStorage(event.target.value);
                    setVolid('');
                  }}
                  options={bStorageOptions}
                  placeholder={t('backups.storagePlaceholder')}
                  disabled={
                    !bNode ||
                    bStoragesQuery.isLoading ||
                    bStorageOptions.length === 0
                  }
                />
              </Field>
              <Field
                label={t('incident.colVolid')}
                required
                hint={bVolidHint}
                className="field-wide"
              >
                <Select
                  value={volid}
                  onChange={(event) => setVolid(event.target.value)}
                  options={bArchiveOptions}
                  placeholder={t('incident.volidSelectPlaceholder')}
                  disabled={
                    !bStorage ||
                    bArchivesQuery.isLoading ||
                    bArchiveOptions.length === 0
                  }
                />
              </Field>
              <Field label={t('incident.fieldNote')}>
                <Input
                  placeholder={t('incident.notePlaceholder')}
                  value={bNote}
                  onChange={(event) => setBNote(event.target.value)}
                />
              </Field>
              <Button
                variant="primary"
                icon={<IconShield size={15} />}
                loading={busy}
                onClick={() => void protectBackup()}
              >
                {t('incident.protectBtn')}
              </Button>
            </div>
          </div>
        ) : null}

        {protectedQuery.isError ? (
          <ErrorState
            title={t('incident.listLoadFailed')}
            message={errorMessage(protectedQuery.error)}
            onRetry={() => void protectedQuery.refetch()}
          />
        ) : items.length ? (
          <Table
            columns={columns}
            rows={items}
            rowKey={(row) => `${row.connection ?? ''}:${row.volid}`}
            caption={t('incident.caption')}
            dense
            emptyTitle={t('incident.emptyTitle')}
          />
        ) : (
          <EmptyState
            title={t('incident.emptyTitle')}
            description={t('incident.emptyDesc')}
            icon={<IconShield size={26} />}
          />
        )}

        {protectedQuery.data?.note ? (
          <div className="fs-xs text-muted" style={{ marginTop: 10 }}>
            {protectedQuery.data.note}
          </div>
        ) : null}
        </Card>
      </div>

      <ConfirmDialog
        open={Boolean(unprotectTarget)}
        title={t('incident.unprotectTitle')}
        message={t('incident.unprotectMessage', { volid: unprotectTarget?.volid ?? '' })}
        confirmText={t('incident.unprotectConfirm')}
        danger
        onCancel={() => setUnprotectTarget(null)}
        onConfirm={() => void doUnprotect()}
      />

      <ConfirmDialog
        open={powerOnConfirm}
        title={t('incident.releasePowerOn')}
        message={t('incident.powerOnMessage')}
        confirmText={t('incident.powerOnConfirm')}
        requireText={t('incident.powerOnRequire')}
        danger
        onCancel={() => setPowerOnConfirm(false)}
        onConfirm={() => {
          setPowerOnConfirm(false);
          void runRelease(true);
        }}
      />
    </PageShell>
  );
}
