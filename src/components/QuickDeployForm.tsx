/* ==========================================================================
   pvecenter — 快速部署表单

   原来这是一个独立页面（/deploy，侧边栏「快速部署」）。但「部署」和「创建」
   本来就是同一件事的两种做法：一页下单适合标准机器，六步向导适合要调 NUMA /
   PCI 直通 / 多网卡多磁盘的机器。做成两个入口后，用户得先想清楚「我该进哪个
   页面」—— 而这个问题本身就不该存在。

   现在它内嵌在「创建虚拟机」与「创建容器」的同一个弹窗里，顶部一个
   「快速部署 / 自定义部署」开关切换，入口只有一个。

   顺序按运维的实际填写习惯排：**先起名，再选规格与位置，最后选镜像与网络**。
   规格由管理员在「设置 → 资源规格」里定义，用户不必自己算核数与内存。

   两个刻意的固定做法：

   * **克隆方式**：初值取「设置 → 创建默认值」里的面板默认（未配置即链接克隆），
     「系统镜像」卡里那个开关紧挨着模板选择器 —— 它决定的正是「这个模板怎么
     变成一台机器」，与模板本就是一回事。存储下拉框只在「磁盘真要落到某个
     存储上」时出现：链接克隆不写任何存储（磁盘留在模板所在存储），摆一个
     必填的下拉框只会让人以为必须选。
   * **静态 IP**：绝大多数机器要固定地址，所以默认静态，并从「网络 → IP 地址池」
     里自动挑一个空闲地址（可改）。池子为空时退回 DHCP，而不是让用户卡在
     一个选不出东西的下拉框前。

   下单走两条路，但只有一层业务逻辑：数量 1 仍发 POST /api/vms / /api/lxc；
   数量 > 1 发 POST /api/vms/batch / /api/lxc/batch。后端批量入口只在外面加
   「总量配额预检 + VMID 分配 + 逐台结果」，每台仍调用与单台相同的创建主体，
   归属、创建时间、审计、通知不会分成两套实现。

   客户机类型（虚拟机 / 容器）由外层向导决定，本组件不再自选 —— 用户点的就是
   「创建虚拟机」或「创建容器」。
   ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  configApi,
  connectionsApi,
  ipPoolsApi,
  lxcApi,
  nodesApi,
  quotaApi,
  templatesApi,
  vmsApi,
} from '../api/endpoints';
import { errorMessage } from '../api/client';
import { invalidateQuota } from '../hooks/quotaCache';
import { getGlobalScope } from '../api/scope';
import { checkGuestSpec, remainingSummary, type QuotaViolation } from '../utils/quotaSpec';
import { Card, CardHeader } from './ui/Card';
import { Button, IconButton } from './ui/Button';
import { Field, Input, Select, Switch } from './ui/Input';
import { Notice } from './ui/EmptyState';
import { Spinner } from './ui/Spinner';
import { NodePicker } from './NodePicker';
import { SystemTemplatePicker } from './SystemTemplatePicker';
import {
  clonePlacement,
  ctTemplatePickItems,
  elsewhereTemplatesHint,
  vmTemplatePickItems,
  type TemplatePickItem,
} from '../utils/templatePick';
import { isPathDisabled, SSH_SECURITY_NAV_PATH } from './Sidebar';
import { IconCloud, IconPlus, IconTrash } from './Icons';
import { OsLogo } from './OsLogo';
import { osFamilyLabel, templateFamily } from '../utils/osFamily';
import { useNodeBridges } from '../hooks/useNodeBridges';
import { useToast } from '../hooks/useToast';
import { useUiPrefs } from '../hooks/useUiPrefs';
import { useAuth } from '../hooks/useAuth';
import { useNodeStorages } from '../hooks/useNodeStorages';
import { useT, type MessageKey } from '../i18n';
import { formatBytes, normalizeSizeText } from '../utils/format';
import { parseVlan, vlanInvalid } from '../utils/vlan';
import { BatchCreateProgress } from './BatchCreateProgress';
import { NicSegmentFields, type IpMode } from './NicSegmentFields';
import { SpecPicker } from './SpecPicker';
import type {
  BatchCreateResponse,
  LxcCreateRequest,
  VmCreateRequest,
} from '../api/types';

/** 客户机类型：与向导的 kind 对齐（qemu = 虚拟机，lxc = 容器） */
export type QuickDeployKind = 'qemu' | 'lxc';

/** GB → MB（规格里存 MB，界面上说 GB） */
const gb = (mb: number) => Math.round(mb / 1024);

/* 上次用过的那一档规格：只记在本机（机房里的机器很杂，同一套规格不会到处通用），
   用于「推荐」标记 —— 不替用户选中，只在他犹豫时给一个默认答案。
   localStorage 在隐私模式下会抛，读写都包一层：记不住也不能影响下单。 */
const LAST_SPEC_KEY = 'pvecenter:last-spec';

function readLastSpec(kind: QuickDeployKind): string {
  try {
    return localStorage.getItem(`${LAST_SPEC_KEY}:${kind}`) ?? '';
  } catch {
    return '';
  }
}

function writeLastSpec(kind: QuickDeployKind, specId: string) {
  try {
    localStorage.setItem(`${LAST_SPEC_KEY}:${kind}`, specId);
  } catch {
    /* 忽略 */
  }
}

/**
 * 越界维度 → 界面词条。
 *
 * 单台封顶（``max_*``）与对应的总量维度并成一项：用户能做的动作都是「把这一项
 * 调小」，分开列只会让提示变长。额度被顶掉时必须说清是**哪一项** —— 只说
 * 「超出额度」的话，用户还得自己拿规格去对剩余读数。
 */
const QUOTA_FIELD_LABEL: Record<QuotaViolation, MessageKey> = {
  count: 'quota.fieldLabel.count',
  cores: 'quota.fieldLabel.cores',
  max_cores: 'quota.fieldLabel.cores',
  memory_mb: 'quota.fieldLabel.memory',
  max_memory_mb: 'quota.fieldLabel.memory',
  disk_gb: 'quota.fieldLabel.disk',
  max_disk_gb: 'quota.fieldLabel.disk',
};

/**
 * 快速部署的一张网卡。
 *
 * 主网卡（列表第一张）额外走「IP 池自动挑地址」那套，附加网卡手填即可 ——
 * 快速部署要的是「少填几个空就下单」，不是把自定义向导整个搬过来。
 */
interface QuickNic {
  id: string;
  bridge: string;
  /** VLAN 标签（PVE 的 tag）；'' = 不划 VLAN */
  vlan: string;
  /** 速率上限（MB/s）；'' = 不限速 */
  rate: string;
  ipMode: IpMode;
  ip: string;
  gateway: string;
  /** 选中的 IP 池（只有主网卡用） */
  poolId: string;
}

let nicSeq = 0;

function blankNic(ipMode: IpMode = 'static'): QuickNic {
  nicSeq += 1;
  return {
    id: `nic-${nicSeq}`,
    bridge: '',
    vlan: '',
    rate: '',
    ipMode,
    ip: '',
    gateway: '',
    poolId: '',
  };
}

/** 批量命名的序号从 1 开始；``{i:02}`` 表示至少两位、不足补零 */
function expandName(pattern: string, base: string, index: number): string {
  return pattern
    .replace(/\{name\}/g, base)
    .replace(/\{i(?::(\d+))?\}/g, (_match, width: string | undefined) => {
      const digits = Number(width || 0);
      return digits > 0 ? String(index).padStart(digits, '0') : String(index);
    })
    .trim();
}

export interface QuickDeployFormProps {
  /** 弹窗是否打开：关闭时停掉所有取数，打开时把表单重置为干净状态 */
  open: boolean;
  /** 由外层向导决定，本组件不再自选类型 */
  kind: QuickDeployKind;
  /** 创建成功（拿得到 VMID 时带上） */
  onCreated?: (vmid: number) => void;
  /** 取消 / 关闭弹窗 */
  onClose: () => void;
  /** 切到同弹窗里的自定义向导 */
  onSwitchToCustom: () => void;
}

export function QuickDeployForm({
  open,
  kind,
  onCreated,
  onClose,
  onSwitchToCustom,
}: QuickDeployFormProps) {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { hasPermission, isAdmin } = useAuth();
  const isVm = kind === 'qemu';
  /* 建机权限随类型走：虚拟机 vm.create、容器 ct.create */
  const canCreate = hasPermission(isVm ? 'vm.create' : 'ct.create');

  /* ---- 选择项 ---- */
  const [name, setName] = useState('');
  /* 批量下发：1 = 保持原来的单台表单与单台接口；>1 才走 /vms/batch。
     命名模板只在前端展开，后端收到的仍是完整、互不相同的 name。 */
  const [quantity, setQuantity] = useState(1);
  const [namePattern, setNamePattern] = useState('{name}-{i:02}');
  const [batchResult, setBatchResult] = useState<BatchCreateResponse | null>(null);
  const [tags, setTags] = useState('');
  const [specId, setSpecId] = useState('');
  const [targetConn, setTargetConn] = useState('');
  const [node, setNode] = useState('');
  const [templateRef, setTemplateRef] = useState(''); // "node:vmid"
  const [osTemplate, setOsTemplate] = useState('');
  const [storage, setStorage] = useState('');
  /* 完整克隆：关 = 链接克隆（秒级完成、不额外占空间，但磁盘依赖模板）。
     开启后把模板磁盘整份复制到所选存储 —— 创建慢一些、多占一份空间，换来的是
     这台机器与模板彻底脱钩（模板删改都不影响它）。与自定义部署是同一件事。
     初值不是写死的 false：打开时按面板默认（见下面的预填效果）落一次。 */
  const [fullClone, setFullClone] = useState(false);
  /* 默认静态：机器一般要固定地址；网段与地址由下面的 effect 自动挑 */
  const [nics, setNics] = useState<QuickNic[]>(() => [blankNic()]);
  const [ciUser, setCiUser] = useState('root');
  const [password, setPassword] = useState('');
  /* 接入安全管控：把面板公钥写进 cloud-init，机器起来后自动登记为受管主机。
     与「自定义部署」向导里的那个开关同一套后端能力。 */
  const [manage, setManage] = useState(false);
  const [busy, setBusy] = useState(false);

  /* 「SSH 安全」入口被面板级开关关掉时，「接入安全管控」整个收起来（见下面的
     渲染）：勾了它机器会被登记成受管主机，而看这些主机的地方（SSH 安全 /
     登录审计 / 端口进程 / 安全基线）都进不去了 —— 留一个「勾了没处看」的
     开关只会让人困惑。
     取值一并钉住：先勾上、之后入口被关掉时，不会把 true 悄悄提交上去。 */
  const navDisabled = useUiPrefs().nav_disabled;
  const sshNavOff = isPathDisabled(SSH_SECURITY_NAV_PATH, navDisabled);
  const manageOn = manage && !sshNavOff;

  /** 用户自己点过 IP 获取方式：之后不再被「有没有可用网段」的自动判断覆盖 */

  /** 已经按面板默认值预选过克隆方式：每次打开只填一次，不覆盖用户的选择 */
  const cloneDefaultRef = useRef(false);

  /** 主网卡：IP 池自动挑地址那套只作用于它 */
  const primaryNic = nics[0];

  /* 批量下发：N 台共用同一份网络配置，静态地址会让它们撞在同一个 IP 上 */
  const batchMode = quantity > 1;

  const updateNic = (index: number, patch: Partial<QuickNic>) => {
    setNics((prev) => prev.map((n, i) => (i === index ? { ...n, ...patch } : n)));
  };

  const addNic = () => setNics((prev) => [...prev, blankNic('dhcp')]);

  const removeNic = (index: number) =>
    setNics((prev) => (prev.length > 1 ? prev.filter((_, i) => i !== index) : prev));

  /* 每次打开都从干净状态开始：弹窗关闭时组件并没有卸载，残留的字段会带到下一台 */
  useEffect(() => {
    if (!open) return;
    setName('');
    setQuantity(1);
    setNamePattern('{name}-{i:02}');
    setBatchResult(null);
    setTags('');
    setSpecId('');
    setNode('');
    setTemplateRef('');
    setOsTemplate('');
    setStorage('');
    setNics([blankNic()]);
    setCiUser('root');
    setPassword('');
    setManage(false);
    /* 克隆方式回到面板默认值（下面那个效果会按「设置 → 创建默认值」预选）；
       先落回链接克隆是为了「默认值还没取回来」时不残留上一次的选择 ——
       上一次开了完整克隆就悄悄沿用下去，等于替用户改了磁盘语义 */
    setFullClone(false);
    cloneDefaultRef.current = false;
    /* targetConn 不清：保留上次选的主机（多台 PVE 时省一次选择），下面那个
       effect 只在它为空时兜底选当前连接 */
  }, [open]);

  /* ---- 数据 ---- */
  const specsQuery = useQuery({
    queryKey: ['config', 'specs'],
    queryFn: configApi.getSpecs,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  /* 面板默认克隆方式（服务端设置，见「设置 → 创建默认值」）。这里只当**初值** */
  const vmDefaultsQuery = useQuery({
    queryKey: ['config', 'vm-defaults'],
    queryFn: configApi.getVmDefaults,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  /* 按面板默认值预选克隆方式。只在每次打开后**第一次**拿到默认值时填一次：
     用户点过那个开关之后再回填，等于把他刚改的选择顶掉。 */
  useEffect(() => {
    if (!open || cloneDefaultRef.current || !vmDefaultsQuery.data) return;
    cloneDefaultRef.current = true;
    setFullClone(Boolean(vmDefaultsQuery.data.full_clone));
  }, [open, vmDefaultsQuery.data]);

  /* 目标主机列表只有管理员拿得到（/connections 需要 settings.manage）；
     普通用户用面板当前连接 —— 单台 PVE 的面板上本来就该是这样 */
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  const nodesQuery = useQuery({
    queryKey: ['nodes', targetConn],
    queryFn: () => nodesApi.list(targetConn),
    enabled: open,
    staleTime: 60_000,
  });

  /* 两处需要选存储：容器建 rootfs；虚拟机开了「完整克隆」时磁盘要有落点。
     虚拟机那条**即使链接克隆也要读**：候选模板里可能有别的节点上、盘在共享存储
     的模板，要不要让它跨过来，取决于所选节点有没有挂载那块存储（见
     utils/templatePick 的 clonePlacement）。多一次请求换「共享存储模板看不见」
     这个坑，值得。
     候选只列**所选节点上**的存储池（含显式带连接，见 useNodeStorages）。 */
  const storagesQuery = useNodeStorages(node, targetConn, open);

  /* 网桥候选：与新建虚拟机 / 新建容器 / 重装共用同一套筛选（见 useNodeBridges）。
     静态地址与 DHCP 都用这块网卡，所以两种模式下候选完全相同。 */
  const bridges = useNodeBridges(node, targetConn, open && Boolean(node));

  /* 只列虚拟机模板：快速部署固定「从模板克隆」（容器走系统模板） */
  const templatesQuery = useQuery({
    queryKey: ['templates', 'all', targetConn],
    queryFn: () => templatesApi.list(targetConn, 'qemu'),
    enabled: open && isVm,
    staleTime: 30_000,
  });

  const osTemplatesQuery = useQuery({
    queryKey: ['lxc', 'templates', targetConn, node],
    queryFn: () => lxcApi.templates(node, targetConn),
    enabled: open && !isVm && Boolean(node),
    staleTime: 60_000,
    retry: false,
  });

  const poolsQuery = useQuery({
    queryKey: ['ip-pools'],
    queryFn: ipPoolsApi.get,
    /* 静态与 DHCP 都要读：静态按池分配地址；DHCP 用池来确定「接入哪个网段」
       （池记着它的网桥与 VLAN），选完把网卡对齐过去，DHCP 请求就从那条二层
       网络出去。之前这里卡了 ipMode === 'static'，DHCP 模式下连池都不拉，
       界面上自然没有任何网段可选。 */
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });

  const quotaQuery = useQuery({
    queryKey: [isVm ? 'vms' : 'lxc', 'quota'],
    queryFn: isVm ? vmsApi.quota : lxcApi.quota,
    enabled: open,
  });

  /* ---- 派生 ---- */
  const specs = useMemo(
    () =>
      (specsQuery.data?.specs ?? []).filter(
        (s) => s.kind === (isVm ? 'vm' : 'lxc') || s.kind === 'both',
      ),
    [specsQuery.data, isVm],
  );
  const spec = specs.find((s) => s.id === specId);

  const connectionOptions = useMemo(() => {
    const list = connectionsQuery.data ?? [];
    if (list.length === 0) return [];
    return list.map((c) => ({
      /* 标签里**不放主机地址**：普通用户不需要直接连这台 PVE，而「面板接了哪些
         主机、地址是什么」属于管理信息，不该从一个创建表单里漏出去。连接名在
         创建时默认就是主机地址，够认得出是哪台了。 */
      label: `${c.name || c.host}${
        c.active ? t('quickDeploy.currentConn') : ''
      }`,
      value: c.id,
    }));
  }, [connectionsQuery.data, t]);

  /* 默认落在顶栏「集群作用域」选的那一套，其次是兜底连接。
     不指定连接时后端会把所有 PVE 的节点合并返回 —— 那个列表在创建表单里没法用。
     先看作用域：用户刚把作用域切到 B，进向导却默认在 A，多半不是他想要的。 */
  useEffect(() => {
    if (!open || targetConn) return;
    const list = connectionsQuery.data ?? [];
    const target =
      list.find((c) => c.id === getGlobalScope()) ??
      list.find((c) => c.active) ??
      list[0];
    if (target) setTargetConn(target.id);
  }, [open, targetConn, connectionsQuery.data]);

  /* 换主机：节点、存储、模板都不通用，全部清掉重选。模板本来就要求用户自己确认，
     清掉之后停在「可选模板列表」上，正好是他该做的那一步。 */
  useEffect(() => {
    setNode('');
    setStorage('');
    setTemplateRef('');
    setOsTemplate('');
  }, [targetConn]);

  /* 换节点：节点名不通用，存储与模板也跟着变。模板列表现在按节点收窄了（见
     vmPickItems），旧的那个选中值已经不在候选里 —— 不清掉的话，界面上显示的是
     A 节点的模板、提交出去的却是 B 节点的 node:vmid。 */
  useEffect(() => {
    setStorage('');
    setOsTemplate('');
    setTemplateRef('');
  }, [node]);

  /* 网桥默认取节点上的第一个（通常是 vmbr0），所有还没选网桥的网卡一起填上。
     与地址模式无关：先选 DHCP 再切静态、或反过来，这个默认值都留在原地。 */
  useEffect(() => {
    const first =
      bridges.options.find((o) => o.value.startsWith('vmbr')) ?? bridges.options[0];
    const iface = first?.value;
    if (!iface) return;
    setNics((prev) =>
      prev.some((n) => !n.bridge)
        ? prev.map((n) => (n.bridge ? n : { ...n, bridge: iface }))
        : prev,
    );
  }, [bridges.options]);

  /* 存储默认：容器建 rootfs 要 rootdir 存储 */
  useEffect(() => {
    if (isVm || storage) return;
    const first = (storagesQuery.data ?? []).find(
      (s) => s.active && s.content.split(/[,;]/).some((c) => c.trim() === 'rootdir'),
    );
    if (first) setStorage(first.storage);
  }, [storage, storagesQuery.data, isVm]);

  /* 网段 / 地址的判定与联动整块都在 NicSegmentFields 里（与自定义部署共用同一
     份实现）：池的适用范围、选网段对齐网桥与 VLAN、没池时退回 DHCP，一处实现，
     三个入口不会各说各话。这里只负责把池清单与当前网卡值喂给它。 */



  /* 批量下发时每台拿到的是同一份网络配置。主网卡可以静态，但地址必须由 IP 池
     按台分配；附加网卡没有第二套池可用，统一走 DHCP —— 否则 N 台会在第二张
     网卡上撞同一个地址。 */
  useEffect(() => {
    if (!open || !batchMode) return;
    setNics((prev) => {
      const next = prev.map((nic, index) =>
        index > 0 && nic.ipMode !== 'dhcp'
          ? { ...nic, ipMode: 'dhcp' as IpMode }
          : nic,
      );
      return next.some((nic, index) => nic !== prev[index]) ? next : prev;
    });
  }, [open, batchMode]);

  /* 批量静态：N 台要 N 个不同地址，默认从所选网段的空闲表里按台序预填前 N 个。
     用户可以逐台改（池子里没有他要的地址、或想留一段给别的用途），所以真正
     下发的是「预填 + 覆盖表」，表单里那个 IP 输入框这时不参与下发（见 nicIp）。 */
  const batchPool =
    batchMode && primaryNic?.ipMode === 'static'
      ? (poolsQuery.data?.pools ?? []).find((p) => p.id === primaryNic.poolId)
      : undefined;
  const batchIps = batchPool ? batchPool.free.slice(0, quantity) : [];

  /** 逐台手改的地址：只记被改过的那几台，没改的继续用池子预填 */
  const [batchIpEdits, setBatchIpEdits] = useState<Record<number, string>>({});

  /* 换网段：旧地址多半不属于新网段，整份丢掉重新预填 */
  useEffect(() => {
    setBatchIpEdits((prev) => (Object.keys(prev).length ? {} : prev));
  }, [primaryNic?.poolId]);

  /* 改数量：只保留还在范围内的手改值 —— 3 台减到 2 台不该把前两台的修改
     一起清掉，加台时新行自动用池子的预填值 */
  useEffect(() => {
    setBatchIpEdits((prev) => {
      const kept = Object.fromEntries(
        Object.entries(prev).filter(([key]) => Number(key) < quantity),
      );
      return Object.keys(kept).length === Object.keys(prev).length ? prev : kept;
    });
  }, [quantity]);

  /** 某一台最终用的地址（未 trim，输入框要按用户敲的原样显示） */
  const batchIpRaw = (index: number) => batchIpEdits[index] ?? batchIps[index] ?? '';

  const quota = quotaQuery.data;

  /* 按人分配的额度（资源池）：与上面那份全局台数上限是两层。
     快速部署也要按选中的规格判一次，否则它会绕过自定义向导里的那套预检
     （后端仍然会拒，但用户该在提交前就看到原因）。 */
  const myQuotaQuery = useQuery({
    queryKey: ['quota', 'me'],
    queryFn: quotaApi.mine,
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  const myGroup = myQuotaQuery.data?.groups[isVm ? 'vm' : 'ct'];
  const quotaOver = useMemo(
    () =>
      checkGuestSpec(myGroup, {
        count: 1,
        cores: spec?.cores ?? null,
        memory_mb: spec?.memory ?? null,
        disk_gb: spec?.disk ?? null,
      }),
    [myGroup, spec?.cores, spec?.memory, spec?.disk],
  );
  /* 哪一项超了：同维度去重（核数同时踩上「总量」和「单台上限」时只报一次） */
  const quotaOverLabels = useMemo(
    () =>
      Array.from(new Set(quotaOver.map((field) => t(QUOTA_FIELD_LABEL[field])))).join(
        ' / ',
      ),
    [quotaOver, t],
  );
  /* 我名下还剩多少：与两个创建向导共用 remainingSummary，两处口径一致 */
  const mySummary = remainingSummary(
    myGroup,
    {
      count: t('quota.fieldLabel.count'),
      cores: t('quota.fieldLabel.cores'),
      memory: t('quota.fieldLabel.memory'),
      disk: t('quota.fieldLabel.disk'),
    },
    {
      count: isVm ? t('quota.unitGuest') : t('quota.unitItem'),
      cores: t('quota.unitCore'),
    },
  );
  /* 两层额度：全局台数上限（管理员在「设置 → 创建默认值」里配的）与按人分配的
     资源池。要分别报，各报各的数字 —— 混成一条会把「个人额度超了」说成
     「全局额度已用尽」，数字还是全局那份，用户看着只有困惑。 */
  const globalQuotaBlocked = Boolean(quota && !quota.can_create);
  const quotaBlocked = globalQuotaBlocked || quotaOver.length > 0;

  /* 当前额度下建不了的规格：卡片上标「超出额度」，让人在挑的时候就看得见，
     不必点进去、再被下面那条警告顶回来。读数不可信时（error）不下结论 ——
     与 checkGuestSpec 的纪律一致：不替后端猜，也不给人错误的灰名单。 */
  const blockedSpecIds = useMemo(() => {
    const out = new Set<string>();
    if (!myGroup || myGroup.error) return out;
    for (const item of specs) {
      if (
        checkGuestSpec(myGroup, {
          count: 1,
          cores: item.cores,
          memory_mb: item.memory,
          disk_gb: item.disk,
        }).length > 0
      ) {
        out.add(item.id);
      }
    }
    return out;
  }, [specs, myGroup]);

  /* 「推荐」标哪一档（只加标记，不替用户选中）：
     1. 额度有限额 → 额度内最大的一档，点了就能直接下发；
     2. 没限额 → 上次用过的那档（同一个人通常反复开同一规格）；
     3. 都没有 → 中间档，两头（最小 / 最大）最容易选错。
     额度有限但一档都装不下时不给推荐 —— 那时任何一档都需要找管理员调额度。 */
  const recommendedSpecId = useMemo(() => {
    if (!specs.length) return '';
    const limited = Boolean(
      myGroup &&
        !myGroup.error &&
        myGroup.remaining &&
        Object.values(myGroup.remaining).some((v) => typeof v === 'number'),
    );
    if (limited) {
      const fit = specs
        .filter((item) => !blockedSpecIds.has(item.id))
        .sort((a, b) => b.cores - a.cores || b.memory - a.memory || b.disk - a.disk);
      return fit[0]?.id ?? '';
    }
    const last = readLastSpec(kind);
    if (last && specs.some((item) => item.id === last)) return last;
    const sorted = [...specs].sort(
      (a, b) => a.cores - b.cores || a.memory - b.memory || a.disk - b.disk,
    );
    return sorted[Math.floor((sorted.length - 1) / 2)]?.id ?? '';
  }, [specs, blockedSpecIds, myGroup, kind]);

  /* 模板选择器的候选：只有「属于当前连接」这一层过滤是快速部署自己的业务，
     其余映射（名称 / 两列容量 / 标签 / 摘要说明）与自定义部署共用同一份
     （见 utils/templatePick）。

     原来这里还要求「模板必须与目标节点同节点」—— 理由是模板的盘在模板那台机器
     上。这对本地存储成立，但**共享存储上的模板本来就能跨节点克隆**（PVE 的 clone
     接口带 target 即可），一视同仁地挡掉，就变成「模板明明在 Ceph 上、却提示这个
     节点没有模板」。现在按盘判定：别节点的模板，只要磁盘全在共享存储上、且本节点
     挂得到，就允许选（克隆体落到本节点）。 */
  const placementStorages = useMemo(
    () => (storagesQuery.isLoading || storagesQuery.error ? undefined : storagesQuery.data),
    [storagesQuery.isLoading, storagesQuery.error, storagesQuery.data],
  );

  const vmPickItems = useMemo(
    () =>
      vmTemplatePickItems(
        (templatesQuery.data ?? []).filter(
          (tpl) =>
            (!targetConn ||
              !tpl.connection_id ||
              tpl.connection_id === targetConn) &&
            (!node || clonePlacement(tpl, node, placementStorages).kind !== 'blocked'),
        ),
        t,
        node,
      ),
    [templatesQuery.data, targetConn, node, t, placementStorages],
  );

  /* 当前模板的系统盘容量（GB）：规格的硬盘不得小于它。
     模板的分区表是按这块盘记录的，缩盘会把分区截断（机器停在 "Boot failed"）；
     而静默按模板建又会让规格、额度同时失真。null = 后端读不到，按未知放行。 */
  const requiredDiskGb = useMemo(() => {
    if (!isVm || !templateRef) return null;
    return vmPickItems.find((item) => item.value === templateRef)?.minDiskGb ?? null;
  }, [isVm, templateRef, vmPickItems]);

  /* 装不下当前模板的规格：行内置灰 + 写明原因（见 SpecPicker.unfitReasons）。 */
  const diskUnfitReasons = useMemo(() => {
    const out: Record<string, string> = {};
    if (!requiredDiskGb) return out;
    for (const item of specs) {
      if (item.disk < requiredDiskGb) {
        out[item.id] = t('quickDeploy.specDiskTooSmallHint', {
          required: requiredDiskGb,
        });
      }
    }
    return out;
  }, [requiredDiskGb, specs, t]);

  /* 换模板后已选规格装不下：自动切到「满足条件里最小的一档」，别让用户停在
     一个建不出来的组合上。一档都没有时清空，由下面的 validate 说清怎么办。 */
  useEffect(() => {
    if (!open || !isVm || !requiredDiskGb || !specId) return;
    const current = specs.find((item) => item.id === specId);
    if (current && current.disk >= requiredDiskGb) return;
    const fit = specs
      .filter((item) => item.disk >= requiredDiskGb)
      .sort((a, b) => a.cores - b.cores || a.memory - b.memory || a.disk - b.disk)[0];
    if (!fit) {
      setSpecId('');
      return;
    }
    setSpecId(fit.id);
    toast.info(t('quickDeploy.specAutoSwitched', { name: fit.name }));
  }, [open, isVm, requiredDiskGb, specId, specs, t, toast]);

  /* 规格盘 > 模板盘时要不要完整克隆，交给用户自己拨 —— 这里不自动改开关，
     也不拦提交。链接克隆扩不动时后端会在 disk_note 里说明。 */

  /* 别的节点上有什么、为什么用不了 —— 只用于把话说清楚，不进候选。
     「缺共享存储」与「本地盘」是两种修法，必须分开讲（见 elsewhereTemplatesHint）。 */
  const vmTemplatesElsewhere = useMemo(() => {
    const list = (templatesQuery.data ?? []).filter(
      (tpl) =>
        (!targetConn ||
          !tpl.connection_id ||
          tpl.connection_id === targetConn) &&
        Boolean(node) &&
        tpl.node !== node,
    );
    const nodes = Array.from(new Set(list.map((tpl) => tpl.node)));
    const missing: string[] = [];
    const local: string[] = [];
    let blocked = 0;
    for (const tpl of list) {
      const placement = clonePlacement(tpl, node, placementStorages);
      if (placement.kind !== 'blocked') continue;
      blocked += 1;
      for (const name of placement.missing) {
        if (!missing.includes(name)) missing.push(name);
      }
      for (const name of placement.local) {
        if (!local.includes(name)) local.push(name);
      }
    }
    return { count: list.length, nodes, blocked, missing, local };
  }, [templatesQuery.data, targetConn, node, placementStorages]);

  const ctPickItems = useMemo(
    () => ctTemplatePickItems(osTemplatesQuery.data ?? []),
    [osTemplatesQuery.data],
  );

  /* 当前选中的模板（还没选时 undefined）：提交摘要里那块「模板信息」用它。
     直接取选择器的候选 —— 摘要显示的名称 / 内存 / 磁盘必须与选择器逐字一致，
     再回原接口记录里拼一份，迟早会长出两种写法（见 utils/templatePick 文件头）。 */
  const selectedTemplate = useMemo(() => {
    const value = isVm ? templateRef : osTemplate;
    if (!value) return undefined;
    return (isVm ? vmPickItems : ctPickItems).find((item) => item.value === value);
  }, [isVm, templateRef, osTemplate, vmPickItems, ctPickItems]);

  /* 模板**不预选**：装哪套系统是要用户自己确认的决定，替他选第一张等于把
     「随机的默认值」当成答案 —— 选错了要到开机才发现。所以打开时模板为空，
     选择器直接铺出可选列表（见 SystemTemplatePicker：没选中值时列表常驻）。 */

  const storageOptions = useMemo(() => {
    const want = isVm ? 'images' : 'rootdir';
    return [
      { label: t('quickDeploy.selectStorage'), value: '' },
      ...(storagesQuery.data ?? [])
        .filter(
          (s) => s.active && s.content.split(/[,;]/).some((c) => c.trim() === want),
        )
        .map((s) => ({
          label: t('quickDeploy.storageOption', {
            name: s.storage,
            type: s.type,
            avail: formatBytes(s.avail, 0),
          }),
          value: s.storage,
        })),
    ];
  }, [storagesQuery.data, isVm, t]);

  const bridgeOptions = bridges.options;

  /* 什么时候要选存储：容器建 rootfs，或**虚拟机开了完整克隆**（磁盘要复制到
     某个存储上）。链接克隆不往任何存储写数据（磁盘留在模板所在存储），
     所以那条路不出现存储下拉 —— 摆一个必填框只会让人以为必须选。 */
  const storageNeeded = !isVm || fullClone;

  /* 批量命名的展开结果：数量为 1 时保持原名称，不强迫用户理解模板 */
  const batchNames = useMemo(() => {
    const base = name.trim();
    const count = Math.max(1, Math.floor(Number(quantity) || 1));
    if (count <= 1) return [base];
    return Array.from({ length: count }, (_, index) =>
      expandName(namePattern, base, index + 1),
    );
  }, [name, quantity, namePattern]);

  const error = useMemo(() => {
    if (!name.trim()) return t('quickDeploy.errName');
    if (!Number.isInteger(quantity) || quantity < 1 || quantity > 50) {
      return t('batchCreate.errQuantity');
    }
    if (quantity > 1) {
      if (
        batchNames.some((item) => !item) ||
        new Set(batchNames).size !== batchNames.length
      ) {
        return t('batchCreate.errPattern');
      }
      /* 批量可以静态，但每台必须拿到不同地址：默认由池子预填、用户可逐台改，
         所以这里校验最终名单 —— 有空位或两台撞同一地址都不放行。 */
      if (primaryNic?.ipMode === 'static') {
        if (!primaryNic.poolId) return t('quickDeploy.errBatchPool');
        const ips = Array.from({ length: quantity }, (_, i) =>
          batchIpRaw(i).trim(),
        );
        const emptyAt = ips.findIndex((ip) => !ip);
        if (emptyAt >= 0) {
          return t('quickDeploy.errBatchIpEmpty', { n: emptyAt + 1 });
        }
        if (new Set(ips).size !== ips.length) {
          return t('quickDeploy.errBatchIpDup');
        }
      }
      const remaining = quota?.remaining;
      if (remaining !== null && remaining !== undefined && quantity > remaining) {
        return t('batchCreate.errQuota', { n: remaining });
      }
    }
    if (!spec) return t('quickDeploy.errSpec');
    /* 规格盘 < 模板系统盘：装不下。规格行已经禁用，这里兜住残留状态与批量路径。 */
    if (isVm && requiredDiskGb && spec.disk < requiredDiskGb) {
      return t('quickDeploy.errSpecDiskSmall', {
        required: requiredDiskGb,
        selected: spec.disk,
      });
    }
    if (!node) return t('quickDeploy.errNode');
    if (isVm && !templateRef) return t('quickDeploy.errTemplate');
    if (!isVm && !osTemplate) return t('quickDeploy.errOsTemplate');
    if (storageNeeded && !storage) return t('quickDeploy.errStorage');
    if (nics.some((n) => vlanInvalid(n.vlan))) {
      return t('quickDeploy.errVlan');
    }
    if (nics.some((n) => n.ipMode === 'static' && !n.ip.trim())) {
      return t('quickDeploy.errStaticIp');
    }
    if (quotaBlocked) {
      return quotaOver.length
        ? t('quickDeploy.errQuotaSpec')
        : t('quickDeploy.errQuota', {
            label: quota?.label ?? t('quickDeploy.quotaFallback'),
          });
    }
    return '';
  }, [
    name,
    quantity,
    namePattern,
    batchNames,
    spec,
    nics,
    node,
    isVm,
    requiredDiskGb,
    templateRef,
    osTemplate,
    storageNeeded,
    storage,
    quotaBlocked,
    quota,
    /* 批量静态的最终名单参与校验：换网段、改数量、手改地址都要重算 */
    primaryNic,
    batchIps.length,
    batchIpEdits,
    t,
  ]);

  const submit = async () => {
    if (!spec || error || !canCreate) return;
    setBusy(true);
    try {
      /* 静态时补 /24（云镜像默认掩码），网关留空就交给 DHCP/RA —— 后端对空
         网关不下发 gw=，与面板其它地方一致。

         批量时的主网卡不能用表单里那一个地址（N 台会撞车），改成「这台该拿的
         那个」：池子空闲表里的第 batchIndex 个，网关默认取池子的，用户手填的
         优先。附加网卡没有第二套池，effect 已把它们固定在 DHCP。 */
      const nicIp = (nic: QuickNic, nicIndex: number, batchIndex: number) => {
        if (nic.ipMode !== 'static') return { ip: 'dhcp', gateway: '' };
        /* 批量的网关固定取池子里那一份：这时网关输入框是收起的，沿用上一次
           单台填过的值会让整批静默走错网关，而用户看不到、也改不了 */
        if (batchMode && nicIndex === 0) {
          const ip = batchIpRaw(batchIndex) || nic.ip;
          return {
            ip: ip.includes('/') ? ip : `${ip}/24`,
            gateway: batchPool?.gateway ?? '',
          };
        }
        return {
          ip: nic.ip.includes('/') ? nic.ip : `${nic.ip}/24`,
          gateway: nic.gateway || '',
        };
      };

      const nicRate = (nic: QuickNic) =>
        nic.rate.trim() ? Number(nic.rate) : undefined;

      /* 选择器里存的是**唯一值**（连接|节点|VMID），提交要的是 `node:vmid`。
         必须回候选表里按值取回原始标识：不同 PVE 上节点名与 VMID 都可能重复，
         直接 split 会把「另一个连接上的同名模板」当成克隆源。 */
      const pickedVmTpl = vmPickItems.find((item) => item.value === templateRef);
      const [tplNode, tplVmid] = (pickedVmTpl?.raw ?? '').split(':');

      const buildVmPayload = (
        instanceName: string,
        batchIndex: number,
      ): VmCreateRequest => ({
        node,
        /* 0 = 交给后端取下一个可用 VMID（PVE 的 VMID 空间每台主机各自独立） */
        vmid: 0,
        name: instanceName,
        memory: spec.memory,
        cores: spec.cores,
        sockets: 1,
        disks: [],
        networks: nics.map((nic) => ({
          bridge: nic.bridge,
          model: 'virtio',
          /* 留空 = 不划 VLAN。克隆后由后端把 net0/net1… 整体重写成
             bridge=…,tag=…,rate=…（见 vmconfig.format_network_spec） */
          vlan_tag: parseVlan(nic.vlan) ?? null,
          rate: nicRate(nic),
          /* DHCP 模式下用户选的网段（池）：后端据此只从这个池分配，而不是按
             bridge + VLAN 自然排序取第一个（多个池共享同一网段时靠它区分）。 */
          pool_id: nic.poolId || null,
        })),
        tags: tags || undefined,
        cloudinit: {
          enabled: true,
          user: ciUser || 'root',
          password: password || undefined,
          ip_configs: nics.map((nic, nicIndex) => nicIp(nic, nicIndex, batchIndex)),
        },
        manage: manageOn,
        clone_from: {
          node: tplNode,
          vmid: Number(tplVmid),
          /* 克隆方式由「系统镜像」卡里的开关决定：
             链接克隆（默认）：磁盘留在模板所在存储，秒级完成、不额外占空间；
             完整克隆：整份复制到所选存储（要带 target_storage），创建慢、
             多占一份空间，换来的是这台机器与模板彻底脱钩。 */
          full: fullClone,
          target_storage: fullClone ? storage : undefined,
          /* 模板在别的节点（盘在共享存储上）时必须显式给落点，否则后端只能按
             `node`（模板那台）落点，克隆体会悄悄建到模板所在节点上。 */
          target_node: tplNode && tplNode !== node ? node : undefined,
          /* 规格里的磁盘是给新机器用的：模板盘更小时由后端扩容系统盘 */
          disk_size: spec.disk > 0 ? spec.disk : undefined,
        },
      });

      const buildLxcPayload = (
        instanceName: string,
        batchIndex: number,
      ): LxcCreateRequest => ({
        node,
        /* 选择值是唯一标识（连接|节点|volid），PVE 要的是 volid 本身 */
        ostemplate:
          ctPickItems.find((item) => item.value === osTemplate)?.raw ??
          osTemplate,
        storage,
        name: instanceName,
        rootfs: spec.disk,
        memory: spec.memory,
        /* 交换分区沿用后端默认量级：容器很少需要单独调它 */
        swap: 512,
        cores: spec.cores,
        unprivileged: true,
        start: true,
        tags: tags || undefined,
        networks: nics.map((nic, nicIndex) => {
          const addr = nicIp(nic, nicIndex, batchIndex);
          return {
            name: `eth${nicIndex}`,
            bridge: nic.bridge,
            ip: addr.ip,
            gateway:
              nic.ipMode === 'static' ? addr.gateway || undefined : undefined,
            firewall: true,
            /* 容器同样支持 VLAN 标签（后端 LxcNetworkSpec.vlan_tag → tag=） */
            vlan_tag: parseVlan(nic.vlan) ?? null,
            rate: nicRate(nic),
            /* DHCP 模式下用户选的网段（池）：后端只从这个池分配 */
            pool_id: nic.poolId || null,
          };
        }),
        setup: {
          password: password || undefined,
        },
      });

      /* 批量：一次请求交给后端做总量预检与 VMID 分配，返回逐台 UPID；
         表单留在原地，由 BatchCreateProgress 继续轮询每台任务（方案 A）。
         stop_on_error=false：单台失败（命名冲突、某一节点存储不足等）不应
         连坐后面的机器；结果页会按「部分成功」展示，剩余台数照常尝试。 */
      if (quantity > 1) {
        const batchId =
          typeof crypto !== 'undefined' && 'randomUUID' in crypto
            ? crypto.randomUUID().replace(/-/g, '')
            : `batch${Date.now()}`;
        const result = isVm
          ? await vmsApi.createBatch(
              {
                items: batchNames.map((item, index) => buildVmPayload(item, index)),
                batch_id: batchId,
                stop_on_error: false,
              },
              targetConn,
            )
          : await lxcApi.createBatch(
              {
                items: batchNames.map((item, index) => buildLxcPayload(item, index)),
                batch_id: batchId,
                stop_on_error: false,
              },
              targetConn,
            );
        setBatchResult(result);
        /* 记住这一档：下次打开时它就是「推荐」（见 recommendedSpecId） */
        writeLastSpec(kind, spec.id);
        queryClient.invalidateQueries({ queryKey: [isVm ? 'vms' : 'lxc'] });
        invalidateQuota(queryClient);
        toast.success(
          t('batchCreate.toastAccepted', { n: result.total }),
          t('batchCreate.toastAcceptedHint'),
        );
        return;
      }

      if (isVm) {
        const result = await vmsApi.create(
          buildVmPayload(name.trim(), 0),
          targetConn,
        );
        /* 扩容结果如实带出来：「模板盘 20G ≥ 规格 20G」和「已扩到 100G」
           必须让人看到，否则规格是否生效只能靠猜。disk_ok=false 时说明机器
           建出来了、但系统盘没达到规格容量，不能报成一次干净的创建。 */
        const createdHint = result.disk_note
          ? `${t('quickDeploy.createdVmHint', { vmid: result.vmid ?? '-' })} · ${result.disk_note}`
          : t('quickDeploy.createdVmHint', { vmid: result.vmid ?? '-' });
        if (result.disk_ok === false) {
          toast.info(t('quickDeploy.createdDiskMismatch'), createdHint);
        } else {
          toast.success(t('quickDeploy.created'), createdHint);
        }
        queryClient.invalidateQueries({ queryKey: ['vms'] });
        /* 多了一台机器 = 名下用量变了。额度读数有**好几份**（我的额度、平台台数、
           用户/角色额度…），只刷 ['quota'] 会漏掉其余几份 —— 统一走 invalidateQuota。 */
        invalidateQuota(queryClient);
        onCreated?.(result.vmid ?? 0);
        onClose();
      } else {
        const result = await lxcApi.create(
          buildLxcPayload(name.trim(), 0),
          targetConn,
        );
        toast.success(
          t('quickDeploy.created'),
          t('quickDeploy.createdLxcHint', { vmid: result.vmid ?? '-' }),
        );
        queryClient.invalidateQueries({ queryKey: ['lxc'] });
        /* 同虚拟机那条 */
        invalidateQuota(queryClient);
        onCreated?.(result.vmid ?? 0);
        onClose();
      }
      writeLastSpec(kind, spec.id);
    } catch (err) {
      toast.error(t('quickDeploy.createFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  /* ---- 提交摘要：额度（还能建几台）× 规格（每台多大） ----
     这两件事原来被塞在按钮右边同一行灰字里，用「·」连着，还夹着一个空数字
     （quota 为 null = 这一层没设台数上限时，会把 null 直接插成「已用 3 / ，还剩 台」）。
     现在拆成两行两列：左侧固定标签、右侧数值，规格与额度不再混成一句话。 */
  const unit = isVm ? t('quota.unitGuest') : t('quota.unitItem');

  /* 台数不够：表单已经据此禁用提交，这里把话说满（还差多少） */
  const quotaShort = Boolean(
    quota?.limited &&
      quota.remaining !== null &&
      quantity > quota.remaining,
  );

  /* 额度占用比例：只用来画进度条，不参与任何判定 —— 能不能建仍由后端额度
     与 quotaShort 决定。80% 起转琥珀、放不下时转红，与额度徽标的配色一致。 */
  const quotaPct = useMemo(() => {
    const cap = quota?.quota ?? 0;
    if (!quota?.limited || cap <= 0) return 0;
    return Math.min(100, Math.round((quota.used / cap) * 100));
  }, [quota]);
  const quotaTone = quotaShort ? 'is-danger' : quotaPct >= 80 ? 'is-warn' : '';

  /* 批量提交后不再显示表单：保留结果页，让每台的 PVE 任务在这里跑完 */
  if (batchResult) {
    const firstVmid = batchResult.results.find((item) => item.ok)?.vmid ?? 0;
    return (
      <BatchCreateProgress
        response={batchResult}
        /* 这批任务是在哪条连接上发起的：轮询任务与回查列表都要带上，
           否则多连接部署里普通用户会被当成「这不是你的任务」 */
        connectionId={targetConn}
        onClose={() => {
          setBatchResult(null);
          onCreated?.(firstVmid);
          onClose();
        }}
      />
    );
  }

  return (
    <div className="wizard-body">
      <div className="wizard-section">
        {!canCreate ? (
          <Notice tone="warning" title={t('quickDeploy.noPermTitle')}>
            {t('quickDeploy.noPermBody')}
          </Notice>
        ) : null}

        {/* 全局台数上限用尽。管理员要「已用 / 上限」和去哪儿改（就是他配的那一份）；
            普通用户看了这两个数字只会困惑 —— 设置页是管理员的，他也改不了，
            所以只留一句「找管理员」。 */}
        {globalQuotaBlocked ? (
          <Notice
            tone="warning"
            title={t('quickDeploy.quotaBlockedTitle', {
              label: quota?.label ?? '',
            })}
          >
            {isAdmin
              ? t('quickDeploy.quotaBlockedBody', {
                  used: quota?.used,
                  quota: quota?.quota,
                })
              : t('quota.askAdmin')}
          </Notice>
        ) : null}

        {/* 按人分配的额度超了：说清超的是哪一项、名下还剩多少。出口与上面那条
            不同 —— 这不是用户在「设置」里能改的东西，得找管理员调额度。 */}
        {quotaOver.length > 0 ? (
          <Notice
            tone="warning"
            title={t('quickDeploy.quotaSpecTitle', {
              label: quota?.label ?? t('quickDeploy.quotaFallback'),
            })}
          >
            {t('quickDeploy.quotaSpecBody', {
              over: quotaOverLabels,
              summary: mySummary,
            })}
          </Notice>
        ) : null}

        {!specsQuery.isLoading && specs.length === 0 ? (
          <Notice tone="info" title={t('quickDeploy.noSpecTitle')}>
            {t('quickDeploy.noSpecPre')}{' '}
            <button type="button" className="link-button" onClick={onSwitchToCustom}>
              {t('quickDeploy.noSpecCustom')}
            </button>
            {t('quickDeploy.noSpecPost')}
          </Notice>
        ) : null}

        {/* ---- 1. 实例名称 ---- */}
        <Card>
          <CardHeader
            title={t('quickDeploy.sectionName')}
            subtitle={
              isVm
                ? t('quickDeploy.nameSubtitleVm')
                : t('quickDeploy.nameSubtitleLxc')
            }
          />
          {/* 名称 / 数量 / 标签同一行：名称要装得下完整机器名，数量与标签都是
              短串，按 2:1:1 分列，而不是让数量独占半行、标签掉到下一行 */}
          <div className="form-grid-3-wide">
            <Input
              label={t('quickDeploy.labelName')}
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={
                isVm
                  ? t('quickDeploy.namePlaceholderVm')
                  : t('quickDeploy.namePlaceholderLxc')
              }
            />
            <Input
              label={t('batchCreate.quantity')}
              type="number"
              min={1}
              max={50}
              value={quantity}
              onChange={(e) => {
                const next = Number(e.target.value);
                setQuantity(Number.isFinite(next) ? Math.floor(next) : 1);
              }}
              hint={t('batchCreate.quantityHint')}
            />
            <Input
              label={t('quickDeploy.labelTags')}
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder={t('quickDeploy.tagsPlaceholder')}
            />
          </div>
          {quantity > 1 ? (
            <div className="form-grid-2">
              <Input
                label={t('batchCreate.pattern')}
                value={namePattern}
                onChange={(e) => setNamePattern(e.target.value)}
                hint={t('batchCreate.patternHint')}
              />
            </div>
          ) : null}
          {quantity > 1 ? (
            <div className="fs-sm text-secondary">
              {t('batchCreate.preview', {
                names: batchNames.join(', '),
              })}
            </div>
          ) : null}
        </Card>

        {/* ---- 2. 部署位置 ---- */}
        <Card>
          <CardHeader
            title={t('quickDeploy.sectionLocation')}
            subtitle={t('quickDeploy.locationSubtitle')}
          />
          {/* 「连到哪套 PVE」和「落在哪个节点」是同一个决定的两半，并排一行；
              只有一套连接时不给节点留半行空位，让它独占整行 */}
          <div className={connectionOptions.length > 0 ? 'form-grid-2' : undefined}>
            {connectionOptions.length > 0 ? (
              <Select
                label={t('quickDeploy.labelConn')}
                value={targetConn}
                onChange={(e) => setTargetConn(e.target.value)}
                options={connectionOptions}
                hint={t('quickDeploy.connHint')}
              />
            ) : null}
            <Field
              label={t('quickDeploy.labelNode')}
              required
              error={nodesQuery.isError ? t('quickDeploy.nodeError') : undefined}
              hint={
                nodesQuery.isError ? errorMessage(nodesQuery.error) : undefined
              }
            >
              <NodePicker
                nodes={nodesQuery.data ?? []}
                value={node}
                onChange={setNode}
                loading={nodesQuery.isLoading}
              />
            </Field>
          </div>
        </Card>

        {/* ---- 3. 资源规格 ----
            排在部署位置之后：档位能不能用是按「所选节点上的存储」判的（装不下模板
            系统盘的会被禁用），先定落点再挑档位，才不会点了一个禁用项才发现换台
            节点就能用。 */}
        <Card>
          <CardHeader
            title={t('quickDeploy.sectionSpec')}
            subtitle={t('quickDeploy.specSubtitle')}
          />
          {specsQuery.isLoading ? (
            <Spinner />
          ) : specs.length === 0 ? (
            /* 空态已在上面用 Notice 说明；这里不重复占位 */
            null
          ) : (
            /* 与「系统镜像」同一套列表：一行一档、字段成列，档位分组带色。
               规格多了才有搜索 / 档位 chip / 分组，见 SpecPicker。 */
            <SpecPicker
              specs={specs}
              value={specId}
              onChange={setSpecId}
              recommendedId={recommendedSpecId}
              blockedIds={blockedSpecIds}
              /* 装不下模板系统盘的档位：禁用 + 行内写明差多少 */
              unfitReasons={diskUnfitReasons}
              disabled={!canCreate}
            />
          )}
        </Card>

        {/* ---- 4. 系统镜像 ---- */}
        <Card>
          <CardHeader
            title={t('quickDeploy.sectionImage')}
            subtitle={
              isVm
                ? t('quickDeploy.imageSubtitleVm')
                : t('quickDeploy.imageSubtitleLxc')
            }
          />
          {/* 系统模板用与「创建虚拟机 / 创建容器」向导同一套选择器（见
              SystemTemplatePicker）：一两张时直接平铺成行，几十张时给一张可滚动
              列表（搜索 / 分类 / 分组），选定后再收成摘要卡。原来的原生下拉把发行版、
              版本、容量全塞进一行，模板一多就只能靠运气翻。
              **这里不预选**：装哪套系统由用户自己确认，见上面那段说明。 */}
          <Field
            label={isVm ? t('templatePicker.vmLabel') : t('templatePicker.ctLabel')}
            required
            hint={
              isVm
                ? templatesQuery.isError
                  ? errorMessage(templatesQuery.error)
                  : vmPickItems.length
                    ? t('templatePicker.vmHint')
                    : /* 这台节点没有可用模板时，点名「哪台节点上有几个、为什么用不了」：
                         用户刚在截图里见过 ubuntu24，不点名他会以为列表坏了；
                         共享存储没挂到本节点与「模板盘在本地」是两种不同的修法。 */
                      (elsewhereTemplatesHint(t, node, vmTemplatesElsewhere) ??
                        t('templatePicker.vmEmptyHint'))
                : osTemplatesQuery.isError
                  ? errorMessage(osTemplatesQuery.error)
                  : ctPickItems.length || !node
                    ? t('templatePicker.ctHint')
                    : t('templatePicker.ctEmptyOnNode', { node })
            }
          >
            <SystemTemplatePicker
              kind={isVm ? 'vm' : 'ct'}
              items={isVm ? vmPickItems : ctPickItems}
              value={isVm ? templateRef : osTemplate}
              onChange={(next) => {
                if (isVm) setTemplateRef(next);
                else setOsTemplate(next);
              }}
            />
          </Field>

          <div className="form-grid-2">
            {/* 克隆方式紧挨着模板：它决定的正是「这个模板怎么变成一台机器」。
                开关只留「完整克隆」四个字、不挂说明文字 —— 后果交给卡片底部那句
                随开关变化的短提示（见下），这张卡要紧凑。
                只有虚拟机克隆，容器是从 OS 模板建 rootfs，没有这一层。 */}
            {isVm ? (
              <div className="field-toggle">
                <Switch
                  checked={fullClone}
                  onChange={setFullClone}
                  label={t('quickDeploy.fullClone')}
                />
              </div>
            ) : null}

            {/* 容器建 rootfs 要选存储；虚拟机只有开了完整克隆才需要（磁盘要有落点）。
                它排在模板选择器下方：模板占满整行，存储是「这个模板落到哪」的
                下一步，不该再挤回模板那一行。 */}
            {storageNeeded ? (
              <Select
                label={t('quickDeploy.labelStorage')}
                required
                value={storage}
                onChange={(e) => setStorage(e.target.value)}
                options={storageOptions}
                hint={
                  isVm
                    ? t('quickDeploy.storageHintVm')
                    : t('quickDeploy.storageHint')
                }
              />
            ) : null}
          </div>

          {isVm && spec ? (
            <div className="mt-12 field-message">
              {fullClone
                ? t('quickDeploy.fullCloneNote', { n: spec.disk })
                : t('quickDeploy.linkedCloneNote', { n: spec.disk })}
            </div>
          ) : null}
        </Card>

        {/* ---- 5. 网络 ---- */}
        <Card>
          <CardHeader
            title={t('quickDeploy.sectionNetwork')}
            subtitle={
              batchMode
                ? t('quickDeploy.networkSubtitleBatch')
                : isVm
                  ? t('quickDeploy.networkSubtitleVm')
                  : t('quickDeploy.networkSubtitleLxc')
            }
            actions={
              <Button
                variant="secondary"
                size="sm"
                icon={<IconPlus size={14} />}
                onClick={addNic}
              >
                {t('quickDeploy.addNic')}
              </Button>
            }
          />
          {/* 每张网卡一张卡片：多张时才有边界，否则「移除」按钮会贴在下一张的
              字段上（看起来像属于下面那张）；也标出「1 号网卡 / 2 号网卡」。 */}
          <div className="dyn-list">
            {nics.map((nic, index) => (
              <div className="dyn-item" key={nic.id}>
                <div className="dyn-item-fields">
                  {nics.length > 1 ? (
                    <div className="dyn-item-title">
                      {t('quickDeploy.nicTitle', { index: index + 1 })}
                    </div>
                  ) : null}

                  <div className="qd-nic-row">
                    <Select
                      label={t('quickDeploy.labelBridge')}
                      value={nic.bridge}
                      onChange={(e) => updateNic(index, { bridge: e.target.value })}
                      options={
                        bridgeOptions.length
                          ? bridgeOptions
                          : [{ label: nic.bridge || 'vmbr0', value: nic.bridge || 'vmbr0' }]
                      }
                      hint={
                        index === 0 && bridges.isError
                          ? errorMessage(bridges.error)
                          : undefined
                      }
                    />
                    <Input
                      label={t('quickDeploy.labelVlan')}
                      type="number"
                      min={1}
                      max={4094}
                      value={nic.vlan}
                      onChange={(e) => updateNic(index, { vlan: e.target.value })}
                      placeholder={t('quickDeploy.vlanPlaceholder')}
                    />
                    <Input
                      label={t('quickDeploy.labelRate')}
                      type="number"
                      min={0}
                      step={10}
                      value={nic.rate}
                      onChange={(e) => updateNic(index, { rate: e.target.value })}
                      placeholder={t('quickDeploy.ratePlaceholder')}
                    />
                  </div>

                  {/* 网段 / IP：与自定义部署共用同一块界面（NicSegmentFields）。
                      批量时主网卡仍可选网段，但地址由池按台分配，所以这里关掉手填。 */}
                  <NicSegmentFields
                    value={{
                      bridge: nic.bridge,
                      vlan: nic.vlan,
                      ipMode: nic.ipMode,
                      ip: nic.ip,
                      gateway: nic.gateway,
                      poolId: nic.poolId,
                    }}
                    onChange={(patch) => updateNic(index, patch)}
                    node={node}
                    connection={targetConn}
                    pools={poolsQuery.data?.pools ?? []}
                    ready={Boolean(poolsQuery.data)}
                    poolError={
                      poolsQuery.isError ? errorMessage(poolsQuery.error) : undefined
                    }
                    /* 附加网卡没有第二套池可分配：批量时只能是 DHCP */
                    forceDhcp={batchMode && index > 0}
                    showPool={index === 0}
                    hideAddress={batchMode && index === 0}
                  />

                  {batchMode && index === 0 && nic.ipMode === 'static'
                    ? nic.poolId
                      ? /* 已选网段：逐台列出系统预填的地址，允许改单台 */
                        (
                          <div className="qd-batch-ips">
                            <div className="fs-sm text-muted">
                              {batchIps.length < quantity
                                ? t('quickDeploy.batchIpScarce', {
                                    n: batchIps.length,
                                  })
                                : t('quickDeploy.batchIpHint')}
                            </div>
                            <div className="qd-batch-ip-list">
                              {Array.from({ length: quantity }, (_, i) => (
                                <div className="qd-batch-ip-row" key={i}>
                                  <span
                                    className="qd-batch-ip-name"
                                    title={batchNames[i]}
                                  >
                                    {batchNames[i]}
                                  </span>
                                  <Input
                                    mono
                                    value={batchIpRaw(i)}
                                    onChange={(e) =>
                                      setBatchIpEdits((prev) => ({
                                        ...prev,
                                        [i]: e.target.value,
                                      }))
                                    }
                                    placeholder={t('quickDeploy.ipPlaceholder')}
                                    aria-label={`${batchNames[i]} ${t(
                                      'quickDeploy.labelIp',
                                    )}`}
                                  />
                                </div>
                              ))}
                            </div>
                          </div>
                        )
                      : /* 还没选网段：先引导，不摆一排空输入框 */
                        (
                          <div className="fs-sm text-secondary mt-8">
                            {t('quickDeploy.batchIpPickPool', {
                              count: quantity,
                            })}
                          </div>
                        )
                    : null}
                </div>

                {nics.length > 1 ? (
                  <div className="dyn-item-remove">
                    <IconButton
                      label={t('quickDeploy.removeNic')}
                      title={t('quickDeploy.removeNic')}
                      variant="danger"
                      onClick={() => removeNic(index)}
                    >
                      <IconTrash size={15} />
                    </IconButton>
                  </div>
                ) : null}
              </div>
            ))}
          </div>

          {/* 接入安全管控：**只有虚拟机有这条能力**（与「自定义部署」向导里同一个
              开关）。快速部署走链接克隆，公钥由后端注入 cloud-init，机器起来后自动
              登记为受管主机。容器没有 cloud-init，后端 /api/lxc 也不接受这个字段。
              与网卡列表用一条分隔线隔开 —— 它不属于「某一张网卡」，而是整台机器的
              选项，挨着最后一张卡会让人以为在说那张卡。
              「SSH 安全」入口被面板级开关关掉时（sshNavOff）整项不出现：机器登记成
              受管主机之后要在那个入口下面才能看到与管理。 */}
          {isVm && !sshNavOff ? (
            <div className="qd-divider">
              <Switch
                label={t('vmCreate.ciManage')}
                checked={manageOn}
                onChange={setManage}
                /* DHCP 与静态都能接入，差别只在登记时机（静态立刻登记，DHCP 等机器
                   自报地址）—— 文案里说清这件事，别让人以为 DHCP 用不了 */
                hint={
                  primaryNic?.ipMode === 'static'
                    ? t('vmCreate.ciManageHint')
                    : t('vmCreate.ciManageDhcpHint')
                }
              />
            </div>
          ) : null}
        </Card>

        {/* ---- 6. 登录信息 ---- */}
        <Card>
          <CardHeader
            title={t('quickDeploy.sectionLogin')}
            subtitle={
              isVm
                ? t('quickDeploy.loginSubtitleVm')
                : t('quickDeploy.loginSubtitleLxc')
            }
          />
          <div className="form-grid-2">
            {isVm ? (
              <Input
                label={t('quickDeploy.labelUser')}
                value={ciUser}
                onChange={(e) => setCiUser(e.target.value)}
                placeholder="root"
              />
            ) : null}
            <Input
              label={
                isVm
                  ? t('quickDeploy.labelPassword')
                  : t('quickDeploy.labelRootPassword')
              }
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={t('quickDeploy.passwordPlaceholder')}
            />
          </div>
        </Card>

        {/* ---- 提交 ---- */}
        <div className="deploy-submit">
          {/* 用中性提示而不是红色报错：这是「还差什么」的引导，不是用户填错了 */}
          {error ? <div className="field-message">{error}</div> : null}

          {/* 提交摘要：额度与规格各占一行，左侧标签成立一列，数值在右列对齐。
              原来挤在按钮右边那一行里，「已用 3 / 」后面直接断掉、紧接「1C · 1G · 20G」，
              既看不出还够不够建，也读不出单位。 */}
          <dl className="deploy-summary">
            <div className="deploy-summary-row">
              <dt className="deploy-summary-key">
                {/* 口径写在标签上：普通用户看到的是自己名下（并点明是虚拟机还是容器），
                    能管设置的人看到的是全平台 —— 同一个数字两种含义，不能都写「额度」 */}
                {quota?.scope === 'global'
                  ? t('quota.platformLabel')
                  : t('quickDeploy.quotaKindLabel', {
                      label: quota?.label ?? t('quickDeploy.quotaFallback'),
                    })}
              </dt>
              <dd className="deploy-summary-value deploy-summary-quota">
                {/* 一行两段：左 = 额度读数（还能建几台），右 = 模板信息（按哪张
                    模板建）。额度读数右边原来空着半行，把模板放在那儿，正好
                    补上那块空白；窄屏时模板整块折到下一行、贴右边缘，不与额度
                    数字挤在同一截里。 */}
                <span className="deploy-quota-main">
                  {!quota ? (
                    <span className="deploy-summary-loading">
                      <Spinner size={12} /> {t('quickDeploy.loadingQuota')}
                    </span>
                  ) : !quota.limited ? (
                    /* 两层都没设限：只报已用，别把「不限制」写成「已用 x / 」 */
                    t('quickDeploy.quotaUnlimitedUsed', {
                      used: quota.used,
                      unit,
                    })
                  ) : quota.quota === null ? (
                    /* 只限了规格、没限台数（或个人池在限、全局护栏没设）：
                       台数这一维本来就是无限，说「台数不限」比留一个空数字诚实 */
                    t('quickDeploy.quotaNoCapUsed', {
                      used: quota.used,
                      unit,
                    })
                  ) : (
                    <>
                      {/* 已用 / 上限 + 占用条：一眼看出「还剩多少空间」，
                          不必先在脑里做一次除法 */}
                      <span className="deploy-quota-text">
                        {t('quickDeploy.quotaUsedOf', {
                          used: quota.used,
                          quota: quota.quota,
                          unit,
                        })}
                      </span>
                      {/* 纯视觉辅助：同一信息已由左边那句「已用 15 / 30 台」表达，
                          进度条不再重复朗读 */}
                      <span
                        className={`deploy-quota-bar ${quotaTone}`}
                        aria-hidden="true"
                      >
                        <span
                          className="deploy-quota-fill"
                          style={{ width: `${quotaPct}%` }}
                        />
                      </span>
                      {quantity > 1 ? (
                        <span className="deploy-chip">
                          {t('quickDeploy.quotaThisBatch', {
                            qty: quantity,
                            unit,
                          })}
                        </span>
                      ) : null}
                      <span className="deploy-summary-divider" aria-hidden="true" />
                      {quotaShort ? (
                        /* 拆成两段：前半（含数字与单位）不许断行，后半才折行 ——
                           否则窄屏会把「2 台」拆成「2 / 台」 */
                        <span className="deploy-quota-short">
                          <span className="deploy-summary-em is-warn">
                            {t('quickDeploy.quotaShortLeft', {
                              n: quota.remaining,
                              unit,
                            })}
                          </span>
                          <span className="deploy-summary-warn">
                            {t('quickDeploy.quotaShortNeed', {
                              qty: quantity,
                              unit,
                            })}
                          </span>
                        </span>
                      ) : (
                        <span className="deploy-summary-em">
                          {t('quickDeploy.quotaAfter', {
                            n: (quota.remaining ?? 0) - quantity,
                            unit,
                          })}
                        </span>
                      )}
                    </>
                  )}
                </span>
                <TemplateBrief
                  item={selectedTemplate}
                  kind={kind}
                  fullClone={fullClone}
                  storage={storage}
                />
              </dd>
            </div>
            <div className="deploy-summary-row">
              <dt className="deploy-summary-key">
                {t('quickDeploy.summarySpecKey')}
              </dt>
              {/* 规格不再拼成一整句「单台 2 核 · …，共 3 台（合计 …）」：括号里再套
                  一遍同样的三项，读的时候得先拆句再对数。改成三段 —— 单台三个
                  胶囊、乘数、合计 —— 每台与整批各自成组，眼睛扫一眼就能对上
                  「2 核 ×3 = 6 核」。
                  磁盘两种类型都按规格生效（VM 是克隆后扩容系统盘），所以这里直接
                  写实际大小，不再有「虚拟机沿用模板磁盘」那个例外。 */}
              <dd className="deploy-summary-value deploy-summary-spec">
                {spec ? (
                  <span className="deploy-spec">
                    <span className="deploy-spec-label">
                      {t('quickDeploy.specPerGuest')}
                    </span>
                    <span className="deploy-spec-chips">
                      <span className="deploy-spec-chip">
                        {t('quickDeploy.specCores', { n: spec.cores })}
                      </span>
                      <span className="deploy-spec-chip">
                        {t('quickDeploy.specMem', { n: gb(spec.memory) })}
                      </span>
                      <span className="deploy-spec-chip">
                        {t('quickDeploy.summaryDisk', { n: spec.disk })}
                      </span>
                    </span>
                    {quantity > 1 ? (
                      <>
                        {/* 乘数：中文「× 3 台」，英文的台数单位是空串，模板里
                            只写数量 —— 两语各自读得通，不是漏了单位 */}
                        <span className="deploy-spec-times">
                          {t('quickDeploy.specTimes', {
                            qty: quantity,
                            unit,
                          })}
                        </span>
                        {/* 虚点引导线：把「单台」与「合计」之间那截空白变成一条
                            视线路径 —— 两组数字被读成一道乘法，而不是各占一头、
                            互不相干的两条读数。只有批量（有乘数）时才需要。 */}
                        <span className="deploy-spec-leader" aria-hidden="true" />
                        <span className="deploy-spec-total">
                          <span className="deploy-spec-label">
                            {t('quickDeploy.specTotalLabel')}
                          </span>
                          <span className="deploy-spec-chip is-total">
                            {t('quickDeploy.specCores', {
                              n: spec.cores * quantity,
                            })}
                          </span>
                          <span className="deploy-spec-chip is-total">
                            {t('quickDeploy.specMem', {
                              n: gb(spec.memory * quantity),
                            })}
                          </span>
                          <span className="deploy-spec-chip is-total">
                            {t('quickDeploy.summaryDisk', {
                              n: spec.disk * quantity,
                            })}
                          </span>
                        </span>
                      </>
                    ) : null}
                  </span>
                ) : (
                  '—'
                )}
              </dd>
            </div>
          </dl>

          <div className="deploy-actions">
            <Button
              variant="primary"
              icon={<IconCloud size={15} />}
              loading={busy}
              disabled={Boolean(error) || !canCreate}
              onClick={() => void submit()}
            >
              {isVm ? t('quickDeploy.createVm') : t('quickDeploy.createLxc')}
            </Button>
            <Button variant="secondary" onClick={onClose} disabled={busy}>
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * 提交摘要里的「模板信息」卡（额度读数右边那块）。
 *
 * 为什么放在摘要里而不是再开一张卡：下单前用户只确认两件事 —— 「还能建几台」
 * 和「按哪张模板建」。前者是额度行，后者原来只在上面「系统镜像」卡里，滚到底
 * 部按按钮时已经看不见了；额度行右侧本来空着半行，正好把模板的名字、规格与
 * 关键配置摆在那里，一屏之内两件事都读得到。
 *
 * 显示规则：
 * · 名称 / 系统与选择器里的写法逐字一致（都来自 TemplatePickItem），不另拼一份；
 * · 「规格」是模板自带的核数 / 内存 / 磁盘，与下一行「单台」的计划规格是两回事，
 *   所以两者都标了来源（这块写「模板」，那行写「单台」）；
 * · 「关键配置」只放模板自身的身份与落点：VMID、克隆方式、存储、节点。网络、
 *   登录这些字段属于这台机器，不在这里重复。
 */
function TemplateBrief({
  item,
  kind,
  fullClone,
  storage,
}: {
  item: TemplatePickItem | undefined;
  kind: QuickDeployKind;
  fullClone: boolean;
  storage: string;
}) {
  const t = useT();
  const isVm = kind === 'qemu';

  /* 模板还没选：留一个占位而不是整块消失 —— 否则用户看到的是额度行右边一片
     空白，不知道这里本该有什么。 */
  if (!item) {
    return (
      <span className="deploy-tpl-brief is-empty">
        <span className="deploy-tpl-kicker">
          {t('quickDeploy.templateTitle')}
        </span>
        <span className="deploy-tpl-empty">
          {t('quickDeploy.templateEmpty')}
        </span>
      </span>
    );
  }

  const family = templateFamily(item);

  const specs: string[] = [];
  if (isVm) {
    if (item.cores) specs.push(t('quickDeploy.specCores', { n: item.cores }));
    const mem = normalizeSizeText(item.sizeText);
    const disk = normalizeSizeText(item.metaText);
    if (mem !== '—') specs.push(t('quickDeploy.templateMem', { size: mem }));
    if (disk !== '—') specs.push(t('quickDeploy.templateDisk', { size: disk }));
  } else {
    /* 容器模板只报压缩包大小：rootfs 多大由规格决定，模板本身没有「磁盘」 */
    const size = normalizeSizeText(item.sizeText);
    if (size !== '—') specs.push(t('quickDeploy.templateSize', { size }));
  }

  const config: string[] = [];
  if (item.vmid) config.push(t('quickDeploy.templateVmid', { vmid: item.vmid }));
  if (isVm) {
    config.push(fullClone ? t('vmCreate.cloneFull') : t('vmCreate.cloneLinked'));
    /* 链接克隆不写存储（磁盘留在模板那块盘上），只有完整克隆才有落点 */
    if (fullClone && storage) {
      config.push(t('quickDeploy.templateStorage', { storage }));
    }
  } else if (item.metaText) {
    config.push(t('quickDeploy.templateStorage', { storage: item.metaText }));
  }
  if (item.node) config.push(t('quickDeploy.templateNode', { node: item.node }));

  return (
    <span className="deploy-tpl-brief" data-os={family.key}>
      <span className="deploy-tpl-mark" aria-hidden="true">
        <OsLogo family={family} size="chip" />
      </span>
      <span className="deploy-tpl-body">
        <span className="deploy-tpl-head">
          <span className="deploy-tpl-kicker">
            {t('quickDeploy.templateTitle')}
          </span>
          <strong className="deploy-tpl-name" title={item.name}>
            {item.name}
          </strong>
          <span className="deploy-tpl-os">{osFamilyLabel(family, t)}</span>
        </span>
        {/* 一项一个 span：整句拼起来只能整段折行，窄屏上会溢出卡片；拆开后
            每个「4 GB 内存」自己不许断，「·」由 CSS 补在同一组相邻两项之间，
            折行时不会出现以「·」起头的一行。 */}
        <span className="deploy-tpl-meta">
          {specs.map((text, index) => (
            <span
              className={`deploy-tpl-specs${
                index < specs.length - 1 ? ' has-sep' : ''
              }`}
              key={`spec-${text}`}
            >
              {text}
            </span>
          ))}
          {config.map((text, index) => (
            <span
              className={`deploy-tpl-config${
                index === 0 && specs.length > 0 ? ' is-first' : ''
              }${index < config.length - 1 ? ' has-sep' : ''}`}
              key={`config-${text}`}
            >
              {text}
            </span>
          ))}
        </span>
      </span>
    </span>
  );
}
