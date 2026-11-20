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
  | 'redhat'
  | 'centos'
  | 'rocky'
  | 'alma'
  | 'fedora'
  /* 国产发行版：各自有官方标识，且模板名里的写法千差万别，必须单独成组 ——
     混进通用 Linux 会让一排木箱里分不出哪台是麒麟、哪台是统信。 */
  | 'kylin'
  | 'uos'
  | 'openeuler'
  | 'anolis'
  | 'tencent'
  | 'opencloudos'
  | 'deepin'
  | 'alios'
  | 'redflag'
  /* Proxmox 自家的模板 /  appliances（PBS、PMG 等）也常被当成"系统"出现在列表里 */
  | 'proxmox'
  | 'suse'
  | 'coreos'
  | 'openwrt'
  | 'fnos'
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
  /* RHEL 排在 CentOS 族群之前：它是 Rocky / Alma / CentOS 的上游，运维心里
     那张谱系表就是这个顺序。 */
  redhat: { key: 'redhat', label: 'Red Hat', rank: 3 },
  centos: { key: 'centos', label: 'CentOS', rank: 4 },
  rocky: { key: 'rocky', label: 'Rocky Linux', rank: 5 },
  alma: { key: 'alma', label: 'AlmaLinux', rank: 6 },
  fedora: { key: 'fedora', label: 'Fedora', rank: 7 },
  /* 国产发行版集中排在 RHEL 系之后：这是运维挑模板时的实际顺序 —— 先看通用
     发行版，再在国产替代里按「麒麟 / 统信 / 欧拉 / 龙蜥 / 腾讯系」找。 */
  kylin: { key: 'kylin', label: 'Kylin', rank: 8 },
  uos: { key: 'uos', label: 'UOS', rank: 9 },
  openeuler: { key: 'openeuler', label: 'openEuler', rank: 10 },
  anolis: { key: 'anolis', label: 'Anolis OS', rank: 11 },
  tencent: { key: 'tencent', label: 'TencentOS', rank: 12 },
  opencloudos: { key: 'opencloudos', label: 'openCloudOS', rank: 13 },
  deepin: { key: 'deepin', label: 'deepin', rank: 14 },
  alios: { key: 'alios', label: 'Alibaba Cloud Linux', rank: 15 },
  redflag: { key: 'redflag', label: 'Red Flag', rank: 16 },
  proxmox: { key: 'proxmox', label: 'Proxmox', rank: 17 },
  suse: { key: 'suse', label: 'openSUSE', rank: 18 },
  coreos: { key: 'coreos', label: 'CoreOS', rank: 19 },
  openwrt: { key: 'openwrt', label: 'OpenWrt', rank: 20 },
  fnos: { key: 'fnos', label: 'fnOS', rank: 21 },
  alpine: { key: 'alpine', label: 'Alpine', rank: 22 },
  arch: { key: 'arch', label: 'Arch Linux', rank: 23 },
  linux: { key: 'linux', label: 'Linux', rank: 24 },
  other: { key: 'other', label: 'Other', rank: 25 },
};

/** 名称 → 家族。顺序有意义：更具体的词排在更宽泛的前面。 */
const NAME_RULES: Array<[RegExp, OsFamilyKey]> = [
  /* `\bwindows` 而不是 `\bwindows\b`：模板名常把版本直接粘在后面
     （`windows2022-server` / `windows2019-template`），要求词尾边界会把它们
     全漏进「认不出的系统」。`win2019` 这类缩写同样要能认出来。 */
  [/\bwindows|w2k|wxp|win(?:7|8|10|11|20\d\d|vista)/i, 'windows'],
  [/ubuntu/i, 'ubuntu'],
  [/debian/i, 'debian'],
  /* Fedora CoreOS 名字里带 Fedora，必须排在 /fedora/ 之前，否则会被分进 Fedora 组；
     `Container Linux`（CoreOS 的旧名）同样归到这一族。 */
  [/core\s*os|container\s*linux/i, 'coreos'],
  /* `red hat` 中间可能有空格 / 连字符（`red-hat-8` / `Red Hat Enterprise Linux`），
     `rhel` 是最常见的缩写，而且版本号会直接粘在后面（`rhel9`）—— 所以只卡词首边界。
     排在下面通用的 Tux 兜底之前。 */
  [/red\s*[-]?\s*hat|\brhel/i, 'redhat'],
  [/rocky/i, 'rocky'],
  [/alma(?:linux)?/i, 'alma'],
  /* TencentOS Server（原 tlinux）：模板名常见写法是 `TencentOS4` / `tencentos-4` /
     `tlinux4.0`。**必须排在 /centos/ 之前**：`TencentOS` 拼出来就是
     tenc-**entos**，按 centos 先匹配会把这张模板整个分进 CentOS 组（实测过）。
     它有自己的官方标识，也不该落进下面那条通用的 Tux 兜底。 */
  [/tencent\s*os|\btlinux/i, 'tencent'],
  /* 国产发行版：都排在上面的通用兜底之前，否则会被 `/euler|kylin|deepin/` 那条
     一网打进 Linux 组。写法按实际模板名收录 —— PVE 里常见 `Kylin-Server-V10`、
     `UOS-20`、`openEuler-22.03`、`AnolisOS-8`、`aliyun_3`、`RedFlag-8`。
     `NeoKylin`（中标麒麟）含 kylin，自然归到麒麟组。 */
  [/kylin|麒麟/i, 'kylin'],
  [/union\s*tech|\buos\b|统信/i, 'uos'],
  [/open\s*euler|\beuler/i, 'openeuler'],
  [/anolis|龙蜥/i, 'anolis'],
  [/deepin/i, 'deepin'],
  /* 阿里云系的三种写法都见过：`alibaba-cloud-linux-3`（全称带连字符）、
     `aliyun_3`（镜像站的卷名）与 `alinux3`（内部简称，数字直接粘着）——
     所以不做词尾边界，只卡词首。 */
  [/alibaba[\s-]*cloud[\s-]*linux|\baliyun|\balinux|\balios\b/i, 'alios'],
  [/red\s*flag|asianux|红旗/i, 'redflag'],
  [/proxmox/i, 'proxmox'],
  [/centos/i, 'centos'],
  [/fedora/i, 'fedora'],
  /* 腾讯参与的国产服务器发行版，名字里没有能误伤别人的词，但同样要有自己的组。 */
  [/open\s*cloud\s*os/i, 'opencloudos'],
  [/open\s*suse|suse|sles/i, 'suse'],
  /* `lede` 是 OpenWrt 的前身；路由器固件在模板列表里常直接写 `openwrt-23.05` */
  [/open\s*wrt|\blede\b/i, 'openwrt'],
  /* fnOS（飞牛）：名字很短，用词边界避免误伤 `xxxfnosyyy` 这类机器名；
     但版本号会直接粘在后面（`fnos4` / `fnOS-1.0`），所以只卡**词首**边界。 */
  [/\bfnos|\bfn\s*os\b/i, 'fnos'],
  [/alpine/i, 'alpine'],
  [/\barch(?:linux)?\b/i, 'arch'],
  /* 认得出是 Linux、但没有（也不需要）单独 logo 的发行版：Oracle Linux /
     Amazon Linux 等。落到通用的 Tux，比扔进「认不出」诚实 ——
     列表里一排「···」会让人以为面板坏了。 */
  [
    /oracle\s*linux|\bol\d|amazon\s*linux|\bamzn\b|gentoo|slackware|nixos|void\s*linux/i,
    'linux',
  ],
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
 * 名称大部分时候最可信（模板名是运维起的，通常写明发行版），但有一个例外必须让
 * 位给 ostype：**不同节点上的模板可以同名，而系统并不相同** —— 两台 PVE 上各有
 * 一台叫 `ubuntu24` 的模板，其中一台其实是 Windows，按名称优先就会给它配 Ubuntu
 * 图标。`win*` 的 ostype 是硬事实，反方向的误判不存在（真正的 Linux 模板不会是
 * win 系 ostype）。
 *
 * 其余情况仍名称优先：ostype 在建模板时很容易停在默认的 `l26`，一个叫
 * `windows-server-2019` 的模板被分进 Linux 组，比认不出系统更糟。
 */
export function templateFamily(item: OsLabeled): OsFamily {
  const ostype = String(item.ostype ?? '').trim();
  if (isWindowsOstype(ostype)) return FAMILIES.windows;
  /* 容器的 ostype 是**具体发行版**（debian / ubuntu / alpine …），比机器名可信：
     `prokom`、`php74` 这类业务名一个月也命不中一次规则，而 ostype 一次就能对上。
     虚拟机的 ostype 是 l26 / win11 这类大类，不会命中这里的发行版规则，
     仍然交给名称判断。 */
  const typed = matchName(ostype);
  if (typed) return typed;
  const named = matchName(String(item.name ?? '').trim());
  if (named) return named;
  return ostype ? FAMILIES.linux : FAMILIES.other;
}

/** 分组标题：专名直接用，Linux / Other 走 i18n */
export function osFamilyLabel(family: OsFamily, t: TFunc): string {
  if (family.key === 'linux') return t('osFamily.linux');
  if (family.key === 'other') return t('osFamily.other');
  return family.label;
}

/**
 * 家族 key → 家族。
 *
 * 列表「操作系统」列会先读面板侧记录的 key（``vm_meta`` 里的 ``os``），
 * 认不出的 key（旧版本删掉的家族、手改脏数据）一律当没记录，交给来源模板推断。
 */
export function familyByKey(key: string | null | undefined): OsFamily | null {
  const name = String(key ?? '').trim().toLowerCase();
  if (!name) return null;
  return (FAMILIES as Record<string, OsFamily | undefined>)[name] ?? null;
}

/**
 * 全部家族，按 rank 排序。
 *
 * 给「修正系统标识」的下拉用：这里列的是面板认得的全部 key，与
 * :func:`familyByKey` 读取的映射是同一份，新增发行版只需要改上面那张表。
 */
export function allFamilies(): OsFamily[] {
  return Object.values(FAMILIES).sort((a, b) => a.rank - b.rank);
}

/**
 * 家族的「适用场景」一句话。
 *
 * 重装选模板时最大的认知负担不是「找哪张」，而是「哪张适合我」：名字、
 * 版本号、磁盘大小都摆出来之后，用户仍然不知道 Ubuntu 与 Debian 该选哪个。
 * 这里给每个家族一句用途描述（通用服务器 / 企业存量 / 轻量容器……），
 * 让选择依据从「我记得这个名字」变成「我要跑什么」。
 */
export function osFamilyScene(family: OsFamily, t: TFunc): string {
  return t(`osScene.${family.key}`);
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
 * 家族缩写成 3 个字母。
 *
 * **只作回退用**：系统标识的正式形态是各家官方 logo（见 components/OsLogo.tsx），
 * 字母块只在两种情况下顶上来 —— 认不出的系统（`other`）与 logo 资源加载失败。
 * 保留 3 字母而不是一个「?」，是因为「···」也说不清是哪一类，而 LNX / WIN
 * 至少还保留了「大类」这条信息；增删发行版仍然只是改一行映射。
 */
const MARKS: Record<OsFamilyKey, string> = {
  windows: 'WIN',
  ubuntu: 'UBT',
  debian: 'DEB',
  redhat: 'RHT',
  centos: 'CEN',
  rocky: 'RCK',
  alma: 'ALM',
  fedora: 'FED',
  kylin: 'KYL',
  uos: 'UOS',
  openeuler: 'EUL',
  anolis: 'ANO',
  tencent: 'TOS',
  opencloudos: 'OCO',
  deepin: 'DEE',
  alios: 'ALI',
  redflag: 'RFL',
  proxmox: 'PVE',
  suse: 'SUS',
  coreos: 'COS',
  openwrt: 'WRT',
  fnos: 'FNS',
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
/**
 * 找出这台机器**当初用的那一张**模板（名字一致，忽略大小写与分隔符）。
 *
 * 与 :func:`recommendTemplate` 的区别：那个是「找不到就给个同类推荐」，这里只认
 * 真正同名的一张。重装界面要写「当前系统」就必须有这条证据 —— 拿同族的另一张
 * 顶上去，用户会以为在重装同一套系统，实际换了发行版。
 *
 * @param names 原系统的线索，按可信度排列：来源模板名 → 机器名
 */
export function findOriginalTemplate<T extends OsLabeled>(
  templates: T[],
  names: Array<string | null | undefined>,
): T | null {
  for (const raw of names) {
    const key = normName(raw);
    if (!key) continue;
    const hit = templates.find((item) => normName(item.name) === key);
    if (hit) return hit;
  }
  return null;
}

export function recommendTemplate<T extends OsLabeled>(
  templates: T[],
  names: Array<string | null | undefined>,
  ostype?: string | null,
  /**
   * 已经确定的家族（例如面板侧人工标注的 ``vm_meta.os``）。
   *
   * 传了就不再从名字 / ostype 推断：否则清单按「人工确认的系统」过滤，
   * 默认选中项却按错误的 ostype 推荐，两个结论在同一个弹窗里打架。
   */
  knownFamily?: OsFamily | null,
): T | null {
  if (!templates.length) return null;
  const family = knownFamily ?? osFamilyOf(names, ostype);
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
