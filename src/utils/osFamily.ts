/* ==========================================================================
   pvecenter — 操作系统家族

   模板页按它分组；重装向导按它判断「原系统与模板是否同一平台」。

   为什么不能只看 ostype：PVE 把**所有** Linux 都记成 `l26`，分不出 Ubuntu /
   CentOS / Debian —— 发行版只能从模板名里认。反过来，机器名是用户起的（一台
   Linux 完全可能叫 `win-web`），所以：
   - 判断**机器**：ostype 决定 Windows / Linux 大类，名字只在大类内细分；
   - 判断**模板**：名称优先（模板名通常写明发行版），ostype 只做兜底
     （模板的 ostype 很容易停在建模板时的默认值 `l26`）。
   ========================================================================== */
import type { TFunc } from '../i18n';
import { isWindowsOstype } from './status';

export type OsFamilyKey =
  | 'windows'
  | 'ubuntu'
  | 'debian'
  | 'centos'
  | 'rocky'
  | 'alma'
  | 'fedora'
  | 'suse'
  | 'alpine'
  | 'arch'
  | 'linux'
  | 'other';

export interface OsFamily {
  key: OsFamilyKey;
  /** 分组标题。发行版专名（Ubuntu / CentOS / Windows）各语言通用，不翻译 */
  label: string;
  /** 分组排序：常用的放前面 */
  rank: number;
}

/** 只要求「有名字」，方便模板页的 TemplateItem 与重装的 ReinstallTemplate 共用 */
export interface OsLabeled {
  name?: string | null;
  ostype?: string | null;
}

const FAMILIES: Record<OsFamilyKey, OsFamily> = {
  windows: { key: 'windows', label: 'Windows', rank: 0 },
  ubuntu: { key: 'ubuntu', label: 'Ubuntu', rank: 1 },
  debian: { key: 'debian', label: 'Debian', rank: 2 },
  centos: { key: 'centos', label: 'CentOS', rank: 3 },
  rocky: { key: 'rocky', label: 'Rocky Linux', rank: 4 },
  alma: { key: 'alma', label: 'AlmaLinux', rank: 5 },
  fedora: { key: 'fedora', label: 'Fedora', rank: 6 },
  suse: { key: 'suse', label: 'openSUSE', rank: 7 },
  alpine: { key: 'alpine', label: 'Alpine', rank: 8 },
  arch: { key: 'arch', label: 'Arch Linux', rank: 9 },
  linux: { key: 'linux', label: 'Linux', rank: 10 },
  other: { key: 'other', label: 'Other', rank: 11 },
};

/** 名称 → 家族。顺序有意义：更具体的词排在更宽泛的前面。 */
const NAME_RULES: Array<[RegExp, OsFamilyKey]> = [
  [/\bwindows\b|w2k|wxp|win(?:7|8|10|11|vista)/i, 'windows'],
  [/ubuntu/i, 'ubuntu'],
  [/debian/i, 'debian'],
  [/rocky/i, 'rocky'],
  [/alma(?:linux)?/i, 'alma'],
  [/centos/i, 'centos'],
  [/fedora/i, 'fedora'],
  [/open\s*suse|suse|sles/i, 'suse'],
  [/alpine/i, 'alpine'],
  [/\barch(?:linux)?\b/i, 'arch'],
];

function matchName(text: string): OsFamily | null {
  if (!text) return null;
  for (const [re, key] of NAME_RULES) {
    if (re.test(text)) return FAMILIES[key];
  }
  return null;
}

/**
 * 一台**机器**属于哪个家族。
 *
 * @param names  按可信度从高到低：来源模板名 → 机器名
 * @param ostype PVE 的系统类型，只用来卡 Windows / Linux 大类
 */
export function osFamilyOf(
  names: Array<string | null | undefined>,
  ostype?: string | null,
): OsFamily {
  const hasOstype = Boolean(String(ostype ?? '').trim());
  const windows = hasOstype && isWindowsOstype(ostype);
  for (const raw of names) {
    const hit = matchName(String(raw ?? '').trim());
    if (!hit) continue;
    /* 系统类型能判出大类时，名字里相反大类的关键词一律不采信 */
    if (hasOstype && windows !== (hit.key === 'windows')) continue;
    return hit;
  }
  if (windows) return FAMILIES.windows;
  if (hasOstype) return FAMILIES.linux;
  return FAMILIES.other;
}

/**
 * 一个**模板**属于哪个家族。
 *
 * 与 :func:`osFamilyOf` 的取舍相反：名称优先。模板名是运维起的、通常写明发行版；
 * 而 ostype 在建模板时很容易停在默认的 `l26` —— 一个叫 `windows-server-2019`
 * 的模板被分进 Linux 组，比认不出系统更糟。
 */
export function templateFamily(item: OsLabeled): OsFamily {
  const named = matchName(String(item.name ?? '').trim());
  if (named) return named;
  if (isWindowsOstype(item.ostype)) return FAMILIES.windows;
  return String(item.ostype ?? '').trim() ? FAMILIES.linux : FAMILIES.other;
}

/** 分组标题：专名直接用，Linux / Other 走 i18n */
export function osFamilyLabel(family: OsFamily, t: TFunc): string {
  if (family.key === 'linux') return t('osFamily.linux');
  if (family.key === 'other') return t('osFamily.other');
  return family.label;
}

/**
 * 两个家族算不算「同一平台」。
 *
 * 只有 Windows 与「非 Windows」是硬边界 —— 拿 Windows 模板重装 Linux 机器，
 * 装完直接进不去。发行版之间（Ubuntu ↔ CentOS）都属于 Linux，可以互相重装。
 * `other`（认不出）不参与判断，避免把未知模板误判成不兼容。
 */
export function samePlatform(a: OsFamily, b: OsFamily): boolean {
  if (a.key === 'other' || b.key === 'other') return true;
  return (a.key === 'windows') === (b.key === 'windows');
}

/**
 * 名称里的数字段：`ubuntu-22.04` → [22, 4]，`CentOS7.9` → [7, 9]。
 *
 * **单个数字也要算**。PVE 里的主版本号常常只有一位（`centos7` / `rocky9` /
 * `alpine3`），只认两位以上会把它们整段漏掉 —— 曾经就是这样：原系统
 * `CentOS7.9` 取不到任何版本号，重装时排序退化成「名称降序」，把机器装成了
 * `CentOS10`。
 */
export function versionNumbers(text: string | null | undefined): number[] {
  return (String(text ?? '').match(/\d+/g) ?? []).map(Number);
}

/**
 * 名称归一化：只留字母与数字并转小写。
 *
 * `CentOS-7.9` / `centos 7.9` / `CentOS7.9` 是同一张模板的三种写法，模板名由
 * 人手起，分隔符与大小写完全不可控。归一化后才能判「这是不是原来那张」。
 */
function normName(text: string | null | undefined): string {
  return String(text ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '');
}

export interface TemplateGroup<T> {
  family: OsFamily;
  items: T[];
}

/**
 * 一条记录 + 它的家族判定。
 *
 * 判定字符串正则是纯 CPU 计算，但同一张卡在一次渲染里要被问三次（分组、
 * 头像、筛选计数），而它只取决于名字 —— 先算一遍带着走，三处用同一份答案，
 * 也保证「分组标题」与「卡片头像」永远不会各说各话。
 */
export interface FamilyTagged<T> {
  item: T;
  family: OsFamily;
}

/** 给一批记录打上家族标记（顺序不变） */
export function tagFamilies<T extends OsLabeled>(items: T[]): Array<FamilyTagged<T>> {
  return items.map((item) => ({ item, family: templateFamily(item) }));
}

/** 组内排序：按名称自然升序（22.04 在 24.04 前） */
function byName<T extends OsLabeled>(a: T, b: T): number {
  return String(a.name ?? '').localeCompare(String(b.name ?? ''), undefined, {
    numeric: true,
  });
}

/** 按家族分组：组按 rank 排，组内按名称自然升序 */
export function groupTagged<T extends OsLabeled>(
  tagged: Array<FamilyTagged<T>>,
): Array<TemplateGroup<T>> {
  const buckets = new Map<OsFamilyKey, T[]>();
  for (const { item, family } of tagged) {
    const list = buckets.get(family.key);
    if (list) list.push(item);
    else buckets.set(family.key, [item]);
  }
  return [...buckets.entries()]
    .map(([key, items]) => ({
      family: FAMILIES[key],
      items: [...items].sort(byName),
    }))
    .sort((a, b) => a.family.rank - b.family.rank);
}

/** 先打标再分组（等价于 :func:`tagFamilies` + :func:`groupTagged`） */
export function groupTemplates<T extends OsLabeled>(
  templates: T[],
): Array<TemplateGroup<T>> {
  return groupTagged(tagFamilies(templates));
}

/**
 * 家族缩写成 3 个字母，给卡片左侧的系统标识块用。
 *
 * 不做成图标：发行版几十个，手绘图标既画不全也不一致；三个大写字母 + 家族色
 * 在十几张卡里扫一眼就能分辨，而且增删发行版只是改一行映射。
 */
const MARKS: Record<OsFamilyKey, string> = {
  windows: 'WIN',
  ubuntu: 'UBT',
  debian: 'DEB',
  centos: 'CEN',
  rocky: 'RCK',
  alma: 'ALM',
  fedora: 'FED',
  suse: 'SUS',
  alpine: 'ALP',
  arch: 'ARC',
  linux: 'LNX',
  other: '···',
};

export function osFamilyMark(family: OsFamily): string {
  return MARKS[family.key];
}

/**
 * 重装默认选哪个模板：
 *
 * 1. 与原系统同家族优先；
 * 2. 同族里，名字与来源模板共享版本号的优先（22.04 不会跳到 24.04）；
 * 3. 其次版本号高的优先；
 * 4. 同族没有时退到「同一平台」，都没有再退回全部（由调用方决定展示范围）。
 *
 * @param names 原系统的线索，按可信度排列：来源模板名 → 机器名
 */
export function recommendTemplate<T extends OsLabeled>(
  templates: T[],
  names: Array<string | null | undefined>,
  ostype?: string | null,
): T | null {
  if (!templates.length) return null;
  const family = osFamilyOf(names, ostype);
  const familyOf = (item: T) => templateFamily(item);

  const sameFamily = templates.filter((item) => familyOf(item).key === family.key);
  const samePlatformItems = templates.filter((item) =>
    samePlatform(family, familyOf(item)),
  );
  const pool = sameFamily.length
    ? sameFamily
    : samePlatformItems.length
      ? samePlatformItems
      : templates;

  /* 版本号只认最可信的那条线索（来源模板名），机器名里的编号是台账不是版本 */
  const evidence = names.find((name) => String(name ?? '').trim()) ?? '';
  const nums = new Set(versionNumbers(evidence));
  const evidenceNorm = normName(evidence);
  const score = (item: T) =>
    versionNumbers(item.name).filter((n) => nums.has(n)).length;

  /* 与原系统**同名**的模板直接胜出（忽略大小写与分隔符）：这是最强的证据 ——
     原机器很可能就是从这一张克出来的，重装回同一张不会有任何意外。
     版本号交集只是它的补充：拿不到同名模板时才用它挑「最近的版本」。 */
  const sameName = (item: T) =>
    evidenceNorm !== '' && normName(item.name) === evidenceNorm ? 1 : 0;
  /* 其次是一方为另一方的前缀（`centos79` ↔ `centos79minimal`）：模板被改过名，
     或机器名带 `-vm01` 之类的后缀时，仍能认出是同一支系统 */
  const namePrefix = (item: T) => {
    if (!evidenceNorm) return 0;
    const name = normName(item.name);
    if (!name) return 0;
    return name.startsWith(evidenceNorm) || evidenceNorm.startsWith(name) ? 1 : 0;
  };

  return [...pool].sort(
    (a, b) =>
      familyOf(a).rank - familyOf(b).rank ||
      sameName(b) - sameName(a) ||
      namePrefix(b) - namePrefix(a) ||
      score(b) - score(a) ||
      String(b.name ?? '').localeCompare(String(a.name ?? ''), undefined, {
        numeric: true,
      }),
  )[0];
}
