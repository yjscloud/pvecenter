/* ==========================================================================
   pvecenter — 目标地址的搜索匹配（主机名 / IP / VMID）

   内网穿透建规则时要在一长串「虚机 / 容器」里挑一台。三样东西用户都可能
   凭记忆敲出来：主机名（web-01）、地址（10.0.0.11）、VMID（101）。所以这里
   同时按三个字段匹配，并把「匹配强度」和「先看哪个字段」定成一套可解释的
   规则 —— 顺序看不懂的搜索结果，用户只会改回一个个翻。

   规则（全部大小写不敏感，两端空白先 trim）：

   1. 强度分三档，**强度优先于字段**：
        exact(0)  完全相等      10.0.0.11 → 10.0.0.11
        prefix(1) 前缀          10.0.0    → 10.0.0.11
        substr(2) 任意位置包含  tunnel-1 → dev-tunnel-1
   2. 同一强度下按「查询词像什么」决定先看哪个字段：
        纯数字（101）        → vmid、主机名、IP     （多半在敲 VMID）
        只含 IP 字符且有 . / : → IP、主机名、vmid     （多半在敲地址）
        其它（web、tunnel-1）→ 主机名、IP、vmid     （多半在敲名字）
   3. 仍然并列时按 VMID、IP 升序，保证每次得到同样的顺序。

   「无效输入」在这里没有特例：任何字符串都只当子串去比，匹配不到就是空列表
   （空列表的提示与「手动填写」入口由 UI 负责）。
   ========================================================================== */

/** 一个可搜索的目标（虚机 / 容器，或管理员放行的地址）。 */
export interface SearchableTarget {
  /** 选中后真正落库的值 */
  ip: string;
  /** 主机名（虚机 / 容器名） */
  name?: string;
  vmid?: number;
  node?: string;
  type?: string;
  /** 没有主机名时的说明，如「管理员授权」 */
  note?: string;
}

export type MatchField = 'name' | 'ip' | 'vmid';

/** 0 = 完全相等，1 = 前缀，2 = 包含；null = 不匹配 */
export type MatchStrength = 0 | 1 | 2;

export const MAX_RESULTS = 50;

/** 查询词更像 VMID / IP / 主机名？决定同一强度下的字段顺序。 */
export function fieldOrder(query: string): MatchField[] {
  if (/^\d+$/.test(query)) return ['vmid', 'name', 'ip'];
  if (/^[0-9a-fA-F:.]+$/.test(query) && /[.:]/.test(query)) {
    return ['ip', 'name', 'vmid'];
  }
  return ['name', 'ip', 'vmid'];
}

function strength(value: string, query: string): MatchStrength | null {
  if (!value) return null;
  const text = value.toLowerCase();
  if (text === query) return 0;
  if (text.startsWith(query)) return 1;
  if (text.includes(query)) return 2;
  return null;
}

/** 单个目标的匹配结果；不匹配返回 null。 */
export function matchTarget(
  target: SearchableTarget,
  rawQuery: string,
): { strength: MatchStrength; field: MatchField; rank: number } | null {
  const query = rawQuery.trim().toLowerCase();
  if (!query) return null;

  const values: Record<MatchField, string> = {
    name: (target.name ?? '').toLowerCase(),
    ip: target.ip ?? '',
    vmid: target.vmid === undefined || target.vmid === null ? '' : String(target.vmid),
  };

  let best: { strength: MatchStrength; field: MatchField; rank: number } | null = null;
  let index = 0;
  for (const field of fieldOrder(query)) {
    const level = strength(values[field], query);
    if (level !== null && (!best || level < best.strength)) {
      best = { strength: level, field, rank: level * 10 + index };
      /* 已经是完全相等就不用再往下看了 —— 强度优先，不可能更小 */
      if (level === 0) return best;
    }
    index += 1;
  }
  return best;
}

/**
 * 过滤 + 排序。
 *
 * 空查询返回原顺序（后端已按 VMID 排好）。返回的结果不做截断，调用方据
 * ``MAX_RESULTS`` 决定显示多少（要显示「还有 N 个」）。
 */
export function searchTargets<T extends SearchableTarget>(
  targets: T[],
  rawQuery: string,
): T[] {
  const query = rawQuery.trim().toLowerCase();
  if (!query) return targets;

  const matched: { target: T; rank: number }[] = [];
  for (const target of targets) {
    const hit = matchTarget(target, query);
    if (hit) matched.push({ target, rank: hit.rank });
  }
  matched.sort((a, b) => {
    if (a.rank !== b.rank) return a.rank - b.rank;
    const vmidA = a.target.vmid ?? Number.MAX_SAFE_INTEGER;
    const vmidB = b.target.vmid ?? Number.MAX_SAFE_INTEGER;
    if (vmidA !== vmidB) return vmidA - vmidB;
    return a.target.ip.localeCompare(b.target.ip);
  });
  return matched.map((item) => item.target);
}
