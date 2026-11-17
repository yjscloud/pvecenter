/* ==========================================================================
   pvecenter — 备注弹窗里的「来源模板」选择器

   这是备注（IP / 系统标识 / 来源模板）里的第三个字段：手工告诉面板「这台机器
   是从哪张模板来的」。恢复备份、在 PVE 界面克隆、装 ISO 建的机器审计里没有
   克隆记录，只有靠它，列表的系统徽标与重装推荐才认得出来。

   为什么不用原生 <select>：
   模板一多（几十上百张，还分布在多个节点的存储里）原生下拉就变成一条没有搜索、
   没有分类、也不能横向比较的长条 —— 用户得靠鼠标滚轮在几十行里找一个名字。
   这里复用产品里已有的「模板选择器」视觉语言（.os-pick 那一套：搜索 + 分类
   chip + 按系统分组 + 密集行），用户在创建 / 重装流程里已经见过一次，不用学
   第二遍。

   交互（路径尽量短）：
   1. 收起态：一行触发条显示当前关联（logo + 名称 + 节点/VMID），点一下展开；
   2. 展开态：搜索框自动聚焦，输入即筛（空格分隔按「与」处理，与列表页搜索
      同一套规则）；分类 chip 按系统家族过滤；分组头可折叠；
   3. 选中即收起 —— 不用再确认一次，保存按钮才是提交的那一步；
   4. 键盘：↑/↓ 选行、Enter 选中、Esc 收起面板（**不关整个弹窗**）。

   一个容易踩的点：Esc。弹窗自己监听 document 上的 keydown（capture 阶段）来
   关闭，而 `document` 的 capture 早于任何子元素。所以在面板打开期间，本组件在
   **window** 的 capture 阶段截住 Esc —— 那是链路上最早的一环，弹窗收不到它，
   用户按 Esc 只会收起面板，不会把填了一半的备注一起关掉。
   ========================================================================== */

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react';
import { Input } from './ui/Input';
import { OsLogo } from './OsLogo';
import { IconChevronDown, IconChevronRight, IconSearch } from './Icons';
import { useT } from '../i18n';
import {
  groupTemplates,
  osFamilyLabel,
  templateFamily,
  type OsFamily,
  type OsFamilyKey,
} from '../utils/osFamily';

/** 一张可选作来源的模板。`value` 与后端 `vm_meta` 的定位方式一致：`节点|VMID`。 */
export interface SourceTemplateOption {
  value: string;
  name: string;
  node: string;
  vmid: number;
}

export interface SourceTemplatePickerProps {
  options: SourceTemplateOption[];
  /** 空串 = 不关联（回落到克隆记录推断） */
  value: string;
  onChange: (value: string) => void;
  /**
   * 存档里关联的那张模板（可能已被删除、或已被改回普通机器，因而不在 options
   * 里）。它必须仍然出现在列表中并保持选中，否则打开弹窗时草稿落不到任何选项，
   * 看起来像「关联凭空消失」，一保存就真丢了。
   */
  archived?: { node: string; vmid: number; name: string } | null;
  disabled?: boolean;
  loading?: boolean;
  /** 读取失败的原因；只影响列表区域，已保存的关联照常显示 */
  error?: string | null;
  label?: string;
  /** 触发条下方的说明 */
  hint?: string;
}

/** 列表里的行：多一个「已不在模板列表」标记 */
interface Row extends SourceTemplateOption {
  gone?: boolean;
}

const SEARCH_SPLIT = /\s+/;

export function SourceTemplatePicker({
  options,
  value,
  onChange,
  archived = null,
  disabled = false,
  loading = false,
  error = null,
  label,
  hint,
}: SourceTemplatePickerProps) {
  const t = useT();
  const uid = useId().replace(/:/g, '');
  const groupName = `tpl-link-${uid}`;
  const panelId = `tpl-link-panel-${uid}`;

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [family, setFamily] = useState<OsFamilyKey | 'all'>('all');
  const [collapsed, setCollapsed] = useState<Set<OsFamilyKey>>(() => new Set());
  /** 键盘光标所在行（-1 = 没有） */
  const [active, setActive] = useState(-1);
  const listRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  /* 候选全集 = 接口回来的模板 + 存档里那张（不在清单里时补一行） */
  const all = useMemo<Row[]>(() => {
    const rows: Row[] = options.map((item) => ({ ...item }));
    if (
      archived &&
      !rows.some(
        (item) => item.node === archived.node && item.vmid === archived.vmid,
      )
    ) {
      rows.push({
        value: `${archived.node}|${archived.vmid}`,
        name: archived.name?.trim() || `#${archived.vmid}`,
        node: archived.node,
        vmid: archived.vmid,
        gone: true,
      });
    }
    return rows;
  }, [options, archived]);

  const current = all.find((item) => item.value === value) ?? null;

  /* 搜索：空格分隔按「与」处理；名称、节点、VMID、系统名都参与匹配。
     模板名里带版本号（CentOS7.9 / ubuntu24），所以不做前缀匹配，只做包含。 */
  const matched = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(SEARCH_SPLIT).filter(Boolean);
    if (!tokens.length) return all;
    return all.filter((item) => {
      const familyLabel = osFamilyLabel(templateFamily(item), t);
      const haystack =
        `${item.name} ${item.node} ${item.vmid} ${familyLabel}`.toLowerCase();
      return tokens.every((token) => haystack.includes(token));
    });
  }, [all, query, t]);

  const families = useMemo(() => {
    const counts = new Map<OsFamilyKey, { family: OsFamily; count: number }>();
    for (const item of matched) {
      const hit = counts.get(templateFamily(item).key);
      if (hit) hit.count += 1;
      else counts.set(templateFamily(item).key, { family: templateFamily(item), count: 1 });
    }
    return [...counts.values()].sort((a, b) => a.family.rank - b.family.rank);
  }, [matched]);

  /* 选中的分类在搜索后可能一张都不剩：退回「全部」，别给一屏空白 */
  const activeFamily: OsFamilyKey | 'all' =
    family === 'all' || families.some((entry) => entry.family.key === family)
      ? family
      : 'all';

  const scoped = useMemo(
    () =>
      activeFamily === 'all'
        ? matched
        : matched.filter((item) => templateFamily(item).key === activeFamily),
    [matched, activeFamily],
  );

  const groups = useMemo(() => groupTemplates(scoped), [scoped]);

  /* 渲染计划：折叠起来的分组不贡献行。行下标与键盘光标都以此为准 ——
     两处各算一遍，迟早会出现「↑/↓ 跳到看不见的行上」。 */
  const plan = useMemo(
    () =>
      groups.map((group) => ({
        group,
        items: collapsed.has(group.family.key) ? [] : group.items,
      })),
    [groups, collapsed],
  );

  /* 键盘顺序 = 「不关联」（下标 0）+ 各分组可见行，与 DOM 顺序完全一致 */
  const flat = useMemo<(Row | null)[]>(
    () => [null, ...plan.flatMap((entry) => entry.items)],
    [plan],
  );

  /* 每个分组第一行的扁平下标：折叠的分组不占位，所以只能按渲染顺序累加 */
  const groupOffsets = useMemo(() => {
    const offsets = new Map<OsFamilyKey, number>();
    let cursor = 1;
    for (const entry of plan) {
      offsets.set(entry.group.family.key, cursor);
      cursor += entry.items.length;
    }
    return offsets;
  }, [plan]);

  const narrowing = query.trim().length > 0 || activeFamily !== 'all';

  /* ---- 打开：清筛选、把光标放到当前值上、把面板滚进视野 ---- */
  const openPanel = useCallback(() => {
    setQuery('');
    setFamily('all');
    setCollapsed(new Set());
    setOpen(true);
  }, []);

  /* 面板展开后把光标定位到当前选中行；行渲染出来才能算下标，所以放在打开后 */
  useEffect(() => {
    if (!open) return;
    const index = flat.findIndex((item) => (item ? item.value : '') === value);
    setActive(index >= 0 ? index : 0);
    // 只在展开的瞬间定位一次：之后 ↑/↓ 由键盘自己维护
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  /* Esc：见文件头注释 —— 必须在 window 的 capture 阶段截住，否则弹窗先关 */
  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [open]);

  /* 光标行跟着滚动（列表自己滚动，不让弹窗整体跳） */
  useEffect(() => {
    if (!open || active < 0) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-row="${active}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [open, active]);

  /* 展开后把面板滚进视野：备注弹窗里这一块在最后一个字段，手机上常常在屏幕外 */
  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus({ preventScroll: true });
    document
      .getElementById(panelId)
      ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [open, panelId]);

  const pick = (next: string) => {
    onChange(next);
    setOpen(false);
  };

  const move = (delta: number) => {
    if (!flat.length) return;
    setActive((prev) => {
      const start = prev < 0 ? 0 : prev;
      return (start + delta + flat.length) % flat.length;
    });
  };

  const onSearchKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      move(e.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const row = flat[active];
      /* 下标 0 是「不关联」（没有对应的 Row 对象）*/
      pick(row ? row.value : '');
    }
  };

  const renderRow = (row: Row, index: number) => {
    const fam = templateFamily(row);
    const checked = row.value === value;
    return (
      <label
        key={row.value || '__none'}
        data-row={index}
        className={`os-row${checked ? ' is-on' : ''}${active === index ? ' is-active' : ''}`}
        data-os={fam.key}
        onMouseEnter={() => setActive(index)}
      >
        <input
          type="radio"
          className="os-row-input"
          name={groupName}
          value={row.value}
          checked={checked}
          disabled={disabled}
          onChange={() => pick(row.value)}
        />
        <span className="os-row-radio" aria-hidden="true" />
        <span className="os-row-logo">
          {row.gone ? null : <OsLogo family={fam} size="table" />}
        </span>
        <span className="os-row-name tpl-link-name">
          <span className="tpl-link-name-text" title={row.name}>
            {row.name}
          </span>
          {row.gone ? (
            <span className="os-row-tag is-warn">{t('guestList.templateGoneTag')}</span>
          ) : null}
        </span>
        <span className="tpl-link-loc">
          {row.node}/{row.vmid}
        </span>
      </label>
    );
  };

  const toggleGroup = (key: OsFamilyKey) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <div className={`os-pick is-compact is-dialog${open ? ' is-open' : ''}`}>
      {/* ---- 字段名 + 总数 + 搜索（展开时） ---- */}
      <div className="os-pick-bar">
        <span className="os-pick-title">{label ?? t('guestList.templateLabel')}</span>
        <span className="os-pick-count">
          {narrowing
            ? t('guestList.templateMatchCount', {
                n: scoped.length,
                total: all.length,
              })
            : t('guestList.templateCount', { n: all.length })}
        </span>
        {open ? (
          <>
            <div className="os-pick-search">
              <Input
                ref={searchRef}
                prefix={<IconSearch size={14} />}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(0);
                }}
                onKeyDown={onSearchKeyDown}
                placeholder={t('guestList.templateSearch')}
                aria-label={t('guestList.templateSearchAria')}
                autoFocus
              />
            </div>
            <button
              type="button"
              className="os-pick-collapse"
              onClick={() => setOpen(false)}
            >
              {t('guestList.templateCollapse')}
            </button>
          </>
        ) : null}
      </div>

      {/* ---- 收起态：一行触发条 ---- */}
      {!open ? (
        <button
          type="button"
          className="os-pick-trigger"
          aria-expanded={false}
          aria-controls={panelId}
          onClick={openPanel}
          disabled={disabled}
        >
          {current ? (
            <>
              <span className="os-pick-trigger-logo" data-os={templateFamily(current).key}>
                <OsLogo family={templateFamily(current)} size="table" />
              </span>
              <span className="os-pick-trigger-name" title={current.name}>
                {current.name}
              </span>
              <span className="os-pick-trigger-loc">
                {current.node}/{current.vmid}
              </span>
            </>
          ) : (
            <span className="os-pick-trigger-none">{t('guestList.templateNone')}</span>
          )}
          <span className="os-pick-trigger-caret" aria-hidden="true">
            <IconChevronDown size={14} />
          </span>
        </button>
      ) : null}

      {/* ---- 展开态：分类 chip + 分组列表 ---- */}
      {open ? (
        <div className="os-pick-panel" id={panelId}>
          {families.length > 1 ? (
            <div
              className="os-pick-chips"
              role="group"
              aria-label={t('guestList.templateGroupAria')}
            >
              <button
                type="button"
                className={`os-chip${activeFamily === 'all' ? ' is-on' : ''}`}
                aria-pressed={activeFamily === 'all'}
                onClick={() => {
                  setFamily('all');
                  setActive(0);
                }}
              >
                {t('guestList.templateAllFamilies')}
                <span className="os-chip-count">{matched.length}</span>
              </button>
              {families.map((entry) => (
                <button
                  type="button"
                  key={entry.family.key}
                  className={`os-chip${activeFamily === entry.family.key ? ' is-on' : ''}`}
                  data-os={entry.family.key}
                  aria-pressed={activeFamily === entry.family.key}
                  onClick={() => {
                    setFamily(entry.family.key);
                    setActive(0);
                  }}
                >
                  <OsLogo family={entry.family} size="chip" />
                  {osFamilyLabel(entry.family, t)}
                  <span className="os-chip-count">{entry.count}</span>
                </button>
              ))}
            </div>
          ) : null}

          {loading && !all.length ? (
            <div className="os-pick-empty" role="status">
              <div>
                <div className="os-pick-empty-title">
                  {t('guestList.templateLoading')}
                </div>
                <div className="os-pick-empty-body">
                  {t('guestList.templateLoadingBody')}
                </div>
              </div>
            </div>
          ) : error && !all.length ? (
            <div className="os-pick-empty" role="alert">
              <div>
                <div className="os-pick-empty-title">
                  {t('guestList.templateLoadFailed')}
                </div>
                <div className="os-pick-empty-body">{error}</div>
              </div>
            </div>
          ) : (
            <>
              <div
                className="os-pick-list"
                ref={listRef}
                role="group"
                aria-label={t('guestList.templateListAria')}
              >
                {/* 「不关联」固定在顶部：它永远是第一个候选，滚多远都点得到 */}
                <label
                  data-row={0}
                  className={`os-row is-pin${value === '' ? ' is-on' : ''}${
                    active === 0 ? ' is-active' : ''
                  }`}
                  onMouseEnter={() => setActive(0)}
                >
                  <input
                    type="radio"
                    className="os-row-input"
                    name={groupName}
                    value=""
                    checked={value === ''}
                    disabled={disabled}
                    onChange={() => pick('')}
                  />
                  <span className="os-row-radio" aria-hidden="true" />
                  <span className="os-row-logo" aria-hidden="true" />
                  <span className="os-row-name">{t('guestList.templateNone')}</span>
                  <span className="tpl-link-loc">—</span>
                </label>

                {plan.map((entry) => {
                  const isCollapsed = collapsed.has(entry.group.family.key);
                  return (
                    <section className="os-pick-group" key={entry.group.family.key}>
                      <button
                        type="button"
                        className="os-pick-group-head"
                        data-os={entry.group.family.key}
                        aria-expanded={!isCollapsed}
                        onClick={() => toggleGroup(entry.group.family.key)}
                      >
                        <span className="os-pick-caret" aria-hidden="true">
                          <IconChevronRight size={12} />
                        </span>
                        <OsLogo family={entry.group.family} size="group" />
                        <span className="os-pick-group-name">
                          {osFamilyLabel(entry.group.family, t)}
                        </span>
                        <span className="os-pick-group-count">
                          {entry.group.items.length}
                        </span>
                        <span className="os-pick-group-rule" aria-hidden="true" />
                      </button>

                      {isCollapsed
                        ? null
                        : entry.items.map((item, index) =>
                            renderRow(
                              item,
                              (groupOffsets.get(entry.group.family.key) ?? 1) + index,
                            ),
                          )}
                    </section>
                  );
                })}
              </div>

              {!scoped.length ? (
                <div className="os-pick-empty">
                  <div>
                    <div className="os-pick-empty-title">
                      {all.length
                        ? t('guestList.templateEmptyTitle')
                        : t('guestList.templateNoOptions')}
                    </div>
                    {all.length ? (
                      <div className="os-pick-empty-body">
                        {t('guestList.templateEmptyBody')}
                      </div>
                    ) : (
                      <div className="os-pick-empty-body">
                        {t('guestList.templateNoOptionsBody')}
                      </div>
                    )}
                  </div>
                  {all.length ? (
                    <button
                      type="button"
                      className="os-pick-clear"
                      onClick={() => {
                        setQuery('');
                        setFamily('all');
                        setActive(0);
                      }}
                    >
                      {t('guestList.templateClearFilter')}
                    </button>
                  ) : null}
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}

      {hint ? <p className="os-pick-note">{hint}</p> : null}
    </div>
  );
}
