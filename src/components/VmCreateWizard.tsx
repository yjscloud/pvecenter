/* ==========================================================================
   pvecenter — 创建虚拟机向导（4 步 + 确认）

   同一个弹窗里还有「快速部署」模式（QuickDeployForm）：选规格 → 选位置 → 起名
   即可下发，适合标准机器；NUMA / PCI 直通 / 多网卡多磁盘这些仍走本向导。
   两者用一个开关切换 —— 入口只有一个（列表页的「创建虚拟机」）。
   ========================================================================== */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  clusterApi,
  configApi,
  connectionsApi,
  ipPoolsApi,
  nodesApi,
  quotaApi,
  storagesApi,
  templatesApi,
  vmsApi,
} from '../api/endpoints';
import { checkGuestSpec, remainingSummary } from '../utils/quotaSpec';
import { invalidateQuota } from '../hooks/quotaCache';
import { errorMessage } from '../api/client';
import { getGlobalScope } from '../api/scope';
import { Modal } from './ui/Modal';
import { Button, IconButton } from './ui/Button';
import {
  Checkbox,
  Field,
  Input,
  SegmentedControl,
  Select,
  Switch,
  Textarea,
} from './ui/Input';
import { Notice } from './ui/EmptyState';
import { Badge } from './ui/Badge';
import { NodePicker } from './NodePicker';
import { QuickDeployForm } from './QuickDeployForm';
import { SystemTemplatePicker } from './SystemTemplatePicker';
import {
  clonePlacement,
  elsewhereTemplatesHint,
  vmTemplatePickItems,
} from '../utils/templatePick';
import { NicSegmentFields, type IpMode, type SegmentValue } from './NicSegmentFields';
import { isPathDisabled, SSH_SECURITY_NAV_PATH } from './Sidebar';
import {
  IconPlus,
  IconTrash,
  IconCheck,
  IconAlert,
  IconTemplate,
  IconDisk,
} from './Icons';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../hooks/useToast';
import { useUiPrefs } from '../hooks/useUiPrefs';
import { useNodeStorages } from '../hooks/useNodeStorages';
import { useNodeBridges } from '../hooks/useNodeBridges';
import {
  biosOptions,
  cpuTypeOptions,
  DISK_FORMAT_OPTIONS,
  DISK_INTERFACE_OPTIONS,
  efiTypeOptions,
  machineOptions,
  netModelOptions,
  numaPolicyOptions,
  ostypeOptions,
  SCSIHW_OPTIONS,
  tpmVersionOptions,
  initAgentName,
  isWindowsOstype,
  ostypeLabel,
} from '../utils/status'
import { useT, type MessageKey, type TFunc } from '../i18n';
import { formatBytes } from '../utils/format';
import type {
  CloudInitConfig,
  NumaNodeSpec,
  TemplateItem,
  VmCreateDisk,
  VmCreateNetwork,
  VmCreateRequest,
} from '../api/types';

/* ---------------------------------------------------------------------------
   步骤定义
   --------------------------------------------------------------------------- */

/* 步骤标题。第 CI_STEP 项在渲染时会被客户机系统的初始化工具名覆盖（Cloud-Init /
   Cloudbase-Init），这里放「初始化」只是兜底，免得覆盖逻辑漏掉时标题空着 */
const STEP_KEYS: MessageKey[] = [
  'vmCreate.stepBasic',
  'vmCreate.stepMode',
  'vmCreate.stepSystem',
  'vmCreate.stepDisks',
  'vmCreate.stepInit',
  'vmCreate.stepConfirm',
];

/* 第 5 步（初始化）在 STEP_KEYS 里的下标。它的标题要随客户机系统变，步骤条与确认页
   共用这个下标，避免各处硬编码 4。 */
const CI_STEP = 4;

type StepIndex = 0 | 1 | 2 | 3 | 4 | 5;

/** 两种下单方式：快速（选规格）与自定义（逐步填写） */
type CreateMode = 'quick' | 'custom';

/* ---------------------------------------------------------------------------
   表单状态
   --------------------------------------------------------------------------- */

interface DiskRow {
  key: string;
  storage: string;
  size: number;
  interface: string;
  format: string;
}

interface NetRow {
  key: string;
  bridge: string;
  model: string;
  vlan_tag: string;
  firewall: boolean;
  macaddr: string;
  /**
   * 地址怎么来：静态（手填 / 池子预填）还是 DHCP。
   *
   * 与快速部署同一套开关（界面也在同一个组件里，见 NicSegmentFields）：
   * 原先这里只能把一个自由文本填成 `dhcp`，既没有可见的「获取方式」，也没法
   * 指定要哪个网段。
   */
  ipMode: IpMode;
  /** 静态地址：192.168.1.10/24；DHCP 时为空 */
  ip: string;
  /** 静态 IP 对应的网关 */
  gateway: string;
  /** 选中的网段（IP 池 id）：DHCP 时决定从哪个网段拿地址，随请求下发 */
  poolId: string;
  /** 速率上限（MB/s）；'' = 不限速 */
  rate: string;
}

/** 一个 NUMA 节点的绑定（对应一张 numaN 配置项） */
interface NumaRow {
  key: string;
  /** 本节点分到的逻辑 CPU，cpuset 写法，如 "0-3" */
  cpus: string;
  /** 内存 MB，留空由后端均分总内存 */
  memory: string;
  /** 绑定的宿主机 NUMA 节点，如 "0"；留空=只做客户机内部拓扑 */
  hostnodes: string;
  /** 内存策略，仅在填了宿主节点时生效 */
  policy: string;
}

/**
 * 克隆模式下的「IP 列表」一行。
 *
 * 克隆体继承模板的网卡，这里只是**覆盖** cloud-init 里的地址；但「从哪个网段
 * 拿地址」同样要能选（与快速部署一致），所以带上 ipMode / poolId，随
 * ``ip_configs`` 下发，后端按 pool_id 分配（见 app/routers/vms 的地址分配）。
 */
interface IpRow {
  key: string;
  ipMode: IpMode;
  ip: string;
  gateway: string;
  /** 选中的网段（IP 池 id）；DHCP 时决定从哪个网段取地址 */
  poolId: string;
}

interface FormState {
  /* 基本信息（第 1 步） */
  name: string;
  vmid: string;
  node: string;
  tags: string;
  description: string;

  /* 系统配置（第 3 步） */
  ostype: string;
  bios: string;
  machine: string;
  scsihw: string;
  cpuType: string;
  cores: number;
  sockets: number;
  memory: number;
  /**
   * 内存气球的最低保留量（MB），**字符串**：'' = 不下发这个键（PVE 按整份内存
   * 算，宿主机收不回空闲内存）；0 = 显式关掉气球驱动；其它值 = 保留这么多。
   * 需要区分「没填」与「填 0」，所以不用 number。
   */
  balloon: string;
  startOnBoot: boolean;
  bootOrder: string;

  /* 高级硬件（第 3 步）：NUMA 绑定 + EFI 变量盘 / 虚拟 TPM（Windows 11 必需） */
  numaEnabled: boolean;
  /** 整机 CPU 亲和性 cpuset，留空=不限制 */
  numaAffinity: string;
  numaNodes: NumaRow[];
  efiEnabled: boolean;
  efiStorage: string;
  efiType: string;
  efiPreEnrolled: boolean;
  tpmEnabled: boolean;
  tpmStorage: string;
  tpmVersion: string;

  /* 创建方式（第 2 步）/ 磁盘与网络（第 4 步） */
  mode: 'new' | 'clone';
  cloneFrom: string;
  cloneFull: boolean;
  cloneStorage: string;
  /**
   * 克隆体接到哪个网桥（空串 = 沿用模板）。只替换网卡的 bridge 一段，
   * MAC / VLAN / 速率保持模板里的值；静态地址与 DHCP 都适用。
   */
  cloneBridge: string;
  /** 克隆完成后是否自动开机 */
  cloneStart: boolean;
  disks: DiskRow[];
  networks: NetRow[];
  iso: string;

  /* Cloud-Init（第 5 步） */
  ciEnabled: boolean;
  ciUser: string;
  ciPassword: string;
  ciSshKeys: string;
  /** 接入安全管控：把面板公钥写进 cloud-init，机器起来后自动登记为受管主机 */
  ciManage: boolean;
  ciDns: string;
  ciIps: IpRow[];
}

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

const initialState: FormState = {
  name: '',
  vmid: '',
  node: '',
  tags: '',
  description: '',

  ostype: 'l26',
  bios: 'seabios',
  /* 空串 = 默认机型（i440fx），也就是不把 machine 写进 PVE 配置。理由见
     utils/status.ts 的 machineOptions：各版本 PVE 对默认机型的叫法不同。 */
  machine: '',
  scsihw: 'virtio-scsi-single',
  cpuType: 'host',
  cores: 2,
  sockets: 1,
  memory: 2048,
  balloon: '',
  startOnBoot: false,
  /* 启动顺序留空 = 让后端按第一块磁盘的总线自动设置（boot=order=<磁盘>）。
     刻意不写死 scsi0：磁盘总线是用户可改的，一旦从 scsi 换成 sata（Windows
     就必须换），引导顺序还指着不存在的 scsi0，表现就是「装系统时看不到盘」。 */
  bootOrder: '',

  numaEnabled: false,
  numaAffinity: '',
  numaNodes: [],
  efiEnabled: false,
  efiStorage: '',
  /** 4m 才支持预置安全启动密钥，Win11 用它 */
  efiType: '4m',
  efiPreEnrolled: true,
  tpmEnabled: false,
  tpmStorage: '',
  tpmVersion: 'v2.0',

  mode: 'clone',
  cloneFrom: '',
  /** 默认链接克隆 */
  cloneFull: false,
  cloneStorage: '',
  /** 空串 = 沿用模板网卡所在的网桥 */
  cloneBridge: '',
  /** 默认克隆后自动开机 */
  cloneStart: true,
  disks: [
    { key: uid(), storage: '', size: 20, interface: 'scsi0', format: 'qcow2' },
  ],
  networks: [
    /* 主网卡默认「静态」并由 IP 池预填地址，与快速部署一致；一个池都匹配不上时
       NicSegmentFields 会自动退回 DHCP，所以没配池的环境行为不变。 */
    { key: uid(), bridge: 'vmbr0', model: 'virtio', vlan_tag: '', firewall: true, macaddr: '', ipMode: 'static', ip: '', gateway: '', poolId: '', rate: '' },
  ],
  iso: '',

  ciEnabled: false,
  ciUser: 'ubuntu',
  ciPassword: '',
  ciSshKeys: '',
  ciManage: false,
  ciDns: '',
  ciIps: [{ key: uid(), ipMode: 'dhcp', ip: '', gateway: '', poolId: '' }],
};

/**
 * 把 Windows 认不出的那两处 virtio 硬件换成「客户机自带驱动」的型号。
 *
 * Windows 安装介质只带通用驱动，两个 virtio 设备它都不认识：
 *
 * * **磁盘**：virtio-scsi / virtio-blk 没有驱动 → 装到「你想将 Windows 安装
 *   在哪里？」那一步一个分区都列不出来。换成 SATA（Windows 自带 AHCI 驱动）。
 * * **网卡**：virtio-net 没有驱动 → 系统装完是「没有网络适配器」，还得回头补
 *   驱动。换成 Intel E1000（Windows 自带 e1000 驱动，装完即用）。
 *
 * 两处都只动 virtio 系，用户自己选的 SATA / IDE / E1000 / vmxnet3 一概不动 ——
 * 换过去会损失一点性能，这是「先能用」的取舍，不是要把高级用户的选择抹掉。
 *
 * 启动顺序里的设备名必须跟着磁盘一起改：`boot=order=scsi0` 配上只有 sata0 的
 * 机器会变成「没有可引导设备」，比看不到盘更难排查。
 */
function applyWindowsCompat(next: FormState): void {
  const renamed = new Map<string, string>();
  next.disks = next.disks.map((disk) => {
    if (!/^(scsi|virtio)\d+$/.test(disk.interface)) return disk;
    // 保留原有编号（scsi1 → sata1）而不是按下标重排：编号是磁盘的身份，
    // 照搬能保证「换完之后键名依旧互不相同」，也不会撞上别人已经占着的 sata0。
    const target = `sata${disk.interface.replace(/\D/g, '')}`;
    renamed.set(disk.interface, target);
    return { ...disk, interface: target };
  });
  if (renamed.size > 0 && next.bootOrder.trim()) {
    next.bootOrder = next.bootOrder
      .split(';')
      .map((part) => renamed.get(part.trim()) ?? part.trim())
      .join(';');
  }
  next.networks = next.networks.map((net) =>
    net.model === 'virtio' ? { ...net, model: 'e1000' } : net,
  );
}

/* ---------------------------------------------------------------------------
   校验
   --------------------------------------------------------------------------- */

type Errors = Partial<Record<string, string>>;

/** PVE 的 cpuset 写法：逗号分隔的「单核或区间」，如 0 / 0-3 / 0-3,8-11 */
const CPUSET_RE = /^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$/;

const isCpuset = (value: string) => CPUSET_RE.test(value.trim());

/* ---------------------------------------------------------------------------
   初始化配置（Cloud-Init / Cloudbase-Init）：实际会不会下发
   ---------------------------------------------------------------------------
   判据只能有一处 —— 提交、校验、确认页各写一遍必然漂移（早先确认页看的是开关、
   提交看的是「开关 ∨ 有静态 IP」，于是出现过「确认页写着未启用、实际下发了」）。
   Windows 只认开关本身：那边跑的是 Cloudbase-Init，要客户机内先装好，
   面板默认替用户打开只会建出一台拿不到 IP 的机器。 */

/** 网卡里是否填了静态 IP */
function hasStaticIpOnNics(form: FormState): boolean {
  return form.networks.some((n) => {
    /* 按「获取方式」判，而不是看 ip 框里有没有字：DHCP 时那个框不显示，
       里面可能还留着切过去之前的旧地址，不能据此认定要下发静态配置。 */
    return n.ipMode === 'static' && (n.ip || '').trim() !== '';
  });
}

/** 这次创建实际会不会下发初始化配置 */
function initActive(form: FormState): boolean {
  if (form.mode !== 'new') return form.ciEnabled;
  return form.ciEnabled || (hasStaticIpOnNics(form) && !isWindowsOstype(form.ostype));
}

function validateStep(
  t: TFunc,
  step: StepIndex,
  form: FormState,
  takenVmids?: Set<number>,
): Errors {
  const e: Errors = {};

  if (step === 0) {
    if (!form.name.trim()) e.name = t('vmCreate.vNameRequired');
    else if (form.name.length > 63) e.name = t('vmCreate.vNameTooLong');

    if (!form.vmid.trim()) e.vmid = t('vmCreate.vVmidRequired');
    else {
      const n = Number(form.vmid);
      if (!Number.isInteger(n) || n < 100 || n > 999999999) {
        e.vmid = t('vmCreate.vVmidRange');
      } else if (takenVmids?.has(n)) {
        /* VMID 按主机独立分配，照搬另一台主机的号会直接撞车 */
        e.vmid = t('vmCreate.vVmidTaken', { vmid: n });
      }
    }
    if (!form.node) e.node = t('vmCreate.vNodeRequired');
  }

  if (step === 1) {
    if (form.mode === 'clone' && !form.cloneFrom) {
      e.cloneFrom = t('vmCreate.vCloneSource');
    }
  }

  if (step === 2) {
    if (form.cores < 1 || form.cores > 128) e.cores = t('vmCreate.vCores');
    if (form.sockets < 1 || form.sockets > 4) e.sockets = t('vmCreate.vSockets');
    if (form.memory < 512) e.memory = t('vmCreate.vMemoryMin');
    else if (form.memory > 1_048_576) e.memory = t('vmCreate.vMemoryMax');

    /* 内存气球：留空 = 不回收（不下发该键），0 = 关闭气球驱动。填了值就必须
       低于内存上限，否则 PVE 直接拒掉建机请求。 */
    if (form.balloon.trim()) {
      const balloon = Number(form.balloon);
      if (!Number.isFinite(balloon) || balloon < 0) {
        e.balloon = t('vmCreate.vBalloonInt');
      } else if (balloon > 0 && balloon < 128) {
        e.balloon = t('vmCreate.vBalloonMin');
      } else if (balloon > 0 && form.memory && balloon >= form.memory) {
        e.balloon = t('vmCreate.vBalloonVsMemory');
      }
    }

    /* 高级硬件：cpuset 是唯一会被 PVE 直接拒的输入，格式必须在提交前拦住 */
    if (form.numaAffinity.trim() && !isCpuset(form.numaAffinity)) {
      e.numaAffinity = t('vmCreate.vCpuset');
    }
    form.numaNodes.forEach((node, i) => {
      if (!isCpuset(node.cpus)) {
        e[`numa-cpus-${i}`] = t('vmCreate.vNumaCpus');
      }
      if (node.hostnodes.trim() && !isCpuset(node.hostnodes)) {
        e[`numa-hostnodes-${i}`] = t('vmCreate.vNumaHostnodes');
      }
      const mem = Number(node.memory);
      if (node.memory.trim() && (!Number.isFinite(mem) || mem < 16)) {
        e[`numa-memory-${i}`] = t('vmCreate.vNumaMemory');
      }
    });

    if (form.efiEnabled && !form.efiStorage) e.efiStorage = t('vmCreate.vEfiStorage');
    if (form.tpmEnabled && !form.tpmStorage) e.tpmStorage = t('vmCreate.vTpmStorage');
  }

  if (step === 3 && form.mode === 'new') {
    form.disks.forEach((d, i) => {
      if (!d.storage) e[`disk-storage-${i}`] = t('vmDetail.selectStorage');
      if (d.size < 1) e[`disk-size-${i}`] = t('vmCreate.vDiskSize');
    });
    if (form.disks.length === 0) e.disks = t('vmCreate.vDisksRequired');
    form.networks.forEach((n, i) => {
      if (!n.bridge) e[`net-bridge-${i}`] = t('vmDetail.addNicBridgePlaceholder');
      /* 只看静态：DHCP 时 ip 框根本不显示，里面残留的旧值不参与下发 */
      const ip = (n.ip || '').trim();
      if (n.ipMode === 'static' && ip && !ip.includes('/')) {
        e[`net-ip-${i}`] = t('vmDetail.ipCidr');
      }
    });
  }

  // 用 initActive 而不是开关本身：网卡填了静态 IP 时开关可能是关的，但配置
  // 确实会下发，用户名就必须校验。
  if (step === CI_STEP && initActive(form)) {
    if (!form.ciUser.trim()) {
      e.ciUser = t('vmCreate.vCiUser', { agent: initAgentName(form.ostype) });
    }
    // 全新创建时 IP 在「磁盘与网络」的网卡里配置，这里只校验克隆模式下的 IP 列表。
    if (form.mode !== 'new') {
      form.ciIps.forEach((row, i) => {
        const ip = (row.ip || '').trim();
        if (row.ipMode === 'static' && ip && !ip.includes('/')) {
          e[`ip-${i}`] = t('vmDetail.ipCidr');
        }
      });
    }
  }

  return e;
}

/* ---------------------------------------------------------------------------
   主体
   --------------------------------------------------------------------------- */

export interface VmCreateWizardProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (vmid: number) => void;
}

export function VmCreateWizard({ open, onClose, onCreated }: VmCreateWizardProps) {
  const t = useT();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();

  /* 「SSH 安全」入口被面板级开关关掉时，Cloud-Init 那一步的「接入安全管控」整个
     收起来（见下面的渲染）：勾了它机器会被登记成受管主机，而看受管主机的地方都在
     那个入口下面 —— 入口关了，这个开关就是「勾了没处看」。
     取值一并钉住：先勾上、之后入口被关掉时，不会提交一个已经看不见的 true。 */
  const navDisabled = useUiPrefs().nav_disabled;
  const sshNavOff = isPathDisabled(SSH_SECURITY_NAV_PATH, navDisabled);

  const [step, setStep] = useState<StepIndex>(0);
  const [form, setForm] = useState<FormState>(initialState);
  const [errors, setErrors] = useState<Errors>({});
  const [submitting, setSubmitting] = useState(false);
  /* 仅在本轮打开时自动选一次模板，避免用户手动清空后被回填 */
  const autoPickedRef = useRef(false);
  /* 目标 PVE 主机：空串 = 面板当前连接；否则用指定连接创建（多台 PVE 场景） */
  const [targetConn, setTargetConn] = useState('');

  /**
   * 快速部署 / 自定义部署。默认选快速 —— 绝大多数创建都是「标准机器」；
   * 管理员还没定义任何规格时退回自定义（否则一进来就是个空的选择区）。
   * 用户手动切过之后，就不再被自动改回去。
   */
  const [mode, setMode] = useState<CreateMode>('quick');
  const modeTouchedRef = useRef(false);
  /** 用户主动切换：记下来，别再被「有没有规格」的自动判断覆盖 */
  const pickMode = (next: CreateMode) => {
    modeTouchedRef.current = true;
    setMode(next);
  };

  /* 规格表：只为「默认用哪种模式」而读。表单自己也读同一份，react-query 缓存
     共用，不会多一次请求 */
  const specsQuery = useQuery({
    queryKey: ['config', 'specs'],
    queryFn: configApi.getSpecs,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  useEffect(() => {
    if (!open || modeTouchedRef.current || !specsQuery.data) return;
    const usable = specsQuery.data.specs.filter(
      (s) => s.kind === 'vm' || s.kind === 'both',
    );
    setMode(usable.length > 0 ? 'quick' : 'custom');
  }, [open, specsQuery.data]);

  /* ---- 依赖数据 ---- */
  /* 已保存的 PVE 主机列表，供「目标 PVE 主机」选择 */
  const connectionsQuery = useQuery({
    queryKey: ['connections'],
    queryFn: connectionsApi.list,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  /* 区域 / 机房：连接的 region 标签。先选区域可以把「目标 PVE」与节点收窄到
     一个机房内（后端也会把区域外的连接挡在清单之外）。留空 = 不筛选。 */
  const [region, setRegion] = useState('');

  const regionOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const item of connectionsQuery.data ?? []) {
      const label = (item.region ?? '').trim();
      if (label) seen.add(label);
    }
    return [
      { label: t('vmCreate.regionAll'), value: '' },
      ...[...seen].sort().map((label) => ({ label, value: label })),
    ];
  }, [connectionsQuery.data, t]);

  const scopedConnections = useMemo(() => {
    const list = connectionsQuery.data ?? [];
    if (!region) return list;
    return list.filter((c) => (c.region ?? '').trim() === region);
  }, [connectionsQuery.data, region]);

  /* 面板配置的默认 DNS（服务端设置），用于预填 DNS 输入框 */
  const vmDefaultsQuery = useQuery({
    queryKey: ['config', 'vm-defaults'],
    queryFn: configApi.getVmDefaults,
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  /* 下发配额：还能建几台。摆在明面上，别等提交后被后端 403 打回来才知道。 */
  const quotaQuery = useQuery({
    queryKey: ['vms', 'quota'],
    queryFn: vmsApi.quota,
    enabled: open,
    staleTime: 0,
    retry: false,
  });

  /* 按人分配的额度（资源池）：核数 / 内存 / 磁盘也在这里把住 —— 上面那份只管台数 */
  const myQuotaQuery = useQuery({
    queryKey: ['quota', 'me'],
    queryFn: quotaApi.mine,
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });
  const myGroup = myQuotaQuery.data?.groups.vm;
  /* 当前草稿的规格 vs 额度：越界的维度（空 = 没问题） */
  const quotaOver = useMemo(
    () =>
      checkGuestSpec(myGroup, {
        count: 1,
        cores: (form.cores || 0) * (form.sockets || 1),
        memory_mb: form.memory || null,
        disk_gb: form.disks.reduce((sum, d) => sum + (Number(d.size) || 0), 0) || null,
      }),
    [myGroup, form.cores, form.sockets, form.memory, form.disks],
  );
  const mySummary = remainingSummary(
    myGroup,
    {
      count: t('quota.fieldLabel.count'),
      cores: t('quota.fieldLabel.cores'),
      memory: t('quota.fieldLabel.memory'),
      disk: t('quota.fieldLabel.disk'),
    },
    { count: t('quota.unitGuest'), cores: t('quota.unitCore') },
  );

  const nextIdQuery = useQuery({
    queryKey: ['cluster', 'nextid', targetConn],
    queryFn: () => clusterApi.nextId(targetConn),
    enabled: open,
    retry: false,
    staleTime: 0,
  });

  const nodesQuery = useQuery({
    queryKey: ['nodes', targetConn],
    queryFn: () => nodesApi.list(targetConn),
    enabled: open,
    staleTime: 60_000,
  });

  const vmsQuery = useQuery({
    queryKey: ['vms', 'all', targetConn],
    queryFn: () => vmsApi.list(undefined, { connectionId: targetConn }),
    enabled: open,
    staleTime: 30_000,
  });

  /* 克隆源取模板专用接口：模板是共享资源，普通用户也要能用来部署。
     （/api/vms 对普通用户按归属过滤，模板都归属管理员，取不到） */
  const templatesQuery = useQuery({
    queryKey: ['templates', 'all', targetConn],
    /* 只要虚拟机模板：容器模板克隆出来的是容器，混进虚拟机的克隆源里必然失败 */
    queryFn: () => templatesApi.list(targetConn, 'qemu'),
    enabled: open,
    staleTime: 30_000,
  });

  /* 存储：只列**所选节点上**的存储池（含显式带连接，见 useNodeStorages） */
  const storagesQuery = useNodeStorages(form.node, targetConn, open);

  /* ISO 镜像列表 */
  const isoQuery = useQuery({
    queryKey: ['storages', 'content', 'iso', targetConn, form.node],
    queryFn: async () => {
      const isoStorages = (storagesQuery.data ?? []).filter(
        (s) =>
          s.active &&
          s.content.split(/[,;]/).some((c) => c.trim() === 'iso'),
      );
      const results = await Promise.all(
        isoStorages.map((s) =>
          storagesApi
            .content({ node: form.node, storage: s.storage, content: 'iso' }, targetConn)
            .then((items) => items.map((it) => ({ storage: s.storage, item: it })))
            .catch(() => []),
        ),
      );
      return results.flat();
    },
    enabled: open && Boolean(form.node) && (storagesQuery.data?.length ?? 0) > 0,
  });

  /* ---- 打开/关闭时初始化：始终回到默认值
     （创建方式 = 从模板克隆，克隆方式 = 链接克隆）---- */
  useEffect(() => {
    setStep(0);
    setForm(initialState);
    setErrors({});
    setTargetConn('');
    autoPickedRef.current = false;
    setMode('quick');
    modeTouchedRef.current = false;
  }, [open]);

  /* 预填面板默认 DNS：每次打开只填一次，用户自己写的值不会被覆盖。
     留空也不会导致机器没有 DNS —— 后端在写入时仍会套用这个默认值。 */
  const dnsPrefilledRef = useRef(false);
  useEffect(() => {
    if (!open) {
      dnsPrefilledRef.current = false;
      return;
    }
    if (dnsPrefilledRef.current) return;
    const dns = vmDefaultsQuery.data?.dns;
    if (!dns) return;
    dnsPrefilledRef.current = true;
    setForm((f) => (f.ciDns ? f : { ...f, ciDns: dns }));
  }, [open, vmDefaultsQuery.data]);

  /* 预填面板默认克隆方式（「设置 → 创建默认值 → 默认克隆方式」）。同样每次打开
     只填一次，用户在第 2 步点过「链接 / 完整」之后不再覆盖。
     自定义部署默认就是「从模板克隆」，所以这个初值决定的就是它的默认克隆方式；
     快速部署那份由 QuickDeployForm 各自预填，模板页克隆同理。 */
  const cloneFullPrefilledRef = useRef(false);
  useEffect(() => {
    if (!open) {
      cloneFullPrefilledRef.current = false;
      return;
    }
    const defaults = vmDefaultsQuery.data;
    if (cloneFullPrefilledRef.current || !defaults) return;
    cloneFullPrefilledRef.current = true;
    setForm((f) => ({ ...f, cloneFull: Boolean(defaults.full_clone) }));
  }, [open, vmDefaultsQuery.data]);

  /* 默认选中一套具体连接：让所有取数都带上它，
     否则后端会走「未指定连接 → 合并所有 PVE」的分支，节点列表会混入别的主机。
     优先落在顶栏「集群作用域」选的那一套 —— 用户刚把作用域切到 B，进向导却
     默认在 A，多半不是他想要的；作用域为「全部集群」时回落到兜底连接。
     选了区域之后候选集缩小到该区域的连接，原来那套不在里面就重新挑一套。 */
  useEffect(() => {
    if (!open) return;
    if (targetConn && scopedConnections.some((c) => c.id === targetConn)) return;
    const target =
      scopedConnections.find((c) => c.id === getGlobalScope()) ??
      scopedConnections.find((c) => c.active) ??
      scopedConnections[0];
    setTargetConn(target?.id ?? '');
  }, [open, targetConn, scopedConnections]);

  /* 切换目标 PVE 主机后：VMID / 节点名 / 模板都不通用，清空后由下方效果按新主机重新填充。
     VMID 必须一起清掉：每台 PVE 的 VMID 空间各自独立，A 主机取到的号在 B 主机上
     很可能已被占用（就是「config file already exists」的来源）。 */
  useEffect(() => {
    setForm((f) => ({ ...f, vmid: '', node: '', cloneFrom: '' }));
    autoPickedRef.current = false;
  }, [targetConn]);

  /* 自动填充 VMID 与默认节点 */
  useEffect(() => {
    if (!open) return;
    setForm((f) => {
      const next: FormState = { ...f };
      if (!f.vmid && nextIdQuery.data?.vmid) {
        next.vmid = String(nextIdQuery.data.vmid);
      }
      if (!f.node) {
        const nodes = nodesQuery.data ?? [];
        const online = nodes.find((n) => n.status === 'online');
        if (online) next.node = online.node;
        else if (nodes[0]) next.node = nodes[0].node;
      }
      return next;
    });
  }, [open, nextIdQuery.data, nodesQuery.data]);

  /* 存储可选列表 */
  /* ---- IP 地址池（可选）：提供未被占用的静态地址 ---- */
  const poolsQuery = useQuery({
    queryKey: ['ip-pools'],
    queryFn: ipPoolsApi.get,
    enabled: open,
    staleTime: 30_000,
    retry: false,
  });



  const storageOptions = useMemo(
    () => [
      { label: t('vmDetail.selectStorage'), value: '' },
      ...(storagesQuery.data ?? [])
        .filter(
          (s) =>
            s.active &&
            s.content.split(/[,;]/).some((c) => c.trim() === 'images'),
        )
        .map((s) => ({
          label: t('vmCreate.storageOption', {
            storage: s.storage,
            type: s.type,
            avail: formatBytes(s.avail),
          }),
          value: s.storage,
        })),
    ],
    [storagesQuery.data, t],
  );

  /* 「目标 PVE 主机」：始终对应一条具体连接。
     取值必须落到 id 上——若留空，后端会认为「未指定连接」而把所有主机的节点
     合并返回，创建向导的节点下拉就会混入别的主机的节点。 */
  const connectionOptions = useMemo(
    () =>
      scopedConnections.map((c) => ({
        /* 标签里**不放主机地址**：普通用户不需要直接连这台 PVE，而「面板接了哪些
           主机、地址是什么」属于管理信息，不该从一个创建表单里漏出去 */
        label: `${c.name || c.host}${
          c.active ? t('vmCreate.connCurrentTag') : ''
        }`,
        value: c.id,
      })),
    [scopedConnections, t],
  );

  const targetConnLabel =
    connectionOptions.find((o) => o.value === targetConn)?.label ??
    t('vmCreate.connCurrent');

  /* 可用作网桥的网卡：与快速部署 / 新建容器 / 重装用同一套筛选（含 OVS 桥）。
     静态地址与 DHCP 都走这块网卡，候选范围不随地址模式变化。 */
  const bridges = useNodeBridges(form.node, targetConn, open && Boolean(form.node));

  const bridgeOptions = useMemo(
    () => [
      { label: t('vmDetail.addNicBridgePlaceholder'), value: '' },
      ...(bridges.options.length
        ? bridges.options
        : [{ label: 'vmbr0', value: 'vmbr0' }]),
    ],
    [bridges.options, t],
  );

  /* ---- 克隆源：只给模板 ----
     后端 /api/templates 已经筛过「type = qemu 且 template = 1」，拿到的就是
     可以用来克隆的虚拟机模板，且不受归属过滤（模板是共享资源，普通用户也要能
     用它部署 —— 而 /api/vms 对普通用户按归属过滤，取不到别人名下的模板）。

     这里刻意**不并入自己名下的普通虚拟机**：克隆一台正在跑的机器会连带复制它
     的运行时状态（磁盘里正在写入的数据、网卡 MAC、cloud-init 已经固化过的
     配置），拿去批量开出来的机器要么起不来，要么几台之间 MAC 冲突。模板才是
     PVE 认可的、被「冻结」过的干净基线。没有模板时宁可让克隆源为空并给出提示，
     也不要给出一堆看着能用、实际会出问题的选项。 */
  const templatesOnConn = useMemo(() => {
    /* 两道过滤：

       * 类型 —— 只能克隆虚拟机模板。后端已按 ``guest_type=qemu`` 收窄，这里再挡
         一次是为了兜住缓存里的旧响应（模板页与本向导共用 ``/templates``，改口径
         时先到的那份数据可能还混着容器模板）；
       * 归属 —— 模板必须来自「即将创建虚拟机的那台 PVE」：不同主机的节点名与
         VMID 不通用，混入别家的模板会导致克隆打到错误的连接上。 */
    return (templatesQuery.data ?? []).filter(
      (tpl) =>
        (tpl.guest_type ?? 'qemu') !== 'lxc' &&
        (!targetConn ||
          !tpl.connection_id ||
          tpl.connection_id === targetConn),
    );
  }, [templatesQuery.data, targetConn]);

  /* 目标节点的存储清单：判定「别节点的模板能不能跨过来」要用它的 shared 标记。
     还没读到（加载中 / 读失败）时传 undefined，判定按「未知」放行 —— 拦住的话
     用户会看到一张空列表，比交给后端报错更糟。 */
  const placementStorages = useMemo(
    () =>
      storagesQuery.isLoading || storagesQuery.error
        ? undefined
        : storagesQuery.data,
    [storagesQuery.isLoading, storagesQuery.error, storagesQuery.data],
  );

  /* 克隆源与新建目标是两个节点：`clone_from.node` 取自模板自己所在的那台，跟
     `form.node` 无关。原先这里只用 `tpl.node === form.node` 收紧，于是**共享存储
     上的模板永远跨不过来** —— 模板明明在各节点都挂着的 Ceph/NFS 上，却提示
     「这个节点没有模板」。跨节点克隆在 PVE 里是支持的（clone 接口带 `target`），
     前提是模板盘在共享存储上且目标节点挂得到，判定见 utils/templatePick 的
     clonePlacement。本地盘的模板仍然只在本节点出现。

     节点还没定下来时（首屏自动填节点之前）先不收紧，避免闪一下空列表。 */
  const cloneSources = useMemo(() => {
    if (!form.node) return templatesOnConn;
    return templatesOnConn.filter((tpl) => {
      const placement = clonePlacement(tpl, form.node, placementStorages);
      return placement.kind !== 'blocked';
    });
  }, [templatesOnConn, form.node, placementStorages]);

  /* 别的节点上有什么、为什么用不了 —— 只用于把话说清楚，不进候选。
     「缺共享存储」与「本地盘」是两种完全不同的修法，必须分开讲。 */
  const cloneTemplatesElsewhere = useMemo(() => {
    const list = form.node
      ? templatesOnConn.filter((tpl) => tpl.node !== form.node)
      : [];
    const nodes = Array.from(new Set(list.map((tpl) => tpl.node)));
    const missing: string[] = [];
    const local: string[] = [];
    let blocked = 0;
    for (const tpl of list) {
      const placement = clonePlacement(tpl, form.node, placementStorages);
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
  }, [templatesOnConn, form.node, placementStorages]);

  /* 目标主机上已被占用的 VMID（含模板）。普通用户拿到的列表按归属过滤过，
     只包含自己名下的机器，所以这里只做「发现即拦截」，不声称覆盖全部占用；
     没拦住的仍由后端把 PVE 的报错翻译成可操作提示。 */
  const takenVmids = useMemo(
    () => new Set((vmsQuery.data ?? []).map((v) => Number(v.vmid))),
    [vmsQuery.data],
  );

  /* 选择器只吃「已格式化好的文本」：映射统一在 utils/templatePick —— 快速部署
     那条流程用的是同一个函数，同一张模板在两个入口里的名称 / 容量 / 标签才不会
     各写一套。 */
  const clonePickItems = useMemo(
    /* 带上目标节点：跨节点候选（共享存储上的模板）的摘要行要点明「将克隆到哪」 */
    () => vmTemplatePickItems(cloneSources, t, form.node),
    [cloneSources, t, form.node],
  );

  /* form.cloneFrom 存的是选择器的唯一值（连接|节点|VMID）：不同 PVE 上节点名与
     VMID 都可能重复，只按 `node:vmid` 找会命中另一台主机的同名模板 —— 摘要卡与
     Logo 都会显示成别人的。先从候选回原始标识，再回接口记录。 */
  const selectedCloneSource: TemplateItem | undefined = useMemo(() => {
    if (!form.cloneFrom) return undefined;
    const picked = clonePickItems.find((item) => item.value === form.cloneFrom);
    if (!picked) return undefined;
    const [node, vmidStr] = picked.raw.split(':');
    const vmid = Number(vmidStr);
    return cloneSources.find((v) => v.node === node && v.vmid === vmid);
  }, [form.cloneFrom, clonePickItems, cloneSources]);

  /* 面板默认克隆方式：只用来给「链接 / 完整」两张卡片标一个「默认」（见下面
     第 2 步的 clone-method 卡片），不改写用户已选的值 */
  const panelDefaultFull = Boolean(vmDefaultsQuery.data?.full_clone);

  /* 默认选中第一个模板，省去手动选择。
     取的是**过滤后**的 cloneSources：直接用接口返回的第一条，可能是一张容器
     模板或别的主机的模板，写进 cloneFrom 后选择器找不到对应项，摘要卡会整个
     消失 —— 看着像「没选」。 */
  useEffect(() => {
    if (!open || autoPickedRef.current) return;
    if (form.mode !== 'clone' || form.cloneFrom) return;
    const first = clonePickItems[0];
    if (first) {
      autoPickedRef.current = true;
      setForm((f) => (f.cloneFrom ? f : { ...f, cloneFrom: first.value }));
    }
  }, [open, form.mode, form.cloneFrom, clonePickItems]);

  /* 目标节点一换，候选就换了一份：原来选中的模板若已不在列表里就清掉，交给上面
     那段「默认选中第一个」按新节点重挑。不清的话界面上一个都没选中，提交时却还
     带着上一台节点的 `clone_from.node` —— 源节点与目标节点是两回事，后端不会替
     你纠正，克隆会直接打到模板不在的那台机器上。 */
  useEffect(() => {
    if (!open || !form.cloneFrom) return;
    if (clonePickItems.some((item) => item.value === form.cloneFrom)) return;
    autoPickedRef.current = false;
    setForm((f) => (f.cloneFrom === form.cloneFrom ? { ...f, cloneFrom: '' } : f));
  }, [open, form.cloneFrom, clonePickItems]);

  /* ---- 通用更新 ---- */
  const update = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
    setErrors((e) => {
      if (!e[key as string]) return e;
      const next = { ...e };
      delete next[key as string];
      return next;
    });
  }, []);

  /* ---- 客户机初始化工具：Cloud-Init / Cloudbase-Init ----
     Windows 上跑的**不是** cloud-init（官方版装不上 Windows 原生系统，换成
     Cloudbase-Init），所以名字与默认值都得按系统分流。派生值在这里算一次，
     步骤条 / 表单 / 确认页共用，免得某处漏改又冒出「Windows 的 cloud-init」。 */
  const windowsGuest = isWindowsOstype(form.ostype);
  const ciName = initAgentName(form.ostype);

  /* Windows 客户机的磁盘总线固定 SATA、网卡固定 Intel E1000（安装介质里没有
     virtio 驱动）：切换系统与「一键推荐配置」都由 applyWindowsCompat 自动换好，
     第 4 步这两个选择框只读 —— 用户看不到、也改不了那两个会让他装不上/连不上的
     选项，但 Linux 侧仍然完全可选。 */
  /* 启动顺序留空时后端会按「磁盘 → 光驱 → 网卡」自动设置（见
     backend/app/vmconfig.py 的 boot 注释：光驱必须进名单，否则空盘时引导不起来）。 */

  /* 网卡是否填了静态 IP（确认页用它区分「Windows 不会自动下发 IP」） */
  const netHasStaticIp = hasStaticIpOnNics(form);
  /* 实际是否下发初始化配置：与提交、校验共用 initActive，只有一处判据 */
  const ciActive = initActive(form);

  /* 切换操作系统：换了体系（Windows ↔ Linux）就复位初始化开关与默认用户名，
     并把 virtio 的磁盘 / 网卡换成 SATA / E1000（Windows 认不出 virtio 硬件，
     见 applyWindowsCompat）。同体系内换版本（win10 → win11）不动用户已经填过
     的东西；用户名只在「还是对面体系默认值」时才跟着换，用户改过的名字不覆盖。 */
  const changeOstype = (value: string) => {
    setForm((f) => {
      const wasWindows = isWindowsOstype(f.ostype);
      const nowWindows = isWindowsOstype(value);
      const next: FormState = { ...f, ostype: value };
      if (wasWindows === nowWindows) return next;
      next.ciEnabled = false;
      if (nowWindows && f.ciUser.trim() === 'ubuntu') next.ciUser = 'Administrator';
      if (!nowWindows && f.ciUser.trim() === 'Administrator') next.ciUser = 'ubuntu';
      if (nowWindows) applyWindowsCompat(next);
      return next;
    });
  };

  /* ---- 步骤导航 ---- */
  const goNext = () => {
    const e = validateStep(t, step, form, takenVmids);
    setErrors(e);
    if (Object.keys(e).length > 0) {
      toast.warning(
        t('vmCreate.toastCheckForm'),
        t('vmCreate.toastCheckFormHint'),
      );
      return;
    }
    if (step < 5) setStep((s) => (s + 1) as StepIndex);
  };

  const goPrev = () => {
    if (step > 0) setStep((s) => (s - 1) as StepIndex);
  };

  /* ---- 提交 ---- */
  const submit = async () => {
    /* 全量校验 */
    const allErrors: Errors = {
      ...validateStep(t, 0, form, takenVmids),
      ...validateStep(t, 1, form, takenVmids),
      ...validateStep(t, 2, form, takenVmids),
      ...validateStep(t, 3, form, takenVmids),
      ...validateStep(t, 4, form, takenVmids),
    };
    setErrors(allErrors);
    if (Object.keys(allErrors).length > 0) {
      toast.error(
        t('vmCreate.toastInvalid'),
        t('vmCreate.toastInvalidHint'),
      );
      return;
    }

    /* 「从模板克隆」但选中的模板已经不在列表里（模板被删 / 换了主机）：
       此时 cloneFrom 是一段失效的引用，若继续往下走会掉进「全新安装」分支，
       悄悄建出一台空盘机器 —— 宁可退回上一步让用户重选。 */
    if (form.mode === 'clone' && !selectedCloneSource) {
      toast.error(
        t('vmCreate.cloneSourceEmptyTitle'),
        t('templatePicker.vmEmptyHint'),
      );
      setStep(1);
      return;
    }

    // 全新创建：IP 在网卡上配置，折叠进初始化下发；克隆：用初始化步骤里的 IP 列表。
    // ciActive（实际是否下发）在组件体里算好了 —— 确认页显示的就是同一个值。
    const netIpConfigs = form.networks.map((n) => {
      /* 静态但地址空着（池子没配 / 用户清空了）：退回 DHCP 而不是提交一个
         空 ip，否则 PVE 会写出一行 `ip=` 的 cloud-init 配置，机器起来没地址。 */
      const ip = n.ipMode === 'static' ? (n.ip || '').trim() : '';
      return {
        ip: ip || 'dhcp',
        gateway: ip ? (n.gateway || '').trim() : '',
      };
    });

    const cloudinit: CloudInitConfig | undefined = ciActive
      ? {
          enabled: true,
          user: form.ciUser || undefined,
          password: form.ciPassword || undefined,
          ssh_keys: form.ciSshKeys || undefined,
          nameserver: form.ciDns || undefined,
          ip_configs:
            form.mode === 'new'
              ? netIpConfigs
              : form.ciIps.map((r) => {
                  const ip = r.ipMode === 'static' ? (r.ip || '').trim() : '';
                  return {
                    ip: ip || 'dhcp',
                    gateway: ip ? r.gateway || '' : '',
                    /* 克隆体沿用模板网卡，这里能指定的就是「地址从哪个网段来」：
                       pool_id 让后端按该池分配，而不是按网桥猜一个。 */
                    pool_id: r.poolId || undefined,
                  };
                }),
        }
      : undefined;

    /* 高级硬件：NUMA 绑定 + EFI 盘 / 虚拟 TPM。
       克隆分支只带 NUMA 与亲和性 —— EFI 盘与 TPM 是存储上真实存在的卷，
       Win11 模板本身就该自带，克隆时随模板一起继承（后端也不会在克隆时重复下发）。 */
    const numaNodes: NumaNodeSpec[] = form.numaNodes
      .filter((n) => n.cpus.trim())
      .map((n) => {
        const spec: NumaNodeSpec = { cpus: n.cpus.trim() };
        const mem = Number(n.memory);
        if (n.memory.trim() && Number.isFinite(mem) && mem > 0) spec.memory = mem;
        if (n.hostnodes.trim()) {
          spec.hostnodes = n.hostnodes.trim();
          // policy 只在绑定了宿主节点时才有意义，没绑定就不下发
          if (n.policy) spec.policy = n.policy;
        }
        return spec;
      });

    const numaOptions = {
      numa: form.numaEnabled || numaNodes.length > 0,
      numa_nodes: numaNodes,
      affinity: form.numaAffinity.trim() || undefined,
    };

    const firmwareOptions = {
      efi_disk: form.efiEnabled
        ? {
            storage: form.efiStorage,
            efitype: form.efiType,
            pre_enrolled_keys: form.efiPreEnrolled,
          }
        : undefined,
      tpm: form.tpmEnabled
        ? { storage: form.tpmStorage, version: form.tpmVersion }
        : undefined,
    };

    let payload: VmCreateRequest;
    /* 内存气球：留空就不下发这个键（沿用 PVE 默认 —— 整份内存不回收） */
    const balloonOption = form.balloon.trim() ? Number(form.balloon) : undefined;

    if (form.mode === 'clone' && selectedCloneSource) {
      payload = {
        node: form.node,
        vmid: Number(form.vmid),
        name: form.name.trim(),
        memory: form.memory,
        cores: form.cores,
        sockets: form.sockets,
        cpu_type: form.cpuType,
        ostype: form.ostype,
        balloon: balloonOption,
        ...numaOptions,
        disks: [],
        /* 空 = 继承模板网卡；选了桥就只改 bridge 那一段（见后端 bridge_override） */
        networks: [],
        bridge_override: form.cloneBridge || undefined,
        tags: form.tags || undefined,
        description: form.description || undefined,
        start_on_boot: form.startOnBoot,
        cloudinit,
        clone_from: {
          vmid: selectedCloneSource.vmid,
          node: selectedCloneSource.node,
          full: form.cloneFull,
          // 链接克隆不允许指定目标存储（PVE 会报错），仅完整克隆携带。
          target_storage: form.cloneFull ? form.cloneStorage || undefined : undefined,
          /* 模板在别的节点时（共享存储上的模板）必须显式告诉后端「克隆体落到
             哪台节点」：clone_from.node 是**模板**所在的节点，不带 target_node
             的话后端只能按它落点 —— 克隆体会悄悄建在模板那台机器上。 */
          target_node:
            selectedCloneSource.node !== form.node ? form.node : undefined,
          start: form.cloneStart,
        },
      };
    } else {
      const disks: VmCreateDisk[] = form.disks.map((d) => ({
        storage: d.storage,
        size: d.size,
        interface: d.interface,
        format: d.format,
      }));

      const networks: VmCreateNetwork[] = form.networks.map((n) => ({
        bridge: n.bridge,
        model: n.model,
        vlan_tag: n.vlan_tag ? Number(n.vlan_tag) : null,
        firewall: n.firewall,
        macaddr: n.macaddr || undefined,
        rate: n.rate.trim() ? Number(n.rate) : undefined,
        /* 选了网段就把它带下去：DHCP 时后端**只从这个池**分配（不然多个池共用
           同一个网桥时会永远取配置里的第一个，用户选的「测试网」就失效了）。
           静态地址的池只用于预填，这里带不带都不影响下发，带上无害。 */
        pool_id: n.poolId || undefined,
      }));

      payload = {
        node: form.node,
        vmid: Number(form.vmid),
        name: form.name.trim(),
        memory: form.memory,
        cores: form.cores,
        sockets: form.sockets,
        cpu_type: form.cpuType,
        ostype: form.ostype,
        balloon: balloonOption,
        disks,
        networks,
        iso: form.iso || undefined,
        scsi_hw: form.scsihw,
        bios: form.bios,
        /* 空串 = 默认机型：后端按「不写 machine」处理（见 vmconfig.normalize_machine） */
        machine: form.machine || undefined,
        ...numaOptions,
        ...firmwareOptions,
        boot_order: form.bootOrder || undefined,
        start_on_boot: form.startOnBoot,
        cloudinit,
        /* 接入安全管控只在「全新创建 + cloud-init + 静态 IP」下有意义：
           克隆走的是后端另一条路径（原样复制源机配置），不看这个开关。 */
        manage: sshNavOff ? false : form.ciManage,
        tags: form.tags || undefined,
        description: form.description || undefined,
      };
    }

    setSubmitting(true);
    try {
      /* targetConn 为空表示用面板当前连接，否则在指定 PVE 主机上创建 */
      const result = await vmsApi.create(payload, targetConn);
      toast.success(
        t('vmCreate.toastCreated'),
        t('vmCreate.toastCreatedHint', { vmid: result.vmid ?? form.vmid }),
      );
      /* 这个组件**不走 runner**（创建是直连调用），所以列表与派生数据都得自己
         作废：向导是盖在列表页上的，只刷额度的话，关掉向导后新机器那一行要等
         「数据刷新间隔」（默认 30 秒）才出现 —— 用户会以为没建成，转头再点一次。
         ['storages'] 一并刷：新磁盘 / cloud-init 盘会改变存储用量。 */
      void queryClient.invalidateQueries({ queryKey: ['vms'] });
      void queryClient.invalidateQueries({ queryKey: ['lxc'] });
      void queryClient.invalidateQueries({ queryKey: ['cluster'] });
      void queryClient.invalidateQueries({ queryKey: ['guests'] });
      void queryClient.invalidateQueries({ queryKey: ['storages'] });
      invalidateQuota(queryClient);
      onCreated?.(result.vmid ?? Number(form.vmid));
      onClose();
    } catch (err) {
      toast.error(t('vmCreate.toastCreateFailed'), errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  /* ---- 动态列表操作 ---- */
  const addDisk = () =>
    setForm((f) => ({
      ...f,
      disks: [
        ...f.disks,
        {
          key: uid(),
          storage: f.disks[0]?.storage ?? '',
          size: 20,
          // Windows 必须 SATA：新盘也得跟着系统走。否则「选择框已锁成 SATA」
          // 而新加的盘是 scsi —— 用户改不动，装系统时这盘又看不见。
          interface: `${isWindowsOstype(f.ostype) ? 'sata' : 'scsi'}${f.disks.length}`,
          format: 'qcow2',
        },
      ],
    }));

  const removeDisk = (key: string) =>
    setForm((f) => ({ ...f, disks: f.disks.filter((d) => d.key !== key) }));

  const patchDisk = (key: string, patch: Partial<DiskRow>) =>
    setForm((f) => ({
      ...f,
      disks: f.disks.map((d) => (d.key === key ? { ...d, ...patch } : d)),
    }));

  const addNet = () =>
    setForm((f) => ({
      ...f,
      networks: [
        ...f.networks,
        {
          key: uid(),
          bridge: f.networks[0]?.bridge ?? 'vmbr0',
          // 同 addDisk：Windows 必须 E1000，否则新网卡是 virtio，而选择框已锁死
          model: isWindowsOstype(f.ostype) ? 'e1000' : 'virtio',
          vlan_tag: '',
          firewall: true,
          macaddr: '',
          /* 与快速部署一致：附加网卡不抢池子里的地址，默认 DHCP */
          ipMode: 'dhcp',
          ip: '',
          gateway: '',
          poolId: '',
          rate: '',
        },
      ],
    }));

  const removeNet = (key: string) =>
    setForm((f) => ({
      ...f,
      networks: f.networks.filter((n) => n.key !== key),
    }));

  const patchNet = (key: string, patch: Partial<NetRow>) =>
    setForm((f) => {
      const next: FormState = {
        ...f,
        networks: f.networks.map((n) => (n.key === key ? { ...n, ...patch } : n)),
      };
      // 网卡填了静态 IP 就得靠初始化工具下发，自动打开这一开关 —— Windows 除外：
      // 那边跑的是 Cloudbase-Init，要客户机内先装好，不能默认替用户打开。
      if (patch.ip !== undefined) {
        const static_ = patch.ip.trim() !== '' && patch.ip.trim().toLowerCase() !== 'dhcp';
        if (static_ && !isWindowsOstype(next.ostype)) next.ciEnabled = true;
      }
      return next;
    });

  /**
   * 网段编辑块（NicSegmentFields）的改动 → 网卡状态。
   *
   * 组件用的是通用字段名（``vlan``），网卡状态里叫 ``vlan_tag``；两边对不上会
   * 静默丢掉「选网段顺带带上 VLAN」这一步，所以映射显式写出来。
   */
  const patchNetSegment = (key: string, patch: Partial<SegmentValue>) =>
    patchNet(key, {
      ...(patch.bridge !== undefined ? { bridge: patch.bridge } : {}),
      ...(patch.vlan !== undefined ? { vlan_tag: patch.vlan } : {}),
      ...(patch.ipMode !== undefined ? { ipMode: patch.ipMode } : {}),
      ...(patch.ip !== undefined ? { ip: patch.ip } : {}),
      ...(patch.gateway !== undefined ? { gateway: patch.gateway } : {}),
      ...(patch.poolId !== undefined ? { poolId: patch.poolId } : {}),
    });

  /* ---- NUMA 节点（动态列表，与磁盘/网卡同一套操作）---- */
  const addNumaNode = () =>
    setForm((f) => ({
      ...f,
      numaEnabled: true,
      numaNodes: [
        ...f.numaNodes,
        {
          key: uid(),
          cpus: '',
          memory: '',
          hostnodes: '',
          policy: '',
        },
      ],
    }));

  const removeNumaNode = (key: string) =>
    setForm((f) => ({
      ...f,
      numaNodes: f.numaNodes.filter((n) => n.key !== key),
    }));

  const patchNumaNode = (key: string, patch: Partial<NumaRow>) =>
    setForm((f) => ({
      ...f,
      numaNodes: f.numaNodes.map((n) => (n.key === key ? { ...n, ...patch } : n)),
    }));

  /** 一键把「Windows 11 必需」的那几项一起设好（OVMF + q35 + EFI + TPM + SATA 磁盘）。 */
  const applyWindows11Preset = () => {
    setForm((f) => {
      const fallback = f.disks[0]?.storage ?? '';
      const next: FormState = {
        ...f,
        ostype: 'win11',
        bios: 'ovmf',
        machine: 'q35',
        cpuType: 'host',
        efiEnabled: true,
        efiStorage: f.efiStorage || fallback,
        efiType: '4m',
        efiPreEnrolled: true,
        tpmEnabled: true,
        tpmStorage: f.tpmStorage || fallback,
        tpmVersion: 'v2.0',
        // Windows 走的是 Cloudbase-Init，必须客户机内先装好 —— 默认关掉，
        // 用户装好之后自己到第 5 步打开（顺带把默认用户名换成 Administrator）。
        ciEnabled: false,
        ciUser: f.ciUser.trim() === 'ubuntu' ? 'Administrator' : f.ciUser,
      };
      // 磁盘换 SATA、网卡换 E1000：Windows 安装介质里没有 virtio 驱动
      applyWindowsCompat(next);
      return next;
    });
    setErrors((e) => {
      const next = { ...e };
      delete next.efiStorage;
      delete next.tpmStorage;
      return next;
    });
    toast.success(
      t('vmCreate.toastWin11'),
      t('vmCreate.toastWin11Hint'),
    );
  };

  const addIp = () =>
    setForm((f) => ({
      ...f,
      ciIps: [...f.ciIps, { key: uid(), ipMode: 'dhcp', ip: '', gateway: '', poolId: '' }],
    }));

  const removeIp = (key: string) =>
    setForm((f) => ({ ...f, ciIps: f.ciIps.filter((r) => r.key !== key) }));

  const patchIp = (key: string, patch: Partial<IpRow>) =>
    setForm((f) => ({
      ...f,
      ciIps: f.ciIps.map((r) => (r.key === key ? { ...r, ...patch } : r)),
    }));

  /**
   * 网段编辑块的改动 → 克隆模式的 IP 行。
   *
   * 克隆体沿用模板的网卡，网桥 / VLAN 不在这里改（组件带回来的 bridge / vlan
   * 忽略掉）；只有地址与网段生效。
   */
  const patchIpSegment = (key: string, patch: Partial<SegmentValue>) =>
    patchIp(key, {
      ...(patch.ipMode !== undefined ? { ipMode: patch.ipMode } : {}),
      ...(patch.ip !== undefined ? { ip: patch.ip } : {}),
      ...(patch.gateway !== undefined ? { gateway: patch.gateway } : {}),
      ...(patch.poolId !== undefined ? { poolId: patch.poolId } : {}),
    });

  /* ---- 磁盘总量估算 ---- */
  const diskTotal = useMemo(
    () => form.disks.reduce((s, d) => s + d.size * 1024 ** 3, 0),
    [form.disks],
  );

  /* ---- 当前步骤错误数量 ---- */
  const currentErrors = Object.keys(errors).length;

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('vmCreate.modalTitle')}
      description={t('vmCreate.modalDesc')}
      size="lg"
      closeOnOverlay={false}
      footer={
        /* 快速模式自带提交按钮（在表单末尾），这里不再重复一套 footer */
        mode === 'quick' ? undefined : (
        <>
          <div className="flex-1">
            {currentErrors > 0 ? (
              <span className="fs-sm text-danger flex items-center gap-6">
                <IconAlert size={14} />
                {t('vmCreate.errorsCount', { count: currentErrors })}
              </span>
            ) : null}
          </div>
          <Button
            variant="secondary"
            onClick={step === 0 ? onClose : goPrev}
            disabled={submitting}
          >
            {step === 0 ? t('common.cancel') : t('vmCreate.prev')}
          </Button>
          {step < 5 ? (
            <Button variant="primary" onClick={goNext}>
              {t('vmCreate.next')}
            </Button>
          ) : (
            <Button
              variant="primary"
              onClick={submit}
              loading={submitting}
              disabled={
                (quotaQuery.data ? !quotaQuery.data.can_create : false) ||
                quotaOver.length > 0
              }
              title={
                quotaOver.length
                  ? t('vmCreate.quotaSpecOver')
                  : quotaQuery.data && !quotaQuery.data.can_create
                    ? isAdmin
                      ? t('vmCreate.quotaUsedUp')
                      : t('quota.askAdmin')
                    : undefined
              }
            >
              {t('vmCreate.submit')}
            </Button>
          )}
        </>
        )
      }
    >
      {/* ---- 快速 / 自定义：入口只有一个（列表页的「创建虚拟机」）---- */}
      <div className="flex items-center gap-12 flex-wrap mb-16">
        <SegmentedControl<CreateMode>
          value={mode}
          onChange={pickMode}
          ariaLabel={t('vmCreate.modeAria')}
          options={[
            { label: t('vmCreate.modeQuick'), value: 'quick' },
            { label: t('vmCreate.modeCustom'), value: 'custom' },
          ]}
        />
        <span className="fs-sm text-muted">
          {mode === 'quick'
            ? t('vmCreate.modeQuickHint')
            : t('vmCreate.modeCustomHint')}
        </span>
      </div>

      {mode === 'quick' ? (
        /* 快速模式自带提交按钮（在表单末尾），所以上面的 footer 让位 */
        <QuickDeployForm
          open={open}
          kind="qemu"
          onCreated={onCreated}
          onClose={onClose}
          onSwitchToCustom={() => pickMode('custom')}
        />
      ) : (
        <>
      {/* ---- 步骤条 ---- */}
      <div
        className="wizard-steps"
        role="list"
        aria-label={t('vmCreate.stepsAria')}
      >
        {STEP_KEYS.map((key, i) => (
          <div
            key={key}
            role="listitem"
            className={`wizard-step ${i === step ? 'is-active' : ''} ${
              i < step ? 'is-done' : ''
            }`}
          >
            <span className="wizard-step-num" aria-hidden="true">
              {i < step ? <IconCheck size={12} /> : i + 1}
            </span>
            <span className="wizard-step-label">
              {i === CI_STEP ? ciName : t(key)}
            </span>
            {i < STEP_KEYS.length - 1 ? (
              <span
                className={`wizard-connector ${i < step ? 'is-done' : ''}`}
                aria-hidden="true"
              />
            ) : null}
          </div>
        ))}
      </div>

      <div className="wizard-body">
        {/* ================= 第 1 步：基本信息 ================= */}
        {step === 0 ? (
          <div className="wizard-section">
            {/* 可下发数量：只在管理员设了配额时出现，没设就是不限 */}
            {quotaQuery.data?.limited ? (
              <Notice
                tone={quotaQuery.data.can_create ? 'info' : 'warning'}
                title={
                  quotaQuery.data.can_create
                    ? t('vmCreate.quotaRemaining', {
                        count: quotaQuery.data.remaining,
                      })
                    : t('vmCreate.quotaUsedUp')
                }
              >
                {/* 用尽时的「总量 / 已用」只给管理员：普通用户改不了那份设置，
                    数字对他没有出口，只需要知道去找谁 */}
                {quotaQuery.data.can_create
                  ? t('vmCreate.quotaSummaryOk', {
                      quota: quotaQuery.data.quota,
                      used: quotaQuery.data.used,
                    })
                  : isAdmin
                    ? t('vmCreate.quotaSummaryFull', {
                        quota: quotaQuery.data.quota,
                        used: quotaQuery.data.used,
                      })
                    : t('quota.askAdmin')}
                {quotaQuery.data.count_error ? (
                  <>
                    {t('vmCreate.quotaCountError', {
                      count: quotaQuery.data.count_error,
                    })}
                  </>
                ) : null}
              </Notice>
            ) : null}

            {/* 按人分配的额度：与上面那份「全局台数上限」是两层，这里报的是「我名下」 */}
            {mySummary ? (
              <Notice tone={quotaOver.length ? 'warning' : 'info'}>
                {t('vmCreate.quotaSpecRemaining', { summary: mySummary })}
                {quotaOver.length ? ` ${t('vmCreate.quotaSpecOver')}` : ''}
              </Notice>
            ) : null}

            <div className="form-grid">
              {/* 限宽必须包在外层：给 Select 传 style 只会缩窄内层输入框，
                  箭头（相对外层容器定位）会飘到框外 */}
              {regionOptions.length > 1 ? (
                <div className="field-compact">
                  <Select
                    label={t('vmCreate.fieldRegion')}
                    value={region}
                    onChange={(e) => setRegion(e.target.value)}
                    options={regionOptions}
                    hint={t('vmCreate.fieldRegionHint')}
                  />
                </div>
              ) : null}
              <div className="field-compact">
                <Select
                  label={t('vmCreate.fieldTargetConn')}
                  value={targetConn}
                  onChange={(e) => setTargetConn(e.target.value)}
                  options={connectionOptions}
                  hint={t('vmCreate.fieldTargetConnHint')}
                />
              </div>
              <Field
                label={t('common.node')}
                required
                error={
                  errors.node ??
                  (nodesQuery.isError
                    ? t('vmCreate.nodesLoadFailed')
                    : undefined)
                }
                hint={
                  nodesQuery.isError
                    ? errorMessage(nodesQuery.error)
                    : t('vmCreate.fieldNodeHint')
                }
              >
                <NodePicker
                  nodes={nodesQuery.data ?? []}
                  value={form.node}
                  onChange={(node) => update('node', node)}
                  loading={nodesQuery.isLoading}
                />
              </Field>
              <Input
                label={t('vmCreate.fieldName')}
                required
                value={form.name}
                onChange={(e) => update('name', e.target.value)}
                placeholder={t('vmCreate.fieldNamePlaceholder')}
                error={errors.name}
                autoFocus
              />
              <Input
                label="VMID"
                required
                value={form.vmid}
                onChange={(e) => update('vmid', e.target.value.replace(/\D/g, ''))}
                placeholder={t('vmCreate.fieldVmidPlaceholder')}
                error={errors.vmid}
                hint={
                  nextIdQuery.data
                    ? t('vmCreate.vmidHintSuggested', {
                        conn: targetConnLabel,
                        vmid: nextIdQuery.data.vmid,
                      })
                    : t('vmCreate.vmidHint')
                }
              />
              <Input
                label={t('vmCreate.fieldTags')}
                value={form.tags}
                onChange={(e) => update('tags', e.target.value)}
                placeholder={t('vmCreate.fieldTagsPlaceholder')}
              />
            </div>

            <Textarea
              label={t('common.description')}
              value={form.description}
              onChange={(e) => update('description', e.target.value)}
              placeholder={t('vmCreate.fieldDescPlaceholder')}
              rows={3}
            />
          </div>
        ) : null}

        {/* ================= 第 2 步：创建方式 ================= */}
        {step === 1 ? (
          <div className="wizard-section">
            <div className="wizard-section-title">{t('vmCreate.stepMode')}</div>
            <div
              className="choice-cards"
              role="radiogroup"
              aria-label={t('vmCreate.modeAria')}
            >
              <button
                type="button"
                role="radio"
                aria-checked={form.mode === 'clone'}
                className={`choice-card ${form.mode === 'clone' ? 'is-active' : ''}`}
                onClick={() => update('mode', 'clone')}
              >
                <span className="choice-card-icon">
                  <IconTemplate size={20} />
                </span>
                <span className="choice-card-body">
                  <span className="choice-card-title">
                    {t('vmCreate.cloneFromTemplate')}
                    <Badge variant="accent" size="sm">
                      {t('vmCreate.recommended')}
                    </Badge>
                  </span>
                  <span className="choice-card-desc">
                    {t('vmCreate.cloneFromTemplateDesc')}
                  </span>
                </span>
                <span className="choice-card-check" aria-hidden="true">
                  {form.mode === 'clone' ? <IconCheck size={13} /> : null}
                </span>
              </button>

              <button
                type="button"
                role="radio"
                aria-checked={form.mode === 'new'}
                className={`choice-card ${form.mode === 'new' ? 'is-active' : ''}`}
                onClick={() => update('mode', 'new')}
              >
                <span className="choice-card-icon">
                  <IconDisk size={20} />
                </span>
                <span className="choice-card-body">
                  <span className="choice-card-title">{t('vmCreate.installFresh')}</span>
                  <span className="choice-card-desc">
                    {t('vmCreate.installFreshDesc')}
                  </span>
                </span>
                <span className="choice-card-check" aria-hidden="true">
                  {form.mode === 'new' ? <IconCheck size={13} /> : null}
                </span>
              </button>
            </div>

            {form.mode === 'clone' ? (
              <div className="choice-panel">
                <div className="choice-panel-head">
                  <span className="choice-panel-title">{t('vmCreate.cloneParams')}</span>
                  <span className="choice-panel-hint">{t('vmCreate.cloneParamsHint')}</span>
                </div>

                {/* 克隆源：模板少时直接平铺，多时先给一张「将使用此模板」摘要卡，
                    其余收进「更换模板」—— 与重装选模板同一套交互（见
                    SystemTemplatePicker）。 */}
                <Field
                  label={t('templatePicker.vmLabel')}
                  required
                  error={errors.cloneFrom}
                  hint={
                    cloneSources.length === 0
                      ? /* 这台节点没有可用模板时，点名「哪台节点上有几个、为什么用不了」：
                           用户刚在模板页见过 ubuntu24，不点名他会以为列表坏了，反复刷新；
                           共享存储没挂到本节点与「模板盘在本地」是两种不同的修法。 */
                        elsewhereTemplatesHint(t, form.node, cloneTemplatesElsewhere) ??
                        t('templatePicker.vmEmptyHint')
                      : t('templatePicker.vmHint')
                  }
                >
                  <SystemTemplatePicker
                    kind="vm"
                    items={clonePickItems}
                    value={form.cloneFrom}
                    onChange={(next) => update('cloneFrom', next)}
                  />
                </Field>

                {vmsQuery.isError || templatesQuery.isError ? (
                  <Notice
                    tone="danger"
                    title={t('vmCreate.cloneSourceLoadFailed')}
                  >
                    {errorMessage(vmsQuery.error ?? templatesQuery.error)}
                  </Notice>
                ) : null}

                {/* 只有**整条连接**上都没有模板时才说「这台 PVE 没有克隆源」；
                    仅仅是当前节点没有的话，上面的 hint 已经点名别的节点了。 */}
                {templatesOnConn.length === 0 && !templatesQuery.isLoading ? (
                  <Notice
                    tone="warning"
                    title={t('vmCreate.cloneSourceEmptyTitle')}
                  >
                    {t('vmCreate.cloneSourceEmptyBody', { conn: targetConnLabel })}
                  </Notice>
                ) : null}

                <Select
                  label={t('vmCreate.cloneStorage')}
                  value={form.cloneStorage}
                  onChange={(e) => update('cloneStorage', e.target.value)}
                  options={storageOptions}
                  disabled={!form.cloneFull}
                  hint={
                    form.cloneFull
                      ? t('vmCreate.cloneStorageHintFull')
                      : t('vmCreate.cloneStorageHintLinked')
                  }
                />

                {/* 接入网桥：克隆体默认沿用模板的网卡。要换网络（模板在 A 网、
                    新机器要进 B 网）时在这里选 —— 只替换 bridge 一段，MAC 与
                    VLAN 不变。地址是静态还是 DHCP 都能选，二者共用同一块网卡。 */}
                <Select
                  label={t('vmCreate.cloneBridge')}
                  value={form.cloneBridge}
                  onChange={(e) => update('cloneBridge', e.target.value)}
                  options={[
                    { label: t('vmCreate.cloneBridgeKeep'), value: '' },
                    ...(bridges.options.length
                      ? bridges.options
                      : [{ label: 'vmbr0', value: 'vmbr0' }]),
                  ]}
                  hint={
                    bridges.isError
                      ? t('vmCreate.nicBridgeLoadFailed')
                      : t('vmCreate.cloneBridgeHint')
                  }
                />

                <Field label={t('vmCreate.cloneMethod')}>
                  <div className="clone-methods">
                    <button
                      type="button"
                      className={`clone-method ${form.cloneFull ? '' : 'is-active'}`}
                      onClick={() => {
                        update('cloneFull', false);
                        update('cloneStorage', '');
                      }}
                    >
                      <span className="clone-method-radio">
                        {form.cloneFull ? null : <IconCheck size={12} />}
                      </span>
                      <span className="clone-method-body">
                        <span className="clone-method-title">
                          {t('vmCreate.cloneLinked')}
                          {/* 「默认」标在面板默认的那一项上（设置 → 创建默认值）：
                              原先固定标在链接克隆上，管理员把默认改成完整克隆后
                              它就成了一句假话 */}
                          {panelDefaultFull ? null : (
                            <span className="clone-method-tag">
                              {t('vmCreate.cloneDefault')}
                            </span>
                          )}
                        </span>
                        <span className="clone-method-desc">
                          {t('vmCreate.cloneLinkedDesc')}
                        </span>
                      </span>
                    </button>
                    <button
                      type="button"
                      className={`clone-method ${form.cloneFull ? 'is-active' : ''}`}
                      onClick={() => update('cloneFull', true)}
                    >
                      <span className="clone-method-radio">
                        {form.cloneFull ? <IconCheck size={12} /> : null}
                      </span>
                      <span className="clone-method-body">
                        <span className="clone-method-title">
                          {t('vmCreate.cloneFull')}
                          {panelDefaultFull ? (
                            <span className="clone-method-tag">
                              {t('vmCreate.cloneDefault')}
                            </span>
                          ) : null}
                        </span>
                        <span className="clone-method-desc">
                          {t('vmCreate.cloneFullDesc')}
                        </span>
                      </span>
                    </button>
                  </div>
                </Field>

                <Switch
                  checked={form.cloneStart}
                  onChange={(v) => update('cloneStart', v)}
                  label={t('vmCreate.cloneStart')}
                  hint={t('vmCreate.cloneStartHint')}
                />
              </div>
            ) : (
              <div className="choice-panel">
                <Notice tone="warning" title={t('vmCreate.installFresh')}>
                  {t('vmCreate.installFreshNotice')}
                </Notice>
              </div>
            )}
          </div>
        ) : null}

        {/* ================= 第 3 步：系统配置 ================= */}
        {step === 2 ? (
          <div className="wizard-section">
            <div className="wizard-section-title">{t('vmCreate.secOsFirmware')}</div>
            <div className="form-grid">
              <Select
                label={t('vmCreate.fieldOstype')}
                value={form.ostype}
                onChange={(e) => changeOstype(e.target.value)}
                options={ostypeOptions(t).map((o) => ({
                  label: o.label,
                  value: o.value,
                }))}
                hint={t('vmCreate.fieldOstypeHint')}
              />
              <Select
                label="BIOS"
                value={form.bios}
                onChange={(e) => update('bios', e.target.value)}
                options={biosOptions(t).map((o) => ({
                  label: o.label,
                  value: o.value,
                }))}
                hint={t('vmCreate.biosWin11Hint')}
              />
              <Select
                label={t('vmDetail.hwMachine')}
                value={form.machine}
                onChange={(e) => update('machine', e.target.value)}
                options={machineOptions(t).map((o) => ({
                  label: o.label,
                  value: o.value,
                }))}
              />
              <Select
                label={t('vmDetail.hwScsihw')}
                value={form.scsihw}
                onChange={(e) => update('scsihw', e.target.value)}
                options={SCSIHW_OPTIONS.map((o) => ({
                  label: o.label,
                  value: o.value,
                }))}
              />
            </div>

            {/* 固件三件套（OVMF / EFI 变量盘 / q35）要配套，缺一个就起不来。
                后端只在「请求了 EFI 盘或 TPM」时才自动校准，所以这里要主动提醒。 */}
            {form.bios === 'ovmf' && !form.efiEnabled ? (
              <Notice tone="warning" title={t('vmCreate.ovmfNoEfiTitle')}>
                {t('vmCreate.ovmfNoEfiBody')}
              </Notice>
            ) : null}
            {form.bios === 'ovmf' && form.machine !== 'q35' ? (
              <Notice tone="warning" title={t('vmCreate.ovmfNeedQ35Title')}>
                {t('vmCreate.ovmfNeedQ35Body')}
              </Notice>
            ) : null}

            {/* --- 高级硬件：EFI 变量盘与虚拟 TPM（Windows 11 必需）--- */}
            <div className="wizard-section-title mt-8">
              {t('vmCreate.secFirmwareExt')}
            </div>
            <Notice tone="info" title={t('vmCreate.win11NoticeTitle')}>
              {t('vmCreate.win11NoticeBody')}
            </Notice>

            <div className="flex items-center gap-8 flex-wrap mt-16">
              <Button
                variant="secondary"
                size="sm"
                icon={<IconCheck size={14} />}
                onClick={applyWindows11Preset}
              >
                {t('vmCreate.applyWin11')}
              </Button>
              <span className="fs-sm text-muted">
                {t('vmCreate.applyWin11Hint')}
              </span>
            </div>

            <div className="form-grid mt-16">
              <Switch
                checked={form.efiEnabled}
                onChange={(v) => update('efiEnabled', v)}
                label={t('vmCreate.fieldEfi')}
                hint={t('vmCreate.fieldEfiHint')}
              />
              {form.efiEnabled ? (
                <>
                  <Select
                    label={t('vmCreate.fieldEfiStorage')}
                    required
                    value={form.efiStorage}
                    onChange={(e) => update('efiStorage', e.target.value)}
                    options={storageOptions}
                    error={errors.efiStorage}
                  />
                  <Select
                    label={t('vmCreate.fieldEfiFormat')}
                    value={form.efiType}
                    onChange={(e) => update('efiType', e.target.value)}
                    options={efiTypeOptions(t).map((o) => ({
                      label: o.label,
                      value: o.value,
                    }))}
                    hint={t('vmCreate.fieldEfiFormatHint')}
                  />
                  <div className="mt-8">
                    <Checkbox
                      checked={form.efiPreEnrolled}
                      onChange={(e) => update('efiPreEnrolled', e.target.checked)}
                      label={t('vmCreate.fieldEfiKeys')}
                    />
                  </div>
                </>
              ) : null}

              <Switch
                checked={form.tpmEnabled}
                onChange={(v) => update('tpmEnabled', v)}
                label={t('vmCreate.fieldTpm')}
                hint={t('vmCreate.fieldTpmHint')}
              />
              {form.tpmEnabled ? (
                <>
                  <Select
                    label={t('vmCreate.fieldTpmStorage')}
                    required
                    value={form.tpmStorage}
                    onChange={(e) => update('tpmStorage', e.target.value)}
                    options={storageOptions}
                    error={errors.tpmStorage}
                  />
                  <Select
                    label={t('vmCreate.fieldTpmVersion')}
                    value={form.tpmVersion}
                    onChange={(e) => update('tpmVersion', e.target.value)}
                    options={tpmVersionOptions(t).map((o) => ({
                      label: o.label,
                      value: o.value,
                    }))}
                  />
                </>
              ) : null}
            </div>

            <div className="wizard-section-title mt-8">{t('vmCreate.secCpuMem')}</div>
            <div className="form-grid">
              <Select
                label={t('vmDetail.hwCpuType')}
                value={form.cpuType}
                onChange={(e) => update('cpuType', e.target.value)}
                options={cpuTypeOptions(t).map((o) => ({
                  label: o.label,
                  value: o.value,
                }))}
                hint={t('vmCreate.fieldCpuTypeHint')}
              />
              <Input
                label={t('vmDetail.cfgCores')}
                type="number"
                min={1}
                max={128}
                value={form.cores}
                onChange={(e) => update('cores', Number(e.target.value) || 0)}
                error={errors.cores}
                hint={t('vmCreate.totalVcpu', { count: form.cores * form.sockets })}
              />
              <Input
                label={t('vmDetail.hwSockets')}
                type="number"
                min={1}
                max={4}
                value={form.sockets}
                onChange={(e) => update('sockets', Number(e.target.value) || 0)}
                error={errors.sockets}
              />
              <Input
                label={t('vmDetail.cfgMemory')}
                type="number"
                min={512}
                step={512}
                value={form.memory}
                onChange={(e) => update('memory', Number(e.target.value) || 0)}
                error={errors.memory}
                hint={t('vmCreate.approxSize', {
                  size: formatBytes(form.memory * 1024 ** 2),
                })}
              />
              {/* 气球：PVE 没这个键时按整份内存算，宿主机收不回空闲内存。
                  留空保持原行为，填了才有回收空间。 */}
              <Input
                label={t('vmCreate.fieldBalloon')}
                type="number"
                min={0}
                step={256}
                value={form.balloon}
                onChange={(e) => update('balloon', e.target.value)}
                error={errors.balloon}
                hint={t('vmCreate.fieldBalloonHint')}
              />
            </div>

            {/* --- 高级硬件：NUMA 绑定与 CPU 亲和性 --- */}
            <div className="wizard-section-title mt-8">{t('vmCreate.secNuma')}</div>
            <Notice tone="info" title={t('vmCreate.numaWhenTitle')}>
              {t('vmCreate.numaWhenBody')}
            </Notice>

            <div className="form-grid mt-16">
              <Switch
                checked={form.numaEnabled}
                onChange={(v) => update('numaEnabled', v)}
                label={t('vmCreate.fieldNuma')}
                hint={t('vmCreate.fieldNumaHint')}
              />
              <Input
                label={t('vmCreate.fieldAffinity')}
                value={form.numaAffinity}
                onChange={(e) => update('numaAffinity', e.target.value)}
                placeholder={t('vmDetail.affinityPlaceholder')}
                mono
                error={errors.numaAffinity}
                hint={t('vmCreate.fieldAffinityHint')}
              />
            </div>

            <div className="mt-16">
              <div className="flex items-center justify-between">
                <span className="fs-sm fw-500">{t('vmCreate.numaBind')}</span>
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<IconPlus size={14} />}
                  onClick={addNumaNode}
                >
                  {t('vmCreate.numaAddNode')}
                </Button>
              </div>

              {form.numaNodes.length === 0 ? (
                <div className="fs-sm text-muted mt-8">
                  {t('vmCreate.numaNoNodes')}
                </div>
              ) : (
                <div className="dyn-list">
                  {form.numaNodes.map((node, i) => (
                    <div className="dyn-item" key={node.key}>
                      <div className="dyn-item-fields">
                        <div className="form-grid-2">
                          <Input
                            label={t('vmCreate.numaCpus', { index: i + 1 })}
                            required
                            value={node.cpus}
                            onChange={(e) =>
                              patchNumaNode(node.key, { cpus: e.target.value })
                            }
                            placeholder={t('vmCreate.numaCpusPlaceholder')}
                            mono
                            error={errors[`numa-cpus-${i}`]}
                          />
                          <Input
                            label={t('vmDetail.cfgMemory')}
                            value={node.memory}
                            onChange={(e) =>
                              patchNumaNode(node.key, { memory: e.target.value })
                            }
                            placeholder={t('vmCreate.numaMemoryPlaceholder')}
                            error={errors[`numa-memory-${i}`]}
                          />
                          <Input
                            label={t('vmCreate.numaHostnodes')}
                            value={node.hostnodes}
                            onChange={(e) =>
                              patchNumaNode(node.key, { hostnodes: e.target.value })
                            }
                            placeholder={t('vmCreate.numaHostnodesPlaceholder')}
                            mono
                            error={errors[`numa-hostnodes-${i}`]}
                            hint={t('vmCreate.numaHostnodesHint')}
                          />
                          <Select
                            label={t('vmCreate.numaPolicy')}
                            value={node.policy}
                            onChange={(e) =>
                              patchNumaNode(node.key, { policy: e.target.value })
                            }
                            options={[
                              { label: t('vmCreate.numaPolicyNone'), value: '' },
                              ...numaPolicyOptions(t).map((o) => ({
                                label: o.label,
                                value: o.value,
                              })),
                            ]}
                            disabled={!node.hostnodes.trim()}
                            hint={
                              node.hostnodes.trim()
                                ? undefined
                                : t('vmCreate.numaPolicyNeedHost')
                            }
                          />
                        </div>
                      </div>
                      <div className="dyn-item-remove">
                        <IconButton
                          label={t('vmCreate.numaRemoveNode', { index: i + 1 })}
                          variant="danger"
                          onClick={() => removeNumaNode(node.key)}
                        >
                          <IconTrash size={15} />
                        </IconButton>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <div className="wizard-section-title mt-8">{t('vmCreate.secBoot')}</div>
            <div className="form-grid">
              <Input
                label={t('vmDetail.hwBoot')}
                value={form.bootOrder}
                onChange={(e) => update('bootOrder', e.target.value)}
                placeholder={t('vmCreate.bootPlaceholderAuto')}
                hint={t('vmCreate.bootHintFull')}
              />
            </div>
            <Switch
              checked={form.startOnBoot}
              onChange={(v) => update('startOnBoot', v)}
              label={t('vmCreate.fieldStartOnBoot')}
              hint={t('vmCreate.fieldStartOnBootHint')}
            />
          </div>
        ) : null}

        {/* ================= 第 4 步：磁盘与网络 ================= */}
        {step === 3 ? (
          <div className="wizard-section">
            {form.mode === 'clone' ? (
              <Notice tone="info" title={t('vmCreate.cloneDisksNoticeTitle')}>
                {t('vmCreate.cloneDisksNoticeBody')}
              </Notice>
            ) : (
              <>
                {/* --- 磁盘 --- */}
                <div className="wizard-section-title">
                  {t('vmCreate.secDisks')}
                  {errors.disks ? (
                    <span className="fs-xs text-danger">{errors.disks}</span>
                  ) : null}
                </div>
                <div className="dyn-list">
                  {form.disks.map((disk, i) => (
                    <div className="dyn-item" key={disk.key}>
                      <div className="dyn-item-fields">
                        <div className="form-grid-2">
                          <Select
                            label={t('vmCreate.diskStorage', { index: i + 1 })}
                            required
                            value={disk.storage}
                            onChange={(e) => patchDisk(disk.key, { storage: e.target.value })}
                            options={storageOptions}
                            error={errors[`disk-storage-${i}`]}
                          />
                          <Input
                            label={t('vmCreate.diskSize')}
                            required
                            type="number"
                            min={1}
                            value={disk.size}
                            onChange={(e) =>
                              patchDisk(disk.key, { size: Number(e.target.value) || 0 })
                            }
                            error={errors[`disk-size-${i}`]}
                          />
                          <Select
                            label={t('vmCreate.diskBus')}
                            /* Windows 客户机锁死 SATA：安装程序没有 virtio 驱动，
                               挂在 virtio 上会卡在「选择安装位置」一个分区都看不到。
                               与其让用户踩进去再解释，不如这里不让改（切换系统时
                               已由 applyWindowsCompat 自动换好，所以显示的必然是 SATA）。*/
                            disabled={windowsGuest}
                            hint={
                              windowsGuest
                                ? t('vmCreate.diskBusWinHint')
                                : undefined
                            }
                            value={disk.interface.replace(/\d+$/, '')}
                            onChange={(e) =>
                              patchDisk(disk.key, {
                                interface: `${e.target.value}${disk.interface.replace(/\D/g, '') || i}`,
                              })
                            }
                            options={DISK_INTERFACE_OPTIONS.map((o) => ({
                              label: o.label,
                              value: o.value,
                            }))}
                          />
                          <Select
                            label={t('vmDetail.addDiskFormat')}
                            value={disk.format}
                            onChange={(e) => patchDisk(disk.key, { format: e.target.value })}
                            options={DISK_FORMAT_OPTIONS.map((o) => ({
                              label: o.label,
                              value: o.value,
                            }))}
                          />
                        </div>
                      </div>
                      {form.disks.length > 1 ? (
                        <div className="dyn-item-remove">
                          <IconButton
                            label={t('vmCreate.diskRemove', { index: i + 1 })}
                            variant="danger"
                            onClick={() => removeDisk(disk.key)}
                          >
                            <IconTrash size={15} />
                          </IconButton>
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<IconPlus size={14} />}
                  onClick={addDisk}
                >
                  {t('vmCreate.diskAdd')}
                </Button>

                {/* --- 网卡 --- */}
                <div className="wizard-section-title mt-8">{t('vmCreate.secNetwork')}</div>
                <div className="dyn-list">
                  {form.networks.map((net, i) => (
                    <div className="dyn-item" key={net.key}>
                      <div className="dyn-item-fields">
                        <div className="form-grid-2">
                          <Select
                            label={t('vmCreate.nicBridge', { index: i + 1 })}
                            required
                            value={net.bridge}
                            onChange={(e) => patchNet(net.key, { bridge: e.target.value })}
                            options={bridgeOptions}
                            error={errors[`net-bridge-${i}`]}
                            hint={
                              bridges.isError
                                ? t('vmCreate.nicBridgeLoadFailed')
                                : undefined
                            }
                          />
                          <Select
                            label={t('vmDetail.addNicModel')}
                            /* Windows 同样锁死 E1000：安装介质里没有 virtio-net
                               驱动，装完系统会是没有网络适配器的状态。切换系统时
                               已由 applyWindowsCompat 换好，所以显示的必然是 E1000。*/
                            disabled={windowsGuest}
                            hint={
                              windowsGuest
                                ? t('vmCreate.nicModelWinHint')
                                : undefined
                            }
                            value={net.model}
                            onChange={(e) => patchNet(net.key, { model: e.target.value })}
                            options={netModelOptions(t).map((o) => ({
                              label: o.label,
                              value: o.value,
                            }))}
                          />
                          <Input
                            label={t('vmDetail.addNicVlan')}
                            type="number"
                            min={1}
                            max={4094}
                            value={net.vlan_tag}
                            onChange={(e) => patchNet(net.key, { vlan_tag: e.target.value })}
                            placeholder={t('vmCreate.nicVlanPlaceholder')}
                          />
                          <Input
                            label={t('vmCreate.nicMac')}
                            value={net.macaddr}
                            onChange={(e) => patchNet(net.key, { macaddr: e.target.value })}
                            placeholder={t('vmCreate.nicMacPlaceholder')}
                            mono
                          />
                          <Input
                            label={t('vmCreate.nicRate')}
                            type="number"
                            min={0}
                            value={net.rate}
                            onChange={(e) => patchNet(net.key, { rate: e.target.value })}
                            placeholder={t('vmCreate.nicRatePlaceholder')}
                            hint={t('vmCreate.nicRateHint')}
                            mono
                          />
                        </div>
                        {/* 网段 / 地址：与快速部署同一块界面（NicSegmentFields）。
                            选网段会把网桥与 VLAN 一起对齐，静态地址由池子预填，
                            DHCP 时随请求带上 pool_id，后端按该网段分配。 */}
                        <NicSegmentFields
                          value={{
                            bridge: net.bridge,
                            vlan: net.vlan_tag,
                            ipMode: net.ipMode,
                            ip: net.ip,
                            gateway: net.gateway,
                            poolId: net.poolId,
                          }}
                          onChange={(patch) => patchNetSegment(net.key, patch)}
                          node={form.node}
                          connection={targetConn}
                          pools={poolsQuery.data?.pools ?? []}
                          ready={Boolean(poolsQuery.data)}
                          poolError={
                            poolsQuery.isError
                              ? errorMessage(poolsQuery.error)
                              : undefined
                          }
                          ipError={errors[`net-ip-${i}`]}
                        />
                        <div className="mt-8">
                          <Checkbox
                            checked={net.firewall}
                            onChange={(e) => patchNet(net.key, { firewall: e.target.checked })}
                            label={t('vmDetail.addNicFirewall')}
                          />
                        </div>
                      </div>
                      {form.networks.length > 1 ? (
                        <div className="dyn-item-remove">
                          <IconButton
                            label={t('vmCreate.nicRemove', { index: i + 1 })}
                            variant="danger"
                            onClick={() => removeNet(net.key)}
                          >
                            <IconTrash size={15} />
                          </IconButton>
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<IconPlus size={14} />}
                  onClick={addNet}
                >
                  {t('vmDetail.addNic')}
                </Button>

                <Notice tone="info" title={t('vmCreate.netGuideTitle')}>
                  {t('vmCreate.netGuideBody')}
                  <br />· {t('vmCreate.netGuideDhcp')}
                  <br />· {t('vmCreate.netGuideStatic')}
                  <br />· {t('vmCreate.netGuideCi', { agent: ciName })}
                  {windowsGuest
                    ? t('vmCreate.netGuideCiWin')
                    : t('vmCreate.netGuideCiLinux')}
                </Notice>

                {/* --- ISO --- */}
                <div className="wizard-section-title mt-8">{t('vmCreate.secIso')}</div>
                <Select
                  label={t('vmCreate.fieldIso')}
                  value={form.iso}
                  onChange={(e) => update('iso', e.target.value)}
                  options={[
                    { label: t('vmCreate.isoNone'), value: '' },
                    ...(isoQuery.data ?? []).map(({ storage, item }) => ({
                      label: `${item.volid} (${formatBytes(item.size)}, ${storage})`,
                      value: item.volid,
                    })),
                  ]}
                  hint={
                    isoQuery.isLoading
                      ? t('vmCreate.isoLoading')
                      : (isoQuery.data ?? []).length === 0
                        ? t('vmCreate.isoEmpty')
                        : undefined
                  }
                />

                {diskTotal > 0 ? (
                  <Notice tone="info">
                    {t('vmCreate.diskTotal', { size: formatBytes(diskTotal) })}
                  </Notice>
                ) : null}
              </>
            )}
          </div>
        ) : null}

        {/* ================= 第 5 步：初始化（Cloud-Init / Cloudbase-Init） ================= */}
        {step === CI_STEP ? (
          <div className="wizard-section">
            <Switch
              checked={form.ciEnabled}
              onChange={(v) => update('ciEnabled', v)}
              label={t('vmCreate.ciEnable', { agent: ciName })}
              hint={
                windowsGuest ? t('vmCreate.ciWinHint') : t('vmCreate.ciHint')
              }
            />

            {!form.ciEnabled ? (
              <Notice tone="info" title={t('vmCreate.ciOffTitle', { agent: ciName })}>
                {windowsGuest ? (
                  <>{t('vmCreate.ciOffWinBody')}</>
                ) : (
                  <>{t('vmCreate.ciOffBody')}</>
                )}
              </Notice>
            ) : (
              <>
                <div className="form-grid-2">
                  <Input
                    label={t('vmCreate.ciUser')}
                    required
                    value={form.ciUser}
                    onChange={(e) => update('ciUser', e.target.value)}
                    placeholder={windowsGuest ? 'Administrator' : 'ubuntu'}
                    error={errors.ciUser}
                    hint={
                      windowsGuest ? t('vmCreate.ciUserWinHint') : undefined
                    }
                  />
                  <Input
                    label={t('vmCreate.ciPassword')}
                    type="password"
                    value={form.ciPassword}
                    onChange={(e) => update('ciPassword', e.target.value)}
                    placeholder={t('vmCreate.ciPasswordPlaceholder')}
                    autoComplete="new-password"
                  />
                </div>

                <Textarea
                  label={t('vmCreate.ciSshKeys')}
                  mono
                  rows={4}
                  value={form.ciSshKeys}
                  onChange={(e) => update('ciSshKeys', e.target.value)}
                  placeholder="ssh-ed25519 AAAAC3Nza... user@host"
                  hint={
                    windowsGuest
                      ? t('vmCreate.ciSshKeysWinHint')
                      : t('vmCreate.ciSshKeysHint')
                  }
                />

                {/* 「SSH 安全」入口被面板级开关关掉时整项不出现（见 sshNavOff）：
                    机器会被登记成受管主机，而看受管主机的页面都在那个入口下面 */}
                {sshNavOff ? null : (
                  <Switch
                    label={t('vmCreate.ciManage')}
                    checked={form.ciManage}
                    onChange={(v) => update('ciManage', v)}
                    hint={
                      /* 两种都能接入，差别只在登记时机：静态地址创建时就定了、立刻登记；
                         DHCP 则等机器通过 Guest Agent 自报地址（创建时的后台轮询 +
                         待登记队列两条路兜底）。这里只把差别讲清楚，不做任何限制。 */
                      hasStaticIpOnNics(form)
                        ? t('vmCreate.ciManageHint')
                        : t('vmCreate.ciManageDhcpHint')
                    }
                  />
                )}

                <Input
                  label={t('vmCreate.ciDns')}
                  value={form.ciDns}
                  onChange={(e) => update('ciDns', e.target.value)}
                  placeholder={t('vmCreate.ciDnsPlaceholder')}
                  hint={
                    vmDefaultsQuery.data?.dns
                      ? t('vmCreate.ciDnsHintDefault', {
                          dns: vmDefaultsQuery.data.dns,
                        })
                      : t('vmCreate.ciDnsHint')
                  }
                />

                <div className="wizard-section-title">{t('vmCreate.secNetConfig')}</div>
                {form.mode === 'new' ? (
                  <Notice tone="info">{t('vmCreate.ciNetworkNotice')}</Notice>
                ) : (
                  <>
                    <Notice tone="info" title={t('vmCreate.netGuideTitle')}>
                      {t('vmCreate.netGuideBody')}
                      <br />
                      {t('vmCreate.netGuideDhcp')}
                      <br />
                      {t('vmCreate.netGuideStatic')}
                    </Notice>
                    <div className="dyn-list">
                      {form.ciIps.map((row, i) => (
                    <div className="dyn-item" key={row.key}>
                      <div className="dyn-item-fields">
                        {/* 与快速部署同一套网段选择：克隆体的网卡沿用模板，这里
                            选的是「地址从哪个网段来」，静态直接填地址、DHCP 把
                            pool_id 交给后端分配。 */}
                        <NicSegmentFields
                          value={{
                            bridge: '',
                            vlan: '',
                            ipMode: row.ipMode,
                            ip: row.ip,
                            gateway: row.gateway,
                            poolId: row.poolId,
                          }}
                          onChange={(patch) => patchIpSegment(row.key, patch)}
                          node={form.node}
                          connection={targetConn}
                          pools={poolsQuery.data?.pools ?? []}
                          ready={Boolean(poolsQuery.data)}
                          poolError={
                            poolsQuery.isError
                              ? errorMessage(poolsQuery.error)
                              : undefined
                          }
                          ipError={errors[`ip-${i}`]}
                          /* IP 列表是「覆盖项」：用户明确要静态时不要被自动退回 DHCP */
                          autoFallbackDhcp={false}
                        />
                      </div>
                      {form.ciIps.length > 1 ? (
                        <div className="dyn-item-remove">
                          <IconButton
                            label={t('vmCreate.ciIpRemove', { index: i + 1 })}
                            variant="danger"
                            onClick={() => removeIp(row.key)}
                          >
                            <IconTrash size={15} />
                          </IconButton>
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
                    <Button
                      variant="secondary"
                      size="sm"
                      icon={<IconPlus size={14} />}
                      onClick={addIp}
                    >
                      {t('vmCreate.ciIpAdd')}
                    </Button>
                  </>
                )}

                <Notice tone="warning" title={t('vmCreate.ciPrereqTitle')}>
                  {windowsGuest ? (
                    <>{t('vmCreate.ciPrereqWin')}</>
                  ) : (
                    <>{t('vmCreate.ciPrereqLinux')}</>
                  )}
                </Notice>
              </>
            )}
          </div>
        ) : null}

        {/* ================= 第 6 步：确认 ================= */}
        {step === 5 ? (
          <div className="wizard-section">
            <Notice tone="info" title={t('vmCreate.confirmTitle')}>
              {t('vmCreate.confirmBody')}
            </Notice>

            <div className="summary-list">
              <div className="summary-group">
                <div className="summary-group-title">{t('vmCreate.stepBasic')}</div>
                <SummaryRow label={t('common.name')} value={form.name || '—'} />
                <SummaryRow label="VMID" value={form.vmid || '—'} mono />
                <SummaryRow
                  label={t('vmCreate.confirmTargetConn')}
                  value={targetConnLabel}
                  mono
                />
                <SummaryRow label={t('common.node')} value={form.node || '—'} mono />
                <SummaryRow label={t('vmCreate.fieldTags')} value={form.tags || '—'} />
                <SummaryRow
                  label={t('common.description')}
                  value={form.description || '—'}
                />
              </div>

              <div className="summary-group">
                <div className="summary-group-title">{t('vmCreate.stepSystem')}</div>
                <SummaryRow
                  label={t('vmCreate.confirmOstype')}
                  value={ostypeLabel(form.ostype, t)}
                />
                <SummaryRow label="BIOS" value={form.bios} mono />
                <SummaryRow
                  label={t('vmDetail.hwMachine')}
                  value={form.machine || t('vmDetail.machineDefault')}
                  mono
                />
                <SummaryRow label={t('vmDetail.hwScsihw')} value={form.scsihw} mono />
                <SummaryRow
                  label="CPU"
                  value={t('vmCreate.confirmCpuSummary', {
                    cpu: form.cpuType,
                    cores: form.cores,
                    sockets: form.sockets,
                  })}
                  mono
                />
                <SummaryRow
                  label={t('vmCreate.confirmMemory')}
                  value={`${form.memory} MB (${formatBytes(form.memory * 1024 ** 2)})`}
                  mono
                />
                <SummaryRow
                  label={t('vmDetail.hwBoot')}
                  value={form.bootOrder || t('vmCreate.bootAuto')}
                  mono
                />
                <SummaryRow
                  label={t('vmDetail.onboot')}
                  value={form.startOnBoot ? t('common.yes') : t('common.no')}
                />
              </div>

              <div className="summary-group">
                <div className="summary-group-title">
                  {form.mode === 'clone'
                    ? t('vmCreate.confirmCloneSource')
                    : t('vmCreate.stepDisks')}
                </div>
                {form.mode === 'clone' ? (
                  <>
                    <SummaryRow
                      label={t('templatePicker.vmLabel')}
                      value={
                        selectedCloneSource
                          ? `${selectedCloneSource.name || selectedCloneSource.vmid} @ ${selectedCloneSource.node}`
                          : '—'
                      }
                    />
                    <SummaryRow
                      label={t('vmCreate.cloneMethod')}
                      value={
                        form.cloneFull
                          ? t('vmCreate.cloneFull')
                          : t('vmCreate.cloneLinked')
                      }
                    />
                    <SummaryRow
                      label={t('vmCreate.confirmCloneStorage')}
                      value={form.cloneStorage || t('vmCreate.cloneStorageInherit')}
                      mono
                    />
                    <SummaryRow
                      label={t('vmCreate.confirmCloneStart')}
                      value={form.cloneStart ? t('common.yes') : t('common.no')}
                    />
                  </>
                ) : (
                  <>
                    {form.disks.map((d, i) => (
                      <SummaryRow
                        key={d.key}
                        label={t('vmCreate.confirmDisk', { index: i + 1 })}
                        value={`${d.storage || '?'} · ${d.size} GB · ${d.interface} · ${d.format}`}
                        mono
                      />
                    ))}
                    {form.networks.map((n, i) => (
                      <SummaryRow
                        key={n.key}
                        label={t('vmCreate.confirmNic', { index: i + 1 })}
                        value={`${n.bridge || '?'} · ${n.model}${
                          n.vlan_tag ? ` · VLAN ${n.vlan_tag}` : ''
                        }${
                          n.firewall ? t('vmCreate.confirmNicFirewall') : ''
                        }                        ${
                          n.ipMode === 'static' && n.ip.trim()
                            ? t('vmCreate.confirmNicIp', { ip: n.ip }) +
                              (n.gateway
                                ? t('vmCreate.confirmNicGateway', {
                                    gateway: n.gateway,
                                  })
                                : '')
                            : ' · DHCP'
                        }`}
                        mono
                      />
                    ))}
                    <SummaryRow
                      label="ISO"
                      value={form.iso || t('vmCreate.isoNotMounted')}
                      mono
                    />
                  </>
                )}
              </div>

              <div className="summary-group">
                {/* 名字随系统变；状态用 ciActive（实际会不会下发）而不是开关本身 ——
                    网卡填了静态 IP 时开关可能是关的，但配置确实下发了。 */}
                <div className="summary-group-title">{ciName}</div>
                {ciActive ? (
                  <>
                    <SummaryRow
                      label={t('vmCreate.confirmCiStatus')}
                      value={t('vmCreate.ciEnabled')}
                    />
                    <SummaryRow
                      label={t('vmCreate.confirmCiUser')}
                      value={form.ciUser || '—'}
                      mono
                    />
                    <SummaryRow
                      label={t('vmCreate.ciPassword')}
                      value={
                        form.ciPassword
                          ? t('vmCreate.ciPasswordSet')
                          : t('vmCreate.ciPasswordUnset')
                      }
                    />
                    <SummaryRow
                      label={t('vmCreate.ciSshKeys')}
                      value={
                        form.ciSshKeys
                          ? t('vmCreate.ciSshKeysCount', {
                              count: form.ciSshKeys
                                .split('\n')
                                .filter(Boolean).length,
                            })
                          : t('vmCreate.ciPasswordUnset')
                      }
                    />
                    <SummaryRow
                      label="DNS"
                      value={
                        form.ciDns ||
                        vmDefaultsQuery.data?.dns ||
                        t('vmCreate.ciDnsInherit')
                      }
                      mono
                    />
                    {(form.mode === 'new' ? form.networks : form.ciIps).map((r: any, i) => (
                      <SummaryRow
                        key={r.key}
                        label={`IP ${i + 1}`}
                        value={`${
                          r.ipMode === 'static' && (r.ip || '').trim()
                            ? r.ip.trim()
                            : 'dhcp'
                        }${
                          r.ipMode === 'static' && r.gateway
                            ? t('vmCreate.ciRouteGateway', { gateway: r.gateway })
                            : ''
                        }`}
                        mono
                      />
                    ))}
                  </>
                ) : (
                  <SummaryRow
                    label={t('vmCreate.confirmCiStatus')}
                    value={
                      windowsGuest && netHasStaticIp
                        ? t('vmCreate.ciDisabledWin')
                        : t('vmCreate.ciDisabled')
                    }
                  />
                )}
              </div>
            </div>
          </div>
        ) : null}
      </div>

      {/* 底部提示 */}
      <div className="mt-16 flex items-center gap-8">
        <Badge variant="neutral" size="sm">
          {t('vmCreate.footerSteps', {
            current: step + 1,
            total: STEP_KEYS.length,
          })}
        </Badge>
        <span className="fs-sm text-muted">{t('vmCreate.footerRequired')}</span>
      </div>
        </>
      )}
    </Modal>
  );
}

/* ---------------------------------------------------------------------------
   汇总行
   --------------------------------------------------------------------------- */

function SummaryRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="desc-item">
      <span className="desc-label">{label}</span>
      <span className={`desc-value ${mono ? 'mono' : ''}`}>{value}</span>
    </div>
  );
}
