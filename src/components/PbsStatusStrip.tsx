/* ==========================================================================
   pvecenter — 集群节点页的 PBS 卡片
   --------------------------------------------------------------------------
   节点页回答「机器活没活」。备份链路活没活是同一层健康问题，所以 PBS 也是这张
   树的一部分 —— 但放在哪一层，取决于它自己填没填区域：

     * **区域对得上**（PBS 的区域 / 机房 = 某条 PVE 连接的区域）：它就是那个机房
       的备份服务器，卡片并进该区域分组（`PbsRegionSection`），和机房里的 PVE
       连接并排。判 PVE 掉线时能顺手看到备份链路，这才是「同一个机房」的意义。
     * **没填区域，或填的区域 PVE 里没有**：留在页面最后单独那一组
       （`PbsStatusStrip`），不能凭空塞进别人的机房 —— 那会让人以为这台 PBS
       归那个机房管。

   交互与 PVE 节点保持一致（这一版的关键改动）：

     * 分组头 = `.tree-head.is-region`，整条可点、箭头随开合旋转，右侧不挂统计
      （在线率 / 仓库数 / 容量 / 更新时间都在展开后的卡片里）；
       开合状态由节点页的 `collapsedGroups` 管，工具栏「全部展开 / 收起」能覆盖它。
     * **三级与 PVE 完全同构**：区域 → PBS 连接（`.tree-head` 一行）→ PBS 节点
       （与 `NodeCard` 同款的 `Card`，`defaultOpen={false}`）。
       连接条只回答「这台服务器是什么」（名称、在线、节点数、仓库越线），点开才出现
       节点卡；节点卡再回答「它现在怎么样」（仓库使用率 / 内存 / 磁盘健康 + 右侧 GC
       与最近任务），「查看详情」在卡头右侧。**点行不再直接跳走**。
     * 分组头外侧只放「刷新」这一个分组级操作，不塞进 tree-head 按钮；
      管理入口在连接配置页，节点页不重复放跳转按钮。

   不在这里做编辑：凭据清洗、401/403 诊断、CA PEM 校验都在连接页
   （`PbsConnectionCard`），两套表单必然漂移；而且这一页面向所有有 node.view 的
   用户，token_id / CA 内容属于凭据面，不该摊开。

   权限：整块按 `pbs.view` 决定是否请求（默认只有管理员）。无权限、403、上游
   失败一律不渲染 —— 节点页的主体是节点，这一块是增值信息，出错时不该占版面。
   ========================================================================== */

import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { pbsApi } from '../api/endpoints';
import type {
  PbsConfigItem,
  PbsConnectionOverview,
} from '../api/types';
import { useAuth } from '../hooks/useAuth';
import { useRefreshInterval } from '../hooks/useRefreshInterval';
import { useT } from '../i18n';
import { formatBytes, formatRelative, usageColor } from '../utils/format';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card, CardHeader } from './ui/Card';
import { ProgressBar } from './ui/ProgressBar';
import {
  IconActivity,
  IconBackup,
  IconClock,
  IconCpu,
  IconDisk,
  IconChevronDown,
  IconChevronRight,
  IconMemory,
  IconRefresh,
  IconServer,
} from './Icons';

/** 排序：不可达 → 仓库越线 → 其余 → 已暂停。同档按名称，刷新时行不会来回跳。 */
function rowRank(conn: PbsConnectionOverview): number {
  /* 暂停接入排在最后：它不是故障，不该顶到列表最上面抢注意力 */
  if (conn.paused) return 4;
  if (!conn.online) return 0;
  if (conn.datastores.some((store) => store.level === 'crit')) return 1;
  if (conn.datastores.some((store) => store.level === 'warn')) return 2;
  return 3;
}

/** 区域只用于界面分组：比较前统一去空白，别让「广州机房」和「广州机房 」被当成两处 */
const normRegion = (value?: string) => (value ?? '').trim();

function sortRows(rows: PbsConnectionOverview[]): PbsConnectionOverview[] {
  return [...rows].sort(
    (a, b) => rowRank(a) - rowRank(b) || a.name.localeCompare(b.name),
  );
}

/* ---------------------------------------------------------------------------
   取数：PBS 概览与连接配置只有一份来源
   ---------------------------------------------------------------------------
   底部那个独立分组、以及「已并入区域分组」的卡片用的是同一份缓存
   （queryKey 相同，react-query 只会打一次），所以两边不会出现一份在线、
   另一份离线的割裂。 */
function usePbsOverview() {
  const { hasPermission } = useAuth();
  const canView = hasPermission('pbs.view');
  const refresh = useRefreshInterval();

  /* 与备份页共用同一个 queryKey：同一份 20 秒缓存，来回切页不会重复打 PBS */
  const overview = useQuery({
    queryKey: ['pbs', 'overview'],
    queryFn: pbsApi.overview,
    enabled: canView,
    staleTime: 20_000,
    refetchInterval: refresh,
    retry: false,
  });

  return { canView, overview };
}

function usePbsStripData() {
  const { canView, overview } = usePbsOverview();
  const { hasPermission } = useAuth();
  const canManage = hasPermission('pbs.manage');

  /* 相对时间自走：关掉自动刷新时「更新于 x 分钟前」不能冻在那一刻 */
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowTick(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  /* 证书策略在 overview 里没有，只能读配置；这个接口要 pbs.manage，
     所以只有管理员会多这一次请求 */
  const config = useQuery({
    queryKey: ['pbs', 'config'],
    queryFn: pbsApi.config,
    enabled: canView && canManage,
    staleTime: 60_000,
    retry: false,
  });

  const configById = useMemo(
    () => new Map((config.data ?? []).map((item) => [item.id, item])),
    [config.data],
  );

  return {
    canView,
    overview,
    data: overview.data,
    connections: overview.data?.connections ?? [],
    configById,
    nowTick,
  };
}

/**
 * 哪些 PBS 该并进 PVE 的区域分组。
 *
 * 规则：PBS 的区域/机房**填了值**，且 PVE 连接里存在同名区域 —— 这时它就是那个
 * 机房里的备份服务器，跟着区域一起看才有意义（判 PVE 掉线时能顺手看到备份链路）。
 * 没填区域、或填的区域 PVE 侧不存在，仍然是「下面那一组」，不能凭空塞进别人的机房。
 *
 * 节点页要先用它决定两件事：每个区域里要不要挂 PBS 卡片、底部那个独立分组还留不留。
 */
export function usePbsRegionMerge(pveRegions: readonly string[]) {
  const { canView, overview } = usePbsOverview();
  const connections = overview.data?.connections ?? [];

  return useMemo(() => {
    const known = new Set(pveRegions.map(normRegion).filter(Boolean));
    const mergedRegions = new Set<string>();
    const pbsCount = new Map<string, number>();
    let standalone = 0;
    for (const conn of connections) {
      const region = normRegion(conn.region);
      if (region && known.has(region)) {
        mergedRegions.add(region);
        pbsCount.set(region, (pbsCount.get(region) ?? 0) + 1);
      } else {
        standalone += 1;
      }
    }
    return {
      /** 已经并进区域分组的区域名（去空白后） */
      mergedRegions,
      /** 底部那个独立分组还留不留：一条没归进区域，或压根没有直连连接（只有 L1 提示） */
      hasStandalone: !canView ? false : standalone > 0 || connections.length === 0,
      /**
       * 某个区域里并进了几台 PBS。
       *
       * 区域头原来只报「N 台 PVE / N 台节点」，而 PBS 行就挂在这个区域下面 ——
       * 两台 PVE 节点加一台 PBS 的机房，头上写「2 台节点」会让人以为少算了一台。
       * 类型分开报（节点是节点、PBS 是 PBS），但加起来要和眼睛看到的一致。
       */
      pbsCountOfRegion: (region: string) => pbsCount.get(normRegion(region)) ?? 0,
    };
  }, [canView, connections, pveRegions]);
}

/** 指标格：与 NodeCard 的 `.node-metric` 同款，只是允许磁盘健康自定义颜色。 */
function PbsMetric({
  icon,
  label,
  percent,
  sub,
  color,
  unavailable,
}: {
  icon: ReactNode;
  label: string;
  percent: number;
  sub: string;
  color?: string;
  unavailable?: boolean;
}) {
  const tone = unavailable
    ? 'var(--text-muted)'
    : color ?? usageColor(percent);
  return (
    <div className="node-metric">
      <div className="node-metric-head">
        <span className="node-metric-label">
          {icon} {label}
        </span>
        <span className="node-metric-value" style={{ color: tone }}>
          {unavailable ? '—' : `${percent.toFixed(1)}%`}
        </span>
      </div>
      <ProgressBar value={unavailable ? 0 : percent} color={tone} height={6} />
      <span className="node-metric-sub">{sub}</span>
    </div>
  );
}

function taskTypeLabel(type: string, t: ReturnType<typeof useT>): string {
  const key = `pbs.task${type.charAt(0).toUpperCase()}${type.slice(1)}` as
    | 'pbs.taskBackup'
    | 'pbs.taskVerify'
    | 'pbs.taskPrune'
    | 'pbs.taskGc'
    | 'pbs.taskSync';
  return ['backup', 'verify', 'prune', 'gc', 'sync'].includes(type)
    ? t(key)
    : type;
}

/**
 * 一台 PBS 的**节点卡**（三级里的第三级）。
 *
 * 与 PVE 的 `NodeCard` 同型：卡头一行结论（节点名 + 在线 + 越线），副标题是
 * 地址 / 版本 / 证书策略，卡体是两根三根指标 + 右侧 GC 与最近任务，默认收起。
 * 这样「区域 → 连接 → 节点」三层在 PVE 与 PBS 上是同一套读法：连接条只回答
 * 「这台服务器是什么」，节点卡才回答「它现在怎么样」。
 */
function PbsNodeCard({
  conn,
  cfg,
  nowTick,
  onOpen,
}: {
  conn: PbsConnectionOverview;
  cfg?: PbsConfigItem;
  nowTick: number;
  onOpen: () => void;
}) {
  const t = useT();
  const online = conn.online;
  const stores = conn.datastores;
  const known = stores.filter((store) => store.total > 0);
  const total = known.reduce((sum, store) => sum + store.total, 0);
  const used = known.reduce((sum, store) => sum + store.used, 0);
  const usagePct = total > 0 ? Math.min(100, (used / total) * 100) : 0;

  const memTotal = conn.node?.mem_total ?? 0;
  const memUsed = conn.node?.mem_used ?? 0;
  const memPct = memTotal > 0 ? Math.min(100, (memUsed / memTotal) * 100) : 0;

  /* 读不到 ≠ 0：内存与磁盘健康走的是 /nodes/{node}/status 与 /nodes/{node}/disks/list，
     两者都要 Sys.Audit。缺权限时后端会写明原因（status_hint / disks_hint），
     这里把原因摊在指标下面 —— 只挂一个「—」会让人以为是面板坏了。 */
  const statusHint = conn.node?.status_hint;
  const disksHint = conn.node?.disks_hint;
  const unreadableText = (hint?: 'no_permission' | 'error') =>
    hint === 'no_permission'
      ? t('pbs.stripNeedAudit')
      : hint === 'error'
        ? t('pbs.stripNodeUnreadable')
        : '';

  const badDisks = conn.node?.disks?.length ?? 0;
  const diskTotal = conn.node?.disks_summary?.count ?? conn.node?.disks_total ?? 0;
  const diskHealthy =
    conn.node?.disks_summary?.healthy ?? Math.max(0, diskTotal - badDisks);
  const diskPct = diskTotal > 0 ? (diskHealthy / diskTotal) * 100 : 0;

  const gc = conn.node?.gc;
  const gcLabel = !gc
    ? t('pbs.gcNone')
    : gc.running
      ? t('pbs.gcRunning')
      : gc.status.toUpperCase() !== 'OK'
        ? t('pbs.taskFailed')
        : t('pbs.taskOk');
  const lastTask = conn.tasks[0];
  const lastTaskText = lastTask
    ? `${taskTypeLabel(lastTask.type, t)} · ${formatRelative(lastTask.starttime, nowTick)}`
    : t('pbs.noTasks');

  /* 卡头副标题：地址 / 版本 / 读取节点 / 证书策略 */
  const spec = [
    `${conn.host}:${conn.port}`,
    conn.version ? `v${conn.version}` : '',
    conn.node?.name || t('pbs.stripNodeAuto'),
    cfg
      ? cfg.verify_ssl === true
        ? t('pbs.stripTlsOn')
        : cfg.verify_ssl === false
          ? t('pbs.stripTlsOff')
          : t('pbs.stripTlsDefault')
      : '',
  ]
    .filter(Boolean)
    .join(' · ');

  const critStores = stores.filter((store) => store.level === 'crit').length;
  const warnStores = stores.filter(
    (store) => store.level === 'warn' || store.level === 'crit',
  ).length;

  /* 节点卡默认收起：这一页先回答「机房里有哪几台机器、谁不在线」，
     指标点开再看 —— 与 PVE 的 NodeCard 完全同一种读法。 */
  const nodeName = conn.node?.name || t('pbs.stripNodeAuto');

  return (
    <Card padded={false} className="card-head-inset" defaultOpen={false}>
      <CardHeader
        icon={<IconServer size={16} />}
        title={
          <span className="flex items-center gap-8 flex-wrap">
            {/* 走 localhost 别名时把原因挂在悬停上：这不是「节点叫 localhost」，
                而是缺 Sys.Audit 读不到真名，用户可以在连接里填 */}
            <span
              title={
                nodeName === 'localhost' ? t('pbs.nodeAliasHint') : undefined
              }
            >
              {nodeName}
            </span>
            <Badge
              variant={conn.paused ? 'neutral' : online ? 'success' : 'danger'}
              dot
              size="sm"
              title={conn.paused ? t('pbs.pausedHint') : undefined}
            >
              {conn.paused
                ? t('pbs.paused')
                : online
                  ? t('pbs.online')
                  : t('pbs.offline')}
            </Badge>
            {critStores > 0 ? (
              <Badge variant="danger" size="sm">
                {t('pbs.stripStoreCrit', { n: critStores })}
              </Badge>
            ) : warnStores > 0 ? (
              <Badge variant="warning" size="sm">
                {t('pbs.stripStoreWarn', { n: warnStores })}
              </Badge>
            ) : null}
            {/* 节点指标读不到（多半是 token 缺 Sys.Audit）：卡头就写清楚，
                免得展开只看到「—」和「读不到」以为是面板坏了 */}
            {conn.node?.status_available === false ? (
              <Badge
                variant="warning"
                size="sm"
                title={
                  (statusHint ?? disksHint) === 'no_permission'
                    ? t('pbs.nodeUnavailable')
                    : conn.node?.status_error ||
                      conn.node?.disks_error ||
                      t('pbs.stripNodeUnreadable')
                }
              >
                {unreadableText(statusHint) ||
                  unreadableText(disksHint) ||
                  t('pbs.stripNodeUnreadable')}
              </Badge>
            ) : null}
            {cfg?.ca_cert ? (
              <Badge variant="neutral" size="sm" title={t('pbs.stripCaPinned')}>
                {t('pbs.stripCaPinned')}
              </Badge>
            ) : null}
          </span>
        }
        subtitle={spec || undefined}
        actions={
          <Button
            variant="ghost"
            size="sm"
            iconRight={<IconChevronRight size={14} />}
            aria-label={t('pbs.stripViewAria', { name: conn.name })}
            onClick={onOpen}
          >
            {t('nodes.viewDetail')}
          </Button>
        }
      />

      <div className="node-card-body">
        <div className="node-metrics">
          <PbsMetric
            icon={<IconDisk size={12} />}
            label={t('pbs.stripMetricUsage')}
            percent={usagePct}
            sub={
              total > 0
                ? t('pbs.stripUsedTotal', {
                    used: formatBytes(used),
                    total: formatBytes(total),
                  })
                : t('pbs.usageUnavailable')
            }
            unavailable={!online || total === 0}
          />
          <PbsMetric
            icon={<IconMemory size={12} />}
            label={t('pbs.memory')}
            percent={memPct}
            sub={
              memTotal > 0 ? formatBytes(memTotal) : unreadableText(statusHint) || '—'
            }
            unavailable={!online || memTotal === 0}
          />
          <PbsMetric
            icon={<IconCpu size={12} />}
            label={t('pbs.diskHealth')}
            percent={diskPct}
            sub={
              diskTotal > 0
                ? t('pbs.stripDiskHealthy', {
                    healthy: diskHealthy,
                    total: diskTotal,
                  })
                : unreadableText(disksHint) || t('pbs.diskUnknownShort')
            }
            color={diskPct >= 100 ? 'var(--success)' : 'var(--danger)'}
            unavailable={!online || diskTotal === 0}
          />
        </div>

        <div className="node-card-aside">
          <span className="fs-sm text-secondary flex items-center gap-6">
            <IconActivity size={12} />
            {t('pbs.stripGc', { status: gcLabel })}
          </span>
          <span className="fs-sm text-secondary flex items-center gap-6">
            <IconClock size={12} />
            {lastTaskText}
          </span>
          {conn.paused ? (
            <span className="fs-xs text-muted">{t('pbs.pausedHint')}</span>
          ) : !online && conn.error ? (
            <span className="fs-xs text-danger">{conn.error}</span>
          ) : null}
        </div>
      </div>
    </Card>
  );
}

/**
 * 一台 PBS = 三级里的**第二级：连接**。
 *
 * 和 PVE 的连接行同型：一条 `.tree-head`（整行可点、箭头随开合旋转），右侧挂
 * 「区域 / PBS 徽章 + 在线 + 节点数 + 仓库越线」；点开才出现第三级的节点卡。
 * 二级 / 三级分开之后，PBS 与 PVE 是同一套读法：连接条只回答「这台服务器是
 * 什么」，节点卡才回答「它现在怎么样」。
 */
function PbsConnectionRow({
  conn,
  cfg,
  nowTick,
  onOpen,
  hideRegion,
  pbsCount,
}: {
  conn: PbsConnectionOverview;
  cfg?: PbsConfigItem;
  nowTick: number;
  onOpen: () => void;
  /** 已并进区域分组时不重复标区域：外层区域头已经说明了这是哪个机房 */
  hideRegion?: boolean;
  /** 本分组里有几台 PBS：一台写「1 台 PBS」，两台写「2 台 PBS」 */
  pbsCount: number;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const critStores = conn.datastores.filter((store) => store.level === 'crit').length;
  const warnStores = conn.datastores.filter(
    (store) => store.level === 'warn' || store.level === 'crit',
  ).length;
  const spec = [
    `${conn.host}:${conn.port}`,
    conn.version ? `v${conn.version}` : '',
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <section className="tree-group pbs-node">
      <button
        type="button"
        className="tree-head"
        aria-expanded={open}
        title={spec}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="tree-head-icon">
          <IconChevronDown size={15} />
        </span>
        <IconBackup size={15} />
        <span className="tree-name">{conn.name}</span>
        <span className="tree-meta">
          {/* 没并进区域分组时，区域是本组仅有的空间信息，标在标题栏上；
              并进区域后区域已经写在区域头上，重画一遍是噪音 */}
          {hideRegion ? null : conn.region ? (
            <Badge variant="neutral" size="sm" title={t('pbs.fieldRegion')}>
              {conn.region}
            </Badge>
          ) : null}
          {/* 数量按「台」算：一台写 1 台 PBS，两台写 2 台 PBS。
              这枚徽章同时充当类型标记，并进区域后也认得出这是备份服务器 */}
          <Badge variant="neutral" size="sm" title={t('pbs.title')}>
            {t('pbs.stripPbsCount', { n: pbsCount })}
          </Badge>
          {/* 在线是常态，不再常驻一枚绿徽章；只有不可达才出声。
              已暂停单独一档：主动停用不该显示成红的「不可达」 */}
          {conn.online ? null : conn.paused ? (
            <Badge variant="neutral" dot size="sm" title={t('pbs.pausedHint')}>
              {t('pbs.paused')}
            </Badge>
          ) : (
            <Badge variant="danger" dot size="sm">
              {t('pbs.offline')}
            </Badge>
          )}
          {critStores > 0 ? (
            <Badge variant="danger" size="sm">
              {t('pbs.stripStoreCrit', { n: critStores })}
            </Badge>
          ) : warnStores > 0 ? (
            <Badge variant="warning" size="sm">
              {t('pbs.stripStoreWarn', { n: warnStores })}
            </Badge>
          ) : null}
        </span>
      </button>

      {open ? (
        <div className="tree-body">
          <PbsNodeCard
            conn={conn}
            cfg={cfg}
            nowTick={nowTick}
            onOpen={onOpen}
          />
        </div>
      ) : null}
    </section>
  );
}

/**
 * 已并入某个区域分组的 PBS 连接。
 *
 * 不再单独给一组、也不带分组头：外层就是「区域 / 机房」，这里的连接行和该机房
 * 里的 PVE 连接并排，一眼能看出「这个机房的备份链路归谁」。区域徽章也不再重复
 * 画 —— 外层区域头已经回答过「这是哪个机房」，连接行上换成 PBS 徽章。
 */
export function PbsRegionSection({ region }: { region: string }) {
  const navigate = useNavigate();
  const { connections, configById, nowTick } = usePbsStripData();

  const rows = useMemo(
    () =>
      sortRows(
        connections.filter((conn) => normRegion(conn.region) === normRegion(region)),
      ),
    [connections, region],
  );

  if (!normRegion(region) || rows.length === 0) return null;

  return (
    <div className="pbs-region-body">
      {rows.map((conn) => (
        <PbsConnectionRow
          key={conn.id}
          conn={conn}
          cfg={configById.get(conn.id)}
          nowTick={nowTick}
          hideRegion
          pbsCount={rows.length}
          onOpen={() => navigate('/backups')}
        />
      ))}
    </div>
  );
}

export function PbsStatusStrip({
  open,
  onToggle,
  excludeRegions,
}: {
  /** 由节点页统一管：工具栏「全部展开 / 收起」要能带上这一组 */
  open: boolean;
  onToggle: () => void;
  /**
   * 已经并进区域分组的区域名：这些 PBS 不在本组里重复出现。
   * 传了以后一条都不剩时整组不渲染（都被区域分组收走了）。
   */
  excludeRegions?: readonly string[];
}) {
  const t = useT();
  const navigate = useNavigate();
  const { canView, overview, data, connections, configById, nowTick } =
    usePbsStripData();

  if (!canView) return null;
  /* 加载中 / 403 / 上游失败都不渲染：这一块是增值信息，不是页面主体 */
  if (!data) return null;

  const pveStorages = data.pve_storages ?? [];
  /* 一台都没接：不在节点页凭空多出一张「你没有 PBS」的卡 */
  if (!data.configured && pveStorages.length === 0) return null;

  const excluded = new Set((excludeRegions ?? []).map(normRegion));
  const sorted = sortRows(
    connections.filter((conn) => !excluded.has(normRegion(conn.region))),
  );
  /* 全部并进了各自的区域分组：这一组不再单独出现 */
  if (sorted.length === 0) return null;

  return (
    <section className="tree-group pbs-strip">
      <div className="pbs-strip-head">
        {/* 整条标题栏都是开关，和上面 PVE 分组的 tree-head 完全同款 */}
        <button
          type="button"
          className="tree-head is-region pbs-strip-toggle"
          aria-expanded={open}
          onClick={onToggle}
        >
          <span className="tree-head-icon" aria-hidden="true">
            <IconChevronDown size={15} />
          </span>
          <IconBackup size={15} />
          <span className="tree-name">{t('pbs.title')}</span>
          {/* 右侧不再挂在线率 / 仓库数 / 容量 / 更新时间这一串统计：
              分组头只负责「这是哪一组、能不能收起来」，与上面 PVE 分组头
              保持同一节奏；在线状态、仓库用量、最近任务都在下面的卡片里 */}
          {data.configured ? null : (
            <span className="tree-meta">
              <Badge
                variant="warning"
                size="sm"
                title={t('pbs.stripL1', { n: pveStorages.length })}
              >
                {t('pbs.stripL1Short')}
              </Badge>
            </span>
          )}
        </button>
        {/* 操作按钮不能塞进上面那个 button 里，单独排在同一行右侧。
            这里只留「刷新」：管理入口属于连接配置页，节点页不再重复放一个
            跳转按钮（PBS 卡片里的「查看详情」已经能把人带到备份详情） */}
        {data.configured ? (
          <div className="pbs-strip-actions">
            <Button
              variant="ghost"
              size="sm"
              icon={<IconRefresh size={14} />}
              disabled={overview.isFetching}
              onClick={() => void overview.refetch()}
            >
              {t('common.refresh')}
            </Button>
          </div>
        ) : null}
      </div>

      {/* 没有直连连接（L1：PVE 挂了 PBS 存储但没凭据）时不再补一句
          「还没有保存任何 PBS 连接」—— 标题栏徽章已经把这件事说明白了。
          走到这里至少还剩一条（全部并入区域时上面已经 return null） */}
      {!open ? null : (
        <div className="tree-body pbs-strip-body">
          {sorted.map((conn) => (
            <PbsConnectionRow
              key={conn.id}
              conn={conn}
              cfg={configById.get(conn.id)}
              nowTick={nowTick}
              pbsCount={sorted.length}
              onOpen={() => navigate('/backups')}
            />
          ))}
        </div>
      )}
    </section>
  );
}
