/* ==========================================================================
   pvecenter — PBS 概览（备份页顶部）
   --------------------------------------------------------------------------
   三态，由后端 `/pbs/overview` 的两个字段决定，前端不自己拼判断：

   1. `pve_storages` 为空且没有直连凭据 —— **未对接**，整块不渲染。
      备份页本来就有「备份存在哪」的说明，再多一块空盒子只是噪音。
   2. `pve_storages` 非空但没有凭据 —— **仅间接**：PVE 眼里的 PBS 存储可用
      （存储页已有容量 / 最近备份 / 校验列），这里只给一条升级引导。
   3. 有凭据 —— **完整**：仓库容量（使用率大字 + 进度条 + 总/已用/剩余，超阈值
      标黄标红）、节点负载、磁盘健康、最近备份任务，任务行可点开看日志。

   这一块只回答「备份盘还剩多少、最近备份成不成」两个问题：磁盘 I/O 之类的
   主机指标不在这里出现 —— 与备份状态无关，只会把真正要看的东西挤下去。

   数据新鲜度：轮询间隔跟随全站刷新设置（可关），后端另有 20 秒缓存兜底 ——
   多人同时打开备份页时，没有它会对同一台 PBS 连打 N 份同样的请求。
   卡片标题栏显示「更新于 x」，时间取后端的采集时刻 `generated_at`，
   不是浏览器收到响应的时间（那会被缓存拉开误差）。
   ========================================================================== */

import { useEffect, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { pbsApi } from '../api/endpoints';
import type {
  PbsConnectionOverview,
  PbsDatastore,
  PbsTask,
  PbsUsageLevel,
} from '../api/types';
import { errorMessage } from '../api/client';
import { useAuth } from '../hooks/useAuth';
import { useRefreshInterval } from '../hooks/useRefreshInterval';
import { useT, type TFunc } from '../i18n';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Card, CardHeader } from './ui/Card';
import { Modal } from './ui/Modal';
import {
  formatBytes,
  formatDateTime,
  formatRelative,
  formatUptimeShort,
} from '../utils/format';
import {
  IconBackup,
  IconChevronDown,
  IconChevronUp,
  IconClock,
  IconLink,
  IconRefresh,
  IconServer,
} from './Icons';

/** PBS 任务类型 → 人话。未知类型原样显示，不吞线索。 */
function taskLabel(task: PbsTask, t: TFunc): string {
  switch (task.type) {
    case 'backup':
      return t('pbs.taskBackup');
    case 'verify':
      return t('pbs.taskVerify');
    case 'prune':
      return t('pbs.taskPrune');
    case 'garbage_collection':
    case 'gc':
      return t('pbs.taskGc');
    case 'sync':
      return t('pbs.taskSync');
    default:
      return task.type || '—';
  }
}

function percent(used: number, total: number): number {
  return total > 0 ? Math.min(100, (used / total) * 100) : 0;
}

function TaskRow({
  task,
  onOpen,
}: {
  task: PbsTask;
  onOpen: () => void;
}) {
  const t = useT();
  const failed = !task.running && task.status.toUpperCase() !== 'OK';
  /* 耗时：任务列表里最有用的第二层信息 —— 「14 小时前成功」和
     「14 小时前、跑了 2 分钟才成功」是两回事。 */
  const took =
    !task.running && task.endtime > task.starttime
      ? formatUptimeShort(task.endtime - task.starttime)
      : '—';
  return (
    <li>
      <button
        type="button"
        className="pbs-task-row"
        onClick={onOpen}
        title={t('pbs.logOpen')}
      >
        <span className="pbs-task-kind">{taskLabel(task, t)}</span>
        <span className="pbs-task-target mono">
          {task.worker_id ? `#${task.worker_id}` : '—'}
        </span>
        {task.running ? (
          <Badge variant="info" size="sm" dot>
            {t('pbs.taskRunning')}
          </Badge>
        ) : failed ? (
          <Badge variant="danger" size="sm" dot>
            {t('pbs.taskFailed')}
          </Badge>
        ) : (
          <Badge variant="success" size="sm" dot>
            {t('pbs.taskOk')}
          </Badge>
        )}
        <span
          className="pbs-task-dur mono"
          title={took === '—' ? undefined : t('pbs.taskDuration', { value: took })}
        >
          {took}
        </span>
        <span className="pbs-task-time" title={formatDateTime(task.starttime)}>
          {formatRelative(task.starttime)}
        </span>
      </button>
    </li>
  );
}

function storePercent(store: PbsDatastore): number {
  return store.used_percent ?? percent(store.used, store.total);
}

/** level 由后端判定；老后端没有时按同一组阈值兜底，保证行为一致。 */
function storeLevel(store: PbsDatastore): PbsUsageLevel {
  return (
    store.level ??
    (storePercent(store) >= 95
      ? 'crit'
      : storePercent(store) >= 85
        ? 'warn'
        : 'ok')
  );
}

/** 一组仓库里最严重的级别：节点行里的百分比按最紧张的仓库上色。 */
function worstStoreLevel(stores: PbsDatastore[]): PbsUsageLevel {
  const levels = stores.map(storeLevel);
  if (levels.includes('crit')) return 'crit';
  if (levels.includes('warn')) return 'warn';
  return levels.length > 0 ? 'ok' : 'unknown';
}

/** 容量详情：路径 / GC / 校验放在悬停提示里，正文只留四个数字。 */
function storeMeta(store: PbsDatastore, t: TFunc): string {
  const parts = [store.path || store.store];
  if (store.gc_schedule) {
    parts.push(t('pbs.gcSchedule', { schedule: store.gc_schedule }));
  }
  if (store.verify_new) parts.push(t('pbs.verifyNew'));
  return parts.filter(Boolean).join(' · ');
}

/* ---------------------------------------------------------------- 多节点判定 */
/* 一个 PBS 连接 = 一个节点（一台 PBS 服务器）。多节点时，聚合条、节点表与
   单节点详情必须用同一套判定，否则「聚合说 2 块盘异常、行里显示绿色」这种
   自相矛盾很难查 —— 所以下面三个函数是唯一来源。 */

type DiskState = 'ok' | 'bad' | 'unknown';
type GcState = 'ok' | 'bad' | 'busy' | 'unknown';

/** 磁盘：有一块不正常就是 bad；盘数为 0 是「没读到」，不能当成「全好」。 */
function connDiskState(conn: PbsConnectionOverview): DiskState {
  const disks = conn.node?.disks ?? [];
  if (disks.length > 0) return 'bad';
  return (conn.node?.disks_total ?? 0) > 0 ? 'ok' : 'unknown';
}

/** GC：还在跑的 GC 不是失败，是「进行中」。 */
function connGcState(conn: PbsConnectionOverview): GcState {
  const gc = conn.node?.gc;
  if (!gc) return 'unknown';
  if (gc.running) return 'busy';
  return gc.status.toUpperCase() !== 'OK' ? 'bad' : 'ok';
}

/** 节点健康度：离线 / 坏盘 / GC 失败 / 仓库越线 = crit；只用于排序与色点。 */
type NodeHealth = 'crit' | 'warn' | 'unknown' | 'ok';

function connHealth(conn: PbsConnectionOverview): NodeHealth {
  /* 暂停接入不是故障：给灰点（排在问题之后），否则整块面板会为一次主动停用
     报红 */
  if (conn.paused) return 'unknown';
  if (!conn.online) return 'crit';
  if (connDiskState(conn) === 'bad') return 'crit';
  if (connGcState(conn) === 'bad') return 'crit';
  const levels = conn.datastores.map(storeLevel);
  if (levels.includes('crit')) return 'crit';
  if (levels.includes('warn')) return 'warn';
  /* 容量都读不到时给灰点：既不冒充正常，也不谎报故障 */
  if (!conn.datastores.some((store) => store.total > 0)) return 'unknown';
  return 'ok';
}

const HEALTH_RANK: Record<NodeHealth, number> = {
  crit: 0,
  warn: 1,
  unknown: 2,
  ok: 3,
};

/** 排序：问题节点顶到最上面，其余按名称；同分时保持稳定，避免刷新时跳动。 */
function sortConnections(conns: PbsConnectionOverview[]): PbsConnectionOverview[] {
  return [...conns].sort((a, b) => {
    const d = HEALTH_RANK[connHealth(a)] - HEALTH_RANK[connHealth(b)];
    return d !== 0 ? d : a.name.localeCompare(b.name);
  });
}

/**
 * 主仓库：整块面板唯一的「大数字」。
 *
 * 自上而下三级：仓库名（这是哪块盘）→ 使用率大字 + 右侧三联容量数字
 * （已用 / 剩余 / 总容量）→ 进度条与一行明细。容量三联横排是为了把这行撑满：
 * 只放一个「剩余」时，大数字右边会留出一大片空白。次要仓库走
 * {@link StoreRow}，一行一条，不抢版面。
 */
function StoreHero({ store, hint }: { store: PbsDatastore; hint: string }) {
  const t = useT();
  const total = store.total;
  const used = store.used;
  const free = store.free ?? store.avail ?? Math.max(total - used, 0);
  const pct = storePercent(store);
  const level = storeLevel(store);

  if (total <= 0) {
    return (
      <div className="pbs-hero is-empty">
        <span className="pbs-hero-name mono">{store.store}</span>
        <div className="fs-xs text-muted">
          {store.capacity_error
            ? t('pbs.capacityError', { code: store.capacity_error })
            : t('pbs.usageUnavailable')}
        </div>
      </div>
    );
  }

  return (
    <div className={`pbs-hero is-${level}`}>
      <div className="pbs-hero-head">
        <span className="pbs-hero-name mono" title={hint}>
          {store.store}
        </span>
        {store.maintenance_mode ? (
          <span className="pbs-hero-tag">{t('pbs.maintenance')}</span>
        ) : null}
      </div>
      <div className="pbs-hero-main">
        <span className="pbs-hero-gauge">
          <span className="pbs-hero-label">{t('pbs.usage')}</span>
          <span className={`pbs-hero-value is-${level}`}>
            {pct.toFixed(1)}
            <span className="pbs-hero-unit">%</span>
          </span>
        </span>
        {/* 已用 / 剩余 / 总容量横排在右侧：大数字右边的空档正好被这三个数填满，
            而且三者同一基线，比原来只放一个「剩余」更好比对 */}
        <div className="pbs-hero-stats">
          <span className="pbs-hero-stat">
            <span className="pbs-hero-label">{t('pbs.usedSpace')}</span>
            <b className="mono">{formatBytes(used)}</b>
          </span>
          <span className="pbs-hero-stat">
            <span className="pbs-hero-label">{t('pbs.freeSpace')}</span>
            <b className="mono">{formatBytes(free)}</b>
          </span>
          <span className="pbs-hero-stat">
            <span className="pbs-hero-label">{t('pbs.totalSpace')}</span>
            <b className="mono">{formatBytes(total)}</b>
          </span>
        </div>
      </div>
      <div className="pbs-hero-bar">
        {/* 0.2% 也要看得见：给一个最小可见宽度，但数字仍是真实值 */}
        <i style={{ width: `${Math.max(pct, 0.6)}%` }} />
      </div>
      {store.gc_schedule || store.verify_new ? (
        <div className="pbs-hero-sub">
          {store.gc_schedule ? (
            <span>{t('pbs.gcSchedule', { schedule: store.gc_schedule })}</span>
          ) : null}
          {store.verify_new ? <span>{t('pbs.verifyNew')}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

/** 次要仓库：名称 · 细条 · 百分比 · 用量，一行结束。 */
function StoreRow({ store, hint }: { store: PbsDatastore; hint: string }) {
  const pct = storePercent(store);
  const level = storeLevel(store);
  return (
    <div className={`pbs-store-row is-${level}`} title={hint}>
      <span className="pbs-store-name mono">{store.store}</span>
      <span className="pbs-store-bar">
        <i style={{ width: `${Math.max(pct, 0.6)}%` }} />
      </span>
      <span className="pbs-store-pct mono">{pct.toFixed(1)}%</span>
      <span className="pbs-store-size mono">
        {formatBytes(store.used)} / {formatBytes(store.total)}
      </span>
    </div>
  );
}

function ConnectionBlock({
  conn,
  onOpenTask,
  thresholds,
  headless = false,
}: {
  conn: PbsConnectionOverview;
  onOpenTask: (task: PbsTask) => void;
  thresholds: { warn: number; crit: number };
  /** 多节点时这棵子树挂在节点行下面：名称 / 状态 / 地址已经在行里，不再重复 */
  headless?: boolean;
}) {
  const t = useT();
  const memTotal = conn.node?.mem_total ?? 0;
  const memUsed = conn.node?.mem_used ?? 0;
  const memPct = memTotal > 0 ? Math.round((memUsed / memTotal) * 100) : 0;
  const loads = [conn.node?.load1, conn.node?.load5, conn.node?.load15].filter(
    (v): v is number => typeof v === 'number',
  );
  const disks = conn.node?.disks ?? [];
  const disksTotal = conn.node?.disks_total ?? 0;
  const gc = conn.node?.gc ?? null;
  /* 还在跑的 GC 不算失败：status 为空是「没结束」，不是「没成功」 */
  const gcState = connGcState(conn);
  const gcFailed = gcState === 'bad';
  /* 主仓库取使用率最高的那块：多仓库时把最紧张的一块顶到「大数字」位置 */
  const stores = [...conn.datastores].sort(
    (a, b) => storePercent(b) - storePercent(a),
  );
  const primary = stores[0];
  const extra = stores.slice(1);

  /* 磁盘格：值说「几块正常 / 几块异常」，副行说容量与 SSD/HDD 构成；
     有坏盘时副行换成设备名与健康值。整格的 title 放全文（窄列会截断）。 */
  const diskSummary = conn.node?.disks_summary;
  const diskCount = diskSummary?.count ?? disksTotal;
  const diskValue =
    disks.length > 0
      ? t('pbs.diskAbnormal', { n: disks.length })
      : diskCount > 0
        ? t('pbs.diskOk', { n: diskCount })
        : '—';
  const diskText =
    disks.length > 0
      ? disks.map((disk) => `${disk.device} ${disk.health}`).join(' · ')
      : diskSummary && diskSummary.count > 0
        ? [
            diskSummary.total_bytes > 0
              ? formatBytes(diskSummary.total_bytes)
              : '',
            diskSummary.ssd > 0 ? `SSD ×${diskSummary.ssd}` : '',
            diskSummary.hdd > 0 ? `HDD ×${diskSummary.hdd}` : '',
          ]
            .filter(Boolean)
            .join(' · ')
        : t('pbs.diskUnknown');
  /* 悬停看每块盘的型号 / 容量 / 健康：汇总说不清「这批盘是什么」 */
  const diskRows = conn.node?.disks_all ?? [];
  const diskTitle =
    diskRows.length > 0
      ? diskRows
          .map((row) =>
            [
              row.device,
              row.model,
              row.size > 0 ? formatBytes(row.size) : '',
              row.health || '—',
            ]
              .filter(Boolean)
              .join(' · '),
          )
          .join('\n')
      : diskText;
  /* 色点与数值颜色只由这三个状态决定：正常绿、异常红、读不到灰。
     「读不到」必须和「正常」长得不一样 —— 否则缺权限会被当成一切正常。 */
  const diskState = connDiskState(conn);

  /* GC 格：值只看成 / 败 / 在跑；副行给「多久之前 + 耗时」，没有记录时给计划 */
  const gcValue = !gc
    ? t('pbs.gcNone')
    : gc.running
      ? t('pbs.gcRunning')
      : gcFailed
        ? t('pbs.taskFailed')
        : t('pbs.taskOk');
  const gcSub = !gc
    ? primary?.gc_schedule
      ? t('pbs.gcSchedule', { schedule: primary.gc_schedule })
      : '—'
    : [
        formatRelative(gc.starttime),
        gc.endtime > gc.starttime
          ? formatUptimeShort(gc.endtime - gc.starttime)
          : '',
      ]
        .filter(Boolean)
        .join(' · ');
  const gcText = !gc
    ? gcSub
    : gc.running
      ? t('pbs.gcRunning')
      : gcFailed
        ? t('pbs.gcFailed', {
            status: gc.status || '—',
            time: formatDateTime(gc.starttime),
          })
        : t('pbs.gcOk', { time: formatDateTime(gc.starttime) });

  /* 任务默认只露 3 条：备份页上「最近有没有跑成」比「列出全部」重要得多 */
  const [expanded, setExpanded] = useState(false);
  const taskVisible = Math.min(conn.tasks.length, 8);
  const visibleTasks = expanded ? taskVisible : Math.min(taskVisible, 3);

  /* 阈值超限的仓库集中成一条提示：一眼看到「哪块盘、多少、超过哪条线」，
     而不是让人在几个仓库条里逐个找颜色。 */
  const overLimit = conn.datastores.filter(
    (store) => store.level === 'crit' || store.level === 'warn',
  );
  const worst: 'crit' | 'warn' | null = overLimit.some(
    (store) => store.level === 'crit',
  )
    ? 'crit'
    : overLimit.length > 0
      ? 'warn'
      : null;

  return (
    <div className={`pbs-connection${headless ? ' is-embedded' : ''}`}>
      {headless ? null : (
        <div className="pbs-connection-head">
          <span className="pbs-connection-name">
            <IconServer size={15} />
            {conn.name}
          </span>
          {/* 区域与连接配置里的「区域 / 机房」同源：多机房时一眼看出这台属于哪儿 */}
          {conn.region ? (
            <Badge variant="neutral" size="sm" title={t('pbs.fieldRegion')}>
              {conn.region}
            </Badge>
          ) : null}
          <Badge
            variant={conn.paused ? 'neutral' : conn.online ? 'success' : 'danger'}
            size="sm"
            dot
            title={conn.paused ? t('pbs.pausedHint') : undefined}
          >
            {conn.paused
              ? t('pbs.paused')
              : conn.online
                ? t('pbs.online')
                : t('pbs.offline')}
          </Badge>
          {conn.version ? (
            <span className="fs-xs text-muted">v{conn.version}</span>
          ) : null}
          <span className="fs-xs text-muted mono">
            {conn.host}:{conn.port}
          </span>
          {/* 同一 host:port 登记了多条配置时，后端已合并成一行，这里说明被合并的数量 */}
          {conn.shared_with && conn.shared_with.length > 0 ? (
            <span className="fs-xs text-muted">
              {t('pbs.sharedWith', { names: conn.shared_with.join('、') })}
            </span>
          ) : null}
          <a
            className="btn-icon-only btn-ghost"
            href={`https://${conn.host}:${conn.port}/`}
            target="_blank"
            rel="noopener noreferrer"
            title={t('pbs.openConsole')}
            aria-label={t('pbs.openConsole')}
          >
            <IconLink size={15} />
          </a>
        </div>
      )}

      {conn.error && !conn.online ? (
        <div className="fs-sm text-danger pbs-connection-error">{conn.error}</div>
      ) : null}

      {/* 阈值告警：压成一条，仍然「哪块盘、多少、超过哪条线」齐全 */}
      {worst ? (
        <div className={`pbs-alert is-${worst}`}>
          <strong>
            {worst === 'crit'
              ? t('pbs.alertCritTitle', { n: thresholds.crit })
              : t('pbs.alertWarnTitle', { n: thresholds.warn })}
          </strong>
          <span>
            {overLimit
              .map((store) =>
                t('pbs.alertItem', {
                  store: store.store,
                  percent: storePercent(store).toFixed(1),
                  threshold:
                    store.level === 'crit' ? thresholds.crit : thresholds.warn,
                }),
              )
              .join(' · ')}
          </span>
        </div>
      ) : null}

      {/* 监控面板：左侧主仓库（全卡唯一的大数字），右侧 2×2 指标格。
          每个格子都是「标签 → 数值 → 备注」三层，扫视时对齐的是同一条基线。 */}
      <div className="pbs-panel">
        <div className="pbs-panel-main">
          {primary ? (
            <StoreHero
              store={primary}
              hint={[storeMeta(primary, t), t('pbs.datastoreHint')].join('\n')}
            />
          ) : (
            <div className="pbs-hero is-empty">
              <div className="fs-xs text-muted">
                {conn.paused
                  ? t('pbs.pausedHint')
                  : conn.datastore_hint === 'no_permission'
                    ? t('pbs.datastoreNoPerm')
                    : t('pbs.noDatastore')}
              </div>
            </div>
          )}
        </div>

        {/* 右列 2×2：主机四项。磁盘 / GC 已挪到下面整行的状态条 ——
            六个格子排三行会把左栏主仓库拉高，大数字周围就多出一片空白。 */}
        <div className="pbs-side">
          <div className="pbs-tiles">
            <div className="pbs-tile">
              <span className="pbs-tile-label">{t('pbs.memory')}</span>
              <span className="pbs-tile-value mono">
                {memTotal > 0
                  ? `${formatBytes(memUsed)} / ${formatBytes(memTotal)}`
                  : '—'}
              </span>
              <span className={memTotal > 0 ? 'pbs-tile-sub is-bar' : 'pbs-tile-sub'}>
                {memTotal > 0 ? (
                  <>
                    <span className="pbs-mini-bar">
                      <i style={{ width: `${Math.max(memPct, 2)}%` }} />
                    </span>
                    {memPct}%
                  </>
                ) : null}
              </span>
            </div>
            <div className="pbs-tile">
              <span className="pbs-tile-label">{t('pbs.load')}</span>
              <span className="pbs-tile-value mono">
                {loads.length > 0 ? loads.map((v) => v.toFixed(2)).join(' / ') : '—'}
              </span>
              <span className="pbs-tile-sub">1m / 5m / 15m</span>
            </div>
            <div className="pbs-tile">
              <span className="pbs-tile-label">{t('pbs.nodeTitle')}</span>
              <span className="pbs-tile-value mono">{conn.node?.name || '—'}</span>
              <span className="pbs-tile-sub">
                {conn.node?.cpus ? t('pbs.cpus', { n: conn.node.cpus }) : ''}
              </span>
            </div>
            <div className="pbs-tile">
              <span className="pbs-tile-label">{t('pbs.uptimeLabel')}</span>
              <span className="pbs-tile-value mono">
                {conn.node?.uptime ? formatUptimeShort(conn.node.uptime) : '—'}
              </span>
              <span className="pbs-tile-sub">
                {conn.paused
                  ? t('pbs.paused')
                  : conn.online
                    ? t('pbs.online')
                    : t('pbs.offline')}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* 运行状态：磁盘健康与 GC 整行常驻，左侧色点 + 数值染色 ——
          正常绿、异常红、读不到灰，「一切正常」和「没采到」不会混成一种样子。 */}
      <div className="pbs-status">
        <div className={`pbs-status-item is-${diskState}`} title={diskTitle}>
          <span className="pbs-status-label">
            <i className="pbs-status-dot" />
            {t('pbs.diskHealth')}
          </span>
          <span className="pbs-status-value">{diskValue}</span>
          <span className="pbs-status-sub">{diskText}</span>
        </div>
        <div className={`pbs-status-item is-${gcState}`} title={gcText}>
          <span className="pbs-status-label">
            <i className="pbs-status-dot" />
            {t('pbs.gcTitle')}
          </span>
          <span className="pbs-status-value">{gcValue}</span>
          <span className="pbs-status-sub">{gcSub}</span>
        </div>
      </div>

      {/* 次要仓库：一行一条，不与主仓库抢「大数字」 */}
      {extra.length > 0 ? (
        <div className="pbs-store-rows">
          {extra.map((store) => (
            <StoreRow
              key={store.store}
              store={store}
              hint={[storeMeta(store, t), t('pbs.datastoreHint')].join('\n')}
            />
          ))}
        </div>
      ) : null}

      {/* 节点状态读不到（缺 Sys.Audit）时只提一句，不占一整栏 */}
      {conn.node?.status_available === false ? (
        <div className="fs-xs text-muted pbs-need-audit">
          {/* 缺 Sys.Audit 只是其中一种原因：节点名写错（PBS 回 400）也读不到，
              那时把上游原文摊出来，别一律赖到权限上 */}
          {conn.node.status_hint === 'no_permission'
            ? t('pbs.needSysAudit')
            : conn.node.status_error || t('pbs.stripNodeUnreadable')}
        </div>
      ) : null}
      {conn.node?.name_ignored ? (
        <div className="fs-xs text-muted">
          {t('pbs.nodeNameIgnored', {
            name: conn.node.name_ignored,
            used: conn.node.name,
          })}
        </div>
      ) : null}

      {/* 任务：默认 3 条，其余折叠 —— 备份页高频页，任务不是主角 */}
      <div className="pbs-tasks">
        <div className="pbs-tasks-head">
          <span className="pbs-tasks-title">
            <IconClock size={13} />
            {t('pbs.tasksTitle')}
          </span>
          <span className="fs-xs text-muted">{t('pbs.tasksFiltered')}</span>
          {taskVisible > visibleTasks ? (
            <button
              type="button"
              className="pbs-tasks-toggle"
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded
                ? t('pbs.tasksCollapse')
                : t('pbs.tasksExpand', { n: taskVisible })}
            </button>
          ) : null}
        </div>
        {conn.tasks.length > 0 ? (
          <ul className="pbs-task-list">
            {conn.tasks.slice(0, visibleTasks).map((task) => (
              <TaskRow
                key={task.upid}
                task={task}
                onOpen={() => onOpenTask(task)}
              />
            ))}
          </ul>
        ) : (
          /* 任务列表只需要 Datastore.Audit（不是 Sys.Audit）：这里读不到任务
             多半是缺 Datastore.Audit，而不是“真的没跑过任务”。 */
          <div className="fs-sm text-muted">
            {conn.node ? t('pbs.noTasks') : t('pbs.needSysAudit')}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- 多节点视图 */
/**
 * 多节点：顶部一条聚合带 + 一张节点对比表，点行展开该节点的完整面板。
 *
 * 为什么不直接把每个节点的整块面板堆起来：一个节点约 200px（主仓库 + 指标格 +
 * 状态条 + 任务），三台 PBS 就是一屏半，想知道「哪台有问题」得从头读到尾。
 * 多节点时只把能横向比较的列摆进表格（使用率 / 剩余 / 磁盘 / GC / 最近任务），
 * 完整明细按需展开；问题节点排最前，第一行默认展开。
 *
 * 新增节点只是多一行，布局无需改动；聚合带的数字全部现算，不额外维护状态。
 */
function PbsFleet({
  connections,
  thresholds,
  onOpenTask,
}: {
  connections: PbsConnectionOverview[];
  thresholds: { warn: number; crit: number };
  onOpenTask: (conn: PbsConnectionOverview, task: PbsTask) => void;
}) {
  const t = useT();
  const sorted = sortConnections(connections);
  /* 默认展开排序后的第一行：保证进来就能看到一份完整明细，
     同时表格本身仍可横向比较；其余节点点一下展开 */
  const [openIds, setOpenIds] = useState<string[]>(() =>
    sorted.length > 0 ? [sorted[0].id] : [],
  );
  const toggle = (id: string) =>
    setOpenIds((ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));

  /* 聚合：容量按仓库跨节点求和（加权使用率，而不是把各节点的百分比平均），
     这样「一台 90% 一台 10%」不会显示成 50% 的假象。 */
  /* 在线率只算「在采的」：暂停接入的连接后端根本不发请求，算进分母等于把
     一次主动停用显示成一台掉线 */
  const activeConns = connections.filter((conn) => !conn.paused);
  const pausedCount = connections.length - activeConns.length;
  const onlineCount = activeConns.filter((conn) => conn.online).length;
  const stores = connections.flatMap((conn) => conn.datastores);
  const knownStores = stores.filter((store) => store.total > 0);
  const totalBytes = knownStores.reduce((sum, store) => sum + store.total, 0);
  const usedBytes = knownStores.reduce((sum, store) => sum + store.used, 0);
  const usagePct = totalBytes > 0 ? Math.min(100, (usedBytes / totalBytes) * 100) : 0;
  const levels = stores.map(storeLevel);
  const worstLevel: 'crit' | 'warn' | null = levels.includes('crit')
    ? 'crit'
    : levels.includes('warn')
      ? 'warn'
      : null;
  const badDisks = connections.reduce(
    (sum, conn) => sum + (conn.node?.disks?.length ?? 0),
    0,
  );
  const diskKnown = connections.some((conn) => (conn.node?.disks_total ?? 0) > 0);
  const gcStates = connections.map(connGcState);
  const gcBad = gcStates.filter((state) => state === 'bad').length;
  const gcBusy = gcStates.filter((state) => state === 'busy').length;
  const gcKnown = gcStates.some((state) => state !== 'unknown');

  return (
    <>
      {/* 聚合带：整块面板先给一个「全部节点加起来怎么样」的结论 */}
      <div className="pbs-fleet">
        <span className={`pbs-fleet-chip${onlineCount < activeConns.length ? ' is-bad' : ''}`}>
          <i className="pbs-status-dot" />
          {t('pbs.fleetOnline', {
            online: onlineCount,
            total: activeConns.length,
          })}
        </span>
        {pausedCount > 0 ? (
          <span className="pbs-fleet-chip is-unknown" title={t('pbs.pausedHint')}>
            {t('pbs.fleetPaused', { n: pausedCount })}
          </span>
        ) : null}
        <span className="pbs-fleet-chip">{t('pbs.fleetStores', { n: stores.length })}</span>
        {totalBytes > 0 ? (
          <span className={`pbs-fleet-chip${worstLevel ? ` is-${worstLevel}` : ''}`}>
            {t('pbs.fleetCapacity', {
              used: formatBytes(usedBytes),
              total: formatBytes(totalBytes),
              pct: usagePct.toFixed(1),
            })}
          </span>
        ) : (
          <span className="pbs-fleet-chip is-unknown">{t('pbs.usageUnavailable')}</span>
        )}
        <span
          className={`pbs-fleet-chip is-${badDisks > 0 ? 'bad' : diskKnown ? 'ok' : 'unknown'}`}
        >
          <i className="pbs-status-dot" />
          {badDisks > 0
            ? t('pbs.fleetDiskBad', { n: badDisks })
            : diskKnown
              ? t('pbs.fleetDiskOk')
              : t('pbs.fleetDiskUnknown')}
        </span>
        <span
          className={`pbs-fleet-chip is-${
            gcBad > 0 ? 'bad' : gcBusy > 0 ? 'busy' : gcKnown ? 'ok' : 'unknown'
          }`}
        >
          <i className="pbs-status-dot" />
          {gcBad > 0
            ? t('pbs.fleetGcBad', { n: gcBad })
            : gcBusy > 0
              ? t('pbs.gcRunning')
              : gcKnown
                ? t('pbs.fleetGcOk')
                : t('pbs.fleetGcUnknown')}
        </span>
      </div>

      <div className="pbs-nodes-head">
        <span className="pbs-tasks-title">
          <IconServer size={13} />
          {t('pbs.fleetTitle')}
        </span>
        <span className="fs-xs text-muted">{t('pbs.fleetHint')}</span>
      </div>

      <ul className="pbs-node-list">
        {sorted.map((conn) => {
          const open = openIds.includes(conn.id);
          const health = connHealth(conn);
          const diskState = connDiskState(conn);
          const gcState = connGcState(conn);
          const nodeStores = conn.datastores.filter((store) => store.total > 0);
          const used = nodeStores.reduce((sum, store) => sum + store.used, 0);
          const total = nodeStores.reduce((sum, store) => sum + store.total, 0);
          const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
          const free = nodeStores.reduce(
            (sum, store) => sum + (store.free ?? store.avail ?? 0),
            0,
          );
          const lastTask = conn.tasks[0];
          /* 暂停接入：没采集不等于「未知」，更不等于「坏」—— 直接说已暂停 */
          const idleLabel = conn.paused ? t('pbs.paused') : t('pbs.diskUnknownShort');
          const diskFlag = !conn.online
            ? idleLabel
            : diskState === 'bad'
              ? t('pbs.diskAbnormal', { n: conn.node?.disks?.length ?? 0 })
              : diskState === 'ok'
                ? t('pbs.diskOk', { n: conn.node?.disks_summary?.count ?? conn.node?.disks_total ?? 0 })
                : t('pbs.diskUnknownShort');
          const gcFlag = !conn.online
            ? idleLabel
            : gcState === 'bad'
              ? t('pbs.taskFailed')
              : gcState === 'busy'
                ? t('pbs.gcRunning')
                : gcState === 'ok'
                  ? t('pbs.taskOk')
                  : t('pbs.gcNone');

          return (
            <li key={conn.id} className={`pbs-node is-${health}${open ? ' is-open' : ''}`}>
              <button
                type="button"
                className="pbs-node-row"
                aria-expanded={open}
                onClick={() => toggle(conn.id)}
              >
                <span className="pbs-node-id">
                  <i className={`pbs-health-dot is-${health}`} />
                  <span className="pbs-node-name" title={`${conn.host}:${conn.port}`}>
                    {conn.name}
                  </span>
                  <Badge
                    variant={conn.paused ? 'neutral' : conn.online ? 'success' : 'danger'}
                    size="sm"
                    dot
                    title={conn.paused ? t('pbs.pausedHint') : undefined}
                  >
                    {conn.paused
                      ? t('pbs.paused')
                      : conn.online
                        ? t('pbs.online')
                        : t('pbs.offline')}
                  </Badge>
                  {conn.version ? <span className="pbs-node-ver">v{conn.version}</span> : null}
                </span>

                <span className={`pbs-node-usage is-${worstStoreLevel(nodeStores)}`}>
                  <span className="pbs-node-bar">
                    <i style={{ width: `${Math.max(pct, 0.6)}%` }} />
                  </span>
                  <span className="pbs-node-pct mono">
                    {total > 0 ? `${pct.toFixed(1)}%` : '—'}
                  </span>
                </span>

                <span className="pbs-node-free mono">
                  {total > 0 ? t('pbs.nodeFree', { value: formatBytes(free) }) : '—'}
                </span>

                <span className={`pbs-node-flag is-${diskState}`}>
                  <i className="pbs-status-dot" />
                  {diskFlag}
                </span>

                <span className={`pbs-node-flag is-${gcState}`}>
                  <i className="pbs-status-dot" />
                  {`GC ${gcFlag}`}
                </span>

                <span className="pbs-node-task">
                  {lastTask
                    ? `${taskLabel(lastTask, t)} · ${formatRelative(lastTask.starttime)}`
                    : t('pbs.noTasks')}
                </span>

                <span className="pbs-node-caret" aria-hidden="true">
                  {open ? <IconChevronUp size={14} /> : <IconChevronDown size={14} />}
                </span>
              </button>

              {open ? (
                <div className="pbs-node-body">
                  {/* 行里放不下地址与控制台外链（整行是按钮，不能嵌 <a>），
                      这两样挪到展开区顶部 */}
                  <div className="pbs-node-meta">
                    <span className="mono">
                      {conn.host}:{conn.port}
                    </span>
                    {conn.shared_with && conn.shared_with.length > 0 ? (
                      <span>{t('pbs.sharedWith', { names: conn.shared_with.join('、') })}</span>
                    ) : null}
                    <a
                      className="btn-icon-only btn-ghost"
                      href={`https://${conn.host}:${conn.port}/`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={t('pbs.openConsole')}
                      aria-label={t('pbs.openConsole')}
                    >
                      <IconLink size={14} />
                    </a>
                  </div>
                  <ConnectionBlock
                    conn={conn}
                    thresholds={thresholds}
                    headless
                    onOpenTask={(task) => onOpenTask(conn, task)}
                  />
                </div>
              ) : null}
            </li>
          );
        })}
      </ul>
    </>
  );
}

export function PbsOverviewCard({ children }: { children?: ReactNode }) {
  const t = useT();
  const { hasPermission } = useAuth();
  const [logTarget, setLogTarget] = useState<{
    conn: PbsConnectionOverview;
    task: PbsTask;
  } | null>(null);

  /* pbs.view 默认只给管理员（PBS 数据没有“归属”维度）。没有权限就整块不请求，
     而不是请求后吃 403 —— 备份页是普通用户的高频页，不该多一条红字。 */
  const allowed = hasPermission('pbs.view');

  /* 跟随全站刷新设置（用户可以在顶栏选「不自动刷新」）；
     后端另有 20 秒缓存兜底，多人同时打不会穿到 PBS。 */
  const refresh = useRefreshInterval();

  /* 「更新于 x 分钟前」必须自走：关掉自动刷新时，相对时间不能冻在那一刻 */
  const [nowTick, setNowTick] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowTick(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  const query = useQuery({
    queryKey: ['pbs', 'overview'],
    queryFn: pbsApi.overview,
    enabled: allowed,
    staleTime: 20_000,
    refetchInterval: refresh,
    // 403 / 连接失败时不要反复重试：这一块是增值信息，不是页面主体
    retry: false,
  });

  /* 日志按需拉：只有点了任务行才请求，且不缓存（“还在跑”的任务日志会变） */
  const logQuery = useQuery({
    queryKey: ['pbs', 'task-log', logTarget?.conn.id, logTarget?.task.upid],
    queryFn: () =>
      pbsApi.taskLog({
        upid: logTarget!.task.upid,
        node: logTarget!.task.node || logTarget!.conn.node?.name,
        connectionId: logTarget!.conn.id,
      }),
    enabled: Boolean(logTarget),
    staleTime: 0,
    retry: false,
  });

  /* 三个「不渲染 PBS 卡片」的分支都必须把 children 交回去：
     备份 KPI 与 PBS 共用一张卡，PBS 缺席时 KPI 仍要留在页面上。 */
  if (!allowed) return <>{children}</>;
  const data = query.data;
  if (!data) return <>{children}</>;

  const pveStorages = data.pve_storages ?? [];
  /* 未对接：不渲染。用户没 PBS 时，备份页不该多出一张“你没配 PBS”的卡。 */
  if (!data.configured && pveStorages.length === 0) return <>{children}</>;

  /* 仅间接：PVE 侧已有 PBS 存储，但没有直连凭据。 */
  if (!data.configured) {
    return (
      <Card className="page-block">
        <CardHeader
          title={t('pbs.title')}
          subtitle={t('pbs.subtitleL1')}
          icon={<IconBackup size={16} />}
        />
        <div className="pbs-l1">
          <span className="fs-sm">
            {t('pbs.detected', {
              n: pveStorages.length,
              names: pveStorages.map((item) => item.storage).join('、'),
            })}
            {pveStorages.some((item) => (item.used_by?.length ?? 0) > 1)
              ? ` · ${t('pbs.usedBy', {
                  names: pveStorages
                    .map((item) => item.used_by?.join('、') ?? '')
                    .filter(Boolean)
                    .join(' / '),
                })}`
              : ''}
          </span>
          <Link className="btn btn-secondary btn-sm" to="/nodes/connections">
            {t('pbs.openConnections')}
          </Link>
        </div>
        {children ? <div className="pbs-kpis">{children}</div> : null}
      </Card>
    );
  }

  const connections = data.connections ?? [];
  /* 阈值由后端下发（默认 85 / 95）；界面提示里的数字与后端判定用的是同一组 */
  const thresholds = data.thresholds ?? { warn: 85, crit: 95 };
  /* 采集时刻优先用它：后端有 20 秒缓存，用响应到达时间会显示成「刚刚」。 */
  const updatedAt = data.generated_at
    ? data.generated_at * 1000
    : query.dataUpdatedAt;

  return (
    <>
      <Card className="page-block">
        <CardHeader
          title={t('pbs.title')}
          subtitle={t('pbs.subtitle')}
          icon={<IconBackup size={16} />}
          actions={
            <>
              {/* 数据新鲜度：相对时间 + 绝对时间（title），并给一个手动刷新入口 */}
              <span className="dash-updated" title={formatDateTime(updatedAt)}>
                {t('pbs.updatedAt', {
                  time: formatRelative(updatedAt, nowTick),
                })}
              </span>
              <Button
                variant="ghost"
                size="sm"
                icon={<IconRefresh size={14} />}
                disabled={query.isFetching}
                onClick={() => void query.refetch()}
              >
                {t('common.refresh')}
              </Button>
            </>
          }
        />
        {connections.length > 0 ? (
          /* 单节点保持原来的整块面板：没有可对比的对象，多一层表格只是噪音。
             两个及以上节点才切到「聚合带 + 节点对比表 + 按需展开」。 */
          connections.length === 1 ? (
            <ConnectionBlock
              conn={connections[0]}
              thresholds={thresholds}
              onOpenTask={(task) => setLogTarget({ conn: connections[0], task })}
            />
          ) : (
            <PbsFleet
              connections={connections}
              thresholds={thresholds}
              onOpenTask={(conn, task) => setLogTarget({ conn, task })}
            />
          )
        ) : (
          <div className="fs-sm text-muted">{t('pbs.noConnection')}</div>
        )}
        {/* 备份 KPI 与 PBS 监控同一张卡：这是「备份的整体样子」，
            拆成两块只是给页面多加一条分割线。 */}
        {children ? <div className="pbs-kpis">{children}</div> : null}
      </Card>

      <Modal
        open={Boolean(logTarget)}
        onClose={() => setLogTarget(null)}
        title={t('pbs.logTitle')}
        description={
          logTarget
            ? `${taskLabel(logTarget.task, t)} · ${logTarget.task.upid}`
            : undefined
        }
        size="lg"
      >
        {logQuery.isLoading ? (
          <div className="fs-sm text-muted">{t('pbs.logLoading')}</div>
        ) : logQuery.isError ? (
          <div className="fs-sm text-danger">
            {errorMessage(logQuery.error)}
          </div>
        ) : (
          <pre className="pbs-log">
            {(logQuery.data?.lines ?? []).join('\n') || t('pbs.logEmpty')}
          </pre>
        )}
      </Modal>
    </>
  );
}
