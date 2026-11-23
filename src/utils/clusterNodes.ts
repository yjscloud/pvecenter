/**
 * 迁移目标节点的过滤。
 *
 * 面板可以把多套彼此独立的 Proxmox 当同级管理，而 `/nodes` 是**合并**返回的
 * （每一项都带 `connection_id`）。PVE 的迁移只能在**同一个集群**里换节点：
 * 跨两套 PVE 没有共享的集群网络与 `pmxcfs`，机器根本搬不过去。界面若直接拿
 * 合并清单当下拉，就会出现「另一套集群的节点」这种永远不可能成功的选项 ——
 * 选中之后要么报一句看不懂的 PVE 错误，要么（两套集群有同名节点时）看起来
 * 成功了而实际什么都没发生。
 *
 * 所以下拉只保留**与这台机器同属一条连接（= 一个 PVE 集群）**的其它节点。
 * 后端还会再校验一次（见 `app/migrateguard`）：界面只是体验，接口才是边界。
 */
import type { NodeInfo } from '../api/types';

/** 迁移对象的身份：节点 + 所属连接（虚拟机 / 容器都满足这个形状） */
export interface GuestClusterRef {
  node: string;
  connection_id?: string;
}

function clusterOf(node: NodeInfo): string {
  return String(node.connection_id ?? '');
}

/**
 * 可作为迁移目标的节点（同集群、非当前节点）。
 *
 * - 连接已知：严格按 `connection_id` 过滤，同名节点也分得清是哪一套集群的；
 * - 连接未知（旧链接没带 `?connection=`）：用「当前节点所在的那条连接」推定；
 * - 连当前节点都定位不到：返回空数组 —— 此时没有任何依据判断哪个节点是同一
 *   集群的，宁可让用户看到「没有可迁移的目标节点」，也不列出跨集群的选项。
 */
export function migrationTargetNodes(
  nodes: NodeInfo[] | undefined | null,
  guest: GuestClusterRef | null | undefined,
): NodeInfo[] {
  const all = nodes ?? [];
  if (!guest) return [];

  const wanted = String(guest.connection_id ?? '');
  if (wanted) {
    return all.filter((n) => n.node !== guest.node && clusterOf(n) === wanted);
  }

  const home = all.find((n) => n.node === guest.node);
  if (!home) return [];
  const homeCluster = clusterOf(home);
  return all.filter((n) => n.node !== guest.node && clusterOf(n) === homeCluster);
}
