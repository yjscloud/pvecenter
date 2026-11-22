/* ==========================================================================
   pvecenter — 创建流程里的系统模板快速选择器

   场景：创建虚拟机（克隆源）与创建容器（系统模板）时选一张模板。与重装
   （ReinstallTemplatePicker）的差别在于「没有原系统可比」，相同的是两个
   决定性的约束：

   1. 模板可能很多（几十上百张），也可能只有两三张；
   2. 选模板是创建流程里唯一需要「看清楚再决定」的一步 —— 名字、发行版、
      容量、落点都要能横向比较。

   所以这里不是重装那套的复制品，而是按规模自适应：

   · **模板少（≤ inlineMax，默认 4 张）**：直接平铺成行，没有搜索、没有分类
     chip、没有分组头、没有「更换」这一步 —— 一共四行，多一次点击都是浪费。
   · **模板多**：同样直接把可选列表铺出来（搜索 / 分类 chip / 分组 / 折叠行），
     与重装选模板完全同一套视觉语言，用户不用学第二遍。

   **不预选、不替用户决定装哪套系统。** 调用方不会给默认值，所以这里也没有
   「已选」态可言：没选中值时列表常驻，选中一行之后才收成一张摘要卡（系统、
   版本、容量、节点/存储），并留一条「更换模板」把它重新展开 —— 收起只是把
   占位还给表单，不是把决定权收回去。选错的代价要到开机才看得见，所以这一步
   必须是用户亲自点的。

   列表细节沿用重装那套密集行：一行 38px、字段横向对齐、内部滚动封顶、
   每个分组默认铺 5 行、搜索/选分类时自动全展开。选择结果只回传 value：
   虚拟机是 `node:vmid`，容器是 `volid`，本组件不引入第二套标识。
   ========================================================================== */

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button } from './ui/Button';
import { Input } from './ui/Input';
import {
  IconCheck,
  IconChevronRight,
  IconChevronUp,
  IconSearch,
} from './Icons';
import { OsLogo } from './OsLogo';
import { normalizeSizeText } from '../utils/format';
import { useT } from '../i18n';
import {
  groupTemplates,
  osFamilyLabel,
  osFamilyScene,
  templateFamily,
  type OsFamily,
  type OsFamilyKey,
} from '../utils/osFamily';
import type { TemplatePickItem } from '../utils/templatePick';

export interface SystemTemplatePickerProps {
  /**
   * 候选来自哪种模板：只影响右侧两列的列名（内存 / 磁盘 vs 大小 / 存储）。
   * 由组件自己决定文案，而不是让调用方传字符串 —— 四个入口（快速部署 / 自定义
   * 部署 × 虚拟机 / 容器）传进来的必须一模一样，否则又会长出两套说法。
   */
  kind: 'vm' | 'ct';
  items: TemplatePickItem[];
  value: string;
  onChange: (value: string) => void;
  /** 不超过这个数量时直接平铺，不做摘要卡 */
  inlineMax?: number;
  disabled?: boolean;
}

/** 每个分组默认铺几行 */
const GROUP_PREVIEW = 5;

/** 名称里的第一段数字：`ubuntu-24.04` → 24.04、`centos7` → 7 */
function versionOf(name: string): string {
  const hit = /\d+(?:\.\d+)*/.exec(String(name ?? ''));
  return hit ? hit[0] : '';
}

export function SystemTemplatePicker({
  kind,
  items,
  value,
  onChange,
  inlineMax = 4,
  disabled = false,
}: SystemTemplatePickerProps) {
  const t = useT();
  const sizeLabel =
    kind === 'vm' ? t('templatePicker.colMem') : t('templatePicker.colSize');
  const metaLabel =
    kind === 'vm' ? t('templatePicker.colDisk') : t('templatePicker.colStorage');
  const groupName = `template-pick-${useId().replace(/:/g, '')}`;
  const [search, setSearch] = useState('');
  const [familyFilter, setFamilyFilter] = useState<OsFamilyKey | 'all'>('all');
  const [collapsed, setCollapsed] = useState<Set<OsFamilyKey>>(() => new Set());
  const [expanded, setExpanded] = useState<Set<OsFamilyKey>>(() => new Set());
  /** 多模板场景下是否展开完整列表（没选中时无论如何都会展开，见 showList） */
  const [listOpen, setListOpen] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  /* 候选集整个换掉（切主机 / 切节点）时把筛选状态清干净：上一台的搜索词或
     分类在整个新集合里可能一条都匹配不上，留下来的只有一屏空白。 */
  const signature = items.map((item) => item.value).join('|');
  const lastSignature = useRef(signature);
  useEffect(() => {
    if (lastSignature.current === signature) return;
    lastSignature.current = signature;
    setSearch('');
    setFamilyFilter('all');
    setCollapsed(new Set());
    setExpanded(new Set());
    setListOpen(false);
  }, [signature]);

  const matched = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) =>
      `${item.name} ${item.keywords ?? ''}`.toLowerCase().includes(q),
    );
  }, [items, search]);

  const families = useMemo(() => {
    const counts = new Map<OsFamilyKey, { family: OsFamily; count: number }>();
    for (const item of matched) {
      const family = templateFamily(item);
      const hit = counts.get(family.key);
      if (hit) hit.count += 1;
      else counts.set(family.key, { family, count: 1 });
    }
    return [...counts.values()].sort((a, b) => a.family.rank - b.family.rank);
  }, [matched]);

  /* 选中的分类在搜索后可能一张都不剩：按「全部」渲染，别给一屏空白 */
  const activeFamily: OsFamilyKey | 'all' =
    familyFilter === 'all' || families.some((entry) => entry.family.key === familyFilter)
      ? familyFilter
      : 'all';

  const scoped = useMemo(
    () =>
      activeFamily === 'all'
        ? matched
        : matched.filter((item) => templateFamily(item).key === activeFamily),
    [matched, activeFamily],
  );

  const groups = useMemo(() => groupTemplates(scoped), [scoped]);

  /* 模板很少时直接平铺：按家族排好序，但不显示分组头（四行分三组只会更乱） */
  const inline = items.length <= inlineMax;
  const flat = useMemo(() => groupTemplates(items).flatMap((group) => group.items), [items]);

  /* 摘要读全集：模板被当前筛选藏起来时，用户仍要看到自己选的是哪一张 */
  const selected = items.find((item) => item.value === value);
  const showList = inline || listOpen || !selected;
  const narrowing = search.trim().length > 0 || activeFamily !== 'all';

  /* 列表出现时把选中的行滚进可视区（默认选中的那张常在第三、四个分组） */
  useEffect(() => {
    if (!showList) return;
    const list = listRef.current;
    const input = list?.querySelector<HTMLInputElement>('input[type="radio"]:checked');
    const row = input?.closest('label');
    if (!list || !row) return;
    const top = (row as HTMLElement).offsetTop;
    const rowH = (row as HTMLElement).offsetHeight;
    /* 只滚到「刚好露出」而不是居中：居中会把上方两组分类推进吸顶区，
       一打开列表就看到两个分组头贴在一起，像两个空分类。 */
    if (top + rowH > list.clientHeight - 8) {
      list.scrollTop = Math.max(0, top - list.clientHeight + rowH + 40);
    }
  }, [showList]);

  const toggleCollapsed = (key: OsFamilyKey) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  if (!items.length) return null;

  /* ---- 已选态：一张「将使用此模板」摘要卡（选中并收起列表之后才出现） ---- */
  if (!showList && selected) {
    const family = templateFamily(selected);
    const version = versionOf(selected.name);
    return (
      <div className="os-current-wrap">
        <div className="os-current" data-os={family.key}>
          <span className="tpl-os-mark">
            <OsLogo family={family} />
          </span>

          <div className="os-current-text">
            <div className="os-current-line">
              <span className="os-current-kicker">
                {t('templatePicker.selectedKicker')}
              </span>
              <strong className="os-current-name" title={selected.name}>
                {selected.name}
              </strong>
              {version ? <span className="os-current-ver">v{version}</span> : null}
            </div>
            <div className="os-current-meta">
              {osFamilyLabel(family, t)}
              {selected.sizeText
                ? ` · ${sizeLabel} ${normalizeSizeText(selected.sizeText)}`
                : ''}
              {selected.metaText
                ? ` · ${metaLabel} ${normalizeSizeText(selected.metaText)}`
                : ''}
            </div>
            <div className="os-current-scene">
              {selected.note ?? osFamilyScene(family, t)}
            </div>
          </div>

          <div className="os-current-actions">
            <Button
              size="sm"
              variant="secondary"
              iconRight={<IconChevronRight size={14} />}
              onClick={() => setListOpen(true)}
              disabled={disabled}
            >
              {t('templatePicker.change')}
            </Button>
          </div>
        </div>

        <div className="os-current-note">{t('templatePicker.note')}</div>
      </div>
    );
  }

  const renderRow = (item: TemplatePickItem) => {
    const family = templateFamily(item);
    const checked = item.value === value;
    return (
      <label
        key={item.value}
        className={`os-row${checked ? ' is-on' : ''}${
          disabled ? ' is-disabled' : ''
        }`}
        data-os={family.key}
      >
        <input
          type="radio"
          className="os-row-input"
          name={groupName}
          value={item.value}
          checked={checked}
          disabled={disabled}
          onChange={() => {
            /* 点完一行不要把列表收成摘要卡：展开态交给用户（右上角的「收起」），
               否则「从一堆里挑一个」的动线会在点中的瞬间被打断，想改还得再展开 */
            setListOpen(true);
            onChange(item.value);
          }}
        />

        <span className="os-row-radio" aria-hidden="true" />
        <span className="os-row-logo">
          <OsLogo family={family} size="table" />
        </span>
        <span className="os-row-name" title={item.name}>
          {item.name}
        </span>
        {/* 版本列已去掉：名字里本来就有 v11 / v24，再列一列只是把同一句话写两遍 */}
        <span className="os-row-scene" title={osFamilyScene(family, t)}>
          {osFamilyScene(family, t)}
        </span>
        <span className="os-row-disk">{normalizeSizeText(item.sizeText)}</span>
        <span className="os-row-used">{normalizeSizeText(item.metaText)}</span>
        <span className="os-row-tags">
          {(item.tags ?? []).map((tag) => (
            <span className="os-row-tag" key={tag}>
              {tag}
            </span>
          ))}
        </span>
      </label>
    );
  };

  return (
    // is-tpl：本选择器的列模板（7 列，无版本列）只在它身上生效 ——
    // 重装 / 更换模板两个选择器仍是 8 列，共用 .os-pick-list 但不能跟着变
    <div className="os-pick is-compact is-tpl">
      {/* ---- 工具条：平铺态只留标题与总数，多模板时才给搜索与收起 ---- */}
      <div className="os-pick-bar">
        <span className="os-pick-title">{t('templatePicker.listTitle')}</span>
        <span className="os-pick-count">
          {t('templatePicker.count', { n: scoped.length })}
        </span>
        {!inline ? (
          <>
            <div className="os-pick-search">
              <Input
                prefix={<IconSearch size={14} />}
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  if (e.target.value.trim()) setCollapsed(new Set());
                }}
                placeholder={t('templatePicker.search')}
                aria-label={t('templatePicker.searchAria')}
              />
            </div>
            {selected ? (
              <button
                type="button"
                className="os-pick-collapse"
                onClick={() => setListOpen(false)}
                aria-label={t('templatePicker.collapse')}
                title={t('templatePicker.collapse')}
              >
                <IconChevronUp size={14} />
              </button>
            ) : null}
          </>
        ) : null}
      </div>

      {/* ---- 分类 chip：只有一个系统时不铺（点了也不会有任何变化） ---- */}
      {!inline && families.length > 1 ? (
        <div
          className="os-pick-chips"
          role="group"
          aria-label={t('templatePicker.filterAria')}
        >
          <button
            type="button"
            className={`os-chip${activeFamily === 'all' ? ' is-on' : ''}`}
            aria-pressed={activeFamily === 'all'}
            disabled={disabled}
            onClick={() => {
              setFamilyFilter('all');
              setCollapsed(new Set());
            }}
          >
            {t('templatePicker.filterAll')}
            <span className="os-chip-count">{matched.length}</span>
          </button>
          {families.map((entry) => (
            <button
              type="button"
              key={entry.family.key}
              className={`os-chip${activeFamily === entry.family.key ? ' is-on' : ''}`}
              data-os={entry.family.key}
              aria-pressed={activeFamily === entry.family.key}
              disabled={disabled}
              onClick={() => {
                setFamilyFilter(entry.family.key);
                setCollapsed(new Set());
              }}
            >
              <OsLogo family={entry.family} size="chip" />
              {osFamilyLabel(entry.family, t)}
              <span className="os-chip-count">{entry.count}</span>
            </button>
          ))}
        </div>
      ) : null}

      {/* ---- 列表 ---- */}
      {scoped.length ? (
        <div
          className="os-pick-list"
          ref={listRef}
          role="group"
          aria-label={t('templatePicker.listAria')}
        >
          <div className="os-pick-colhead" aria-hidden="true">
            <span />
            <span />
            <span>{t('templatePicker.colName')}</span>
            <span className="is-scene">{t('templatePicker.colScene')}</span>
            <span className="is-disk">{sizeLabel}</span>
            <span className="is-used">{metaLabel}</span>
            <span />
          </div>

          {inline
            ? flat.map(renderRow)
            : groups.map((group) => {
                const isCollapsed = collapsed.has(group.family.key);
                const containsSelected = group.items.some(
                  (item) => item.value === value,
                );
                const showAll =
                  narrowing ||
                  containsSelected ||
                  expanded.has(group.family.key) ||
                  group.items.length <= GROUP_PREVIEW;
                const rows = showAll
                  ? group.items
                  : group.items.slice(0, GROUP_PREVIEW);
                const hiddenRows = group.items.length - rows.length;

                return (
                  <section className="os-pick-group" key={group.family.key}>
                    <button
                      type="button"
                      className="os-pick-group-head"
                      data-os={group.family.key}
                      aria-expanded={!isCollapsed}
                      onClick={() => toggleCollapsed(group.family.key)}
                    >
                      <span className="os-pick-caret" aria-hidden="true">
                        <IconChevronRight size={12} />
                      </span>
                      <OsLogo family={group.family} size="group" />
                      <span className="os-pick-group-name">
                        {osFamilyLabel(group.family, t)}
                      </span>
                      {/* 分组计数去掉了：标题旁、chip 上已经各有一遍，第三遍只添噪音 */}
                      <span className="os-pick-group-rule" aria-hidden="true" />
                    </button>

                    {isCollapsed ? null : (
                      <div className="os-pick-rows">
                        {rows.map(renderRow)}
                        {hiddenRows > 0 ? (
                          <button
                            type="button"
                            className="os-pick-more"
                            onClick={() =>
                              setExpanded((prev) => {
                                const next = new Set(prev);
                                next.add(group.family.key);
                                return next;
                              })
                            }
                          >
                            {t('templatePicker.expand', { n: hiddenRows })}
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
              {t('templatePicker.emptyTitle')}
            </div>
            <div className="os-pick-empty-body">
              {t('templatePicker.emptyBody')}
            </div>
          </div>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              setSearch('');
              setFamilyFilter('all');
            }}
          >
            {t('templatePicker.clearFilter')}
          </Button>
        </div>
      )}

      {/* ---- 已选摘要：滚出视野也能确认选的是哪一张 ----
          平铺态（模板很少）整列都在视野里，这行只是重复，省掉 */}
      {selected && !inline ? (
        <div className="os-pick-foot" role="status">
          <span className="os-pick-foot-icon" aria-hidden="true">
            <IconCheck size={10} />
          </span>
          <span>{t('templatePicker.selected')}</span>
          <strong>{selected.name}</strong>
          <span className="os-pick-foot-meta">
            {osFamilyLabel(templateFamily(selected), t)}
            {selected.sizeText
              ? ` · ${sizeLabel} ${normalizeSizeText(selected.sizeText)}`
              : ''}
            {selected.metaText
              ? ` · ${metaLabel} ${normalizeSizeText(selected.metaText)}`
              : ''}
          </span>
        </div>
      ) : null}
    </div>
  );
}
