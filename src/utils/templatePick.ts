/* ==========================================================================
   pvecenter — 模板选择器的候选数据

   为什么单独放在 utils：模板选择器有四个入口 —— 快速部署（虚拟机 / 容器）与
   自定义部署（虚拟机 / 容器）。每个入口都要把接口返回的记录拍平成选择器要的
   两列，任何一处自己拼都会长出一点差异：名称带不带后缀、容量保留几位小数、
   行尾有没有 VMID、摘要卡第三行写不写节点与核数。

   单看每处都说得过去，但用户在「快速部署」与「自定义部署」之间切换时，看到的
   就是同一张模板长得不一样 —— 要么怀疑自己选错了，要么以为列表刷没了。所以
   映射只写这一份，四个入口都从这里拿。

   两条硬约束：
   * **只做展示**：`value` 才是提交给后端的标识（虚拟机 `node:vmid`、容器 `volid`），
     名称怎么美化都不影响它；
   * **容量统一取整**（0 位小数）：这两列是用来横向比较的，`2.0 GiB` 与 `2 GiB`
     在窄列里只差噪音。
   ========================================================================== */

import type { TFunc } from '../i18n';
import { formatBytes } from './format';
import { ctTemplateName } from './status';

/** 一个可选项。字段刻意做成「已格式化好的文本」，组件只管摆位置。 */
export interface TemplatePickItem {
  /**
   * **唯一**选择值：`连接|节点|标识`。
   *
   * 为什么要带连接与节点：模板在不同节点/不同 PVE 上可以同名，本地存储上甚至是
   * 同一个 ``volid``（都叫 ``local:vztmpl/debian-12-standard.tar.zst``）。只拿
   * `node:vmid` 或 `volid` 当值，两条记录的值会一模一样 —— React 的 key 重复、
   * `items.find(i => i.value === value)` 永远命中第一条，选中 B 节点的那份、
   * 摘要卡却显示 A 节点的信息，图标也就跟着串了。
   */
  value: string;
  /** 提交给后端的原始标识：虚拟机 `node:vmid`，容器 `volid` */
  raw: string;
  name: string;
  /** 所属连接 / 节点（摘要与搜索用，也便于排查串味） */
  connection_id?: string;
  node?: string;
  /** 家族判定的兜底依据（PVE 的 ostype，可能为空） */
  ostype?: string | null;
  /** 第一列：内存 / 模板大小 */
  sizeText?: string;
  /** 第二列：磁盘 / 存储 */
  metaText?: string;
  /** 搜索时额外匹配的文本（VMID / 节点 / 存储 / volid） */
  keywords?: string;
  /** 行尾标签，如 `VMID 9000` */
  tags?: string[];
  /** 摘要卡第三行：节点等补充说明；缺省用家族的「适用场景」 */
  note?: string;
}

/** 虚拟机模板（`TemplateItem` 里选择器用得到的那部分） */
export interface VmTemplateLike {
  node: string;
  vmid: number;
  name: string;
  /** 所属连接：多台 PVE 上节点名与 VMID 都可能重复，必须参与唯一标识 */
  connection_id?: string;
  /** PVE 的 ostype（`/api/templates` 现在会填）；读不到时家族判定靠名称 */
  ostype?: string;
  type?: string;
  maxcpu?: number;
  maxmem?: number;
  maxdisk?: number;
}

/** 容器系统模板（`LxcTemplateItem` / `CtSystemTemplate` 的共同部分） */
export interface CtTemplateLike {
  volid: string;
  name: string;
  storage: string;
  /** 本地存储上同名文件在不同节点是两份，节点必须参与唯一标识 */
  node?: string;
  connection_id?: string;
  size?: number;
}

/**
 * 虚拟机模板 → 选择器候选。
 *
 * 列：内存（`maxmem`）/ 磁盘（`maxdisk`）；行尾挂 VMID —— 模板可以同名，
 * VMID 才是唯一标识；节点与核数放摘要卡第三行，不占每行的横向空间。
 */
export function vmTemplatePickItems(
  templates: VmTemplateLike[],
  t: TFunc,
): TemplatePickItem[] {
  return templates.map((tpl) => ({
    value: `${tpl.connection_id || ''}|${tpl.node}|${tpl.vmid}`,
    raw: `${tpl.node}:${tpl.vmid}`,
    name: tpl.name || t('templatePicker.templateName', { vmid: tpl.vmid }),
    connection_id: tpl.connection_id,
    node: tpl.node,
    // ostype 优先：同名模板可能一个是 Windows、一个是 Linux
    ostype: tpl.ostype || tpl.type,
    sizeText: tpl.maxmem ? formatBytes(tpl.maxmem, 0) : '—',
    metaText: tpl.maxdisk ? formatBytes(tpl.maxdisk, 0) : '—',
    keywords: `${tpl.vmid} ${tpl.node} ${tpl.name}`,
    tags: [`VMID ${tpl.vmid}`],
    note: t('templatePicker.vmNote', {
      node: tpl.node,
      cpu: tpl.maxcpu ?? '?',
    }),
  }));
}

/**
 * 容器系统模板 → 选择器候选。
 *
 * 列：大小 / 存储。名字去掉 PVE 的扩展名与架构后缀
 * （`debian-12-standard_12.7-1_amd64.tar.zst` 一行放不下，而这两段对「装哪套
 * 系统」没有信息量）。
 */
export function ctTemplatePickItems(
  templates: CtTemplateLike[],
): TemplatePickItem[] {
  return templates.map((tpl) => ({
    // volid 在本地存储上会跨节点重名，节点与连接都得进唯一值
    value: `${tpl.connection_id || ''}|${tpl.node || ''}|${tpl.volid}`,
    raw: tpl.volid,
    name: ctTemplateName(tpl.name),
    connection_id: tpl.connection_id,
    node: tpl.node,
    sizeText: tpl.size ? formatBytes(tpl.size, 0) : '—',
    metaText: tpl.storage,
    keywords: `${tpl.volid} ${tpl.storage} ${tpl.node || ''}`,
  }));
}
