/* ===========================================================================
   节点上可供客户机接入的网桥列表

   为什么要有这个 hook：网桥选择出现在五处（新建虚拟机、克隆虚拟机、快速
   部署、新建容器、重装系统），每处原本各写一遍过滤条件，规则还不一样：

   * 快速部署 / 新建虚拟机只认 ``type === 'bridge'`` —— 用 OVS 的环境里
     整张列表是空的，用户只能手打一个名字；
   * 新建容器额外认 ``OVSBridge``，还会标出「未激活」；
   * 重装系统干脆没有网桥这一项。

   静态地址和 DHCP 用的是**同一块网卡**，网桥可选范围就不该跟着地址模式变。
   这里把「哪些接口能当网桥」「怎么排序」「未激活怎么标」收敛成一处，各处
   只负责把它渲染成一个下拉框。
   ========================================================================== */

import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';

import { nodesApi } from '../api/endpoints';
import type { NetworkInterface } from '../api/types';
import { useT } from '../i18n';

export interface BridgeOption {
  label: string;
  value: string;
}

/** PVE 用 ``bridge`` 表示 Linux 网桥，``OVSBridge`` 表示 Open vSwitch 网桥 */
const BRIDGE_TYPES = new Set(['bridge', 'ovsbridge']);

/** 这个接口能不能作为客户机的接入网桥（物理网卡、bond、vlan 都不行） */
export function isBridgeInterface(iface: NetworkInterface): boolean {
  return (
    Boolean(iface.iface) &&
    BRIDGE_TYPES.has(String(iface.type ?? '').toLowerCase())
  );
}

/**
 * 读取某节点上可用的网桥。
 *
 * @param node       节点名（调用方还没选节点时传空，查询不会发出）
 * @param connectionId 目标连接（多集群时必传，否则会读到默认连接的同名节点）
 * @param enabled    额外开关（例如向导还没打开时不请求）
 */
export function useNodeBridges(
  node: string | undefined,
  connectionId: string | undefined,
  enabled = true,
) {
  const t = useT();

  const query = useQuery({
    /* 与各页原本的 key 保持一致：多处同时打开时共用同一份缓存 */
    queryKey: ['nodes', connectionId, node, 'network'],
    queryFn: () => nodesApi.network(node as string, connectionId),
    enabled: enabled && Boolean(node),
    staleTime: 60_000,
    retry: false,
  });

  const options = useMemo<BridgeOption[]>(() => {
    const list = (query.data ?? []).filter(isBridgeInterface);
    return [...list]
      /* 激活的排前面：正在提供服务的网桥是绝大多数人要选的那个；同类里按名字
         排序，避免 PVE 返回顺序变化导致下拉框「跳来跳去」。 */
      .sort(
        (a, b) =>
          Number(Boolean(b.active)) - Number(Boolean(a.active)) ||
          a.iface.localeCompare(b.iface),
      )
      .map((iface) => ({
        label: iface.active === false
          ? `${iface.iface}${t('common.inactiveSuffix')}`
          : iface.iface,
        value: iface.iface,
      }));
  }, [query.data, t]);

  return {
    /** 可直接喂给 ``<Select options>`` 的网桥候选 */
    options,
    /** 节点上确实存在网桥（用来判断要不要提示「该节点没有网桥」） */
    hasBridges: options.length > 0,
    isLoading: query.isLoading,
    isError: query.isError,
    error: query.error,
    /** 原始接口列表：少数场景（如需要区分 OVS）会用到 */
    interfaces: query.data ?? [],
  };
}
