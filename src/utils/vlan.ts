/* ==========================================================================
   pvecenter — VLAN 标签的文本 ↔ 取值
   ==========================================================================
   PVE 的 `tag` 取值域是 1~4094，界面上用文本框收（留空 = 不划 VLAN）。
   两个入口（快速部署 / 自定义部署）都要判断「这个框里是不是一个合法 tag」，
   收在一处，免得一边拦一边放。
   ========================================================================== */

/**
 * 文本 → tag。
 *
 * 留空、非整数、越界都返回 `undefined`（= 不划 VLAN / 无效），调用方自己
 * 决定是「当作没填」还是「报错」—— 见 :func:`vlanInvalid`。
 */
export function parseVlan(value: string): number | undefined {
  const tag = Number(String(value ?? '').trim());
  return Number.isInteger(tag) && tag >= 1 && tag <= 4094 ? tag : undefined;
}

/** 填了但不是 1~4094 的整数 —— 留空合法，写错要拦在提交前 */
export function vlanInvalid(value: string): boolean {
  return Boolean(String(value ?? '').trim()) && parseVlan(value) === undefined;
}
