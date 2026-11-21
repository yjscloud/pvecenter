/* ==========================================================================
   pvecenter — 网卡「IP 获取方式 + 网段」编辑块

   快速部署与自定义部署的**同一块**网络配置界面。抽出来只有一个理由：
   两边原先各写一份，规则就开始漂 —— 快速部署能选网段、能自动挑空闲地址，
   自定义部署却只能手填一个「192.168.1.10/24」并在另一处逐地址下拉里翻，
   同一个集群里两种填法。判定（池的适用范围）、联动（选网段顺带对齐网桥与
   VLAN）、兜底（没池就退回 DHCP）都收在这里，三个入口共用：
   快速部署、虚拟机自定义部署、容器自定义部署。

   交互（与「网络 → IP 地址池」里的定义一一对应）：

   * **IP 获取方式**：静态 / DHCP 两档；
   * **静态**：先选网段（只列本节点、本连接上有空余地址的池，且要与网卡的
     网桥 / VLAN 对得上），选中后自动把网桥与 VLAN 带过去，并预填该网段的
     第一个空闲地址（可改）；
   * **DHCP**：同样选网段 —— DHCP 服务器按二层网络发地址，「选网段」就是选
     VLAN / 网桥，这是唯一能控制「拿到哪个网段」的手段；选中同样对齐网桥与
     VLAN，并把这个池的 id 随请求下发，后端按池分配（见 app/ippool.py）；
   * 换节点 / 换 PVE 连接后，不再适用的选择会被清掉并重挑，不会出现「界面
     空着、状态里却还留着一个旧 id」这种提交后才发现的错。

   组件自己不做任何网络 IO：池清单由调用方查好传进来（同一个 queryKey，
   两个向导共用一份缓存）。
   ========================================================================== */

import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { IpPoolStatus } from '../api/types';
import { fitsPools } from '../utils/ippool';
import { parseVlan } from '../utils/vlan';
import { Badge } from './ui/Badge';
import { Field, Input, SegmentedControl, Select } from './ui/Input';
import { useT } from '../i18n';

/** 网络地址的获取方式 */
export type IpMode = 'dhcp' | 'static';

/** 一张网卡在这块界面里需要的最小状态 */
export interface SegmentValue {
  /** 接入网桥；选网段时会被对齐 */
  bridge: string;
  /** VLAN 原始文本；选网段时会被对齐 */
  vlan: string;
  ipMode: IpMode;
  /** 静态地址（CIDR，如 192.168.1.10/24）；DHCP 时无意义 */
  ip: string;
  gateway: string;
  /** 选中的网段（IP 池 id） */
  poolId: string;
}

export interface NicSegmentFieldsProps {
  value: SegmentValue;
  /** 回写改动；可能同时带上 bridge / vlan（网段自带二层网络） */
  onChange: (patch: Partial<SegmentValue>) => void;
  /** 目标节点 / PVE 连接：池的适用范围判定要用 */
  node: string;
  connection?: string;
  /** 当前用户可见的全部池（未按网桥过滤） */
  pools: IpPoolStatus[];
  /** 池清单是否已到；未到时不做任何自动选择，避免先落一个再改 */
  ready: boolean;
  /** 池清单读取失败时的提示（透传到网段下拉的 hint） */
  poolError?: string;
  /**
   * 是否给这张网卡选网段。
   *
   * 快速部署的附加网卡不选（批量时它们只能是 DHCP，见 forceDhcp）。
   */
  showPool?: boolean;
  /** 批量场景的附加网卡：只能是 DHCP，用徽标说明原因 */
  forceDhcp?: boolean;
  /** 地址由池按台分配（批量主网卡）：不渲染地址输入框 */
  hideAddress?: boolean;
  /** 静态地址的校验错误 */
  ipError?: string;
  disabled?: boolean;
  /**
   * 一个可用网段都没有时是否自动退回 DHCP。
   *
   * 默认开 —— 用户对着一个选不出东西的静态下拉框是查不出原因的；但克隆模式
   * 之类「IP 列表只是覆盖项」的场景应当关掉，否则会把用户明确的静态设置改掉。
   */
  autoFallbackDhcp?: boolean;
}

export function NicSegmentFields({
  value,
  onChange,
  node,
  connection,
  pools,
  ready,
  poolError,
  showPool = true,
  forceDhcp = false,
  hideAddress = false,
  ipError,
  disabled = false,
  autoFallbackDhcp = true,
}: NicSegmentFieldsProps) {
  const t = useT();

  /* onChange 每次渲染都是新函数（父组件里的箭头函数），放进 effect 依赖会
     让自动选择在每次渲染后重跑一遍。用 ref 拿最新的，稳定住 effect。 */
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const emit = useCallback((patch: Partial<SegmentValue>) => {
    changeRef.current(patch);
  }, []);

  /** 用户手动改过「获取方式」：不再自动退回 DHCP，否则会跟他的手选打架 */
  const modeTouchedRef = useRef(false);

  const vlan = parseVlan(value.vlan) ?? null;

  /* 静态可选的网段：要同时满足「网卡当前的网桥 / VLAN」「节点」「连接」。
     池没写网桥 / VLAN 时视为不限（见 utils/ippool 的取值约定）。 */
  const usablePools = useMemo(
    () => fitsPools(pools, { bridge: value.bridge, vlan, node, connection }),
    [pools, value.bridge, vlan, node, connection],
  );

  /* DHCP 可选的网段：**不按当前网桥 / VLAN 过滤** —— 用户正是要先选网段，再
     把网桥与 VLAN 对齐过去；按当前网桥过滤的话，挂在另一块桥上的网段根本选
     不到。节点与连接仍然要看：同一个 vmbr0 在不同节点上常常是不同网段。 */
  const segmentPools = useMemo(
    () => fitsPools(pools, { node, connection }),
    [pools, node, connection],
  );

  const poolOptions = useMemo(
    () => [
      { label: t('quickDeploy.selectPool'), value: '' },
      ...usablePools.map((p) => ({
        /* 池标了自己的 VLAN 就一并显示：同一个网桥下可能挂着好几个 VLAN 网段，
           只写「名称 · 子网」的话，两个池看起来像重复的 */
        label: `${t('quickDeploy.poolOption', {
          name: p.name,
          subnet: p.subnet,
          n: p.free_count,
        })}${p.vlan != null ? ` · ${t('ippool.colVlan')} ${p.vlan}` : ''}`,
        value: p.id,
      })),
    ],
    [usablePools, t],
  );

  /* 网段下拉只列真实网段：DHCP 的目的是「从某个网段拿地址」，摆一个「跟随网桥
     （不指定）」的默认项，等于允许一个语义上说不清的选择。 */
  const segmentOptions = useMemo(
    () =>
      segmentPools.map((p) => ({
        label: `${t('quickDeploy.segmentOption', {
          name: p.name,
          subnet: p.subnet,
        })}${p.vlan != null ? ` · ${t('ippool.colVlan')} ${p.vlan}` : ''}`,
        value: p.id,
      })),
    [segmentPools, t],
  );

  /** 把网段自带的二层网络带到网卡上（池没写 VLAN 时保留网卡当前值） */
  const linkOf = (pool: IpPoolStatus): Partial<SegmentValue> => ({
    bridge: pool.bridge || value.bridge,
    vlan: pool.vlan != null ? String(pool.vlan) : value.vlan,
  });

  const pickMode = (next: IpMode) => {
    modeTouchedRef.current = true;
    emit({ ipMode: next });
  };

  /* --- 1. 换节点 / 换连接后，选中的池可能已经不适用：清掉，别让它假装还在 ---
     地址也一起清：它属于刚被丢掉的那个网段，留着会被当成「已填好」提交。 */
  useEffect(() => {
    if (!ready || !value.poolId) return;
    if (segmentPools.some((p) => p.id === value.poolId)) return;
    emit({ poolId: '', ip: '', gateway: '' });
  }, [ready, value.poolId, segmentPools, emit]);

  /* --- 2. DHCP：没有「不指定」这一档，自动挑一个网段并对齐二层网络 ---
     已选但被后来的网桥 / 手改 VLAN 弄得对不上时，换到一个仍然匹配的；一个都不
     匹配就保留原选择（那是用户明确覆盖的，提交时用的仍是网桥 / VLAN 本身）。

     **不受 showPool 影响**：快速部署里附加网卡不显示网段下拉，但它们的 DHCP
     地址同样要落到某个网段上（否则 N 台机器的第二张网卡会去问外部 DHCP）。 */
  useEffect(() => {
    if (!ready || value.ipMode !== 'dhcp') return;
    if (segmentPools.length === 0) return;

    const fits = fitsPools(segmentPools, {
      bridge: value.bridge,
      vlan,
      node,
      connection,
    });

    if (value.poolId && segmentPools.some((p) => p.id === value.poolId)) {
      if (fits.length === 0 || fits.some((p) => p.id === value.poolId)) return;
      emit({ poolId: fits[0].id, ...linkOf(fits[0]) });
      return;
    }
    const pick = fits[0] ?? segmentPools[0];
    emit({ poolId: pick.id, ...linkOf(pick) });
  }, [
    ready,
    value.ipMode,
    value.poolId,
    value.bridge,
    vlan,
    segmentPools,
    node,
    connection,
    emit,
    // linkOf 只读 value，不单列
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ]);

  /* --- 3. 静态：没选网段（或所选网段已不适用，例如换了网桥）时自动挑一个 ---
     用户不必先知道「哪台机器的地址池在哪」；挑完由下一个 effect 填具体地址。 */
  useEffect(() => {
    if (!ready || !showPool || value.ipMode !== 'static') return;
    if (usablePools.some((p) => p.id === value.poolId)) return;
    const first = usablePools.find((p) => p.free_count > 0) ?? usablePools[0];
    if (!first) return;
    emit({ poolId: first.id, ip: '' });
  }, [ready, showPool, value.ipMode, value.poolId, usablePools, emit]);

  /* --- 4. 选好网段但地址还空着：取该网段的第一个空闲地址（可改）--- */
  useEffect(() => {
    if (value.ipMode !== 'static' || !value.poolId) return;
    if (hideAddress || value.ip.trim()) return;
    const pool = pools.find((p) => p.id === value.poolId);
    const next = pool?.free?.[0];
    if (!next) return;
    emit({ ip: next, gateway: pool?.gateway || value.gateway });
  }, [value.ipMode, value.poolId, value.ip, value.gateway, hideAddress, pools, emit]);

  /* --- 5. 一个可用网段都没有（没配 IP 池 / 池已满）：静态无从选起，退回 DHCP，
     而不是让用户对着空下拉框找原因。用户手动选过静态就不再干预。 --- */
  useEffect(() => {
    if (!ready || !autoFallbackDhcp || modeTouchedRef.current) return;
    if (value.ipMode !== 'static' || usablePools.length > 0) return;
    emit({ ipMode: 'dhcp' });
  }, [ready, autoFallbackDhcp, value.ipMode, usablePools, emit]);

  return (
    <>
      <div className={`dyn-row mt-12 nic-seg-mode${forceDhcp ? ' nic-seg-mode-wide' : ''}`}>
        <Field label={t('quickDeploy.labelIpMode')}>
          {forceDhcp ? (
            /* 附加网卡没有第二套池可分配，批量时只能是 DHCP：
               不给一个点不动的静态按钮，直接说清原因 */
            <div className="flex items-center gap-8 flex-wrap">
              <Badge variant="info">DHCP</Badge>
              <span className="fs-sm text-muted">{t('quickDeploy.batchDhcpOnly')}</span>
            </div>
          ) : (
            <SegmentedControl<IpMode>
              value={value.ipMode}
              onChange={pickMode}
              ariaLabel={t('quickDeploy.labelIpMode')}
              options={[
                { label: t('quickDeploy.ipModeStatic'), value: 'static' },
                { label: 'DHCP', value: 'dhcp' },
              ]}
            />
          )}
        </Field>
      </div>

      {value.ipMode === 'static' ? (
        <div className={`dyn-row mt-12${hideAddress ? ' nic-seg-pool' : ''}`}>
          {showPool ? (
            <Select
              label={t('quickDeploy.labelPool')}
              value={value.poolId}
              disabled={disabled}
              onChange={(e) => {
                /* 池记着它属于哪个二层网络（网桥 + VLAN）：选中后把这两项一起带上，
                   省得用户再对一遍 —— 对不上的池本来也选不到（见 usablePools）。
                   地址随之清空，交给下面的自动预填按新网段重新取。 */
                const picked = usablePools.find((p) => p.id === e.target.value);
                emit({
                  poolId: e.target.value,
                  ip: '',
                  ...(picked ? linkOf(picked) : {}),
                });
              }}
              options={poolOptions}
              hint={poolError || t('quickDeploy.poolHint')}
            />
          ) : null}
          {hideAddress ? null : (
            <>
              <Input
                label={t('quickDeploy.labelIp')}
                required
                value={value.ip}
                disabled={disabled}
                onChange={(e) => emit({ ip: e.target.value })}
                placeholder={t('quickDeploy.ipPlaceholder')}
                error={ipError}
                mono
              />
              <Input
                label={t('quickDeploy.labelGateway')}
                value={value.gateway}
                disabled={disabled}
                onChange={(e) => emit({ gateway: e.target.value })}
                placeholder={t('quickDeploy.gatewayPlaceholder')}
                mono
              />
            </>
          )}
        </div>
      ) : showPool && segmentPools.length > 0 ? (
        /* DHCP 也要能指定「从哪个网段拿地址」：DHCP 服务器按二层网络分地址，
           选网段就是选 VLAN / 网桥，也是唯一能控制「拿到哪个网段」的手段。
           池都没配时干脆不摆这个框，避免出现一个点不出任何东西的空壳。 */
        <div className={`dyn-row mt-12${hideAddress ? ' nic-seg-pool' : ''}`}>
          <Select
            label={t('quickDeploy.labelSegment')}
            value={value.poolId}
            disabled={disabled}
            onChange={(e) => {
              const picked = segmentPools.find((p) => p.id === e.target.value);
              emit({
                poolId: e.target.value,
                ...(picked ? linkOf(picked) : {}),
              });
            }}
            options={segmentOptions}
            hint={poolError || t('quickDeploy.segmentHint')}
          />
        </div>
      ) : null}
    </>
  );
}
