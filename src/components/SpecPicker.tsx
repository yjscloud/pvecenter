/* ==========================================================================
   pvecenter — 资源规格选择器（创建弹窗里的「快速部署」模式）

   规格是管理员在「设置 → 资源规格」里定义的一组数字（几核 / 几 G 内存 / 多大盘）。
   规格少的时候（≤ inlineMax，默认 6 档）就是一张平铺列表，点一下选中，不多一步。

   规格一多，一排卡片就不好用了：64 档铺成一片，找「4 核 8G」只能靠眼扫，而且
   卡片里的数字各写各的、横向比不了。所以这里改成**列表**，与「系统模板选择器」
   （SystemTemplatePicker）完全同一套视觉语言与交互：

   · **密集行**：行高 38px，字段横向成列（名称 / 核 / 内存 / 磁盘 / 说明），
     列名吸顶 —— 同一个字段上下对齐，扫一眼就能比出哪档更大。
   · **按档位分组**：核数是最先被拿来筛的维度，分组按「核 + 内存」落位（入门 /
     通用 / 进阶 / 高配），每组一条档位色带 + 图标 + 计数。分组只依赖规格本身的数字，
     不要求管理员额外填分类字段 —— 老配置照样分组。
   · **搜索**：名称、说明，以及 `4c8g` / `4核` / `8g内存` / `100g磁盘` 这类组合都能
     命中。运维脑子里想的是数字，不是规格名。
   · **分类 chip**：每个档位一个 chip（带条数），点一下只看那一档；搜索词与 chip
     同时生效，chip 的条数跟着搜索结果走。
   · **推荐**：由调用方算出 recommendedId（额度内最合适的一档 / 上次用过的那档），
     这里只负责把它标出来 —— 推荐规则属于业务，不该埋在展示组件里。
   · **额度不足**：blockedIds 里的行标一条「超出额度」。仍然可以点（用户可能只是
     想先看看），但要能一眼看出哪些现在下不了。

   选中态沿用模板列表的三条信号（左侧 2px 竖条、行底色、左侧实心圆点），三者都
   不占布局空间，切换时整列文字不会跟着位移。
   ========================================================================== */

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button } from './ui/Button';
import { Input } from './ui/Input';
import {
  IconCheck,
  IconChevronRight,
  IconCpu,
  IconLayers,
  IconSearch,
  IconServer,
  IconSparkle,
} from './Icons';
import { useT } from '../i18n';
import type { MessageKey } from '../i18n';
import type { ResourceSpec } from '../api/types';

/** 档位：按核数量级分组 */
type TierKey = 'basic' | 'standard' | 'performance' | 'ultra';

const TIER_ORDER: TierKey[] = ['basic', 'standard', 'performance', 'ultra'];

const TIER_LABEL: Record<TierKey, MessageKey> = {
  basic: 'specPicker.tier.basic',
  standard: 'specPicker.tier.standard',
  performance: 'specPicker.tier.performance',
  ultra: 'specPicker.tier.ultra',
};

/**
 * 档位图标：与模板选择器的 OsLogo 占同一个位置（行内 16px / 分组头 16px /
 * chip 13px），颜色不写死 —— 由 data-tier 给出的 --os-color 决定，于是同一个档位
 * 在筛选条、分组带、行内是同一个颜色（见 components.css 的 `.spec-pick [data-tier]`）。
 */
const TIER_GLYPH: Record<TierKey, (p: { size?: number }) => React.ReactElement> = {
  basic: IconLayers,
  standard: IconServer,
  performance: IconCpu,
  ultra: IconSparkle,
};

function TierMark({ tier, size = 16 }: { tier: TierKey; size?: number }) {
  const Glyph = TIER_GLYPH[tier];
  return (
    <span className="spec-tier-mark" aria-hidden="true">
      <Glyph size={size} />
    </span>
  );
}

/** 每个分组默认铺几行（超出部分收在「显示另外 N 档」后面） */
const GROUP_PREVIEW = 5;

/** GB → 「G」读数：规格存 MB，界面一律说 GB（与快速部署的摘要同一口径） */
const gb = (mb: number) => Math.round(mb / 1024);

/**
 * 档位落位：按**核 + 内存**一起判，不只按核。
 *
 * 只看核数的话，2C4G 会被塞进「入门」—— 可它是通用档里的经典尺寸（管理员起的
 * 名字通常就叫「通用型 2C4G」），于是分组带上的字和行里的名字当场打架。
 * 分界：
 *   · 入门：≤ 2 核且 ≤ 2G（1C1G / 1C2G / 2C2G）
 *   · 通用：3~4 核，或 ≤2 核但内存更大（2C4G / 4C8G / 4C16G）
 *   · 进阶：5~16 核（8C16G / 8C32G / 16C32G）
 *   · 高配：> 16 核（32C64G 起）
 */
function tierOf(cores: number, memoryMb: number): TierKey {
  if (cores <= 2 && gb(memoryMb) <= 2) return 'basic';
  if (cores <= 4) return 'standard';
  if (cores <= 16) return 'performance';
  return 'ultra';
}

/**
 * 搜索用的可匹配串：名称 + 说明 + 各种写法。
 *
 * 用户搜的是脑子里那几个数字：`4c`、`4核`、`8g`、`8g内存`、`4c8g` —— 全都命中，
 * 免得他还要猜「这个框到底搜哪些字段」。
 */
function haystack(spec: ResourceSpec): string {
  const mem = gb(spec.memory);
  return [
    spec.name,
    spec.description ?? '',
    `${spec.cores}c`,
    `${spec.cores}核`,
    `${spec.cores}vcpu`,
    `${mem}g`,
    `${mem}gb`,
    `${mem}g内存`,
    `${spec.disk}g`,
    `${spec.disk}gb`,
    `${spec.disk}g磁盘`,
    `${spec.cores}c${mem}g`,
    `${spec.cores}核${mem}g`,
  ]
    .join(' ')
    .toLowerCase();
}

export interface SpecPickerProps {
  /** 已按客户机类型过滤好的候选（vm / lxc 由调用方决定） */
  specs: ResourceSpec[];
  value: string;
  onChange: (id: string) => void;
  /** 推荐档（只加标记，不替用户选中） */
  recommendedId?: string;
  /** 当前额度下建不了的规格：行内标「超出额度」但仍可点 */
  blockedIds?: ReadonlySet<string>;
  /**
   * 硬性不可选的规格：id → 给用户看的原因（如「该模板系统盘 100G」）。
   *
   * 与 ``blockedIds`` 的区别是**能不能点**：额度不足只是提示（用户可能想先看
   * 看），磁盘装不下则选了也建不出能开机的机器，所以直接禁用。
   */
  unfitReasons?: Record<string, string>;
  /** 不超过这个数量时平铺，不给搜索与分类 chip */
  inlineMax?: number;
  disabled?: boolean;
}

export function SpecPicker({
  specs,
  value,
  onChange,
  recommendedId,
  blockedIds,
  unfitReasons,
  inlineMax = 6,
  disabled = false,
}: SpecPickerProps) {
  const t = useT();
  const groupName = `spec-pick-${useId().replace(/:/g, '')}`;
  const [search, setSearch] = useState('');
  const [tierFilter, setTierFilter] = useState<TierKey | 'all'>('all');
  /** 被收起的分组（默认全展开：分组本来就少，先藏起来反而多一次点击） */
  const [collapsed, setCollapsed] = useState<Set<TierKey>>(() => new Set());
  /** 点过「显示另外 N 档」的分组：展开到全部分组行 */
  const [showMore, setShowMore] = useState<Set<TierKey>>(() => new Set());
  const scrollRef = useRef<HTMLDivElement>(null);

  /* 候选集整个换掉（切虚拟机 / 切容器）时清掉筛选：上一类的搜索词在新集合里
     可能一条都匹配不上，留下来的只有一屏空白。 */
  const signature = specs.map((item) => item.id).join('|');
  const lastSignature = useRef(signature);
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setSearch('');
    setTierFilter('all');
    setCollapsed(new Set());
    setShowMore(new Set());
  }, [signature]);

  const inline = specs.length <= inlineMax;

  const matched = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = q ? specs.filter((item) => haystack(item).includes(q)) : specs;
    /* 升序排：同档内从小往大，扫视时是一条自然的阶梯 */
    return [...list].sort(
      (a, b) => a.cores - b.cores || a.memory - b.memory || a.disk - b.disk,
    );
  }, [specs, search]);

  const tiers = useMemo(() => {
    const counts = new Map<TierKey, number>();
    for (const item of matched) {
      const key = tierOf(item.cores, item.memory);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return TIER_ORDER.filter((key) => counts.has(key)).map((key) => ({
      key,
      count: counts.get(key) ?? 0,
    }));
  }, [matched]);

  /* 选中的档位在搜索后可能一档都不剩：按「全部」渲染，别给一屏空白 */
  const activeTier: TierKey | 'all' =
    tierFilter === 'all' || tiers.some((entry) => entry.key === tierFilter)
      ? tierFilter
      : 'all';

  const scoped = useMemo(
    () =>
      activeTier === 'all'
        ? matched
        : matched.filter((item) => tierOf(item.cores, item.memory) === activeTier),
    [matched, activeTier],
  );

  const groups = useMemo(() => {
    const buckets = new Map<TierKey, ResourceSpec[]>();
    for (const item of scoped) {
      const key = tierOf(item.cores, item.memory);
      const bucket = buckets.get(key);
      if (bucket) bucket.push(item);
      else buckets.set(key, [item]);
    }
    return TIER_ORDER.filter((key) => buckets.has(key)).map((key) => ({
      key,
      items: buckets.get(key) ?? [],
    }));
  }, [scoped]);

  const selected = specs.find((item) => item.id === value);
  const narrowing = search.trim().length > 0 || activeTier !== 'all';

  /* 选中的行在视野外时把它滚进来（已可见就不动：用户刚点的那行就在眼前，再滚
     一下只会晃）。分两种情况：
     · 有分组：对齐**分组头**——行居中的话，上方会留半行被切开的规格，看不出这
       组是哪一档；对齐时要减掉吸顶列名的高度。
     · 平铺（规格少 / 筛选后只剩一档）：只滚「差多少」，不把行顶到最上面 ——
       那会把上面几行整片推出视野，看着像跳了一下。
     用 getBoundingClientRect 差算偏移，而不是 offsetTop：后者的参照系是最近的定位
     祖先（这里是整张 Card），跟滚动容器差了一层，算出来的值会偏。 */
  useEffect(() => {
    const list = scrollRef.current;
    const row =
      list?.querySelector<HTMLInputElement>('input[type="radio"]:checked')
        ?.parentElement;
    if (!list || !row) return;
    const listRect = list.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    if (rowRect.top >= listRect.top && rowRect.bottom <= listRect.bottom) return;

    const section = row.closest<HTMLElement>('.os-pick-group');
    if (section) {
      const headH =
        list.querySelector<HTMLElement>('.os-pick-colhead')?.offsetHeight ?? 0;
      list.scrollTop = Math.max(
        0,
        list.scrollTop + section.getBoundingClientRect().top - listRect.top - headH - 2,
      );
      return;
    }
    list.scrollTop = Math.max(
      0,
      list.scrollTop +
        (rowRect.top < listRect.top
          ? rowRect.top - listRect.top - 2
          : rowRect.bottom - listRect.bottom + 2),
    );
  }, [value, inline]);

  const toggleTier = (key: TierKey) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderRow = (item: ResourceSpec) => {
    const checked = item.id === value;
    const tier = tierOf(item.cores, item.memory);
    const recommended = Boolean(recommendedId) && item.id === recommendedId;
    const blocked = Boolean(blockedIds?.has(item.id));
    /* 磁盘装不下：选了也建不出能开机的机器 —— 禁用，并把原因写在行内。
       额度不足仍允许点（用户可能只是先看看），两者不能混成一个状态。 */
    const unfit = unfitReasons?.[item.id];
    return (
      <label
        key={item.id}
        className={`os-row spec-pick-row${checked ? ' is-on' : ''}${
          disabled || unfit ? ' is-disabled' : ''
        }`}
        data-tier={tier}
        title={unfit ? `${item.description ? `${item.description} · ` : ''}${unfit}` : item.description}
      >
        <input
          type="radio"
          className="os-row-input"
          name={groupName}
          value={item.id}
          checked={checked}
          disabled={disabled || Boolean(unfit)}
          onChange={() => onChange(item.id)}
        />

        <span className="os-row-radio" aria-hidden="true" />
        <TierMark tier={tier} />

        <span className="os-row-name" title={item.name}>
          {item.name}
        </span>

        {/* 三个数字各自成列、右对齐、等宽：与列名共用一套网格，上下必然是齐的。
            核数永远写全「2核」—— 列名只说 vCPU，单看一个 2 不知道是核还是别的；
            内存 / 磁盘的单位交给列名，窄屏列名收起后再由 <i> 补回来 */}
        <span className="spec-pick-nums">
          <span className="spec-pick-num">
            {item.cores}
            <i className="spec-pick-unit-core">
              {t('quickDeploy.specUnitCores')}
            </i>
          </span>
          <span className="spec-pick-num">
            {gb(item.memory)}
            <i>{t('quickDeploy.specUnitMem')}</i>
          </span>
          <span className="spec-pick-num">
            {item.disk}
            <i>{t('quickDeploy.specUnitDisk')}</i>
          </span>
        </span>

        <span className="spec-pick-note" title={item.description}>
          {unfit || item.description || '—'}
        </span>

        <span className="os-row-tags">
          {/* 磁盘装不下时不再标「推荐」：推荐的前提是这一档现在能选，
              两个标签并排还会把 88px 的标签列挤爆。 */}
          {recommended && !unfit ? (
            <span className="os-row-tag is-accent">
              {t('specPicker.recommended')}
            </span>
          ) : null}
          {unfit ? (
            <span className="os-row-tag is-warn">
              {t('specPicker.diskTooSmall')}
            </span>
          ) : blocked ? (
            <span className="os-row-tag is-warn">
              {t('specPicker.overQuota')}
            </span>
          ) : null}
        </span>
      </label>
    );
  };

  return (
    <div className="spec-pick os-pick is-compact">
      {/* ---- 工具条：标题 + 总数 + 搜索（规格少时不需要搜索） ---- */}
      <div className="os-pick-bar">
        <span className="os-pick-title">{t('specPicker.title')}</span>
        <span className="os-pick-count">
          {t('specPicker.count', { n: scoped.length })}
        </span>
        {!inline ? (
          <div className="os-pick-search">
            <Input
              prefix={<IconSearch size={14} />}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                /* 搜东西时把收起的分组放开：否则命中的行还藏在折叠头后面 */
                if (e.target.value.trim()) setCollapsed(new Set());
              }}
              placeholder={t('specPicker.search')}
              aria-label={t('specPicker.searchAria')}
            />
          </div>
        ) : null}
      </div>

      {/* ---- 分类 chip：只有一个档位时不铺（点了也不会有任何变化） ---- */}
      {!inline && tiers.length > 1 ? (
        <div
          className="os-pick-chips"
          role="group"
          aria-label={t('specPicker.filterAria')}
        >
          <button
            type="button"
            className={`os-chip${activeTier === 'all' ? ' is-on' : ''}`}
            aria-pressed={activeTier === 'all'}
            onClick={() => {
              setTierFilter('all');
              setCollapsed(new Set());
            }}
          >
            {t('specPicker.filterAll')}
            <span className="os-chip-count">{matched.length}</span>
          </button>
          {tiers.map((entry) => (
            <button
              key={entry.key}
              type="button"
              className={`os-chip${activeTier === entry.key ? ' is-on' : ''}`}
              data-tier={entry.key}
              aria-pressed={activeTier === entry.key}
              onClick={() => {
                setTierFilter(entry.key);
                setCollapsed(new Set());
              }}
            >
              <TierMark tier={entry.key} size={13} />
              {t(TIER_LABEL[entry.key])}
              <span className="os-chip-count">{entry.count}</span>
            </button>
          ))}
        </div>
      ) : null}

      {/* ---- 列表：列名与分组头两层吸顶，列表自己滚动封顶 ---- */}
      {scoped.length ? (
        <div
          className="os-pick-list"
          ref={scrollRef}
          role="group"
          aria-label={t('specPicker.listAria')}
        >
          <div className="os-pick-colhead" aria-hidden="true">
            <span />
            <span />
            <span>{t('specPicker.colName')}</span>
            <span className="spec-pick-colnums">
              <span>{t('specPicker.colCores')}</span>
              <span>{t('specPicker.colMemory')}</span>
              <span>{t('specPicker.colDisk')}</span>
            </span>
            <span className="spec-pick-colnote">
              {t('specPicker.colNote')}
            </span>
            <span />
          </div>

          {/* 只剩一个分组时（规格少，或筛选后只剩一档）不再摆分组头：
              一行标题 + 计数说的都是「就是这些」，只是多占一行 */}
          {inline || groups.length === 1
            ? scoped.map(renderRow)
            : groups.map((group) => {
                const isCollapsed = collapsed.has(group.key);
                const containsSelected = Boolean(
                  selected && tierOf(selected.cores, selected.memory) === group.key,
                );
                /* 搜索 / 选分类 / 选中项在组内时全展开：三种情况用户都在「找东西」 */
                const showAll =
                  narrowing ||
                  containsSelected ||
                  showMore.has(group.key) ||
                  group.items.length <= GROUP_PREVIEW;
                const rows = showAll
                  ? group.items
                  : group.items.slice(0, GROUP_PREVIEW);
                const hidden = group.items.length - rows.length;

                return (
                  <section className="os-pick-group" key={group.key}>
                    <button
                      type="button"
                      className="os-pick-group-head"
                      data-tier={group.key}
                      aria-expanded={!isCollapsed}
                      onClick={() => toggleTier(group.key)}
                    >
                      <span className="os-pick-caret" aria-hidden="true">
                        <IconChevronRight size={12} />
                      </span>
                      <TierMark tier={group.key} />
                      <span className="os-pick-group-name">
                        {t(TIER_LABEL[group.key])}
                      </span>
                      <span className="os-pick-group-count">
                        {group.items.length}
                      </span>
                      <span className="os-pick-group-rule" aria-hidden="true" />
                    </button>

                    {isCollapsed ? null : (
                      <div className="os-pick-rows">
                        {rows.map(renderRow)}
                        {hidden > 0 ? (
                          <button
                            type="button"
                            className="os-pick-more"
                            onClick={() =>
                              setShowMore((prev) => {
                                const next = new Set(prev);
                                next.add(group.key);
                                return next;
                              })
                            }
                          >
                            {t('specPicker.expand', { n: hidden })}
                          </button>
                        ) : null}
                      </div>
                    )}
                  </section>
                );
              })}
        </div>
      ) : (
        <div className="os-pick-empty">
          <div>
            <div className="os-pick-empty-title">
              {t('specPicker.emptyTitle')}
            </div>
            <div className="os-pick-empty-body">
              {t('specPicker.emptyBody')}
            </div>
          </div>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setSearch('');
              setTierFilter('all');
            }}
          >
            {t('specPicker.clearFilter')}
          </Button>
        </div>
      )}

      {/* ---- 已选摘要：滚出视野 / 被筛选藏起来时仍能确认 ----
          平铺态（规格很少）整列都在视野里，这行只是重复，省掉 */}
      {selected && !inline ? (
        <div className="os-pick-foot" role="status">
          <span className="os-pick-foot-icon" aria-hidden="true">
            <IconCheck size={10} />
          </span>
          <span>{t('specPicker.selected')}</span>
          <strong>{selected.name}</strong>
          <span className="os-pick-foot-meta">
            {t('quickDeploy.summary', {
              cores: selected.cores,
              mem: gb(selected.memory),
              disk: t('quickDeploy.summaryDisk', { n: selected.disk }),
            })}
          </span>
        </div>
      ) : null}
    </div>
  );
}
