/* ==========================================================================
   pvecenter — 重装系统：来源模板选择器

   为什么不再用原生 <select>：
   重装是本项目里**最难撤销**的一个操作（整块系统盘换掉），而选模板恰恰是
   用户最需要「看清楚再决定」的一步。原生下拉只能塞一行
   `ubuntu24（Linux 6.x · 系统盘 20.0 GB）`：系统名一长就被截断，版本、适用
   场景、Cloud-Init 支持这些真正影响决定的字段根本没地方放，几台机器的模板
   排在一起也没法比较。

   为什么从「卡片」收紧成「行」：
   卡片一张要 180px 高，一屏看得到 3 张，模板一多就得把弹窗拉得比自己还高；
   而用户在这里做的是**挑一个名字**，不是逛画廊。现在一行 38px、字段横向对齐
   （名字 / 版本 / 场景 / 系统盘 / 已用），同一个字段上下对齐才好比较；列表
   自己滚动并封顶，弹窗高度不随模板数量增长。

   默认只做一件事：
   打开弹窗时，用户要做的通常是「按原样再装一次」。所以有同名模板时这里**收起**
   成一张「当前系统」摘要卡，只有点「更换系统」才展开完整列表 —— 搜索、分类、
   几十行模板都不出现在默认路径上。误点列表换掉系统的机会因此小得多；真换错了，
   摘要卡上还有「恢复当前系统」。

   空间与规模：
   · 顶部只留两行：搜索 + 分类筛选（原系统提示塞进标题行，开关与 chip 同行）；
   · 列表内部滚动，两层吸顶（列名 24px / 分组头 26px），滚到哪都知道在看哪类；
   · 每个分组默认只铺 5 行，剩下的折成「展开其余 N 个」——100 个模板打开时也
     只占一屏，而不是 100 行；搜索或选中某个分类时自动全展开（这时用户就是在
      narrowing，不该再折叠）；
   · 分组头可折叠，折叠的是「这类我先不看」，不是筛选，不影响已选中的模板。

   交互：
   · 整行可点，单选用原生 radio（视觉上隐藏），方向键切换、Tab 聚焦、读屏播报
     全部由浏览器保证；
   · 选中态三层：左侧强调色竖条 + 行底色 + 左侧实心圆点；底部再留一条 18px 的
     「已选」摘要，滚出视野也能确认；
   · 打开时把当前选中的行滚进可视区（默认选中的「推荐」很可能在折叠区下方）。

   选择结果只回传 vmid：调用方（重装向导）本来就用 vmid 串起推荐、提交与
   二次确认，这里不引入第二套标识。
   ========================================================================== */

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Button } from './ui/Button';
import { Input, Switch } from './ui/Input';
import { IconCheck, IconChevronRight, IconSearch } from './Icons';
import { OsLogo } from './OsLogo';
import { useT } from '../i18n';
import { formatBytes } from '../utils/format';
import { ostypeLabel } from '../utils/status';
import {
  groupTemplates,
  osFamilyLabel,
  osFamilyScene,
  samePlatform,
  templateFamily,
  type OsFamily,
  type OsFamilyKey,
} from '../utils/osFamily';
import type { ReinstallTemplate } from '../api/types';

export interface ReinstallTemplatePickerProps {
  /** 节点上的全部模板（兼容过滤前的全集，用于算「隐藏了几个」） */
  templates: ReinstallTemplate[];
  /** 与原系统同平台的子集（Windows ↔ Linux 是硬边界） */
  compatible: ReinstallTemplate[];
  /** 原机器所属家族：提示「原系统是什么」、标出跨平台模板 */
  originalFamily: OsFamily;
  /** 当前选中模板的 vmid（字符串，与向导的 state 同型） */
  value: string;
  onChange: (vmid: string) => void;
  /** 与原系统最匹配的模板，行尾打「推荐」 */
  recommendedVmid?: number;
  /**
   * 「当前系统」对应的模板（同名的那张）。传了且选中它时，组件默认**收起**成
   * 一张摘要卡，只有用户点「更换系统」才展开完整列表。
   * 没有同名模板（模板被删/改名）时传 undefined：直接展开列表，别用同族推荐
   * 冒充「当前系统」。
   */
  currentVmid?: number;
  /** 有异平台模板时才有意义；false 时整个「只显示兼容模板」开关不出现 */
  filterable: boolean;
  compatibleOnly: boolean;
  onCompatibleOnlyChange: (next: boolean) => void;
  /** 一张同平台模板都没有：不隐藏模板，只给说明 */
  noPlatformMatch: boolean;
  disabled?: boolean;
}

/** 每个分组默认铺几行。5 行 ≈ 190px，两类就是「一屏能比完」的密度 */
const GROUP_PREVIEW = 5;

/** 名称里的版本号：`ubuntu-24.04` → 24.04、`CentOS7.9` → 7.9、`Rocky10` → 10。
    取第一段数字即可 —— 模板名由人起，后面的数字多半是日期或序号。 */
function versionOf(item: ReinstallTemplate): string {
  const hit = /\d+(?:\.\d+)*/.exec(String(item.name ?? ''));
  return hit ? hit[0] : '';
}

export function ReinstallTemplatePicker({
  templates,
  compatible,
  originalFamily,
  value,
  onChange,
  recommendedVmid,
  currentVmid,
  filterable,
  compatibleOnly,
  onCompatibleOnlyChange,
  noPlatformMatch,
  disabled = false,
}: ReinstallTemplatePickerProps) {
  const t = useT();
  /* radio 的 name 必须整页唯一：同一时刻只可能开一个重装弹窗，但用 useId 兜住
     「将来出现第二个实例时两组单选框互斥」这件事，代价只有一行。 */
  const groupName = `reinstall-template-${useId().replace(/:/g, '')}`;
  const [search, setSearch] = useState('');
  const [familyFilter, setFamilyFilter] = useState<OsFamilyKey | 'all'>('all');
  /** 被用户折叠的分组：只影响展示，不影响筛选与已选项 */
  const [collapsed, setCollapsed] = useState<Set<OsFamilyKey>>(() => new Set());
  /** 被用户点开「展开其余 N 个」的分组 */
  const [expanded, setExpanded] = useState<Set<OsFamilyKey>>(() => new Set());
  /** 是否展开了完整列表：默认收起，用户点「更换系统」才展开 */
  const [switching, setSwitching] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  const visible = compatibleOnly ? compatible : templates;
  const hiddenCount = Math.max(0, templates.length - compatible.length);

  /* 先按关键词收窄，再按分类收窄。计数与筛选条都基于**关键词过滤后**的集合：
     否则搜索时 chip 上还写着「Ubuntu 3」，点进去却只有 1 张，数字会自己打脸。 */
  const matched = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return visible;
    return visible.filter((item) =>
      `${item.name} ${item.ostype} ${item.disk}`.toLowerCase().includes(q),
    );
  }, [visible, search]);

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

  /* 选中的分类在搜索后可能一个模板都不剩：这时按「全部」渲染，
     而不是给用户一屏空白再让他自己猜发生了什么。 */
  const activeFamily: OsFamilyKey | 'all' =
    familyFilter === 'all' ||
    families.some((entry) => entry.family.key === familyFilter)
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

  /* 摘要读的是**全集**：模板被当前筛选藏起来时，用户仍然需要看到自己选的是哪一张 */
  const selected = templates.find((item) => String(item.vmid) === value);

  /* 收起态展示哪一张：优先当前选中，其次「当前系统」那张 —— 父组件的默认值是在
     effect 里写回的，这里兜一下，避免首帧先闪一遍完整列表再收起。 */
  const summary =
    selected ??
    (currentVmid != null
      ? templates.find((item) => item.vmid === currentVmid)
      : undefined);
  const isOriginal =
    currentVmid != null && (!value || String(currentVmid) === value);
  /* 没有同名模板（或一张都没选中）时没有可信的摘要可展示，直接展开列表 */
  const showList = switching || currentVmid == null || !summary;

  /* 搜索 / 选分类时自动翻开所有分组：结果藏在折叠组里等于没搜到。
     只在用户主动收窄时展开，清空搜索后不会被强行改回展开状态。 */
  const narrowing = search.trim().length > 0 || activeFamily !== 'all';

  const toggleCollapsed = (key: OsFamilyKey) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  /* 搜索或切分类时把折叠全部打开：结果藏在折叠组里等于「没搜到」。
     只在用户主动收窄时展开；清空搜索不会把手动折叠的组翻回来。 */
  const handleSearch = (next: string) => {
    setSearch(next);
    if (next.trim()) setCollapsed(new Set());
  };

  const handleFamily = (key: OsFamilyKey | 'all') => {
    setFamilyFilter(key);
    setCollapsed(new Set());
  };

  /* 打开时把选中的行滚进可视区：默认选中的「推荐」经常在第三、四个分组。
     只在挂载时做一次 —— 之后用户自己滚到哪就是哪，别抢滚动条。 */
  useEffect(() => {
    const list = listRef.current;
    const input = list?.querySelector<HTMLInputElement>('input[type="radio"]:checked');
    const row = input?.closest('label');
    if (!list || !row) return;
    const top = (row as HTMLElement).offsetTop;
    if (top > list.clientHeight - 48) {
      list.scrollTop = Math.max(0, top - list.clientHeight / 2);
    }
  }, []);

  /* 一张模板都没有时整块不渲染：向导正文已经用 Notice 说明了「先去模板页建一个」，
     这里再摆一个「没有匹配的模板 + 清除筛选」，点下去什么都不会变，是假入口。 */
  if (!templates.length) return null;

  /* 默认态：一张卡片说明「就按当前系统重装」，其余系统藏在「更换系统」后面。
     默认流程没有搜索、没有分类、没有列表 —— 打开弹窗确认一下就能直接点开始，
     想换系统的人才会进入完整列表。 */
  if (!showList && summary) {
    const family = templateFamily(summary);
    const version = versionOf(summary);
    return (
      <div className="os-current-wrap">
        <div
          className={`os-current${isOriginal ? '' : ' is-switch'}`}
          data-os={family.key}
        >
          <span className="tpl-os-mark">
            <OsLogo family={family} />
          </span>

          <div className="os-current-text">
            <div className="os-current-line">
              <span
                className={`os-current-kicker${isOriginal ? '' : ' is-switch'}`}
              >
                {isOriginal
                  ? t('vmReinstall.currentSystem')
                  : t('vmReinstall.willSwitchTo')}
              </span>
              <strong className="os-current-name" title={summary.name}>
                {summary.name}
              </strong>
              {version ? <span className="os-current-ver">v{version}</span> : null}
            </div>
            <div className="os-current-meta">
              {osFamilyLabel(family, t)}
              {` · ${t('vmReinstall.templateDisk')} ${formatBytes(summary.size)}`}
              {` · ${t('vmReinstall.templateUsed')} ${
                summary.used > 0 ? formatBytes(summary.used) : '—'
              }`}
            </div>
            <div className="os-current-scene">{osFamilyScene(family, t)}</div>
          </div>

          <div className="os-current-actions">
            {/* 已经换成别的系统时，留一条一步回到原系统的路 —— 避免误点后就只能
                自己翻列表找回来 */}
            {!isOriginal && currentVmid != null ? (
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onChange(String(currentVmid))}
              >
                {t('vmReinstall.restoreCurrent')}
              </Button>
            ) : null}
            <Button
              size="sm"
              variant="secondary"
              iconRight={<IconChevronRight size={14} />}
              onClick={() => setSwitching(true)}
            >
              {t('vmReinstall.changeSystem')}
            </Button>
          </div>
        </div>

        <div className="os-current-note">{t('vmReinstall.prefillNote')}</div>
      </div>
    );
  }

  return (
    <div className="os-pick">
      {/* ---- 第一行：标题 / 总数 / 原系统说明 / 搜索 ----
          原系统与「隐藏了几个」原本各占一行，其实只是选择的上下文，
          收进这一行右侧（窄屏自然换行），能省出整整一行高度。 */}
      <div className="os-pick-bar">
        <span className="os-pick-title">{t('vmReinstall.template')}</span>
        <span className="os-pick-count">
          {t('vmReinstall.templateCount', { n: scoped.length })}
        </span>
        {originalFamily.key !== 'other' ? (
          <span className="os-pick-hint" title={t('vmReinstall.osCurrent', {
            os: osFamilyLabel(originalFamily, t),
          })}>
            {t('vmReinstall.osCurrent', { os: osFamilyLabel(originalFamily, t) })}
            {filterable && compatibleOnly && hiddenCount > 0
              ? ` · ${t('vmReinstall.templateFiltered', { n: hiddenCount })}`
              : ''}
          </span>
        ) : null}
        <div className="os-pick-search">
          <Input
            prefix={<IconSearch size={14} />}
            value={search}
            onChange={(e) => handleSearch(e.target.value)}
            placeholder={t('vmReinstall.templateSearch')}
            aria-label={t('vmReinstall.templateSearchAria')}
          />
        </div>
        {/* 从「当前系统」展开进来的人，随时可以收起列表回到一步直达的默认态 */}
        {currentVmid != null ? (
          <button
            type="button"
            className="os-pick-collapse"
            onClick={() => setSwitching(false)}
          >
            {t('vmReinstall.collapsePicker')}
          </button>
        ) : null}
      </div>

      {/* ---- 第二行：分类筛选 + 兼容开关 ----
          只有一个系统时不铺 chip（点它不会改变任何东西），开关自己靠右。 */}
      <div className="os-pick-tools">
        {families.length > 1 ? (
          <div
            className="os-pick-chips"
            role="group"
            aria-label={t('vmReinstall.templateFilterAria')}
          >
            <button
              type="button"
              className={`os-chip${activeFamily === 'all' ? ' is-on' : ''}`}
              aria-pressed={activeFamily === 'all'}
              onClick={() => handleFamily('all')}
            >
              {t('vmReinstall.templateFilterAll')}
              <span className="os-chip-count">{matched.length}</span>
            </button>
            {families.map((entry) => (
              <button
                type="button"
                key={entry.family.key}
                className={`os-chip${activeFamily === entry.family.key ? ' is-on' : ''}`}
                data-os={entry.family.key}
                aria-pressed={activeFamily === entry.family.key}
                onClick={() => handleFamily(entry.family.key)}
              >
                <OsLogo family={entry.family} size="chip" />
                {osFamilyLabel(entry.family, t)}
                <span className="os-chip-count">{entry.count}</span>
              </button>
            ))}
          </div>
        ) : (
          <span />
        )}

        {filterable ? (
          <span
            className="os-pick-toggle"
            title={t('vmReinstall.templateCompatibleHint', {
              os: osFamilyLabel(originalFamily, t),
            })}
          >
            <Switch
              checked={compatibleOnly}
              onChange={onCompatibleOnlyChange}
              label={t('vmReinstall.templateCompatible')}
            />
          </span>
        ) : null}
      </div>

      {/* 没有任何同平台模板：不隐藏模板，但必须说清「你现在看到的是别的系统的」 */}
      {noPlatformMatch ? (
        <div className="os-pick-hint is-warning">
          {t('vmReinstall.templateNoPlatform', {
            os: osFamilyLabel(originalFamily, t),
          })}
        </div>
      ) : null}

      {/* ---- 模板列表：内部滚动 + 两层吸顶 ---- */}
      {groups.length ? (
        <div
          className="os-pick-list"
          ref={listRef}
          role="group"
          aria-label={t('vmReinstall.templateAria')}
        >
          {/* 列名：只在密集列表里值得占这 24px —— 没有它，「20.0 GB」和「—」
              谁都分不清是系统盘还是已用 */}
          <div className="os-pick-colhead" aria-hidden="true">
            <span />
            <span />
            <span>{t('vmReinstall.templateColName')}</span>
            <span className="is-ver">{t('vmReinstall.templateVersionLabel')}</span>
            <span className="is-scene">{t('vmReinstall.templateScene')}</span>
            <span className="is-disk">{t('vmReinstall.templateDisk')}</span>
            <span className="is-used">{t('vmReinstall.templateUsed')}</span>
            <span />
          </div>

          {groups.map((group) => {
            const isCollapsed = collapsed.has(group.family.key);
            /* 收窄时（搜索 / 选了某类）不再截断：用户已经把范围缩到能看完。
               含已选项的分组也整组展开 —— 否则「已选 X」在下面，行却被截掉，
               用户会以为自己选的东西消失了。 */
            const containsSelected = group.items.some(
              (item) => String(item.vmid) === value,
            );
            const showAll =
              narrowing ||
              containsSelected ||
              expanded.has(group.family.key) ||
              group.items.length <= GROUP_PREVIEW;
            const rows = showAll ? group.items : group.items.slice(0, GROUP_PREVIEW);
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
                  <span className="os-pick-group-count">{group.items.length}</span>
                  <span className="os-pick-group-rule" aria-hidden="true" />
                </button>

                {isCollapsed ? null : (
                  <div className="os-pick-rows">
                    {rows.map((item) => {
                      const family = templateFamily(item);
                      const checked = String(item.vmid) === value;
                      const version = versionOf(item);
                      const scene = osFamilyScene(family, t);
                      const cross = !samePlatform(originalFamily, family);
                      return (
                        <label
                          key={String(item.vmid)}
                          className={`os-row${checked ? ' is-on' : ''}`}
                          data-os={family.key}
                        >
                          <input
                            type="radio"
                            className="os-row-input"
                            name={groupName}
                            value={String(item.vmid)}
                            checked={checked}
                            disabled={disabled}
                            onChange={() => onChange(String(item.vmid))}
                          />

                          <span className="os-row-radio" aria-hidden="true" />
                          <span className="os-row-logo">
                            <OsLogo family={family} size="table" />
                          </span>
                          <span className="os-row-name" title={item.name}>
                            {item.name}
                          </span>
                          <span className="os-row-ver">
                            {version
                              ? `v${version}`
                              : ostypeLabel(item.ostype, t)}
                          </span>
                          <span className="os-row-scene" title={scene}>
                            {scene}
                          </span>
                          <span className="os-row-disk">{formatBytes(item.size)}</span>
                          <span className="os-row-used">
                            {/* 0 不是「一个字节都没用」，而是后端没读到：
                                显示 0 B 会让人以为这是一张空盘模板 */}
                            {item.used > 0 ? formatBytes(item.used) : '—'}
                          </span>
                          <span className="os-row-tags">
                            {recommendedVmid === item.vmid ? (
                              <span className="os-row-tag is-accent">
                                {t('vmReinstall.templateRecommended')}
                              </span>
                            ) : null}
                            {item.cloudinit ? (
                              <span className="os-row-tag">
                                {t('vmReinstall.templateCloudInit')}
                              </span>
                            ) : null}
                            {cross ? (
                              <span className="os-row-tag is-warn">
                                {t('vmReinstall.templateCrossPlatform')}
                              </span>
                            ) : null}
                          </span>
                        </label>
                      );
                    })}

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
                        {t('vmReinstall.templateExpandAll', { n: hiddenRows })}
                      </button>
                    ) : null}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      ) : (
        /* 搜索 / 分类筛到空：给出「下一步做什么」，而不是一句冷冰冰的没有数据 */
        <div className="os-pick-empty">
          <div>
            <div className="os-pick-empty-title">
              {t('vmReinstall.templateEmptyTitle')}
            </div>
            <div className="os-pick-empty-body">
              {t('vmReinstall.templateEmptyBody')}
            </div>
          </div>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              handleSearch('');
              handleFamily('all');
            }}
          >
            {t('vmReinstall.templateClear')}
          </Button>
        </div>
      )}

      {/* ---- 已选摘要：18px 高的一行，滚出视野也能确认 ----
          原来这条是 40px 的色块条，信息与行内重复；缩小成一行文案后，
          既保留「随时可见」的作用，又不再和列表抢空间。 */}
      {selected ? (
        <div className="os-pick-foot" role="status">
          <span className="os-pick-foot-icon" aria-hidden="true">
            <IconCheck size={10} />
          </span>
          <span>{t('vmReinstall.templateSelected')}</span>
          <strong>{selected.name}</strong>
          <span className="os-pick-foot-meta">
            {osFamilyLabel(templateFamily(selected), t)}
            {versionOf(selected)
              ? ` · ${t('vmReinstall.templateVersion', {
                  version: versionOf(selected),
                })}`
              : ''}
            {` · ${t('vmReinstall.templateSize', {
              size: formatBytes(selected.size),
            })}`}
          </span>
        </div>
      ) : null}
    </div>
  );
}
