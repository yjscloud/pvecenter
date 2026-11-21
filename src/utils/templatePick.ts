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
  /**
   * 系统盘容量（GB）：选资源规格时的容量下限。
   *
   * `null` = 读不到（节点离线 / 模板刚删）—— 按「未知」放行，由后端再判一次。
   */
  minDiskGb?: number | null;
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
  /** 系统盘容量（GB）：后端从模板 config 解析，读不到为 null */
  rootfs_gb?: number | null;
  /** 模板磁盘所在的存储名；跨节点克隆的判定依据（见 clonePlacement） */
  storages?: string[];
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

/** 目标节点上的一块存储（只关心判定用得到的两个字段） */
export interface PlacementStorage {
  storage: string;
  shared?: boolean;
}

/**
 * 模板能不能把这台机器克隆到 `targetNode` 上。
 *
 * - `same-node`：模板就在目标节点（默认路径，永远可用）；
 * - `cross-node`：模板在别的节点，但磁盘全在共享存储上、且目标节点挂得到；
 * - `blocked`：跨节点但缺存储 / 存储是本地盘，PVE 一定会拒（原因见 missing/local）；
 * - `unknown`：拿不到判定依据（旧后端没给 `storages`，或存储列表还没到）。
 *
 * 为什么「未知」要放行而不是拦住：拦住的代价是用户看到一张空列表、以为模板
 * 没了（这正是这次要修的问题）；放行的话最坏情况是后端校验返回一条点名存储的
 * 报错（见后端 app/cloneguard.py），照样能自查。
 */
export type ClonePlacementKind = 'same-node' | 'cross-node' | 'blocked' | 'unknown';

export interface ClonePlacement {
  kind: ClonePlacementKind;
  /** 目标节点上根本没挂载的存储 */
  missing: string[];
  /** 挂得到、但是本地存储（跨节点引不到那块卷） */
  local: string[];
}

export function clonePlacement(
  tpl: Pick<VmTemplateLike, 'node' | 'storages'>,
  targetNode: string,
  /** 目标节点的存储清单；`undefined` = 还没读到（按未知处理） */
  targetStorages: PlacementStorage[] | undefined,
): ClonePlacement {
  const empty: ClonePlacement = { kind: 'unknown', missing: [], local: [] };
  if (!targetNode || tpl.node === targetNode) {
    return { kind: 'same-node', missing: [], local: [] };
  }
  const disks = (tpl.storages ?? []).filter(Boolean);
  if (disks.length === 0) return empty;
  if (!targetStorages) return empty;

  const byName = new Map(targetStorages.map((item) => [item.storage, item]));
  const missing: string[] = [];
  const local: string[] = [];
  for (const name of disks) {
    const hit = byName.get(name);
    if (!hit) missing.push(name);
    else if (!hit.shared) local.push(name);
  }
  if (missing.length > 0 || local.length > 0) {
    return { kind: 'blocked', missing, local };
  }
  return { kind: 'cross-node', missing: [], local: [] };
}

/**
 * 虚拟机模板 → 选择器候选。
 *
 * 列：内存（`maxmem`）/ 磁盘（`maxdisk`）；行尾挂 VMID —— 模板可以同名，
 * VMID 才是唯一标识；节点与核数放摘要卡第三行，不占每行的横向空间。
 *
 * 传了 `targetNode` 时，跨节点候选的摘要行会点明「将克隆到哪台节点」：
 * 列表里同时有本节点与别节点的模板时，用户必须一眼看出克隆体会落在哪里。
 */
export function vmTemplatePickItems(
  templates: VmTemplateLike[],
  t: TFunc,
  targetNode?: string,
): TemplatePickItem[] {
  return templates.map((tpl) => {
    const crossNode = Boolean(targetNode) && tpl.node !== targetNode;
    return {
      value: `${tpl.connection_id || ''}|${tpl.node}|${tpl.vmid}`,
      raw: `${tpl.node}:${tpl.vmid}`,
      name: tpl.name || t('templatePicker.templateName', { vmid: tpl.vmid }),
      connection_id: tpl.connection_id,
      node: tpl.node,
      // ostype 优先：同名模板可能一个是 Windows、一个是 Linux
      ostype: tpl.ostype || tpl.type,
      sizeText: tpl.maxmem ? formatBytes(tpl.maxmem, 0) : '—',
      // 系统盘优先用后端解析出的整数 GB：`maxdisk` 是字节、还可能把数据盘算进去。
      metaText: tpl.rootfs_gb
        ? `${tpl.rootfs_gb}G`
        : tpl.maxdisk
          ? formatBytes(tpl.maxdisk, 0)
          : '—',
      /* 规格容量校验用的下限：后端没给 rootfs_gb 时按 maxdisk 向上取整兜底，
         两者都没有就是 null（不拦）。 */
      minDiskGb:
        tpl.rootfs_gb ??
        (tpl.maxdisk ? Math.ceil(tpl.maxdisk / 1024 ** 3) : null),
      keywords: `${tpl.vmid} ${tpl.node} ${tpl.name}`,
      tags: [`VMID ${tpl.vmid}`],
      note: crossNode
        ? t('templatePicker.vmNoteCrossNode', {
            node: tpl.node,
            cpu: tpl.maxcpu ?? '?',
            target: targetNode ?? '',
          })
        : t('templatePicker.vmNote', {
            node: tpl.node,
            cpu: tpl.maxcpu ?? '?',
          }),
    };
  });
}

/** 「这台节点没有可用模板」时要交代的别节点情况（两个创建入口共用） */
export interface ElsewhereTemplates {
  /** 别节点上的模板总数 */
  count: number;
  /** 出现在哪些节点上 */
  nodes: string[];
  /** 其中因为存储原因无法跨节点克隆的条数 */
  blocked: number;
  /** 目标节点上没挂载的共享存储 */
  missing: string[];
  /** 挂得到但属于本地的存储 */
  local: string[];
}

/**
 * 「本节点没有可用克隆源」的提示文案；返回 `null` 表示别节点也没有模板。
 *
 * 三种情况必须分开说，否则用户会朝错误方向排查：
 * 1. 别节点有模板、也能跨节点克隆 —— 那它本来就该出现在候选里，走到这里说明
 *    判定还是「未知」，按原来的「先迁模板」说法；
 * 2. 共享存储没挂到本节点 —— 去 PVE 里给这台节点勾上该存储即可，模板不用动；
 * 3. 模板盘在本地存储上 —— 只能迁模板，或改用同节点克隆。
 */
export function elsewhereTemplatesHint(
  t: TFunc,
  node: string,
  elsewhere: ElsewhereTemplates,
): string | null {
  if (elsewhere.count === 0) return null;
  if (elsewhere.blocked === 0) {
    return t('templatePicker.vmEmptyOnNode', {
      node,
      others: elsewhere.nodes.join(' / '),
      n: elsewhere.count,
    });
  }
  const reasons: string[] = [];
  if (elsewhere.missing.length > 0) {
    reasons.push(
      t('templatePicker.vmMissingStorage', {
        storages: elsewhere.missing.join(' / '),
        node,
      }),
    );
  }
  if (elsewhere.local.length > 0) {
    reasons.push(
      t('templatePicker.vmLocalStorage', {
        storages: elsewhere.local.join(' / '),
      }),
    );
  }
  return t('templatePicker.vmBlockedOnNode', {
    node,
    others: elsewhere.nodes.join(' / '),
    n: elsewhere.blocked,
    reasons: reasons.join('；'),
  });
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
