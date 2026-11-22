/* ==========================================================================
   pvecenter — AI 排查助手

   把平台已有的巡检结果（安全体检 / 阈值告警 / 登录分析 / 备份核对）交给
   大模型做归因与排序，回答的问题是「先修哪一条」。

   L1 阶段刻意保持**只读**：AI 不连目标主机、不执行任何命令，读的全是平台
   已经采集好的结构化数据。所以它做的是「解读」而不是「诊断」——命名也照这个
   来，免得用户以为 AI 会自己动手把问题修掉。

   每条结论都带 source_ref（指回原始检查项）：这是对付幻觉最有效的手段 ——
   用户可以自己核对，而不是只能选择信或不信。页脚也常驻一句同样的提醒。
   ========================================================================== */

import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { aiApi, exportUrl, usersApi, type VmAiLease, type VmAiTarget } from '../api/endpoints';
import { errorMessage } from '../api/client';
import * as aiPerf from '../utils/aiPerf';
import { PageShell } from '../components/Layout';
import { RichText } from '../components/RichText';
import { Card, CardHeader } from '../components/ui/Card';
import { Badge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Field, Input, SegmentedControl, Select, Switch } from '../components/ui/Input';
import { EmptyState, ErrorState, Notice } from '../components/ui/EmptyState';
import { Table } from '../components/ui/Table';
import type { Column } from '../components/ui/Table';
import { Skeleton, Spinner } from '../components/ui/Spinner';
import { Modal } from '../components/ui/Modal';
import { RemediationDialog } from '../components/RemediationDialog';
import { ApprovalCard, type ApprovalRequest } from '../components/AiApprovalCard';
import {
  AiAuthorizationCard,
  type AiAuthorizationPatch,
} from '../components/AiAuthorizationCard';
import { leaseForTarget } from './aiAuthorization';
import { VmAiPolicyCard } from '../components/VmAiPolicyCard';
import { AiTerminal } from '../components/AiTerminal';
import { AiUsageModal } from '../components/AiUsageModal';
import {
  IconActivity,
  IconAlert,
  IconCheck,
  IconChevronDown,
  IconChevronRight,
  IconClose,
  IconCopy,
  IconDownload,
  IconInfo,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSettings,
  IconSparkle,
  IconStop,
  IconTrash,
} from '../components/Icons';
import { useAuth } from '../hooks/useAuth';
import { useToast } from '../hooks/useToast';
import { formatDateTime } from '../utils/format';
import { useT, type MessageKey, type TFunc } from '../i18n';
import type {
  AiChatMessage,
  AiChatStreamEvent,
  AiConfig,
  AiConversation,
  AiConversationDetail,
  AiFinding,
  AiPlaybook,
  AiPreset,
  AiProvider,
  AiResult,
  AiSession,
  AiSeverity,
  BadgeVariant,
} from '../api/types';

/** 正在跑的那一轮的进度摘要：给右栏气泡用，左栏过程流水仍在「执行过程」里 */
interface AiProgress {
  /** 当前是排队中（并发闸门未放行） */
  queued: boolean;
  /** 排队时前面还有几轮（不含正在跑的） */
  ahead: number;
  /** 排队时的粗估等待秒数 */
  etaS: number;
  /** 当前第几轮分析（0 = 还没进入分析） */
  step: number;
  maxSteps: number;
  /** 最近几个工具调用；ok=null 表示还在跑 */
  tools: Array<{ tool: string; ok: boolean | null }>;
  /** 体检报告已生成的字符数（正文不展示，只说明「还在长」） */
  draftChars: number;
}

/**
 * 这几个面板包一层 memo：流式期间父组件每 70ms 重渲染一次，而它们与「正在长
 * 出来的那段文字」无关 —— 终端里跑着 xterm、执行过程在滚流水、报告卡可能有
 * 几十条发现项，跟着每帧重绘纯属浪费（而且会抢主线程，让打字机看起来一顿一顿）。
 * 前提是传进去的 props 引用稳定：回调一律用 useCallback / ref 兜住。
 */
const TerminalPane = memo(AiTerminal);
const AuthorizationCard = memo(AiAuthorizationCard);
const ApprovalCardMemo = memo(ApprovalCard);

/** 排除原因码 → 可读文案。未知码按「读不到」处理，不编新词。 */
const EXCLUDE_REASON_KEY: Record<string, MessageKey> = {
  not_vm: 'ai.excludeReason.not_vm',
  stopped: 'ai.excludeReason.stopped',
  cloudinit: 'ai.excludeReason.cloudinit',
  agent: 'ai.excludeReason.agent',
  unreachable: 'ai.excludeReason.unreachable',
};

function excludeReasonKey(reason: string): MessageKey {
  return EXCLUDE_REASON_KEY[reason] ?? 'ai.excludeReason.unreachable';
}

/** 报告卡的元信息：判断「这份结论值不值信」的参照（模型、耗时、开销、工具数） */
interface ReportMeta {
  model?: string;
  ms?: number;
  tokens?: number;
  tools?: number;
  hostLabel?: string;
  /** 主机 id：报告里「处置」按钮要用它去加固，不能拿展示名顶替 */
  hostId?: string;
  at?: number;
}

/** 把落库的报告 JSON 解析回结构；坏了就当没有，别让一段脏数据崩掉整页 */
function parseReport(raw?: string | null): AiResult | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as AiResult;
    return parsed && Array.isArray(parsed.findings) ? parsed : null;
  } catch {
    return null;
  }
}

/**
 * 结论出处 → 平台里对应的功能页。
 *
 * 每条结论都带 source_ref（baseline:xxx / alerting:yyy / tool:host_disk_usage…）。
 * 之前只对 baseline 前缀给出「照做」按钮，别的出处就只剩一段文字、用户还得自己
 * 去菜单里找。这里把出处直接翻译成一个可点的入口 —— 看完建议顺手就能跳过去核实。
 */
const TOOL_ROUTES: Record<string, { to: string; labelKey: MessageKey }> = {
  host_listening_ports: { to: '/ports', labelKey: 'ai.gotoPorts' },
  host_socket_summary: { to: '/ports', labelKey: 'ai.gotoPorts' },
  host_recent_logins: { to: '/host-audit', labelKey: 'ai.gotoHostAudit' },
  host_disk_usage: { to: '/storages', labelKey: 'ai.gotoStorages' },
  host_block_devices: { to: '/storages', labelKey: 'ai.gotoStorages' },
  get_baseline_report: { to: '/security-baseline', labelKey: 'ai.gotoBaseline' },
  get_ssh_failures: { to: '/ssh-security', labelKey: 'ai.gotoSsh' },
  get_alert_history: { to: '/alerts', labelKey: 'ai.gotoAlerts' },
  get_metrics_history: { to: '/dashboard', labelKey: 'ai.gotoMetrics' },
  get_recent_changes: { to: '/tasks', labelKey: 'ai.gotoTasks' },
  get_related_resources: { to: '/nodes', labelKey: 'ai.gotoNodes' },
};

function sourceRoute(ref: string): { to: string; labelKey: MessageKey } | null {
  const [scheme, rest = ''] = ref.split(':', 2);
  switch (scheme) {
    case 'baseline':
      return { to: '/security-baseline', labelKey: 'ai.gotoBaseline' };
    case 'alert':
    case 'alerting':
      return { to: '/alerts', labelKey: 'ai.gotoAlerts' };
    case 'ssh':
    case 'login':
      return { to: '/ssh-security', labelKey: 'ai.gotoSsh' };
    case 'backup':
    case 'backups':
      return { to: '/backups', labelKey: 'ai.gotoBackups' };
    case 'metrics':
      return { to: '/dashboard', labelKey: 'ai.gotoMetrics' };
    case 'tool':
      return TOOL_ROUTES[rest] ?? null;
    default:
      return null;
  }
}

/** 严重程度 → 徽标配色。high 用 danger，别吝啬红色：这一档就是要让人停下来看 */
const SEVERITY_BADGE: Record<AiSeverity, BadgeVariant> = {
  high: 'danger',
  medium: 'warning',
  low: 'neutral',
};

function severityLabel(t: TFunc, severity: AiSeverity): string {
  return t(`ai.severity.${severity}` as MessageKey);
}

/* 控制台（终端 / 过程 + 对话）的总高由样式表里的 .ai-console 决定：跟着视口走，
   两张卡片用 flex 撑满、内部滚动区吸收剩余 —— 这样底部天然对齐，不依赖魔法
   数字。这里只负责挂上那几个类名。见 styles/layout.css 的「AI 排查助手」一节。 */

function confidenceLabel(t: TFunc, value: string): string {
  const key = `ai.confidence.${value}`;
  return ['high', 'medium', 'low'].includes(value)
    ? t(key as MessageKey)
    : value;
}

/** 配置面板里正在编辑的一条模型；api_key 为空表示「不改动已存的密钥」 */
interface ProviderDraft {
  id: string;
  name: string;
  kind: 'external' | 'internal';
  base_url: string;
  model: string;
  api_key: string;
  api_key_set: boolean;
  enabled: boolean;
  /** 平台共享项对普通用户只读 */
  readonly?: boolean;
  /** 授权给哪些人（所有者决定；被授权者能用，但改不了） */
  granted?: string[];
  /** 当前挂着的厂商预设名，用来展示该厂商的常用模型候选 */
  presetName?: string;
}

function toDraft(item: AiProvider): ProviderDraft {
  return {
    id: item.id,
    name: item.name,
    kind: item.kind,
    base_url: item.base_url,
    model: item.model,
    api_key: '',
    api_key_set: item.api_key_set,
    enabled: item.enabled,
    readonly: item.readonly ?? false,
    granted: item.granted ?? [],
    presetName: '',
  };
}

function emptyDraft(): ProviderDraft {
  return {
    id: '',
    name: '',
    kind: 'external',
    base_url: '',
    model: '',
    api_key: '',
    api_key_set: false,
    enabled: true,
    presetName: '',
  };
}

/* 控制台左右分栏：默认对半，可拖到 32%–74%（留出两边都还能读的最小宽度）。
   对半分是因为两侧都是主要阅读区：终端看命令输出，对话看结论与审批。 */
const SPLIT_STORE_KEY = 'ai.console.split';
const SPLIT_DEFAULT = 50;
const SPLIT_MIN = 32;
const SPLIT_MAX = 74;

/** 预案的收藏 / 最近使用：只存本机，属于「顺手」偏好，不同步到服务端 */
const PLAYBOOK_FAV_KEY = 'ai.playbook.favorites';
const PLAYBOOK_RECENT_KEY = 'ai.playbook.recent';
const PLAYBOOK_RECENT_MAX = 5;

/** 本机记住的模型与上次目标（目标只做回显，仍要用户点一下确认） */
const DEFAULT_MODEL_KEY = 'ai.default.model';
const LAST_HOST_KEY = 'ai.default.host';

function readStoredString(key: string): string {
  try {
    return window.localStorage.getItem(key) || '';
  } catch {
    return '';
  }
}

function writeStoredString(key: string, value: string) {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    /* 隐私模式 / 禁用存储：本次仍生效，只是不持久化 */
  }
}

function readStoredList(key: string): string[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(key) || '[]') as unknown;
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string')
      : [];
  } catch {
    return [];
  }
}

function writeStoredList(key: string, list: string[]) {
  try {
    window.localStorage.setItem(key, JSON.stringify(list));
  } catch {
    /* 隐私模式 / 禁用存储：本次仍生效，只是不持久化 */
  }
}

export function AiAssistant() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { isAdmin, hasPermission } = useAuth();

  const [tab, setTab] = useState<'inspect' | 'records'>('inspect');
  /**
   * 左栏放什么：默认是**终端**（那台被纳管服务器的实况窗口），需要看结构化的
   * 过程流水时切到「执行过程」。默认给终端是因为用户要回答的第一个问题通常是
   * 「这台机器现在什么情况」，而不是「AI 调了几次工具」。
   */
  const [leftTab, setLeftTab] = useState<'terminal' | 'activity'>('terminal');
  /**
   * 左右两栏的宽度占比（左栏百分比），可拖拽 / 键盘调整，双击复位。
   *
   * 只存浏览器本地：这是「看着舒服」的个人偏好，不属于会话状态，换设备没理由
   * 跟着跑。默认 56% —— 终端是看日志的，稍微宽一点更实用。
   */
  const [consoleSplit, setConsoleSplit] = useState<number>(() => {
    try {
      const saved = Number(window.localStorage.getItem(SPLIT_STORE_KEY));
      if (saved >= SPLIT_MIN && saved <= SPLIT_MAX) return saved;
    } catch {
      /* 隐私模式 / 禁用存储时会抛错，退回默认值即可 */
    }
    return SPLIT_DEFAULT;
  });
  const [resizingConsole, setResizingConsole] = useState(false);
  const consoleRef = useRef<HTMLDivElement | null>(null);
  /* 拖拽过程中的最新占比：只改 DOM 变量，松手才提交到 state ——
     这一页挂着对话流与终端，每帧 setState 会让整棵子树重渲染。 */
  const dragSplitRef = useRef(SPLIT_DEFAULT);
  const [hostId, setHostId] = useState('');
  /** 上次用过的模型：本机记住，下次打开直接沿用；失效时回落到平台默认 */
  const [modelId, setModelId] = useState(() => readStoredString(DEFAULT_MODEL_KEY));
  /**
   * 上次排查过的主机。**只做回显 + 一键采用**，绝不自动选中 ——
   * AI 会登录这台机器跑命令，默认选中等于替用户决定把哪台交出去。
   */
  const [lastHostId, setLastHostId] = useState(() => readStoredString(LAST_HOST_KEY));
  /**
   * 是否授权 AI 在目标主机执行只读命令。
   *
   * 默认关闭，且**只有两种**情况会被打开：用户手动勾选，或打开一条明确的
   * 历史会话时恢复它上次的选择。新建会话 / 切换主机一律回到关闭 —— 换了机器
   * 或开了新对话，就是一次全新的授权，不沿用旧的值。
   */
  const [allowExec, setAllowExec] = useState(false);
  /**
   * 主机命令级别：read 只读 / write 允许提议写命令。
   *
   * 与 allowExec 分开：前者是「开没开」，这个是「开到哪一档」。只读级别下不
   * 下发写提议工具，避免「页面写着只读、模型却能提议改系统」。
   */
  const [execAccess, setExecAccess] = useState<'read' | 'write'>('read');
  /**
   * 刚刚（本次操作里）才把「上机执行」打开。
   *
   * 只在这种时候给「继续执行」入口：一直开着开关的人每轮结束都看到它，
   * 就成了一条常驻噪音。开关关掉或已经续跑一次就复位。
   */
  const [execJustGranted, setExecJustGranted] = useState(false);
  /**
   * 虚拟机内取证授权（一张限时票据）。
   *
   * 与 ``allowExec`` 是两件不同的事：那个是「在这台**受管主机**上跑命令」，
   * 这个是「进这台**虚拟机**里看日志、改配置」。分开是因为它们的风险与撤销量级
   * 不同 —— 混成一个开关，用户没法只给其中一种。
   */
  const [vmLease, setVmLease] = useState<VmAiLease | null>(null);
  /**
   * 已经和服务端对齐过租约的主机 id。
   * 带参进入（预案 / 问题）时，要等这一步完成再自动跑 —— 否则第一轮会
   * 在「运维授权明明还在」的情况下仍去要一次授权。
   */
  const [leaseReadyHost, setLeaseReadyHost] = useState('');
  /** 待用户批准的写命令：AI 提议后本轮挂起，等对话流里的审批卡拍板 */
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [approvalExpired, setApprovalExpired] = useState(false);
  /** 本轮是体检还是自由对话：决定「正在生成报告」的骨架要不要出现 */
  const [runMode, setRunMode] = useState<'chat' | 'inspect' | ''>('');
  /** 正在跑那一轮的过程事件（左栏）。历史轮次从会话消息里重建 */
  const [events, setEvents] = useState<AiChatStreamEvent[]>([]);
  const [errorText, setErrorText] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(false);
  const [usageOpen, setUsageOpen] = useState(false);
  /** 终止本轮用。断开连接后后端也会停下（request.is_disconnected），不白烧 token */
  const abortRef = useRef<AbortController | null>(null);

  // ---- 会话：把「一次性排查」升级成可追问的线程 ----
  const [convId, setConvId] = useState('');
  const [conversations, setConversations] = useState<AiConversation[]>([]);
  /** 当前会话的全部消息（服务端权威）。context / tool 行不直接渲染 */
  const [transcript, setTranscript] = useState<AiChatMessage[]>([]);
  /**
   * transcript 的实时镜像。收尾刷新要算「屏上最新一条的 seq」，而它发生在异步
   * 流程里 —— 闭包里那份 transcript 还是点发送时的快照，会把这一轮的消息重复拉
   * 回来（甚至覆盖乐观气泡的替换逻辑）。
   */
  const transcriptRef = useRef<AiChatMessage[]>([]);
  useEffect(() => {
    transcriptRef.current = transcript;
  }, [transcript]);
  /** 首轮体检产出的报告；「仅解读」也会写这里 */
  const [report, setReport] = useState<AiResult | null>(null);
  const [reportMeta, setReportMeta] = useState<ReportMeta | null>(null);
  /** 重新体检时把上一份报告的 findings 留作差异基线 */
  const [prevFindings, setPrevFindings] = useState<AiFinding[] | null>(null);
  const [draft, setDraft] = useState('');
  /** 正在流式到达的回复文本（打字机）。收尾时被权威的 assistant 消息替换 */
  const [streaming, setStreaming] = useState('');
  /** 差异基线要从 ref 同步取 —— setState 是异步的，闭包里的旧值不准 */
  const reportRef = useRef<AiResult | null>(null);
  useEffect(() => {
    reportRef.current = report;
  }, [report]);
  /** 报告卡锚点：体检结束后把用户带过去，免得结论在屏幕外而以为没出结果 */
  const reportAnchorRef = useRef<HTMLDivElement | null>(null);
  /** 只有「本次主动发起体检」才自动滚到报告；翻历史会话不抢滚动位置 */
  const scrollToReportRef = useRef(false);
  useEffect(() => {
    if (!report || !scrollToReportRef.current) return;
    scrollToReportRef.current = false;
    reportAnchorRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, [report]);
  /** 从记录页跳进来时置位：切主机后不要再去抢「最近一条会话」，保留用户点开的那条 */
  const skipAutoSelectRef = useRef(false);
  /** 地址栏参数（`?host=&question=&preset=`）：从别的页面带题跳进来时用 */
  const [searchParams, setSearchParams] = useSearchParams();
  /** 带参进入只消费一次 —— 刷新/回退不该把同一个问题再问一遍 */
  const entryConsumedRef = useRef(false);
  /** 带预案跳进来时，等主机选中之后再跑（setHostId 是异步的） */
  const [pendingPreset, setPendingPreset] = useState('');
  /**
   * 带题进来、却找不到它对应的受管主机（来源页那个对象没登记过）。
   *
   * 这种时候**绝不能**沿用「默认选第一台」：问题问的是 A，AI 却去查 B ——
   * 它答得越自信越危险，用户会照着对一台不相干的机器动手。宁可多问一句。
   */
  const [needsHost, setNeedsHost] = useState(false);
  /** 流式文本的写入缓冲：token 很密，逐个 setState 会触发几百次渲染 */
  const streamBufRef = useRef('');
  const streamTimerRef = useRef<number | null>(null);

  /** 把流式增量写进缓冲，按 ~70ms 的节奏刷到界面 —— 打字机不必逐字渲染。 */
  function pushToken(text: string) {
    // 首字只记第一次：后面每片增量都进这里，重复打点会把 ttfb 改写成末字时间
    aiPerf.firstToken();
    streamBufRef.current += text;
    if (streamTimerRef.current != null) return;
    streamTimerRef.current = window.setTimeout(() => {
      streamTimerRef.current = null;
      setStreaming(streamBufRef.current);
    }, 70);
  }

  /** 清空流式缓冲并取消未落地的刷新（reset 与收尾都走这里）。 */
  function resetStream() {
    streamBufRef.current = '';
    if (streamTimerRef.current != null) {
      window.clearTimeout(streamTimerRef.current);
      streamTimerRef.current = null;
    }
    setStreaming('');
  }

  // 组件卸载时别留着定时器
  useEffect(
    () => () => {
      if (streamTimerRef.current != null) window.clearTimeout(streamTimerRef.current);
    },
    [],
  );

  // 「列表先出、状态后补」：后端先回已有结论的主机，剩下几台在后台探测；
  // 只要还有 pending 就再问一次。间隔**从 0.5 秒开始退避到 15 秒**：
  //   · 后端现在是一轮往返就能补齐一批（状态是整批取的），半秒后再问一次
  //     正好接上 —— 首屏从「等两秒」变成「半秒补齐」，这是用户能感觉到的差别；
  //   · 但一台装不上 agent 的机器会一直 pending，固定半秒就是一份永远不停的
  //     请求流（几十个标签页开着时更明显）。退避之后，长时间挂着的只剩低频
  //     轮询，用户看到的仍是「N 台检测中」。
  // 页面在后台时停掉：看不见的列表没有「实时」价值，回来时 react-query 会
  // 因为 stale 立刻补一次。
  const targetsQuery = useQuery({
    queryKey: ['ai', 'targets'],
    queryFn: aiApi.targets,
    refetchInterval: (query) => {
      if (!query.state.data?.pending_total) return false;
      if (typeof document !== 'undefined' && document.hidden) return false;
      const rounds = query.state.dataUpdateCount ?? 0;
      return Math.min(500 * 2 ** Math.min(Math.max(rounds - 1, 0), 5), 15000);
    },
  });
  /** 虚拟机授权候选：与「受管主机」是两套清单，前者是 SSH 目标，后者是 PVE 里的机器 */
  const vmTargetsQuery = useQuery({ queryKey: ['ai', 'vm', 'targets'], queryFn: aiApi.vmTargets });
  const status = targetsQuery.data?.status;
  // 模型清单对所有用户可见（不含密钥），用来渲染顶部的「已接入」状态条与模型选择
  const modelsQuery = useQuery({ queryKey: ['ai', 'models'], queryFn: aiApi.models });

  // 「已接入几个模型」不再单独占一条状态条：下拉里已经列全了，那是同一份信息的
  // 第二种说法。标注来历的活也搬到选项文字里，省掉一层视觉噪音。
  const modelOptions = useMemo(
    () =>
      (modelsQuery.data?.models ?? []).map((item) => ({
        label:
          (item.model ? `${item.name} · ${item.model}` : item.name) +
          (item.granted ? ` · ${t('ai.grantBadge')}` : ''),
        value: item.id,
      })),
    [modelsQuery.data, t],
  );
  /** 默认跟随管理员设的「当前使用」；用户临时改选只影响这一次排查 */
  const activeModelId = modelsQuery.data?.active_id || '';
  // 记住的模型若已下线 / 被收回，回落到平台默认，避免下拉框显示空白
  const currentModelId =
    modelId && modelOptions.some((item) => item.value === modelId)
      ? modelId
      : activeModelId;

  /** 受管主机的原始记录：选择器要用它的 ``node`` / ``vmid`` 把主机与虚拟机对上 */
  const hostCards = useMemo(() => targetsQuery.data?.hosts ?? [], [targetsQuery.data]);
  /** 还在后台探测的候选台数（>0 时列表不完整，页面据显示「检测中」并继续轮询） */
  const pendingTotal = targetsQuery.data?.pending_total ?? 0;
  /** 本轮探活失败的 PVE 条数：整条连接不可达，与「某台机器不可用」是两回事 */
  const pveDown = targetsQuery.data?.scan?.pve_down ?? 0;

  const hostOptions = useMemo(
    () => hostCards.map((host) => ({ label: host.name || host.id, value: host.id })),
    [hostCards],
  );

  /**
   * 被准入条件排除的主机：只展示原因计数，不展示名字。
   *
   * 页面必须解释「为什么这里比受管主机少」——不解释的话，用户只会以为
   * 助手漏了一台机器，然后去别处找开关。计数用于一句话概括，明细（后端已按
   * 用户可见范围过滤）用于点名到具体机器、给出原因和修复入口。
   */
  const excludeInfo = targetsQuery.data?.excluded;
  const excludedItems = excludeInfo?.items ?? [];
  const excludedSummary = useMemo(() => {
    const info = targetsQuery.data?.excluded;
    if (!info?.total) return '';
    const parts = [
      info.not_vm ? t('ai.excludeNotVm', { n: info.not_vm }) : '',
      info.stopped ? t('ai.excludeStopped', { n: info.stopped }) : '',
      info.cloudinit ? t('ai.excludeCloudinit', { n: info.cloudinit }) : '',
      info.agent ? t('ai.excludeAgent', { n: info.agent }) : '',
      info.unreachable ? t('ai.excludeUnreachable', { n: info.unreachable }) : '',
    ].filter(Boolean);
    return parts.join(t('ai.excludeSeparator'));
  }, [targetsQuery.data, t]);

  /**
   * 虚拟机授权目标只留**运行中**的：停着的机器连不进去，列出来只会让人签完
   * 票才发现进不去，而票本身是有时限的。
   */
  const vmTargets = useMemo(
    () => (vmTargetsQuery.data?.items ?? []).filter((item) => item.status === 'running'),
    [vmTargetsQuery.data],
  );

  /**
   * 受管主机里的那台虚拟机（按「节点|vmid」对上）。
   *
   * 目标机器**只列受管主机**：没被纳管的虚拟机既跑不了主机命令，也不在
   * 权限模型里，出现在候选里只会让人以为它也是可操作的对象。
   */
  const vmByHostId = useMemo(() => {
    const byKey = new Map<string, VmAiTarget>();
    for (const vm of vmTargets) byKey.set(`${vm.node}|${vm.vmid}`, vm);
    const map = new Map<string, VmAiTarget>();
    for (const host of hostCards) {
      if (!host.node || host.vmid == null) continue;
      const vm = byKey.get(`${host.node}|${host.vmid}`);
      if (vm) map.set(host.id, vm);
    }
    return map;
  }, [hostCards, vmTargets]);

  /**
   * 目标机器的候选 = 受管主机清单，一台一行。
   *
   * 之前按「虚拟机 + 主机」两份清单拼，结果没纳管的虚拟机会混进来 —— 那些机器
   * 没有主机凭据，选它就只能进机取证，等于绕开了「AI 只对受管机器动手」这条边界。
   * 现在一律以受管主机为准，选中后自动带出它对应的虚拟机（有才带）。
   */
  const targetOptions = useMemo(
    () => [
      // 占位项必须在：没有匹配项时原生 select 会显示第一台，等于骗用户已经选好了
      { label: t('ai.selectTargetPlaceholder'), value: '' },
      ...hostCards.map((host) => ({
        label: `${host.name || host.id} · ${
          vmByHostId.has(host.id) ? t('ai.targetBoth') : t('ai.targetHostOnly')
        }`,
        value: host.id,
      })),
    ],
    [hostCards, vmByHostId, t],
  );

  const selectedHost = useMemo(
    () => hostCards.find((item) => item.id === hostId) ?? null,
    [hostCards, hostId],
  );
  /** 选中主机对应的虚拟机；没有就为空 —— 授权卡据此提示「这台不能进机取证」 */
  const vmTarget: VmAiTarget | null = hostId ? vmByHostId.get(hostId) ?? null : null;

  /**
   * 目标主机**只由用户指定**，没有默认项。
   *
   * AI 是要登录到这台机器上读日志、跑命令的，「默认选中第一台」等于替用户
   * 决定把哪台机器交出去 —— 而它答得越自信，就越可能被人照着对一台不相干的
   * 机器动手。宁可多一次点击。
   */

  // 对话流里要把「排查哪台、用哪个模型」写成一句人话，所以把内部 id 翻成标签
  const selectedHostLabel =
    hostOptions.find((item) => item.value === hostId)?.label || hostId || '-';
  const selectedModelLabel =
    modelOptions.find((item) => item.value === currentModelId)?.label || currentModelId || '-';

  /** 上次用过的主机（仅当它仍在候选里才回显；已被移出受管主机就静默丢弃） */
  const lastHostHost = useMemo(
    () => hostCards.find((host) => host.id === lastHostId) ?? null,
    [hostCards, lastHostId],
  );

  // 记住当前使用的模型：下次打开直接沿用，减少每次重选
  useEffect(() => {
    if (currentModelId) writeStoredString(DEFAULT_MODEL_KEY, currentModelId);
  }, [currentModelId]);

  /**
   * 排障预案（可插拔）。
   *
   * 清单来自后端 `aiplaybooks.py` —— 加一套新套路只需要改那个文件，这个页面
   * 不用动。预案给的是**起点**：不想用模板的人照样可以直接打字。
   */
  const playbooksQuery = useQuery({
    queryKey: ['ai', 'playbooks'],
    queryFn: aiApi.playbooks,
    staleTime: 10 * 60_000,
  });
  const playbooks: AiPlaybook[] = playbooksQuery.data?.items ?? [];
  /** 收藏的预案 key（本机偏好） */
  const [favPlaybooks, setFavPlaybooks] = useState<string[]>(() =>
    readStoredList(PLAYBOOK_FAV_KEY),
  );
  /** 最近跑过的预案 key（本机偏好，最多 5 个） */
  const [recentPlaybooks, setRecentPlaybooks] = useState<string[]>(() =>
    readStoredList(PLAYBOOK_RECENT_KEY),
  );

  /**
   * 展示顺序：收藏 → 最近用过 → 其余。
   * 不改变后端返回的清单，只影响「先看到哪几个」—— 高频预案不必每次翻找。
   */
  const orderedPlaybooks = useMemo(() => {
    const rank = (key: string) => {
      const fav = favPlaybooks.indexOf(key);
      if (fav >= 0) return fav;
      const recent = recentPlaybooks.indexOf(key);
      if (recent >= 0) return 100 + recent;
      return 1000;
    };
    return [...playbooks].sort((a, b) => rank(a.key) - rank(b.key));
  }, [playbooks, favPlaybooks, recentPlaybooks]);

  function toggleFavoritePlaybook(key: string) {
    setFavPlaybooks((list) => {
      const next = list.includes(key)
        ? list.filter((item) => item !== key)
        : [key, ...list];
      writeStoredList(PLAYBOOK_FAV_KEY, next);
      return next;
    });
  }

  // 左栏在「没有实时事件」时展示本次会话已执行过的操作（回看用）。
  // command 一并带上：回看历史时用户最想核对的就是「AI 当时在他机器上敲了什么」，
  // 只列工具名（host_read_log）等于没说。
  const toolHistory = useMemo(
    () =>
      transcript
        .filter((item) => item.role === 'tool')
        .map((item) => ({
          tool: item.tool_name || 'tool',
          ok: Boolean(item.ok),
          command: item.command || '',
        })),
    [transcript],
  );

  /**
   * 右栏气泡用的进度摘要。
   *
   * 左栏默认是终端，过程事件（stage / thinking / tool_*）在「执行过程」页签里，
   * 用户盯着右栏等报告时看不到。这里把同一批事件压成一句「现在在做什么」+
   * 最近几个工具调用，镜像到右栏，消除「转圈 = 卡住」的错觉。
   */
  const progress = useMemo<AiProgress | null>(() => {
    if (!busy) return null;
    let queued = false;
    let ahead = 0;
    let etaS = 0;
    let step = 0;
    let maxSteps = 0;
    let draftChars = 0;
    const tools: Array<{ tool: string; ok: boolean | null }> = [];
    for (const event of events) {
      if (event.type === 'stage') {
        if (event.stage === 'queued') {
          queued = true;
          ahead = event.ahead ?? 0;
          etaS = event.eta_s ?? 0;
        }
      } else if (event.type === 'thinking') {
        step = event.step;
        maxSteps = event.max_steps;
      } else if (event.type === 'report_progress') {
        draftChars = event.chars;
      } else if (event.type === 'tool_start') {
        tools.push({ tool: event.tool, ok: null });
      } else if (event.type === 'tool_done') {
        // 回填最近一个同名且仍在跑的工具：同一工具可能被调用多次
        for (let i = tools.length - 1; i >= 0; i -= 1) {
          if (tools[i].tool === event.tool && tools[i].ok === null) {
            tools[i].ok = event.ok;
            break;
          }
        }
      }
    }
    return {
      queued,
      ahead,
      etaS,
      step,
      maxSteps,
      tools: tools.slice(-4),
      draftChars,
    };
  }, [events, busy]);

  /** 把会话详情的各字段摊进状态（对话、报告、元信息）。 */
  function applyDetail(detail: AiConversationDetail, restoreExec = true) {
    setConvId(detail.conversation.id);
    setTranscript(detail.messages);
    setReport(parseReport(detail.conversation.report));
    // 恢复这条会话上次的授权选择，别让刷新悄悄把「上机执行」关掉。
    // 但「切主机后自动接续最近一条会话」不算用户主动打开它 —— 那种情况传
    // restoreExec=false：刚选完主机就是一次全新的授权，不该沿用旧会话的开关。
    const restoredExec = restoreExec ? Boolean(detail.conversation.allow_exec) : false;
    setAllowExec(restoredExec);
    // 老会话没有 exec_access：按合并前的语义当作 write，避免恢复后突然降级。
    setExecAccess(
      restoredExec
        ? detail.conversation.exec_access === 'read'
          ? 'read'
          : 'write'
        : 'read',
    );
    const session = detail.session;
    setReportMeta(
      session
        ? {
            model: session.model,
            ms: session.duration_ms,
            tokens: session.total_tokens,
            tools: session.tool_calls,
            hostLabel: detail.conversation.host_name,
            hostId: detail.conversation.host_id,
            at: session.created,
          }
        : null,
    );
  }

  async function loadConversation(
    id: string,
    switchHost = false,
    restoreExec = true,
  ) {
    try {
      const detail = await aiApi.conversation(id);
      // 从记录页跳进来时，会话可能属于另一台主机：先把选择器同步过去，
      // 并标记「别再自动抢最近一条」，否则刚打开就被覆盖掉。
      if (
        switchHost &&
        detail.conversation.host_id &&
        detail.conversation.host_id !== hostId
      ) {
        skipAutoSelectRef.current = true;
        setHostId(detail.conversation.host_id);
      }
      applyDetail(detail, restoreExec);
      setEvents([]);
      setErrorText('');
      setPrevFindings(null);
    } catch (err) {
      toast.error(t('ai.failed'), errorMessage(err));
    }
  }

  /** 记录页点「在对话中打开」：切回排查页并载入那条会话。 */
  function openConversationFromRecords(id: string) {
    setTab('inspect');
    void loadConversation(id, true);
  }

  async function fetchConversations(targetHost: string): Promise<AiConversation[]> {
    try {
      const data = await aiApi.conversations({ host_id: targetHost, limit: 20 });
      setConversations(data.items);
      return data.items;
    } catch {
      return [];
    }
  }

  // 切主机：拉这台机器的会话；当前会话若不属于它，就自动接着最近的一次对话 ——
  // 这也是「刷新后恢复」的落地方式，默认回到最近一条，不用手动找。
  useEffect(() => {
    if (!hostId) return;
    let cancelled = false;
    void (async () => {
      const items = await fetchConversations(hostId);
      if (cancelled) return;
      if (skipAutoSelectRef.current) {
        // 从记录页跳进来的：刚载入的那条会话要保持打开，不去抢最近一条
        skipAutoSelectRef.current = false;
        return;
      }
      if (convId && items.some((item) => item.id === convId)) return;
      setConvId('');
      setTranscript([]);
      setReport(null);
      setReportMeta(null);
      setPrevFindings(null);
      setEvents([]);
      setErrorText('');
      // 换了一台主机就是一次全新的授权：默认关闭，不沿用上一台留下的开关。
      setAllowExec(false);
      setExecAccess('read');
      // 自动接续最近一条会话只为「接着聊」，**不**恢复它的上机执行授权 ——
      // 否则用户刚选完主机，开关就自己亮起来，看起来像默认授权了。
      if (items.length > 0) void loadConversation(items[0].id, false, false);
    })();
    return () => {
      cancelled = true;
    };
    // 只在切主机时重跑；convId 只用于「当前会话是否属于这台机器」的判断
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hostId]);

  /**
   * 切主机 / 首次进入时，把服务端仍生效的运维票对回当前目标。
   *
   * 页面刷新后父组件手里没有租约，若不主动对齐，第一轮会显示「主机命令未授权」，
   * 用户就得再点一次授权 —— 这正是要消除的重复授权。
   */
  useEffect(() => {
    if (!hostId) return;
    let cancelled = false;
    void aiApi
      .vmLeases()
      .then((data) => {
        if (cancelled) return;
        const now = Date.now() / 1000;
        const active = data.items.filter(
          (item) => item.status === 'active' && Number(item.expires_at || 0) > now,
        );
        const found = leaseForTarget(null, active, vmTarget);
        if (found) setVmLease((prev) => (prev?.id === found.id ? prev : found));
      })
      .catch(() => {
        /* 拉不到就保持现状：卡片自己的轮询还会再试 */
      })
      .finally(() => {
        if (!cancelled) setLeaseReadyHost(hostId);
      });
    return () => {
      cancelled = true;
    };
  }, [hostId, vmTarget]);

  /**
   * 从别处带参跳进来（VM / 节点 / 告警 / 基线页的「问 AI」）：
   * `?host=` 选主机、`?question=` 预填问题、`?preset=` 直接跑某个预案。
   *
   * 消费完立刻把参数从地址栏抹掉（replace）—— 否则用户刷新页面，同一个问题会被
   * 再问一遍，而他还以为自己只是刷新了一下。
   */
  useEffect(() => {
    if (entryConsumedRef.current) return;
    const host = searchParams.get('host') ?? '';
    const question = searchParams.get('question') ?? '';
    const preset = searchParams.get('preset') ?? '';
    if (!host && !question && !preset) return;
    // 等主机清单到手再落 hostId，否则会被「默认选第一台」的回退盖掉
    if (targetsQuery.isLoading) return;
    entryConsumedRef.current = true;
    setSearchParams({}, { replace: true });

    if (host) {
      // 来源页给的主机**不一定**是助手的合法目标：最典型的是「面板本机」
      // （id=local）—— 它不在助手的候选清单里（见后端 ai.ai_targets）。
      // 这种情况不能硬把 id 塞进选择框：那个 id 连不出去，用户只会收到
      // 一句「找不到这台主机」，而且选择框还会空着。与「找不到受管主机」
      // 走同一条路 —— 让用户自己确认要查哪台，别替他猜。
      if (hostOptions.some((item) => item.value === host)) {
        if (host !== hostId) {
          // 带着新问题进来：别让「自动接上最近一条会话」把它抢走
          skipAutoSelectRef.current = true;
          setHostId(host);
          setLastHostId(host);
          writeStoredString(LAST_HOST_KEY, host);
        }
        newConversation();
      } else {
        setNeedsHost(true);
        setHostId('');
        newConversation();
      }
    } else if (question || preset) {
      // 来源页那个对象没找到对应的受管主机。清空选择交给用户确认 ——
      // 沿用默认的第一台会让 AI 去查另一台机器，而问题问的根本不是它。
      setNeedsHost(true);
      setHostId('');
      newConversation();
    }
    if (question) setDraft(question);
    if (preset) setPendingPreset(preset);
    // 只在首次带参进入时跑一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, targetsQuery.isLoading]);

  // 预案要等主机就绪才能跑（hostId 可能正是上面刚设的，setState 还没生效）
  useEffect(() => {
    // 等租约对齐后再自动跑：否则第一轮会漏掉仍在生效的运维授权
    if (!pendingPreset || !hostId || leaseReadyHost !== hostId) return;
    const key = pendingPreset;
    setPendingPreset('');
    runPlaybook(key);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingPreset, hostId, leaseReadyHost]);

  /**
   * 一轮的公共骨架：清过程事件 → 跑 → 统一收尾。
   *
   * ``base`` 是本次开跑前的旧报告，成功后作为「重新体检」的差异基线。
   */
  async function execute(
    work: (controller: AbortController, base: AiResult | null) => Promise<void>,
  ) {
    const base = reportRef.current;
    setBusy(true);
    setEvents([]);
    resetStream();
    setErrorText('');
    setApprovalExpired(false);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await work(controller, base);
    } catch (err) {
      // 没跑出报告：清掉「结束后自动滚到报告」的意图，避免下次翻历史时误滚
      scrollToReportRef.current = false;
      // 自己按的终止不是故障 —— 别报成失败吓人
      if (controller.signal.aborted) {
        toast.info(t('ai.abortedTitle'), t('ai.abortedHint'));
      } else {
        const message = errorMessage(err);
        setErrorText(message);
        toast.error(t('ai.failed'), message);
      }
    } finally {
      abortRef.current = null;
      setBusy(false);
      // 抛异常时也要收掉流式缓冲，否则会留一段半截的「打字中」气泡
      resetStream();
    }
  }

  /**
   * 一轮结束后的**增量**合并：只追加服务端新落库的消息。
   *
   * 原来是 `applyDetail` 整份替换（GET 整个会话 + 全量重解析）。会话越长，
   * 收尾那一下越重 —— 用户感受是「答案已经出来了，界面又卡一下」。
   */
  function mergeDetail(detail: AiConversationDetail) {
    setTranscript((prev) => {
      const known = new Set(prev.map((item) => item.id));
      // 乐观气泡（local-*）此时已被权威消息落库取代，先摘掉再追加
      const kept = prev.filter((item) => !item.id.startsWith('local-'));
      const extra = detail.messages.filter((item) => !known.has(item.id));
      if (extra.length === 0 && kept.length === prev.length) return prev;
      return [...kept, ...extra];
    });
    // 报告是整份的（很小），按服务端对齐；会话级授权等状态不动 —— 用户这轮
    // 的开关选择不该被一次刷新改回去。
    setReport(parseReport(detail.conversation.report));
    const session = detail.session;
    if (session) {
      setReportMeta({
        model: session.model,
        ms: session.duration_ms,
        tokens: session.total_tokens,
        tools: session.tool_calls,
        hostLabel: detail.conversation.host_name,
        hostId: detail.conversation.host_id,
        at: session.created,
      });
    }
  }

  /** 一轮结束后把服务端的最新状态拉回来（屏上的乐观消息被权威消息替换）。 */
  async function refreshAfterTurn(conversationId: string) {
    // 只取「比屏上最新那条更新」的消息
    const lastSeq = transcriptRef.current.reduce(
      (max, item) =>
        item.id.startsWith('local-') ? max : Math.max(max, Number(item.seq) || 0),
      0,
    );
    try {
      const detail = await aiApi.conversation(conversationId, lastSeq);
      mergeDetail(detail);
    } catch {
      /* 拉不到就保留屏上的乐观消息 */
    }
    void fetchConversations(hostId);
    // 这一轮已经落库，用量统计跟着变 —— 不刷新的话卡片还停在上一轮的数字上
    void qc.invalidateQueries({ queryKey: ['ai', 'usage'] });
  }

  /**
   * 在一条会话里跑一轮。
   *
   * ``mode`` 决定形态：``chat`` 是自由对话（默认），``inspect`` 是体检模板
   * （采集证据 + 产出报告，text 可留空）。
   */
  async function streamTurn(
    conversationId: string,
    text: string,
    controller: AbortController,
    mode: 'chat' | 'inspect',
    preset = '',
  ) {
    // 本轮实际下发的主机授权：手动开通，或由当前运维授权覆盖。
    // 只随请求下发、不写回会话 —— 覆盖随租约到期而失效，不会变成永久授权。
    const covered = leaseCoversHostNow();
    const turnAllowExec = allowExec || covered;
    const turnExecAccess: 'read' | 'write' = covered ? 'write' : execAccess;
    // 前端秒表：首字 / 整轮 / 长任务。量的是「用户实际等多久」，与后端分段对得上
    // 才能判断慢在哪一层（服务端 llm_ms 正常而这里 longTasks 一堆 = 渲染问题）
    aiPerf.startTurn(mode);
    const turn = aiApi.sendMessageStream(
      conversationId,
      text,
      (event) => {
        // token / token_reset 是文本流控制：进对话气泡，不进左栏的过程流水
        if (event.type === 'token') {
          pushToken(event.text);
          return;
        }
        if (event.type === 'token_reset') {
          // 新的一段流式文本开始：丢掉上一段的缓冲，避免「前言」粘进最终回答
          resetStream();
          return;
        }
        // 左栏「排查过程」只收过程类事件：assistant / report / approval / aborted
        // 各有去处（对话气泡、报告卡、审批卡），混进来只会撑大数组、让
        // ActivityPane 的「有没有内容」判断失准 —— 它们在 TimelineRow 里并不渲染。
        if (
          event.type === 'stage' ||
          event.type === 'thinking' ||
          event.type === 'tool_start' ||
          event.type === 'tool_done' ||
          event.type === 'error'
        ) {
          setEvents((list) => [...list, event]);
        }
        if (event.type === 'report_progress') {
          // 只留最后一条：体检一分钟能来一百多条，全塞进数组既撑大时间线，
          // 又让 ProgressBubble 每次都得从头扫一遍。
          setEvents((list) =>
            list.some((item) => item.type === 'report_progress')
              ? list.map((item) =>
                  item.type === 'report_progress' ? event : item,
                )
              : [...list, event],
          );
          return;
        }
        if (event.type === 'assistant') {
          // 收尾：权威文本落地，流式缓冲功成身退
          resetStream();
          setTranscript((list) => [
            ...list,
            {
              id: event.message_id,
              conversation_id: conversationId,
              seq: Number.MAX_SAFE_INTEGER,
              role: 'assistant',
              content: event.content,
              created: Math.floor(Date.now() / 1000),
            },
          ]);
        } else if (event.type === 'report') {
          setReport(event.report);
        } else if (event.type === 'approval') {
          // 写命令先落在这里：内联审批卡给用户看，本轮在后端挂着等
          setApproval(event);
        } else if (event.type === 'done') {
          resetStream();
          aiPerf.finishTurn('done');
          void refreshAfterTurn(conversationId);
        } else if (event.type === 'error') {
          resetStream();
          aiPerf.finishTurn('error');
          setErrorText(event.message);
          toast.error(t('ai.failed'), event.message);
        } else if (event.type === 'aborted') {
          resetStream();
          aiPerf.finishTurn('aborted');
          toast.info(t('ai.abortedTitle'), t('ai.abortedHint'));
          void refreshAfterTurn(conversationId);
        }
      },
      controller.signal,
      currentModelId || undefined,
      turnAllowExec,
      mode,
      preset,
      // 授权只在**这一轮**随请求下发：没有租约 id，模型连进虚拟机的工具都拿不到，
      // 也就谈不上「偷偷执行」。只发当前目标匹配的那张，避免切机瞬间串台。
      effectiveLease?.id ?? '',
      turnExecAccess,
    );
    // 正常收口由 done / error / aborted 分支负责（finishTurn 幂等）；
    // 这里兜的是读超时、切页、连接静默断开 —— 那几种没有任何终态事件
    await turn.finally(() => aiPerf.finishTurn('aborted'));
  }

  /**
   * 体检：新建一条会话并按模板跑一轮「采集 + 报告」。
   *
   * 这是一个**动作**，不是进入对话的门槛 —— 想直接问问题，在输入框里说就行。
   */
  function startInspection(withText = '') {
    if (!hostId || busy) return;
    setRunMode('inspect');
    scrollToReportRef.current = true;
    void execute(async (controller, base) => {
      setPrevFindings(base?.findings ?? null);
      const conv = await aiApi.createConversation({
        host_id: hostId,
        provider_id: currentModelId || undefined,
        allow_exec: allowExec || undefined,
        exec_access: allowExec ? execAccess : undefined,
      });
      setConvId(conv.id);
      setTranscript([]);
      setReport(null);
      setReportMeta(null);
      // 立刻刷新列表：会话已落库，选单里要马上能看到它，别等这一轮跑完
      void fetchConversations(hostId);
      await streamTurn(conv.id, withText, controller, 'inspect');
      void fetchConversations(hostId);
    });
  }

  /**
   * 传给 memo 面板的回调必须是**稳定引用**，否则 memo 永远命中不了 —— 流式期间
   * 每 70ms 渲染一次，inline 箭头函数等于把省下的重绘又全还回去。这里用 ref 兜住
   * 真正的实现：实现每次渲染都可以换，对外暴露的引用不变。
   */
  const startInspectionRef = useRef(startInspection);
  useEffect(() => {
    startInspectionRef.current = startInspection;
  });
  const onRerunReport = useCallback(() => startInspectionRef.current(), []);

  const patchAuth = useCallback((patch: AiAuthorizationPatch) => {
    if (patch.conversationId) setConvId(patch.conversationId);
    if (patch.allowExec !== undefined) setAllowExec(patch.allowExec);
    if (patch.execAccess) setExecAccess(patch.execAccess);
    if (patch.lease !== undefined) setVmLease(patch.lease);
    if (patch.justGrantedHost || patch.justGrantedLease) setExecJustGranted(true);
  }, []);

  const handleApprovalDone = useCallback(
    (_approved: boolean, reason?: 'timeout' | 'user' | 'stale') => {
      setApproval(null);
      // 批准/主动拒绝都不需要续跑入口；只有超时或失效才给
      if (reason === 'timeout' || reason === 'stale') setApprovalExpired(true);
    },
    [],
  );

  /**
   * 发送一条对话消息。
   *
   * 还没有会话就先开一条 —— **默认走自由对话**，不再顺手做一次体检：
   * 用户问「nginx 为什么 502」，要的是对这个问题的直接回答，而不是一份体检
   * 报告的摘要。需要完整体检时，他会去点那个按钮。
   */
  /** 乐观追加一条用户气泡（服务端落库后会被权威消息替换）。 */
  function pushUserBubble(conversationId: string, content: string) {
    setTranscript((list) => [
      ...list,
      {
        id: 'local-' + Date.now(),
        conversation_id: conversationId,
        seq: Number.MAX_SAFE_INTEGER,
        role: 'user',
        content,
        created: Math.floor(Date.now() / 1000),
      },
    ]);
  }

  /** 取当前会话；没有就新开一条（新会话的前几轮状态一并清干净）。 */
  async function ensureConversation(): Promise<string> {
    if (convId) return convId;
    const conv = await aiApi.createConversation({
      host_id: hostId,
      provider_id: currentModelId || undefined,
      allow_exec: allowExec || undefined,
      exec_access: allowExec ? execAccess : undefined,
    });
    setConvId(conv.id);
    setTranscript([]);
    setReport(null);
    setReportMeta(null);
    setPrevFindings(null);
    // 立刻刷新列表：会话已落库，选单里要马上能看到它
    void fetchConversations(hostId);
    return conv.id;
  }

  function sendMessage() {
    const text = draft.trim();
    if (!text || busy || !hostId) return;
    setDraft('');
    setRunMode('chat');
    void execute(async (controller) => {
      const target = await ensureConversation();
      pushUserBubble(target, text);
      await streamTurn(target, text, controller, 'chat');
    });
  }

  /**
   * 重新请求上一条超时/失效的写命令。
   *
   * 不重跑前面的工具调用：会话上下文还在服务端，这一轮只是让模型把刚被中断的
   * 那一步重新提出来。所以用户不必重新描述问题，也不会重复烧一遍 token。
   */
  function resumeApproval() {
    if (busy || !hostId) return;
    setApprovalExpired(false);
    setRunMode('chat');
    void execute(async (controller) => {
      const target = await ensureConversation();
      const text = t('ai.approval.resumePrompt');
      pushUserBubble(target, text);
      await streamTurn(target, text, controller, 'chat');
    });
  }

  /**
   * 跑一个预案。
   *
   * 前端只给 key：预案的**指令**和**落库用的短标题**由后端分开处理（见
   * `aiplaybooks.py`）—— 这样加一套新套路不用改这个页面，聊天记录里也不会
   * 出现几十行的操作说明。
   */
  function runPlaybook(key: string) {
    if (!key || busy || !hostId) return;
    setRunMode('chat');
    // 记最近使用：下次打开时把常用的排到前面
    setRecentPlaybooks((list) => {
      const next = [key, ...list.filter((item) => item !== key)].slice(
        0,
        PLAYBOOK_RECENT_MAX,
      );
      writeStoredList(PLAYBOOK_RECENT_KEY, next);
      return next;
    });
    void execute(async (controller) => {
      const target = await ensureConversation();
      // 乐观气泡用预案标题，与后端落库的那条消息一致
      const item = playbooks.find((p) => p.key === key);
      pushUserBubble(target, item?.title || key);
      await streamTurn(target, '', controller, 'chat', key);
    });
  }

  function newConversation() {
    setConvId('');
    setTranscript([]);
    setReport(null);
    setReportMeta(null);
    setPrevFindings(null);
    setEvents([]);
    setErrorText('');
    setRunMode('');
    // 新会话默认不授权上机执行：上一条会话开过不等于这条也要开。
    setAllowExec(false);
    setExecAccess('read');
    setExecJustGranted(false);
  }

  function stop() {
    // 正卡在写命令审批上时，必须先把它按「拒绝」收口：后端此刻挂在等待里，
    // 只断开流并不会让它醒 —— 用户之后（或卡片倒计时到点）仍可能把这条命令
    // 放行。先发一次拒绝，再断开 SSE。
    if (approval) {
      void aiApi.approve(approval.approval_id, false).catch(() => {});
      setApproval(null);
    }
    abortRef.current?.abort();
  }

  /**
   * 只解读、不调工具（L1 通路）。
   *
   * 留这个入口是因为「模型不支持 function calling」是常见情况 —— 内网跑的小模型
   * 尤其如此。它出的是一份一次性摘要，不进入会话线程。
   */
  function quickRun() {
    if (!hostId) return;
    setRunMode('inspect');
    scrollToReportRef.current = true;
    void execute(async (_controller, base) => {
      const data = await aiApi.inspect(hostId, currentModelId);
      setPrevFindings(base?.findings ?? null);
      setConvId('');
      setTranscript([]);
      setReport(data.result);
      setReportMeta({
        model: data.model,
        ms: data.timing?.total_ms,
        tokens: data.usage?.total_tokens,
        tools: data.tool_calls?.length ?? 0,
        hostLabel: data.host_name,
        hostId: data.host_id,
        at: data.generated_at,
      });
      toast.success(
        t('ai.doneTitle'),
        t('ai.doneDetail', { n: data.result.findings.length }),
      );
    });
  }

  const ready = Boolean(status?.ready);
  /** 管理员把 AI 助手整个关了：与「没配模型」是两件事，提示也要分开 */
  const aiDisabled = modelsQuery.data?.enabled === false;
  /** 这个用户名下一张可用模型都没有（不是「有但没配好」） */
  const noModelsAtAll = (modelsQuery.data?.models.length ?? 0) === 0;
  // 有没有「授权上机执行」的权限：没有就不显示那个开关（不给点不动的控件）
  const canExec = hasPermission('ai.exec');
  // 远程终端是**另一道门**：能看 AI 的结论，不等于能亲手在这台机器上敲命令
  const canTerminal = hasPermission('ai.terminal');
  const canVmRead = hasPermission('ai.vm.read');
  const canVmWrite = hasPermission('ai.vm.write');

  /** 当前目标上仍生效的虚拟机租约（父组件只持有一张，按目标校验） */
  const effectiveLease = leaseForTarget(vmLease, [], vmTarget);

  /**
   * 「一次授权、全程生效」：当前虚拟机已有运维授权时，主机命令直接沿用。
   *
   * 只在用户本人有 ``ai.exec`` / ``ai.vm.write`` 时成立 —— 业务授权不能绕过
   * 权限位。租约撤销、到期或切到别的虚拟机后自动失效。
   */
  const leaseCoversHost = Boolean(
    canExec &&
      canVmWrite &&
      effectiveLease &&
      effectiveLease.status === 'active' &&
      effectiveLease.access === 'ops',
  );

  /** 请求时再算一次，避免页面停留过久后拿着一张已到期的票当授权用 */
  const leaseCoversHostNow = () =>
    Boolean(
      canExec &&
        canVmWrite &&
        effectiveLease &&
        effectiveLease.status === 'active' &&
        effectiveLease.access === 'ops' &&
        Number(effectiveLease.expires_at || 0) > Date.now() / 1000,
    );

  /** 授权范围是否已经动过一次（用于把 02 标成已完成） */
  const scopeTouched = allowExec || Boolean(vmLease) || leaseCoversHost;

  /**
   * 上一轮停在 AI 说完话的位置、而现在已经拿到上机执行授权：可以一键续跑。
   *
   * 开关只对**下一轮**生效，这是有意的（换一轮就是一次新授权）。但用户看到
   * 「我先看一下磁盘」之后停住、再去打开开关时，界面上不该只剩一片安静 ——
   * 他能明确知道「现在可以继续了」，并且不用重新描述一遍问题。
   */
  const lastSpeaker = [...transcript]
    .reverse()
    .find((item) => item.role === 'user' || item.role === 'assistant');
  const canContinue =
    canExec &&
    (allowExec || leaseCoversHost) &&
    execJustGranted &&
    !busy &&
    Boolean(convId) &&
    lastSpeaker?.role === 'assistant';

  function continueTurn() {
    if (!canContinue) return;
    setExecJustGranted(false);
    setRunMode('chat');
    void execute(async (controller) => {
      const target = await ensureConversation();
      const text = t('ai.continuePrompt');
      pushUserBubble(target, text);
      await streamTurn(target, text, controller, 'chat');
    });
  }

  /* ---------------- 控制台分栏拖拽 ---------------- */

  function clampSplit(value: number): number {
    return Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, value));
  }

  function rememberSplit(value: number) {
    try {
      window.localStorage.setItem(SPLIT_STORE_KEY, String(Math.round(value)));
    } catch {
      /* 存不了不影响本次使用 */
    }
  }

  function startResize(event: ReactPointerEvent<HTMLDivElement>) {
    // 拖拽期间不让浏览器顺手选中文字，否则选区和终端光标会一起跟着跑
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragSplitRef.current = consoleSplit;
    setResizingConsole(true);
  }

  function moveResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (!resizingConsole) return;
    const rect = consoleRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const next = clampSplit(((event.clientX - rect.left) / rect.width) * 100);
    dragSplitRef.current = next;
    consoleRef.current?.style.setProperty('--ai-split', `${next}%`);
  }

  function endResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (!resizingConsole) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    setResizingConsole(false);
    setConsoleSplit(dragSplitRef.current);
    rememberSplit(dragSplitRef.current);
  }

  function resetSplit() {
    setConsoleSplit(SPLIT_DEFAULT);
    rememberSplit(SPLIT_DEFAULT);
  }

  // 键盘也能调：分隔条是标准 separator，Tab 聚焦后用左右键挪
  function onSplitKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const next = clampSplit(
      consoleSplit + (event.key === 'ArrowLeft' ? -3 : 3),
    );
    setConsoleSplit(next);
    rememberSplit(next);
  }

  return (
    <PageShell
      className="ai-page"
      title={
        <span className="flex items-center gap-8">
          <IconSparkle size={20} />
          {t('ai.title')}
        </span>
      }
      subtitle={t('ai.subtitle')}
      actions={
        /* 三个入口都是「打开一个弹窗看看」，彼此没有主次 —— 所以统一用
           secondary。混用 ghost 会让有底有框的那个（我的模型）看起来像主操作，
           其实它只是同一排里的一个入口。（项目里页面级 actions 也都是 secondary，
           见 Backups / Certificates / AuditLog。） */
        <div className="flex items-center gap-8">
          {/* 先说清楚能干什么，再让人动手：这是个会登录到他服务器上执行命令
              的功能，用户有权在按按钮之前知道边界在哪 */}
          <Button
            variant="secondary"
            icon={<IconInfo size={15} />}
            onClick={() => setCapabilitiesOpen(true)}
          >
            {t('ai.capabilities')}
          </Button>
          <Button
            variant="secondary"
            icon={<IconSettings size={15} />}
            onClick={() => setSettingsOpen(true)}
          >
            {isAdmin ? t('ai.settings') : t('ai.myModels')}
          </Button>
          {/* 用量排在这一组的最后：前两个是「动手之前该知道的」（能力边界、
              用哪个模型），用量是「事后回看」的，顺序上也该靠后 */}
          <Button
            variant="secondary"
            icon={<IconActivity size={15} />}
            onClick={() => setUsageOpen(true)}
          >
            {t('ai.usage.open')}
          </Button>
        </div>
      }
    >
      {targetsQuery.isError ? (
        <ErrorState
          title={t('ai.failed')}
          message={errorMessage(targetsQuery.error)}
          onRetry={() => void targetsQuery.refetch()}
        />
      ) : null}

      <SegmentedControl
        value={tab}
        onChange={setTab}
        options={[
          { label: t('ai.tabInspect'), value: 'inspect' },
          { label: t('ai.tabRecords'), value: 'records' },
        ]}
        ariaLabel={t('ai.title')}
      />

      {tab === 'records' ? (
        <RecordsPanel
          hosts={hostOptions}
          onOpenConversation={openConversationFromRecords}
        />
      ) : (
        <>
          {!targetsQuery.isError && !targetsQuery.isLoading && !ready ? (
            /* 「没得用」有三成成因，提示必须分开：平台被关、管理员没配平台模型、
               普通用户自己没有可用模型。最后一种最容易被误导成「去找管理员」，
               其实他可以自己配 —— 两条路都写出来。 */
            aiDisabled ? (
              <Notice tone="info" title={t('ai.disabledTitle')}>
                {t('ai.disabledHint')}
              </Notice>
            ) : (
              <Notice
                tone={isAdmin ? 'warning' : 'info'}
                title={
                  isAdmin
                    ? t('ai.notConfigured')
                    : noModelsAtAll
                      ? t('ai.noModelTitle')
                      : t('ai.noMyModel')
                }
              >
                {isAdmin
                  ? t('ai.notConfiguredHint')
                  : noModelsAtAll
                    ? t('ai.noModelHint')
                    : t('ai.noMyModelHint')}
                <div className="mt-8">
                  <Button
                    variant="primary"
                    icon={<IconSettings size={15} />}
                    onClick={() => setSettingsOpen(true)}
                  >
                    {isAdmin ? t('ai.configureNow') : t('ai.myModels')}
                  </Button>
                </div>
              </Notice>
            )
          ) : null}

          {/* 还有主机在后台探测：先说清楚「在等什么」，否则用户会以为列表就这么几台。
              这时**不能**同时显示「没有可用主机」—— 结论还没出来。 */}
          {pendingTotal > 0 ? (
            <Notice tone="info" title={t('ai.probingTitle', { n: pendingTotal })}>
              {t('ai.probingHint')}
            </Notice>
          ) : null}

          {/* 整条 PVE 连不上：这不是「某台机器不可用」，而是这批机器全都读不到，
              逐台列出来只会让人以为是机器的问题（同样的提示会重复十几行）。 */}
          {pveDown > 0 ? (
            <Notice tone="warning" title={t('ai.scanPveDownTitle', { n: pveDown })}>
              {t('ai.scanPveDownHint')}
            </Notice>
          ) : null}

          {/* 一台都选不出来时才占一条提示；正常的「先选机器」引导写进 01 标题旁，
              不再用一条横贯整页的告警 —— 这一屏真正要做的只有选机器和开始。 */}
          {!hostId &&
          !targetsQuery.isLoading &&
          !pendingTotal &&
          hostCards.length === 0 ? (
            <Notice
              tone="info"
              title={
                excludeInfo?.total ? t('ai.noEligibleHostTitle') : t('ai.noHostTitle')
              }
            >
              {excludeInfo?.total ? t('ai.noEligibleHostHint') : t('ai.noHostHint')}
            </Notice>
          ) : null}

          {/* 工作台：把「目标与模型」「授权范围」「控制台」收进同一张面板，
              区块之间只用分隔线分节，而不是三张各自带边框阴影的卡片 ——
              卡片套卡片会让整页看起来像几个互不相干的工具拼在一起。 */}
          <div className="ai-studio">
            <section className="ai-studio-section">
              <div className="ai-section-head">
                <span className={`ai-section-num${!hostId ? ' is-active' : ' is-done'}`}>
                  01
                </span>
                <div className="ai-section-text">
                  <span className="ai-section-title">
                    {t('ai.sectionTarget')}
                    {!hostId ? (
                      <span className="ai-step-now">{t('ai.stepNow')}</span>
                    ) : null}
                  </span>
                  {/* 没选前置一句为什么不能替你选；选完就回到正常的一句说明，
                      同一位置不再叠第二条告警。 */}
                  <span className="ai-section-desc">
                    {!hostId && !targetsQuery.isLoading && hostCards.length > 0
                      ? needsHost
                        ? t('ai.needsHostHint')
                        : t('ai.pickHostHint')
                      : t('ai.sectionTargetHint')}
                  </span>
                </div>
              </div>
              {/* 左选择、右操作：主操作放最右（视线与操作的终点），次要操作降为
                  ghost 小按钮靠左 —— 两个实心按钮并排会让人分不清该点哪个。 */}
              <div className="ai-picker">
                <div className="ai-picker-fields">
                  {/* 只选一次：候选全是**受管主机**（AI 只对纳管过的机器动手），
                      选中后自动带出同一台机器上的虚拟机，两条通道一起确定。
                      没纳管的虚拟机不在这里出现 —— 它跑不了主机命令。 */}
                  <Field label={t('ai.selectTarget')}>
                    <Select
                      value={hostId}
                      onChange={(e) => {
                        setHostId(e.target.value);
                        if (e.target.value) {
                          setLastHostId(e.target.value);
                          writeStoredString(LAST_HOST_KEY, e.target.value);
                        }
                      }}
                      options={targetOptions}
                      disabled={busy || hostCards.length === 0}
                    />
                  </Field>
                  {/* 排查因题而异：读个负载用快模型就够，真定位故障才值得上强
                      模型。所以这里选的是「这一次用哪个」，不改管理员设的默认；
                     但会记住你上次的选择，下次打开直接沿用（本机记忆）。 */}
                  <Field label={t('ai.selectModel')}>
                    <Select
                      value={currentModelId}
                      onChange={(e) => setModelId(e.target.value)}
                      options={modelOptions}
                      disabled={busy || modelOptions.length === 0}
                      title={t('ai.modelRememberHint')}
                    />
                  </Field>
                </div>
                <div className="ai-picker-actions">
                  {/* 与主操作同一行：ghost 小按钮靠左、主按钮靠右。
                      两个实心按钮并排会分不清主次，所以「仅解读」保持 ghost。 */}
                  {!busy ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={!ready || !hostId}
                      onClick={() => void quickRun()}
                      title={t('ai.quickHint')}
                    >
                      {t('ai.quickInspect')}
                    </Button>
                  ) : null}
                  <Button
                    variant="primary"
                    icon={<IconSparkle size={15} />}
                    loading={busy}
                    disabled={!ready || !hostId || busy}
                    onClick={() => startInspection()}
                    title={t('ai.startHint')}
                  >
                    {busy ? t('ai.running') : t('ai.start')}
                  </Button>
                  {/* 一次排查十几秒到一分钟，中途发现选错主机/模型时能停，
                      比等它跑完再重来省事得多 */}
                  {busy ? (
                    <Button
                      variant="danger"
                      icon={<IconStop size={15} />}
                      onClick={stop}
                    >
                      {t('ai.stop')}
                    </Button>
                  ) : null}
                </div>
              </div>

              {/* 上次排查过的主机：只回显 + 一键采用，绝不自动选中 */}
              {!hostId && lastHostHost ? (
                <div className="ai-last-host">
                  <span>{t('ai.lastHost', { host: lastHostHost.name || lastHostHost.id })}</span>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => {
                      setHostId(lastHostHost.id);
                      writeStoredString(LAST_HOST_KEY, lastHostHost.id);
                    }}
                  >
                    {t('ai.useLastHost')}
                  </Button>
                </div>
              ) : null}

              {/* 列表比「受管主机」短，必须解释原因。默认只占一行摘要，点开才
                  看五个条件、被排除的机器名与修复入口 —— 不解释会被当成漏机器，
                  只报台数又让人不知道是哪台、该去哪儿修。 */}
              <details className="ai-note-more">
                <summary>
                  {t('ai.targetFilterTitle')}
                  {excludeInfo?.total ? (
                    <span className="ai-note-count">
                      {t('ai.targetFilterExcluded', { n: excludeInfo.total })}
                    </span>
                  ) : null}
                </summary>
                <div className="ai-note-body">
                  <p>{t('ai.targetFilterHint')}</p>
                  <div className="ai-cond-list">
                    {[
                      t('ai.condRunning'),
                      t('ai.condManaged'),
                      t('ai.condAgent'),
                      t('ai.condCloudinit'),
                      t('ai.condVm'),
                    ].map((cond) => (
                      <span key={cond}>{cond}</span>
                    ))}
                  </div>
                  {excludedItems.length > 0 ? (
                    <ul className="ai-excluded-list">
                      {excludedItems.map((item) => (
                        <li key={item.id} className="ai-excluded-row">
                          <span className="ai-excluded-name" title={item.id}>
                            {item.name || item.id}
                          </span>
                          <span className="ai-excluded-reason">
                            {t(excludeReasonKey(item.reason))}
                          </span>
                          {item.node && item.vmid != null ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() =>
                                navigate(
                                  `/vms/${encodeURIComponent(item.node as string)}/${item.vmid}`,
                                )
                              }
                            >
                              {t('ai.excludeFix')}
                            </Button>
                          ) : null}
                        </li>
                      ))}
                    </ul>
                  ) : excludedSummary ? (
                    <p className="ai-note-sub">{excludedSummary}</p>
                  ) : null}
                </div>
              </details>
            </section>

            {/* 授权范围紧接「在哪台机器上做」：它是这一轮的能力边界。
                两条通道并列而不是各占一处 —— 主机命令（本次会话）与虚拟机内（限时
                票据）本来就是同一件事的两个面，分开摆会让人以为哪个才是总开关。 */}
            <section className="ai-studio-section">
              <div className="ai-section-head">
                <span className={`ai-section-num${scopeTouched ? ' is-done' : ''}`}>02</span>
                <div className="ai-section-text">
                  <span className="ai-section-title">
                    {t('ai.sectionScope')}
                    {!scopeTouched ? (
                      <span className="ai-step-optional">{t('ai.stepOptional')}</span>
                    ) : null}
                  </span>
                  <span className="ai-section-desc">{t('ai.sectionScopeHint')}</span>
                </div>
              </div>

              <div className="ai-scope-list">
                <AuthorizationCard
                  conversationId={convId}
                  hostId={hostId}
                  hostName={selectedHost?.name || ''}
                  allowExec={allowExec}
                  execAccess={execAccess}
                  lease={vmLease}
                  target={vmTarget}
                  canExec={canExec}
                  canVmRead={canVmRead}
                  canVmWrite={canVmWrite}
                  onPatch={patchAuth}
                />

                {/* 授权是**下一轮**才生效，不会把刚结束的那一轮重新拉起来。
                    用户看到 AI 说「我先看一下磁盘」然后停住、再去授权时，最容易
                    以为「我批准了，它怎么不动」—— 这里给一个显式入口。 */}
                {canContinue ? (
                  <div className="ai-scope-block ai-auth-continue">
                    <span className="ai-scope-hint">{t('ai.continueHint')}</span>
                    <Button size="sm" variant="ghost" onClick={continueTurn}>
                      {t('ai.continue')}
                    </Button>
                  </div>
                ) : null}
              </div>
            </section>
          </div>

          {/* 控制台：左「终端 / 执行过程」、右「对话与授权」。
              左栏默认是终端 —— 那台被纳管服务器的实况窗口：用户自己敲的命令，
              以及 AI 这一轮执行的每条命令与输出，都落在同一个画面里。要看结构
              化的过程流水（哪一步、耗了多久）就切到「执行过程」。
              右栏是对话与逐条授权，两边并排，「它在做什么」和「要不要批准」
              就在同一屏里。 */}
          {/* 「连的是哪台机器」由终端卡片自己那行说明负责，不在这里重复一遍 ——
              同一个句子同时出现在工具栏和卡片里，读起来像出了两次错。 */}
          <div className="ai-console-shell">
            <div className="ai-console-shell-head">
              <div className="ai-section-head" style={{ marginBottom: 0 }}>
                <span className={`ai-section-num${busy ? ' is-active' : convId ? ' is-done' : ''}`}>
                  03
                </span>
                <div className="ai-section-text">
                  <span className="ai-section-title">
                    {t('ai.sectionConsole')}
                    {busy ? <span className="ai-step-now">{t('ai.stepNow')}</span> : null}
                  </span>
                  <span className="ai-section-desc">{t('ai.sectionConsoleHint')}</span>
                </div>
              </div>
              <div className="ai-console-shell-tools">
                {/* 没有上机授权时，AI 只给建议不执行 —— 这个原因必须能在控制台
                    旁边看到，否则用户只会以为「它今天不想动手」 */}
                {canExec && !allowExec && !leaseCoversHost ? (
                  <span className="ai-scope-warn" title={t('ai.execNotGrantedHint')}>
                    <IconAlert size={13} />
                    {t('ai.execNotGranted')}
                  </span>
                ) : null}
                <SegmentedControl
                  value={leftTab}
                  onChange={setLeftTab}
                  options={[
                    { label: t('ai.terminalTitle'), value: 'terminal' },
                    { label: t('ai.progressTitle'), value: 'activity' },
                  ]}
                  ariaLabel={t('ai.terminalTitle')}
                />
              </div>
            </div>
            {/* 分栏宽度写在 CSS 变量上：媒体查询可以直接把它改回单列，
                不必在 JS 里监听断点；拖拽只更新这一个数字。 */}
            <div
              ref={consoleRef}
              className={`ai-console${resizingConsole ? ' is-resizing' : ''}`}
              style={{ '--ai-split': `${consoleSplit}%` } as CSSProperties}
            >
              {/* 两个页签**都保持挂载**，只切换显隐：终端一旦卸载就等于断开 SSH，
                  切去看一眼「执行过程」再切回来会变成一条新连接 —— 当前目录、
                  环境变量、跑着的进程全丢，而且隐藏期间 AI 的镜像输出也收不到了。
                  隐藏时容器尺寸为 0，终端自己会跳过尺寸计算；切回来时再对齐一次
                  （见 AiTerminal 的 visible 处理）。 */}
              <div className="ai-console-col ai-console-col-left">
                <div
                  className={`ai-console-pane${leftTab === 'terminal' ? '' : ' is-hidden'}`}
                >
                  <TerminalPane
                    hostId={hostId}
                    hostName={selectedHostLabel}
                    canUse={canTerminal}
                    visible={leftTab === 'terminal'}
                    fill
                  />
                </div>
                <div
                  className={`ai-console-pane${leftTab === 'activity' ? '' : ' is-hidden'}`}
                >
                  <ActivityPaneMemo
                    events={events}
                    busy={busy}
                    history={toolHistory}
                  />
                </div>
              </div>

              <div
                className={`ai-console-resizer${resizingConsole ? ' is-active' : ''}`}
                role="separator"
                aria-orientation="vertical"
                aria-label={t('ai.consoleResize')}
                aria-valuemin={SPLIT_MIN}
                aria-valuemax={SPLIT_MAX}
                aria-valuenow={Math.round(consoleSplit)}
                title={t('ai.consoleResize')}
                tabIndex={0}
                onPointerDown={startResize}
                onPointerMove={moveResize}
                onPointerUp={endResize}
                onPointerCancel={endResize}
                onDoubleClick={resetSplit}
                onKeyDown={onSplitKeyDown}
              >
                <span className="ai-console-resizer-grip" aria-hidden="true" />
              </div>

              <div className="ai-console-col ai-console-col-right">
                <ChatPane
                  conversations={conversations}
                  convId={convId}
                  onSelectConversation={(id) => void loadConversation(id)}
                  onNewConversation={newConversation}
                  hostLabel={selectedHostLabel}
                  modelLabel={selectedModelLabel}
                  hasHost={Boolean(hostId)}
                  transcript={transcript}
                  busy={busy}
                  approval={approval}
                  onApprovalDone={handleApprovalDone}
                  approvalExpired={approvalExpired}
                  onResumeApproval={resumeApproval}
                  error={errorText}
                  draft={draft}
                  onDraftChange={setDraft}
                  onSend={sendMessage}
                  canSend={ready && Boolean(hostId) && !busy && !approval}
                  streaming={streaming}
                  progress={progress}
                  playbooks={orderedPlaybooks}
                  favorites={favPlaybooks}
                  onToggleFavorite={toggleFavoritePlaybook}
                  onRunPlaybook={runPlaybook}
                />
              </div>
            </div>
          </div>

          {/* 体检进行中：先在结论区放一块骨架，明确「报告会出现在这里」，
              而不是让用户对着一条「正在分析…」干等 */}
          {busy && runMode === 'inspect' && !report ? (
            <Card>
              <CardHeader
                title={t('ai.summaryTitle')}
                subtitle={t('ai.progressReportHint')}
              />
              <div className="ai-report-skeleton">
                <Skeleton width="42%" height={16} />
                <Skeleton width="88%" height={12} />
                <Skeleton width="76%" height={12} />
                <Skeleton width="64%" height={12} />
              </div>
            </Card>
          ) : null}

          {report ? (
            <div ref={reportAnchorRef}>
              <ReportCardMemo
                report={report}
                meta={reportMeta}
                previousFindings={prevFindings}
                onRerun={onRerunReport}
              />
            </div>
          ) : null}

          {report ? <Notice tone="info">{t('ai.disclaimer')}</Notice> : null}
        </>
      )}

      <CapabilitiesModal
        open={capabilitiesOpen}
        onClose={() => setCapabilitiesOpen(false)}
      />

      {/* 用量明细：入口是标题栏那组按钮里的「Token 消耗」 */}
      <AiUsageModal open={usageOpen} onClose={() => setUsageOpen(false)} />

      {/* 配置面板对所有人开放：管理员配全平台的，普通用户配自己的 ——
          界面里平台共享项会置灰，看得见在用哪个，但改不了。 */}
      <SettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={() => {
          void qc.invalidateQueries({ queryKey: ['ai'] });
        }}
      />
    </PageShell>
  );
}

/* ------------------------------------------------------------------ 执行过程 */

/**
 * 实时展示 Agent 在做什么（左栏「执行过程」页签）。
 *
 * 左栏默认是终端 —— 那里看的是**命令与输出**；这一页看的是**步骤与耗时**：
 * 第几轮分析、调了哪个工具、成没成、花了多久。两者回答的是不同的问题，
 * 所以都在，而不是二选一：终端贴原始输出，这里给结构化的过程。
 *
 * 两种数据来源：本轮在跑时看实时事件；翻看历史会话时没有事件可放，就从会话消息
 * 里把「执行过的工具」重建出来 —— 否则切回去会是一片空白，还以为没跑过。
 */
function ActivityPane({
  events,
  busy,
  history,
}: {
  events: AiChatStreamEvent[];
  busy: boolean;
  /** 历史会话里执行过的工具与命令（回看用；有实时事件时忽略） */
  history: Array<{ tool: string; ok: boolean; command: string }>;
}) {
  const t = useT();
  const rows = events.filter((event) => event.type !== 'done');
  const bottomRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'nearest' });
  }, [rows.length, history.length]);

  const empty = rows.length === 0 && history.length === 0;

  return (
    <Card>
      <CardHeader
        title={t('ai.progressTitle')}
        subtitle={
          busy
            ? t('ai.runningHint')
            : rows.length > 0
              ? t('ai.progressDone')
              : history.length > 0
                ? t('ai.progressHistory')
                : t('ai.progressIdle')
        }
        actions={busy ? <Spinner size={14} /> : undefined}
      />
      <div className="flex flex-col gap-6 ai-console-scroll">
        {empty ? (
          <span className="fs-sm text-muted">{t('ai.progressEmpty')}</span>
        ) : rows.length > 0 ? (
          rows.map((event, index) => <TimelineRow key={index} event={event} />)
        ) : (
          history.map((item, index) => (
            <TimelineLine
              key={index}
              icon={item.ok ? <IconCheck size={13} /> : <IconAlert size={13} />}
              text={t('ai.toolHistoryLine', {
                mark: item.ok ? '✓' : '✕',
                tool: item.tool,
              })}
              detail={item.command}
            />
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ 对话栏 */

/**
 * 控制台右栏：对话 + 授权。
 *
 * 对话内容来自**服务端会话**（``ai_messages``）：首轮体检产出一份报告，之后可以
 * 继续追问。刷新或换设备都能接着聊 —— 这也是把它落库、而不是只留前端的理由。
 * 授权卡片内嵌在对话流里：授权本身就是对话中的一个动作。
 */
/** 执行过程面板：只在事件 / 历史变化时重绘，不跟着流式帧走（见文件头的 memo 说明） */
const ActivityPaneMemo = memo(ActivityPane);

function ChatPane({
  conversations,
  convId,
  onSelectConversation,
  onNewConversation,
  hostLabel,
  modelLabel,
  /** 有没有选目标主机。没选时预案入口一并锁住 —— 点下去没反应比没有入口更困惑 */
  hasHost,
  transcript,
  busy,
  approval,
  onApprovalDone,
  approvalExpired,
  onResumeApproval,
  error,
  draft,
  onDraftChange,
  onSend,
  canSend,
  streaming,
  progress,
  playbooks,
  favorites,
  onToggleFavorite,
  onRunPlaybook,
}: {
  conversations: AiConversation[];
  convId: string;
  onSelectConversation: (id: string) => void;
  onNewConversation: () => void;
  hostLabel: string;
  modelLabel: string;
  /** 是否已选定目标主机 */
  hasHost: boolean;
  transcript: AiChatMessage[];
  busy: boolean;
  approval: ApprovalRequest | null;
  onApprovalDone: (
    approved: boolean,
    reason?: 'timeout' | 'user' | 'stale',
  ) => void;
  /** 上一张审批卡超时/失效，且本轮已结束：显示「重新请求这一步」 */
  approvalExpired: boolean;
  onResumeApproval: () => void;
  /** 本轮失败原因：就地显示在对话末尾，而不是页脚一条容易错过的 Notice */
  error: string;
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: () => void;
  canSend: boolean;
  /** 正在流式到达的回复文本（打字机）；收尾后为空 */
  streaming: string;
  /** 本轮进度摘要：镜像到右栏，免得用户只看到「转圈」 */
  progress: AiProgress | null;
  /** 排障预案：空会话时铺成快捷入口，对话中收进标题栏的选单（已按收藏/最近排序） */
  playbooks: AiPlaybook[];
  /** 收藏的预案 key */
  favorites: string[];
  onToggleFavorite: (key: string) => void;
  onRunPlaybook: (key: string) => void;
}) {
  const t = useT();
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // 只渲染「人说的话」：context 是喂给模型的 [数据] 段，tool 行在左栏展示。
  // 缓存住：流式期间每 70ms 一次渲染，不该每次都把整条 transcript 再扫一遍。
  const bubbles = useMemo(
    () =>
      transcript.filter(
        (item) => item.role === 'user' || item.role === 'assistant',
      ),
    [transcript],
  );
  /**
   * 长会话只渲染最近 50 条。
   *
   * 几百轮的会话每帧 reconcile 全量消息（每条还可能带报告卡）是「越聊越卡」的
   * 直接原因。窗口化之后只有用户主动往上翻才展开 —— 比引一个虚拟滚动库省事，
   * 也不会和「贴底自动滚」打架。
   */
  const BUBBLE_WINDOW = 50;
  const [showAllBubbles, setShowAllBubbles] = useState(false);
  useEffect(() => setShowAllBubbles(false), [convId]);
  const hiddenBubbles = showAllBubbles
    ? 0
    : Math.max(0, bubbles.length - BUBBLE_WINDOW);
  const visibleBubbles = hiddenBubbles ? bubbles.slice(hiddenBubbles) : bubbles;

  // 失败也要算「已开始」：否则首轮就失败时错误气泡会被空状态盖住
  const started = Boolean(convId) || bubbles.length > 0 || Boolean(error);
  const lastIsAssistant = bubbles[bubbles.length - 1]?.role === 'assistant';

  /**
   * 是否「贴着底部」。用户往上翻历史时不该被流式输出不断拽回来。
   */
  const stickToBottomRef = useRef(true);

  /**
   * 新消息总落在底部：自动滚到底，省得手动追（流式增长时也要跟着走）。
   *
   * 两处收敛都是为了流式期间不掉帧：
   *  - 合进 rAF：``streaming.length`` 每 70ms 变一次，同步 ``scrollTo`` 会和渲染
   *    交替触发强制布局（layout thrash），一帧里读写交替最费；
   *  - 只在贴底时跟随，见 ``stickToBottomRef``。
   */
  useEffect(() => {
    const el = bodyRef.current;
    if (!el || !stickToBottomRef.current) return;
    const frame = requestAnimationFrame(() => {
      // 只在回调里读 scrollHeight / 写 scrollTop，读写不再交替
      el.scrollTop = el.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [bubbles.length, approval, busy, streaming.length]);

  const handleBodyScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    stickToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  return (
    <Card className="ai-chat-card">
      <CardHeader
        // 同上：右侧在干什么由 03 标题条交代，标题栏只放控件
        title={t('ai.chatTitle')}
        actions={
          <div className="ai-chat-tools">
            {/* 预案在任何时候都够得着（不只是空会话）：聊到一半想换套路是常事。
                value 固定为空 —— 它是个「动作选单」，选完就回到占位文案 */}
            {playbooks.length > 0 ? (
              <Select
                value=""
                onChange={(e) => {
                  const key = e.target.value;
                  if (key) onRunPlaybook(key);
                }}
                options={[
                  { label: t('ai.playbookPick'), value: '' },
                  ...playbooks.map((item) => ({
                    // 收藏项加星标：不必展开就能看出哪几个是自己常用的
                    label: `${favorites.includes(item.key) ? '★ ' : ''}${item.title}`,
                    value: item.key,
                  })),
                ]}
                disabled={busy || !hasHost}
                aria-label={t('ai.playbookPick')}
                title={t('ai.playbookPickHint')}
              />
            ) : null}
            {conversations.length > 0 ? (
              <Select
                value={convId}
                onChange={(e) => onSelectConversation(e.target.value)}
                options={conversations.map((item) => ({
                  value: item.id,
                  label: `${item.host_name} · ${formatDateTime(item.updated)}`,
                }))}
                aria-label={t('ai.chatHistory')}
                title={t('ai.chatHistory')}
              />
            ) : null}
            <Button
              variant="ghost"
              size="sm"
              icon={<IconPlus size={14} />}
              onClick={onNewConversation}
              disabled={busy}
              title={t('ai.chatNewHint')}
            >
              {t('ai.chatNew')}
            </Button>
          </div>
        }
      />
      <div
        ref={bodyRef}
        className="flex flex-col gap-8 ai-console-scroll"
        onScroll={handleBodyScroll}
      >
        {!started ? (
          <>
            <ChatBubble role="assistant">{t('ai.chatIntro')}</ChatBubble>
            {/* 先说清楚问的是哪台机器 —— 自由对话直接在这台上查。
                没选主机时不说：那时这句话只能显示成一个「-」，等于什么都没说 */}
            {hasHost ? (
              <div className="fs-xs text-muted">
                {t('ai.chatTarget', { host: hostLabel })}
              </div>
            ) : null}
            {/* 排障预案：点一下就按这套套路查。给的是**起点**，不是流程 ——
                不想用模板的人照样可以直接在下面打字 */}
            {playbooks.length > 0 && !busy ? (
              <div className="ai-playbook-grid">
                {playbooks.map((item) => {
                  const fav = favorites.includes(item.key);
                  return (
                    <span key={item.key} className="ai-playbook-chip">
                      <Button
                        variant="secondary"
                        size="sm"
                        title={item.hint}
                        disabled={!hasHost}
                        onClick={() => onRunPlaybook(item.key)}
                      >
                        {item.title}
                      </Button>
                      <button
                        type="button"
                        className={`ai-playbook-fav${fav ? ' is-on' : ''}`}
                        aria-pressed={fav}
                        title={fav ? t('ai.playbookUnfav') : t('ai.playbookFav')}
                        onClick={() => onToggleFavorite(item.key)}
                      >
                        {fav ? '★' : '☆'}
                      </button>
                    </span>
                  );
                })}
              </div>
            ) : null}
          </>
        ) : (
          <>
            {/* 首轮没有用户消息（体检是自动发起的），补一句让对话读起来完整 */}
            {bubbles[0]?.role !== 'user' ? (
              <ChatBubble role="user">
                {t('ai.chatUserRequest', { host: hostLabel, model: modelLabel })}
              </ChatBubble>
            ) : null}

            {hiddenBubbles > 0 ? (
              <button
                type="button"
                className="ai-load-earlier"
                onClick={() => setShowAllBubbles(true)}
              >
                {t('ai.loadEarlier', { n: hiddenBubbles })}
              </button>
            ) : null}

            {visibleBubbles.map((item) => (
              <TranscriptBubble
                key={item.id}
                role={item.role}
                content={item.content}
              />
            ))}

            {/* 流式增量优先：它比「分析中…」更具体，逐字长出来的感觉也更好 */}
            {streaming ? (
              <ChatBubble role="assistant">
                <RichText text={streaming} />
              </ChatBubble>
            ) : busy && !approval && !lastIsAssistant ? (
              <ChatBubble role="assistant">
                <ProgressBubble progress={progress} />
              </ChatBubble>
            ) : null}

            {/* 等待审批时不显示「分析中」—— 它其实停在等你拍板 */}

            {approval ? (
              <ApprovalCardMemo request={approval} onDone={onApprovalDone} />
            ) : null}

            {/* 审批超时/失效后本轮已结束：给一个续跑入口，不让人重新描述问题 */}
            {!busy && approvalExpired ? (
              <div className="ai-approval-expired">
                <span className="ai-approval-expired-text">
                  {t('ai.approval.expiredHint')}
                </span>
                <Button size="sm" variant="secondary" onClick={onResumeApproval}>
                  {t('ai.approval.resume')}
                </Button>
              </div>
            ) : null}

            {/* 失败原因就地出现在对话末尾：用户视线的落点在这里，而不是页脚 */}
            {error ? (
              <div className="ai-chat-error" role="alert">
                <IconAlert size={14} />
                <span>{error}</span>
              </div>
            ) : null}
          </>
        )}
      </div>

      {/* 输入区固定在卡片底部：对话再长也不会把输入框挤出可视区 */}
      <div className="ai-composer">
        <Input
          ref={inputRef}
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          placeholder={t('ai.chatPlaceholder')}
          disabled={busy}
          onKeyDown={(e) => {
            // 回车发送；Shift+Enter 留给多行习惯
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              if (canSend && draft.trim()) onSend();
            }
          }}
        />
        <Button
          variant="primary"
          size="sm"
          onClick={onSend}
          loading={busy}
          disabled={!canSend || !draft.trim()}
        >
          {t('ai.chatSend')}
        </Button>
      </div>
    </Card>
  );
}

/** 对话气泡。用户侧靠右、AI 侧靠左，配色区分角色 */
function ChatBubble({
  role,
  children,
  pending = false,
}: {
  role: 'user' | 'assistant';
  children: ReactNode;
  pending?: boolean;
}) {
  const isUser = role === 'user';
  return (
    <div className="ai-bubble-row" data-role={isUser ? 'user' : 'assistant'}>
      <div className="ai-bubble" data-role={isUser ? 'user' : 'assistant'}>
        {pending ? (
          <span className="flex items-center gap-6 text-muted">
            <Spinner size={12} />
            {children}
          </span>
        ) : (
          children
        )}
      </div>
    </div>
  );
}

/**
 * 已落库的一条对话气泡。
 *
 * 按**基本类型**收 props 才能让 memo 真正生效：流式期间父组件每 70ms 重渲染一次，
 * 若像以前那样把 `<RichText/>` 作为 children 传进来，元素引用每次都变，memo 永远
 * 命中不了。现在文字没变的气泡会整条跳过（内部 RichText 再按块 memo）。
 */
const TranscriptBubble = memo(function TranscriptBubble({
  role,
  content,
}: {
  role: string;
  content: string;
}) {
  if (role === 'user') {
    // 用户自己打的文字保持纯文本 —— 他要的是原样显示，不是被解释一遍
    return <ChatBubble role="user">{content}</ChatBubble>;
  }
  // 模型的回答带 Markdown 记号（`**加粗**`、列表、行内命令），交给 RichText 排版
  return (
    <ChatBubble role="assistant">
      <RichText text={content} />
    </ChatBubble>
  );
});

/**
 * 右栏的「正在做什么」气泡。
 *
 * 体检不流式出字（整段 JSON），所以后台跑的时候右栏只有等待。这里把左栏的
 * 过程事件压成一句当前阶段 + 最近几个工具调用，让用户看到进展，而不是一个
 * 转圈的「分析中」。左栏「执行过程」仍然保留完整流水。
 */
function ProgressBubble({ progress }: { progress: AiProgress | null }) {
  const t = useT();
  const tools = progress?.tools ?? [];
  const headline = progress?.queued
    ? progress.ahead > 0
      ? t('ai.stageQueuedAhead', { n: progress.ahead, s: progress.etaS })
      : t('ai.stageQueuedFirst', { s: progress.etaS })
    : progress && progress.draftChars > 0
      ? t('ai.reportProgress', { n: progress.draftChars })
      : progress && progress.step > 0
        ? t('ai.stepN', { n: progress.step, max: progress.maxSteps })
        : t('ai.stageCollect');
  return (
    <div className="ai-progress">
      <div className="ai-progress-head">
        <Spinner size={12} />
        <span>{headline}</span>
      </div>
      {tools.length > 0 ? (
        <div className="ai-progress-tools">
          {tools.map((item, index) => (
            <span
              key={`${item.tool}-${index}`}
              className={`ai-progress-tool${
                item.ok === false ? ' is-fail' : item.ok ? ' is-ok' : ''
              }`}
            >
              <span className="ai-progress-mark">
                {item.ok === false ? '✕' : item.ok ? '✓' : '·'}
              </span>
              {item.tool}
            </span>
          ))}
        </div>
      ) : null}
      <div className="ai-progress-hint">{t('ai.runningHint')}</div>
    </div>
  );
}

/* 时间线是**追加**式的：每来一条事件父组件就重渲染一次，若不加 memo，
   前面已经画好的几十行会跟着全量重算（每行还各调一次 useT）。
   event 对象在数组里保持同一引用，所以 memo 能真正命中，只有新行会渲染。 */
const TimelineRow = memo(function TimelineRow({
  event,
}: {
  event: AiChatStreamEvent;
}) {
  const t = useT();

  if (event.type === 'stage') {
    return (
      <TimelineLine
        icon={<IconRefresh size={13} />}
        text={event.stage === 'queued' ? t('ai.stageQueued') : t('ai.stageCollect')}
      />
    );
  }
  if (event.type === 'thinking') {
    return (
      <TimelineLine
        icon={<IconSparkle size={13} />}
        text={t('ai.stepN', { n: event.step, max: event.max_steps })}
      />
    );
  }
  if (event.type === 'report_progress') {
    // 报告正文是整段 JSON，不能边生成边给用户看 —— 只交代「还在长」
    return (
      <TimelineLine
        icon={<IconSparkle size={13} />}
        text={t('ai.reportProgress', { n: event.chars })}
      />
    );
  }
  if (event.type === 'tool_start') {
    return (
      <TimelineLine
        icon={<IconSearch size={13} />}
        text={t('ai.toolStart', {
          tool: event.tool,
          // 有命令原文时不再重复参数：`journalctl -u sshd -n 60` 已经把
          // 「unit=sshd、lines=60」说清楚了，两个都显示只是噪音。
          // 内部工具（读报表/指标）没有命令，这时参数才是唯一的线索。
          args: event.command ? '' : formatArgs(event.args),
        })}
        detail={event.command}
      />
    );
  }
  if (event.type === 'tool_done') {
    return (
      <TimelineLine
        icon={event.ok ? <IconCheck size={13} /> : <IconAlert size={13} />}
        text={t('ai.toolDone', {
          tool: event.tool,
          mark: event.ok ? '✓' : '✕',
          ms: event.elapsed_ms ?? 0,
        })}
      />
    );
  }
  if (event.type === 'error') {
    return <TimelineLine icon={<IconAlert size={13} />} text={event.message} />;
  }
  return null;
});

/**
 * 过程时间线的一行。
 *
 * ``detail`` 放**命令原文**（见 :func:`CommandLine`）：工具名与参数说的是「调了
 * 什么」，命令说的才是「往这台机器里敲了什么」—— 后者才是用户要核对的东西。
 */
function TimelineLine({
  icon,
  text,
  detail,
}: {
  icon: ReactNode;
  text: string;
  detail?: string;
}) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start gap-8 fs-sm">
        <span className="text-muted">{icon}</span>
        {/* min-width: 0 让长文本能正常折行，而不是把这一行撑宽 */}
        <span style={{ minWidth: 0 }}>{text}</span>
      </div>
      {detail ? <CommandLine command={detail} /> : null}
    </div>
  );
}

/** 一条命令的原文。等宽 + 单行横向滚动：长命令折行会把时间线搅成一团。 */
function CommandLine({ command }: { command: string }) {
  return (
    <code className="ai-command" title={command}>
      {command}
    </code>
  );
}

/** 参数压成一行展示；工具参数都很短（一个路径 / 一个单元名），不必展开 JSON */
function formatArgs(args: Record<string, unknown>): string {
  const parts = Object.entries(args || {}).map(([key, value]) => `${key}=${String(value)}`);
  return parts.length > 0 ? '（' + parts.join('、') + '）' : '';
}

/* ------------------------------------------------------------------ 结果区 */

/** 排序列：严重的在前。人打开这份报告是想先知道「有没有要紧事」 */
const SEVERITY_ORDER: Record<AiSeverity, number> = { high: 0, medium: 1, low: 2 };

/**
 * 单条发现，可折叠。
 *
 * 折起来的理由很实在：一份排查常常有五六条发现，全展开就是一堵墙，用户的
 * 注意力被平均分配到「提示」和「严重」上 —— 而这两者的紧迫程度根本不是一回事。
 * 默认只展开严重的，其余收成一行标题，扫一眼就知道有没有事、要不要点开。
 */
function FindingRow({ finding, hostId }: { finding: AiFinding; hostId: string }) {
  const t = useT();
  const navigate = useNavigate();
  const { hasPermission } = useAuth();
  // 默认展开「紧急」项：那一档就是要让人当场看到证据，多一步点击都可能被跳过；
  // 其余收起，避免一整墙把注意力平均分给「提示」和「紧急」。
  const [open, setOpen] = useState(finding.severity === 'high');
  // 待处置的动作：点「处置」后填好，交给弹窗去预览 / 执行
  const [pending, setPending] = useState<{
    action: string;
    params: Record<string, unknown>;
  } | null>(null);

  // 没有详情就不给按钮：点了没反应比没有按钮更让人困惑
  const hasDetails =
    Boolean(finding.phenomenon) ||
    finding.evidence.length > 0 ||
    finding.source_ref.length > 0;

  // 只有结论指回了具体检查项（baseline:<key>）才谈得上「照这条建议去修」，
  // 且本人得有加固权限 —— 否则给了按钮也点不动，反而更困惑
  const baselineKeys = finding.source_ref
    .filter((ref) => ref.startsWith('baseline:'))
    .map((ref) => ref.slice('baseline:'.length));
  const canRemediate = baselineKeys.length > 0 && hasPermission('baseline.manage');

  // 结论出处能映射到某个功能页时，给一个直达入口（去重：同一页只留一个）
  const links = useMemo(() => {
    const seen = new Set<string>();
    const out: Array<{ to: string; labelKey: MessageKey }> = [];
    for (const ref of finding.source_ref) {
      const hit = sourceRoute(ref);
      if (!hit || seen.has(hit.to)) continue;
      seen.add(hit.to);
      out.push(hit);
    }
    return out;
  }, [finding.source_ref]);

  return (
    <div style={{ padding: '12px 0' }}>
      <div className="flex items-center justify-between gap-12 flex-wrap">
        <div className="flex items-center gap-8" style={{ minWidth: 0 }}>
          <Badge
            variant={SEVERITY_BADGE[finding.severity] ?? 'neutral'}
            size="sm"
            dot
          >
            {severityLabel(t, finding.severity)}
          </Badge>
          <span style={{ fontWeight: 600, fontSize: 15 }}>{finding.title}</span>
        </div>
        {/* 默认只给「有什么问题 + 该怎么办」，现象与证据收在按钮后面 ——
            不是每个人都想看命令输出，但每个人都得知道下一步做什么 */}
        {hasDetails ? (
          <Button
            variant="ghost"
            size="sm"
            icon={
              open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />
            }
            onClick={() => setOpen((prev) => !prev)}
            aria-expanded={open}
          >
            {open ? t('ai.hideDetails') : t('ai.viewDetails')}
          </Button>
        ) : null}
      </div>

      {/* 建议常驻：排查最有用的产出就是「怎么办」，藏进折叠里等于没给 */}
      {finding.suggestion ? (
        <div className="mt-8 fs-sm" style={{ lineHeight: 1.7 }}>
          <span className="fs-xs text-muted" style={{ marginRight: 6 }}>
            {t('ai.suggestion')}
          </span>
          {finding.suggestion}
        </div>
      ) : null}

      {/* 行动区：能一键修的给「处置」，能定位的给「跳转」——
          AI 说该怎么办，用户点一下就能到达，别让人自己去菜单里翻 */}
      {canRemediate || links.length > 0 ? (
        <div className="flex items-center gap-8 flex-wrap" style={{ marginTop: 8 }}>
          {canRemediate ? (
            <Button
              variant="primary"
              size="sm"
              onClick={() =>
                setPending({
                  action: 'baseline_hardening',
                  params: { host_id: hostId, keys: baselineKeys },
                })
              }
            >
              {t('ai.remediation.action')}
            </Button>
          ) : null}
          {links.length > 0 ? (
            <>
              <span className="fs-xs text-muted">{t('ai.gotoLabel')}</span>
              {links.map((link) => (
                <Button
                  key={link.to}
                  variant="ghost"
                  size="sm"
                  onClick={() => navigate(link.to)}
                >
                  {t(link.labelKey)}
                </Button>
              ))}
            </>
          ) : null}
        </div>
      ) : null}

      {open ? (
        <div className="flex flex-col gap-12" style={{ marginTop: 10 }}>
          {finding.phenomenon ? (
            <div style={{ lineHeight: 1.75 }}>{finding.phenomenon}</div>
          ) : null}

          {finding.evidence.length > 0 ? (
            <div>
              <div className="fs-xs text-muted">{t('ai.evidence')}</div>
              {/* 证据多半是命令输出：必须等宽 + 保留换行，
                  否则数字列对不齐，df -h 那种表根本读不成 */}
              <div
                className="mono fs-xs"
                style={{
                  marginTop: 4,
                  padding: '8px 10px',
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border-muted)',
                  borderRadius: 6,
                  whiteSpace: 'pre-wrap',
                  maxHeight: 240,
                  overflow: 'auto',
                  lineHeight: 1.6,
                }}
              >
                {finding.evidence.join('\n')}
              </div>
            </div>
          ) : null}

          {finding.source_ref.length > 0 ? (
            <div className="flex items-center gap-8 flex-wrap fs-xs text-muted">
              {finding.source_ref.map((ref, index) => (
                <span key={index}>{ref}</span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {pending ? (
        <RemediationDialog
          action={pending.action}
          params={pending.params}
          onClose={() => setPending(null)}
        />
      ) : null}
    </div>
  );
}

/**
 * 报告卡（控制台下方，全宽）。
 *
 * 首轮体检的产出落在这里：几个紧急、几个待改进、总体判断、逐条发现。
 * 与对话栏的分工是「对话讲过程、报告讲结论」—— 追问不必把整份报告重读一遍，
 * 而报告本身要能当一份可核对的文档看。
 */
function ReportCard({
  report,
  meta,
  previousFindings,
  onRerun,
}: {
  report: AiResult;
  /** 模型 / 耗时 / token / 工具数等元信息 */
  meta: ReportMeta | null;
  /** 上一次报告的发现；用来做「重新体检」的差异对比 */
  previousFindings: AiFinding[] | null;
  onRerun: () => void;
}) {
  const t = useT();

  const counts = useMemo(() => {
    const acc: Record<AiSeverity, number> = { high: 0, medium: 0, low: 0 };
    for (const item of report.findings) {
      acc[item.severity] = (acc[item.severity] ?? 0) + 1;
    }
    return acc;
  }, [report.findings]);

  const ordered = useMemo(
    () =>
      [...report.findings].sort(
        (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity],
      ),
    [report.findings],
  );

  // 与上一次报告对比：按标题（去空格、忽略大小写）算新增 / 消除。
  // 回答的是「修完之后好点没有」—— 这是重新体检的意义。
  const diff = useMemo(() => {
    if (!previousFindings) return null;
    const norm = (s: string) => s.trim().toLowerCase();
    const cur = new Set(report.findings.map((f) => norm(f.title)));
    const old = new Set(previousFindings.map((f) => norm(f.title)));
    let added = 0;
    let resolved = 0;
    for (const key of cur) if (!old.has(key)) added += 1;
    for (const key of old) if (!cur.has(key)) resolved += 1;
    return { added, resolved };
  }, [previousFindings, report.findings]);

  const [copied, setCopied] = useState(false);

  /** 把报告摊成 Markdown：便于贴进工单 / 聊天转交，而不是让人对着截图抄 */
  const markdown = useMemo(() => {
    const lines: string[] = [];
    lines.push(`# ${t('ai.summaryTitle')}`);
    if (meta?.hostLabel) lines.push(`- ${meta.hostLabel}`);
    if (meta?.at) lines.push(`- ${formatDateTime(meta.at)}`);
    if (meta?.model) lines.push(`- ${t('ai.metaModel', { model: meta.model })}`);
    lines.push('', report.summary, '');
    lines.push(`## ${t('ai.findingsTitle')}（${ordered.length}）`);
    for (const finding of ordered) {
      lines.push('', `### [${severityLabel(t, finding.severity)}] ${finding.title}`);
      if (finding.suggestion) {
        lines.push(`- ${t('ai.suggestion')}：${finding.suggestion}`);
      }
      if (finding.phenomenon) lines.push(`- ${finding.phenomenon}`);
      if (finding.evidence.length > 0) {
        lines.push('', '```', ...finding.evidence, '```');
      }
      if (finding.source_ref.length > 0) {
        lines.push(`- ${t('ai.evidence')}：${finding.source_ref.join('、')}`);
      }
    }
    if (report.data_gaps.length > 0) {
      lines.push('', `## ${t('ai.dataGaps')}`);
      for (const gap of report.data_gaps) lines.push(`- ${gap}`);
    }
    return lines.join('\n');
  }, [report, meta, ordered, t]);

  async function copyReport() {
    try {
      await navigator.clipboard.writeText(markdown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* 剪贴板不可用（非 HTTPS / 无权限）时不打断用户 */
    }
  }

  function downloadReport() {
    const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `ai-report-${meta?.hostLabel || 'host'}.md`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <Card>
        <CardHeader
          title={t('ai.summaryTitle')}
          subtitle={
            meta?.hostLabel
              ? `${meta.hostLabel}${meta.at ? ' · ' + formatDateTime(meta.at) : ''}`
              : undefined
          }
          actions={
            <div className="flex items-center gap-8">
              <Badge variant="neutral" size="sm">
                {t('ai.confidence')}：{confidenceLabel(t, report.confidence)}
              </Badge>
              {/* 结论要能带走：贴进工单、发给同事，而不是让人对着截图抄 */}
              <Button
                variant="ghost"
                size="sm"
                icon={<IconCopy size={14} />}
                onClick={() => void copyReport()}
              >
                {copied ? t('ai.reportCopied') : t('ai.reportCopy')}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                icon={<IconDownload size={14} />}
                onClick={downloadReport}
              >
                {t('ai.reportExport')}
              </Button>
              {/* 重新体检 = 新开一条会话重跑一遍；这次与上次的发现逐条对比 */}
              <Button
                variant="ghost"
                size="sm"
                icon={<IconRefresh size={14} />}
                onClick={onRerun}
              >
                {t('ai.reinspect')}
              </Button>
            </div>
          }
        />
        {/* 先看规模：几个紧急、几个待改进 —— 比一上来读一整段话快得多 */}
        <div className="flex items-center gap-16 flex-wrap">
          {(['high', 'medium', 'low'] as AiSeverity[]).map((level) =>
            counts[level] ? (
              <span key={level} className="flex items-center gap-6">
                <Badge variant={SEVERITY_BADGE[level]} size="sm" dot>
                  {severityLabel(t, level)}
                </Badge>
                <span style={{ fontSize: 22, fontWeight: 700, lineHeight: 1 }}>
                  {counts[level]}
                </span>
              </span>
            ) : null,
          )}
        </div>

        {/* 差异：修完之后问题少了没有，一眼就能看出来 */}
        {diff ? (
          <div className="mt-12 flex items-center gap-8 flex-wrap fs-sm">
            <span className="text-muted">{t('ai.diffTitle')}</span>
            {diff.added > 0 ? (
              <Badge variant="warning" size="sm">
                {t('ai.diffAdded', { n: diff.added })}
              </Badge>
            ) : null}
            {diff.resolved > 0 ? (
              <Badge variant="success" size="sm">
                {t('ai.diffResolved', { n: diff.resolved })}
              </Badge>
            ) : null}
            {diff.added === 0 && diff.resolved === 0 ? (
              <Badge variant="neutral" size="sm">
                {t('ai.diffSame')}
              </Badge>
            ) : null}
          </div>
        ) : null}

        {/* 结论同样走 RichText：它也是模型写的散文，可能带 **加粗** 与行内命令 */}
        <div className="mt-12" style={{ fontSize: 15, lineHeight: 1.75 }}>
          <RichText text={report.summary} />
        </div>

        {/* 元信息摊成一行：模型、耗时、开销。这些是判断「这份结论值不值信」的
            参照，不该缩在角落里当脚注 */}
        {meta ? (
          <div className="mt-12 flex items-center gap-12 flex-wrap fs-xs text-muted">
            {meta.model ? (
              <span>{t('ai.metaModel', { model: meta.model })}</span>
            ) : null}
            {meta.ms != null ? (
              <span>{t('ai.metaTime', { ms: meta.ms })}</span>
            ) : null}
            <span>{t('ai.metaTokens', { tokens: meta.tokens ?? '-' })}</span>
            {meta.tools ? <span>{t('ai.metaTools', { n: meta.tools })}</span> : null}
          </div>
        ) : null}
      </Card>

      {ordered.length === 0 ? (
        <Card>
          <EmptyState title={t('ai.noFindings')} />
        </Card>
      ) : (
        /* 全部收进一张卡：五六张卡竖着排，翻起来像在刷信息流，
           看不出「一共几条、哪条要紧」 */
        <Card>
          <CardHeader
            title={`${t('ai.findingsTitle')}（${ordered.length}）`}
            subtitle={t('ai.findingsHint')}
          />
          <div className="flex flex-col">
            {ordered.map((item, index) => (
              <FindingRow
                key={`${item.title}-${index}`}
                finding={item}
                hostId={meta?.hostId ?? ''}
              />
            ))}
          </div>
        </Card>
      )}

      {report.data_gaps.length > 0 ? (
        <Card>
          <CardHeader title={t('ai.dataGaps')} subtitle={t('ai.dataGapsHint')} />
          <ul className="desc-list" style={{ paddingLeft: 18 }}>
            {report.data_gaps.map((gap, i) => (
              <li key={i} className="desc-item fs-sm">
                {gap}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </>
  );
}

/* ------------------------------------------------------------------ 排查记录 */

const STATUS_BADGE: Record<string, BadgeVariant> = {
  done: 'success',
  failed: 'danger',
  running: 'warning',
};

const SESSION_STATUSES = ['done', 'failed', 'running'];

/** 状态词条只覆盖已知值；库里出现别的值时原样显示，别编一个不存在的词条键 */
/** 报告卡：内容不变就不该被流式帧反复重绘（发现项可能几十条） */
const ReportCardMemo = memo(ReportCard);

function statusLabel(t: TFunc, status: string): string {
  return SESSION_STATUSES.includes(status)
    ? t(`ai.status.${status}` as MessageKey)
    : status || '-';
}

const PAGE_SIZE = 15;

/**
 * 工具调用明细（排查记录）。
 *
 * 这是整个 AI 功能里最该存在的一页：它回答的是「AI 到底在我机器上跑了什么」。
 * 结论可以不信，但过程必须能查 —— 少了这个，用户只能选择相信或拒绝；有了它，
 * 才能核对。数据来自 ai_sessions / ai_tool_calls 两张审计表，普通用户只看到
 * 自己的记录，管理员看全部。
 */
function RecordsPanel({
  hosts,
  onOpenConversation,
}: {
  hosts: Array<{ label: string; value: string }>;
  /** 这条记录属于某条会话时，允许跳回去继续对话 */
  onOpenConversation: (conversationId: string) => void;
}) {
  const t = useT();
  const [hostId, setHostId] = useState('');
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState('');
  /**
   * 两种视角：**运行记录**回答「AI 在我机器上跑了什么命令」（审计），
   * **会话**回答「我和它聊了哪些、结论是什么」（回顾）。同一批数据，两种问法。
   */
  const [view, setView] = useState<'runs' | 'conversations'>('runs');

  const sessionsQuery = useQuery({
    queryKey: ['ai', 'sessions', hostId, page],
    queryFn: () =>
      aiApi.sessions({ limit: PAGE_SIZE, offset: page * PAGE_SIZE, host_id: hostId }),
  });

  const detailQuery = useQuery({
    queryKey: ['ai', 'session', openId],
    queryFn: () => aiApi.sessionDetail(openId),
    enabled: Boolean(openId),
  });

  const total = sessionsQuery.data?.total ?? 0;
  const rows = sessionsQuery.data?.items ?? [];
  const maxPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  // 这条记录所属的会话（一次性排查 / 旧记录为空）；非空时给一个「回去继续对话」的入口
  const detailConversationId = detailQuery.data?.session.conversation_id || '';

  // 落库的结论（summary + findings）。旧记录可能没有 —— 那时退化成只展示工具明细。
  const sessionResult = useMemo<AiResult | null>(() => {
    const raw = detailQuery.data?.session.result;
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as AiResult;
      return parsed && Array.isArray(parsed.findings) ? parsed : null;
    } catch {
      return null;
    }
  }, [detailQuery.data]);

  const columns: Column<AiSession>[] = [
    {
      key: 'created',
      header: t('ai.recTime'),
      width: 170,
      render: (row) => formatDateTime(row.created),
    },
    {
      key: 'host',
      header: t('ai.recHost'),
      render: (row) => row.host_name || row.host_id || '-',
    },
    {
      key: 'model',
      header: t('ai.recModel'),
      render: (row) => row.model || row.provider || '-',
    },
    {
      key: 'status',
      header: t('ai.recStatus'),
      width: 110,
      render: (row) => (
        <Badge variant={STATUS_BADGE[row.status] ?? 'neutral'} size="sm">
          {statusLabel(t, row.status)}
        </Badge>
      ),
    },
    {
      key: 'steps',
      header: t('ai.recSteps'),
      width: 80,
      align: 'right',
      render: (row) => row.steps ?? 0,
    },
    {
      key: 'tools',
      header: t('ai.recTools'),
      width: 90,
      align: 'right',
      render: (row) => row.tool_calls ?? 0,
    },
    {
      key: 'tokens',
      header: t('ai.recTokens'),
      width: 100,
      align: 'right',
      render: (row) => row.total_tokens || '-',
    },
    {
      key: 'duration',
      header: t('ai.recDuration'),
      width: 110,
      align: 'right',
      render: (row) => (row.duration_ms ? `${row.duration_ms} ms` : '-'),
    },
  ];

  return (
    <>
      <Card>
        <SegmentedControl
          value={view}
          onChange={(next) => {
            setView(next);
            setPage(0);
            setOpenId('');
          }}
          options={[
            { label: t('ai.recViewRuns'), value: 'runs' },
            { label: t('ai.recViewConversations'), value: 'conversations' },
          ]}
          ariaLabel={t('ai.tabRecords')}
        />
        <div
          className="flex items-end justify-between gap-12 flex-wrap"
          style={{ marginTop: 12 }}
        >
          <div style={{ flex: '1 1 260px', maxWidth: 360 }}>
            <Field label={t('ai.recFilterHost')}>
              <Select
                value={hostId}
                onChange={(e) => {
                  setHostId(e.target.value);
                  setPage(0);
                  setOpenId('');
                }}
                options={[{ label: t('ai.recAllHosts'), value: '' }, ...hosts]}
              />
            </Field>
          </div>
          {/* 导出的是「运行记录」宽表，会话视角下没有对应物，就不显示 */}
          {view === 'runs' ? (
            <div className="flex items-center gap-12 flex-wrap">
              <span className="fs-sm text-muted">{t('ai.recTotal', { n: total })}</span>
              {/* 走 window.open 而不是取回来拼 Blob：附件下载交给浏览器接管，
                  不必把整张表读进内存；认证走 cookie，新标签页也带得上。 */}
              <Button
                variant="secondary"
                size="sm"
                icon={<IconDownload size={15} />}
                disabled={total === 0}
                onClick={() =>
                  window.open(exportUrl('ai', { host_id: hostId || undefined }), '_blank')
                }
                title={t('ai.recExportHint')}
              >
                {t('ai.recExport')}
              </Button>
            </div>
          ) : null}
        </div>
      </Card>

      {view === 'conversations' ? (
        <ConversationsPanel
          // 换主机就重挂：内部页码与展开项一起归零
          key={hostId}
          hostId={hostId}
          onOpenConversation={onOpenConversation}
        />
      ) : (
        <>
      

      <Card>
        {sessionsQuery.isError ? (
          <ErrorState
            title={t('ai.failed')}
            message={errorMessage(sessionsQuery.error)}
            onRetry={() => void sessionsQuery.refetch()}
          />
        ) : (
          <Table<AiSession>
            caption={t('ai.tabRecords')}
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            loading={sessionsQuery.isLoading}
            emptyTitle={t('ai.recEmptyTitle')}
            emptyDescription={t('ai.recEmptyDesc')}
            onRowClick={(row) => setOpenId((prev) => (prev === row.id ? '' : row.id))}
            isRowSelected={(row) => row.id === openId}
            rowTitle={() => t('ai.recClickHint')}
          />
        )}

        {total > PAGE_SIZE ? (
          <div className="flex items-center justify-end gap-8 mt-12">
            <Button
              variant="secondary"
              size="sm"
              disabled={page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              {t('ai.recPrev')}
            </Button>
            <span className="fs-sm text-muted">
              {page + 1} / {maxPage + 1}
            </span>
            <Button
              variant="secondary"
              size="sm"
              disabled={page >= maxPage}
              onClick={() => setPage((p) => Math.min(maxPage, p + 1))}
            >
              {t('ai.recNext')}
            </Button>
          </div>
        ) : null}
      </Card>

      {openId ? (
        <Card>
          <CardHeader
            title={t('ai.recDetail')}
            subtitle={t('ai.recDetailHint')}
            actions={
              <div className="flex items-center gap-8">
                {/* 这条记录属于某条会话时，给一个「回去接着聊」的入口 ——
                    记录只能看到「跑了什么」，接着聊才是看结论的入口 */}
                {detailConversationId ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    icon={<IconChevronRight size={14} />}
                    onClick={() => onOpenConversation(detailConversationId)}
                    title={t('ai.recOpenConversationHint')}
                  >
                    {t('ai.recOpenConversation')}
                  </Button>
                ) : null}
                <Button variant="ghost" size="sm" onClick={() => setOpenId('')}>
                  {t('ai.recClose')}
                </Button>
              </div>
            }
          />
          {detailQuery.isLoading ? (
            <div className="flex flex-col gap-8">
              <Skeleton height={16} width="40%" />
              <Skeleton height={14} />
              <Skeleton height={14} width="70%" />
            </div>
          ) : (
            <div className="flex flex-col gap-16">
              {/* 先给结论、再给过程：回看一次排查，最想知道的是「当时判断是什么」，
                  其次才是「它跑了哪些命令」。结论是落库的（ai_sessions.result）。 */}
              {sessionResult ? (
                <div className="flex flex-col gap-8">
                  <div className="fs-xs text-muted">{t('ai.recConclusion')}</div>
                  <div style={{ fontSize: 15, lineHeight: 1.75 }}>
                    {sessionResult.summary}
                  </div>
                  {sessionResult.findings.length > 0 ? (
                    <div className="flex flex-col gap-6">
                      {sessionResult.findings.map((item, index) => (
                        <div
                          key={`${item.title}-${index}`}
                          className="flex items-center gap-8 flex-wrap fs-sm"
                        >
                          <Badge
                            variant={SEVERITY_BADGE[item.severity] ?? 'neutral'}
                            size="sm"
                            dot
                          >
                            {severityLabel(t, item.severity)}
                          </Badge>
                          <span>{item.title}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}

              {(detailQuery.data?.calls ?? []).length === 0 ? (
                // 有结论、只是没调工具时，不必占一整块空状态，一句话带过即可
                sessionResult ? (
                  <span className="fs-xs text-muted">{t('ai.recNoCalls')}</span>
                ) : (
                  <EmptyState title={t('ai.recNoCalls')} />
                )
              ) : (
                <div className="flex flex-col gap-12">
                  {(detailQuery.data?.calls ?? []).map((call) => (
                    <div key={call.id} className="flex flex-col gap-6">
                      <div className="flex items-center gap-8 fs-sm flex-wrap">
                        <Badge variant={call.ok ? 'success' : 'warning'} size="sm">
                          {call.ok ? '✓' : '✕'}
                        </Badge>
                        <span className="mono">{call.tool}</span>
                        <span className="text-muted">{call.args}</span>
                        {call.elapsed_ms ? (
                          <span className="fs-xs text-muted">{call.elapsed_ms} ms</span>
                        ) : null}
                      </div>
                      <pre
                        className="mono fs-xs"
                        style={{
                          whiteSpace: 'pre-wrap',
                          margin: 0,
                          maxHeight: 220,
                          overflow: 'auto',
                        }}
                      >
                        {call.output || '-'}
                      </pre>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </Card>
      ) : null}
        </>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ 会话视角 */

/**
 * 记录页的「会话」视角：一条会话一行，展开看它聊了什么。
 *
 * 与「运行记录」的分工：那边是审计（AI 在你机器上跑过哪些命令），这边是回顾
 * （你问过什么、它答了什么、结论是什么）。同一条会话里的工具调用也是审计的一
 * 部分，所以展开后一并列出。
 */
function ConversationsPanel({
  hostId,
  onOpenConversation,
}: {
  hostId: string;
  onOpenConversation: (conversationId: string) => void;
}) {
  const t = useT();
  const [page, setPage] = useState(0);
  const [openId, setOpenId] = useState('');

  const listQuery = useQuery({
    queryKey: ['ai', 'conversations', hostId, page],
    queryFn: () =>
      aiApi.conversations({
        host_id: hostId || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      }),
  });

  const detailQuery = useQuery({
    queryKey: ['ai', 'conversation', openId],
    queryFn: () => aiApi.conversation(openId),
    enabled: Boolean(openId),
  });

  const total = listQuery.data?.total ?? 0;
  const rows = listQuery.data?.items ?? [];
  const maxPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  // context 是喂给模型的 [数据] 段，不是人说的话，不展示
  const messages = (detailQuery.data?.messages ?? []).filter(
    (item) => item.role !== 'context',
  );

  const columns: Column<AiConversation>[] = [
    {
      key: 'updated',
      header: t('ai.recTime'),
      width: 170,
      render: (row) => formatDateTime(row.updated),
    },
    {
      key: 'host',
      header: t('ai.recHost'),
      render: (row) => row.host_name || row.host_id || '-',
    },
    {
      key: 'provider',
      header: t('ai.recModel'),
      render: (row) => row.provider || '-',
    },
    {
      key: 'runs',
      header: t('ai.recConvRuns'),
      width: 90,
      align: 'right',
      render: (row) => row.runs ?? 0,
    },
    {
      key: 'report',
      header: t('ai.recConvReport'),
      width: 90,
      render: (row) =>
        row.report ? (
          <Badge variant="success" size="sm">
            {t('ai.recConvHasReport')}
          </Badge>
        ) : (
          <Badge variant="neutral" size="sm">
            {t('ai.recConvNoReport')}
          </Badge>
        ),
    },
  ];

  return (
    <>
      <Card>
        {listQuery.isError ? (
          <ErrorState
            title={t('ai.failed')}
            message={errorMessage(listQuery.error)}
            onRetry={() => void listQuery.refetch()}
          />
        ) : (
          <Table<AiConversation>
            caption={t('ai.recViewConversations')}
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            loading={listQuery.isLoading}
            emptyTitle={t('ai.recConvEmptyTitle')}
            emptyDescription={t('ai.recConvEmptyDesc')}
            onRowClick={(row) => setOpenId((prev) => (prev === row.id ? '' : row.id))}
            isRowSelected={(row) => row.id === openId}
            rowTitle={() => t('ai.recClickHint')}
          />
        )}

        {total > PAGE_SIZE ? (
          <div className="flex items-center justify-end gap-8 mt-12">
            <Button
              variant="secondary"
              size="sm"
              disabled={page <= 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              {t('ai.recPrev')}
            </Button>
            <span className="fs-sm text-muted">
              {page + 1} / {maxPage + 1}
            </span>
            <Button
              variant="secondary"
              size="sm"
              disabled={page >= maxPage}
              onClick={() => setPage((p) => Math.min(maxPage, p + 1))}
            >
              {t('ai.recNext')}
            </Button>
          </div>
        ) : null}
      </Card>

      {openId ? (
        <Card>
          <CardHeader
            title={t('ai.recConvDetail')}
            subtitle={t('ai.recConvDetailHint')}
            actions={
              <div className="flex items-center gap-8">
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<IconChevronRight size={14} />}
                  onClick={() => onOpenConversation(openId)}
                  title={t('ai.recOpenConversationHint')}
                >
                  {t('ai.recOpenConversation')}
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setOpenId('')}>
                  {t('ai.recClose')}
                </Button>
              </div>
            }
          />
          {detailQuery.isLoading ? (
            <div className="flex flex-col gap-8">
              <Skeleton height={16} width="40%" />
              <Skeleton height={14} />
              <Skeleton height={14} width="70%" />
            </div>
          ) : messages.length === 0 ? (
            <EmptyState title={t('ai.recConvEmptyMessages')} />
          ) : (
            <div className="flex flex-col gap-10">
              {messages.map((item) => (
                <div key={item.id} className="flex flex-col gap-4">
                  <span className="fs-xs text-muted">
                    {item.role === 'user'
                      ? t('ai.recRoleUser')
                      : item.role === 'tool'
                        ? t('ai.recRoleTool')
                        : t('ai.recRoleAssistant')}
                    {item.role === 'tool' && item.tool_name
                      ? ' · ' + item.tool_name
                      : ''}
                    {item.role === 'tool' ? (item.ok ? ' ✓' : ' ✕') : ''}
                  </span>
                  <div
                    className={item.role === 'tool' ? 'mono fs-xs' : 'fs-sm'}
                    style={{
                      lineHeight: 1.7,
                      whiteSpace: 'pre-wrap',
                      maxHeight: item.role === 'tool' ? 160 : undefined,
                      overflow: 'auto',
                    }}
                  >
                    {item.content || '-'}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      ) : null}
    </>
  );
}

/* ------------------------------------------------------------------ 能力说明 */

/**
 * 「这个助手能做什么」。
 *
 * 这是个会**登录到用户服务器上执行命令**的功能。让人在按下按钮之前就知道边界，
 * 比把说明藏起来要好 —— 不清楚它能干什么时，人要么不敢用，要么误以为它什么都能干。
 *
 * 工具清单由后端从 aitools 实时生成、并**按当前用户的权限过滤**
 * （/api/ai/capabilities）：看不到的工具，模型就算被诱导着点名也跑不起来。
 * 命令白名单 / 参数黑名单这类判定规则只留在服务端 —— 页面展示它们对用户没有
 * 帮助，对探测者却是一份现成的绕过指南。
 */
/** 一条「能做什么 + 什么场景用」。序号与工作台的分节编号是同一套视觉语言 */
function CapFeature({
  n,
  title,
  desc,
  scene,
}: {
  n: string;
  title: string;
  desc: string;
  scene: string;
}) {
  return (
    <div className="ai-cap-card">
      <div className="ai-cap-card-title">
        <span className="ai-cap-card-num">{n}</span>
        {title}
      </div>
      <div className="ai-cap-card-desc">{desc}</div>
      <div className="ai-cap-card-scene">{scene}</div>
    </div>
  );
}

function CapabilitiesModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const t = useT();
  const query = useQuery({
    queryKey: ['ai', 'capabilities'],
    queryFn: aiApi.capabilities,
    enabled: open,
  });

  const tools = query.data?.tools ?? [];
  const internal = tools.filter((item) => item.kind === 'internal');
  const host = tools.filter((item) => item.kind === 'host');
  /* 虚拟机内动作：要签了授权才会真的下发，但说明页得列出来 ——
     用户判断「要不要授权」靠的就是这份清单 */
  const guest = tools.filter((item) => item.kind === 'guest');

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('ai.capabilities')}
      description={t('ai.capSubtitle')}
      size="lg"
      footer={
        <Button variant="secondary" onClick={onClose}>
          {t('common.close')}
        </Button>
      }
    >
      {query.isLoading ? (
        <div className="flex flex-col gap-8">
          <Skeleton height={16} />
          <Skeleton height={16} width="80%" />
          <Skeleton height={16} width="60%" />
        </div>
      ) : (
        <div className="ai-cap">
          {/* 先说「能干什么、什么时候用」，再展开工具清单 ——
              顺序反过来，用户看到的是一串函数名，反而不知道这东西能解决什么问题。 */}
          <div className="ai-cap-hero">
            <span className="ai-cap-kicker">{t('ai.capKicker')}</span>
            <p>{t('ai.capIntro')}</p>
          </div>

          <div className="ai-cap-grid">
            <CapFeature
              n="01"
              title={t('ai.capFeature1Title')}
              desc={t('ai.capFeature1Desc')}
              scene={t('ai.capFeature1Scene')}
            />
            <CapFeature
              n="02"
              title={t('ai.capFeature2Title')}
              desc={t('ai.capFeature2Desc')}
              scene={t('ai.capFeature2Scene')}
            />
            <CapFeature
              n="03"
              title={t('ai.capFeature3Title')}
              desc={t('ai.capFeature3Desc')}
              scene={t('ai.capFeature3Scene')}
            />
            <CapFeature
              n="04"
              title={t('ai.capFeature4Title')}
              desc={t('ai.capFeature4Desc')}
              scene={t('ai.capFeature4Scene')}
            />
            <CapFeature
              n="05"
              title={t('ai.capFeature5Title')}
              desc={t('ai.capFeature5Desc')}
              scene={t('ai.capFeature5Scene')}
            />
            <CapFeature
              n="06"
              title={t('ai.capFeature6Title')}
              desc={t('ai.capFeature6Desc')}
              scene={t('ai.capFeature6Scene')}
            />
          </div>

          <div className="ai-cap-scenes">
            <span className="ai-cap-scene-label">{t('ai.capSceneTitle')}</span>
            <span className="ai-cap-chip">{t('ai.capSceneSlow')}</span>
            <span className="ai-cap-chip">{t('ai.capSceneDisk')}</span>
            <span className="ai-cap-chip">{t('ai.capSceneCpu')}</span>
            <span className="ai-cap-chip">{t('ai.capSceneNetwork')}</span>
            <span className="ai-cap-chip">{t('ai.capSceneInstall')}</span>
            <span className="ai-cap-chip">{t('ai.capSceneConfig')}</span>
          </div>

          {/* 工具清单按权限实时生成；默认收起，避免一屏几十个函数名盖住上面的场景 */}
          <details className="ai-cap-tools">
            <summary>
              {t('ai.capToolsTitle')} · {tools.length}
            </summary>

            {internal.length ? (
              <div className="ai-cap-tool-group">
                <span className="ai-cap-tool-group-title">
                  {t('ai.capInternalTitle')}（{internal.length}）
                </span>
                <span className="ai-cap-tool-group-desc">
                  {t('ai.capInternalBody')}
                </span>
                {internal.map((item) => (
                  <div key={item.name} className="ai-cap-tool">
                    <span className="ai-cap-tool-name">{item.name}</span>
                    <span className="ai-cap-tool-desc">{item.description}</span>
                  </div>
                ))}
              </div>
            ) : null}

            {host.length ? (
              <div className="ai-cap-tool-group">
                <span className="ai-cap-tool-group-title">
                  {t('ai.capHostTitle')}（{host.length}）
                </span>
                <span className="ai-cap-tool-group-desc">{t('ai.capHostBody')}</span>
                {host.map((item) => (
                  <div key={item.name} className="ai-cap-tool">
                    <span className="ai-cap-tool-name">{item.name}</span>
                    <span className="ai-cap-tool-desc">{item.description}</span>
                  </div>
                ))}
              </div>
            ) : null}

            {guest.length ? (
              <div className="ai-cap-tool-group">
                <span className="ai-cap-tool-group-title">
                  {t('ai.capGuestTitle')}（{guest.length}）
                </span>
                <span className="ai-cap-tool-group-desc">
                  {t('ai.capGuestBody')}
                </span>
                {guest.map((item) => (
                  <div key={item.name} className="ai-cap-tool">
                    <span className="ai-cap-tool-name">{item.name}</span>
                    <span className="ai-cap-tool-desc">{item.description}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </details>

          {/* 命令白名单 / 参数黑名单 / 路径白名单**不再下发前端**：它们是服务端
              的判定依据，页面只需要「能做什么」。清单留在服务端也少一份可被
              逐条试探的绕过指南（V2-04）。 */}
          <p className="fs-xs text-muted">{t('ai.capPolicyServerSide')}</p>

          <Notice tone="warning" title={t('ai.capLimitTitle')}>
            {t('ai.capLimitBody', {
              steps: query.data?.max_steps ?? 0,
              seconds: query.data?.timeout_seconds ?? 0,
            })}
          </Notice>
        </div>
      )}
    </Modal>
  );
}

/* ------------------------------------------------------------------ 选人下拉 */

/**
 * 可搜索的**多选**下拉，用来挑授权给谁。
 *
 * 为什么不用现成的 ``Select``：它是原生 select，没法边打字边过滤。用户一多，
 * 在一个长列表里翻找某个名字是纯粹的体力活 —— 而授权这种操作，选错人的代价
 * 不小（等于把你的模型额度和数据访问交出去），所以给它一个搜索框是值得的。
 *
 * 选中后**不收起**，因为授权通常是连续勾几个人。
 */
function UserPicker({
  users,
  selected,
  onToggle,
  placeholder,
  emptyText,
}: {
  users: string[];
  selected: string[];
  onToggle: (name: string) => void;
  placeholder: string;
  emptyText: string;
}) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  // 点外面或按 Esc 收起：浮层常驻会挡住下面的字段，也让人以为还在编辑
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!boxRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const keyword = query.trim().toLowerCase();
  const matched = keyword
    ? users.filter((name) => name.toLowerCase().includes(keyword))
    : users;

  return (
    <div ref={boxRef} style={{ position: 'relative' }}>
      <Input
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        aria-expanded={open}
        role="combobox"
      />
      {open ? (
        <div
          role="listbox"
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            left: 0,
            right: 0,
            zIndex: 30,
            maxHeight: 208,
            overflowY: 'auto',
            background: 'var(--bg-surface)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            boxShadow: '0 8px 24px rgba(16, 24, 40, 0.12)',
            padding: 4,
          }}
        >
          {matched.length === 0 ? (
            <div className="fs-xs text-muted" style={{ padding: '8px 10px' }}>
              {emptyText}
            </div>
          ) : (
            matched.map((name) => {
              const on = selected.includes(name);
              return (
                <button
                  key={name}
                  type="button"
                  role="option"
                  aria-selected={on}
                  onClick={() => onToggle(name)}
                  style={
                    {
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      width: '100%',
                      padding: '7px 10px',
                      border: 0,
                      borderRadius: 6,
                      background: on ? 'var(--bg-hover)' : 'transparent',
                      color: 'inherit',
                      font: 'inherit',
                      fontSize: 13,
                      cursor: 'pointer',
                      textAlign: 'left',
                    } as React.CSSProperties
                  }
                >
                  <span style={{ width: 14 }}>{on ? '✓' : ''}</span>
                  <span>{name}</span>
                </button>
              );
            })
          )}
        </div>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ 配置面板 */

function SettingsModal({
  open,
  onClose,
  onSaved,
}: {
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const t = useT();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const configQuery = useQuery({
    queryKey: ['ai', 'config'],
    // 管理员读全平台配置；普通用户只读自己的（外加平台共享的只读项）。
    // 不是权限宽严的问题：别人配的模型背后是别人的 key，看得见就等于能用。
    queryFn: () => (isAdmin ? aiApi.config() : aiApi.myProviders()),
    enabled: open,
  });

  // 授权名单要挑人；只有管理员会用，普通用户不必拉这份列表
  const usersQuery = useQuery({
    queryKey: ['users'],
    queryFn: () => usersApi.list(),
    enabled: open && isAdmin,
  });
  // 下拉要的是字符串列表；其余字段在这里用不上
  const userNames = useMemo(
    () => (usersQuery.data ?? []).map((item) => item.username),
    [usersQuery.data],
  );

  const [enabled, setEnabled] = useState(false);
  const [hours, setHours] = useState(24);
  /** 单次排查累计 token 上限：到顶直接收口，防止一次跑飞烧穿额度 */
  const [tokenBudget, setTokenBudget] = useState(60000);
  /** 回复字数上限的两个档位：自由对话 / 预案（跑完一整套排查）各一个 */
  const [replyCharsTerse, setReplyCharsTerse] = useState(150);
  const [replyCharsDeep, setReplyCharsDeep] = useState(400);
  const [drafts, setDrafts] = useState<ProviderDraft[]>([]);
  const [activeId, setActiveId] = useState('');
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState('');
  /** 连通性测试结果（模型 id → 通不通）。
   只有真测过才敢说「已接入」—— 没验证就标绿，等于骗人。 */
  const [testResults, setTestResults] = useState<Record<string, boolean>>({});

  const presets = useMemo(
    () => configQuery.data?.presets ?? [],
    [configQuery.data],
  );

  /** 厂商名 → 预设：用来展示该厂商的常用模型候选 */
  const presetByName = useMemo(() => {
    const map = new Map<string, AiPreset>();
    for (const item of presets) {
      if (item.name) map.set(item.name, item);
    }
    return map;
  }, [presets]);

  /** 选项带上分组前缀，否则「国内 / 国际 / 本地」在扁平列表里看不出来 */
  const presetOptions = useMemo(
    () =>
      presets.map((item) => ({
        value: item.name,
        label: item.custom
          ? t('ai.presetCustom')
          : `${t(`ai.group.${item.group}` as MessageKey)} · ${item.name}`,
      })),
    [presets, t],
  );

  // 每次打开都从服务端拉一份最新配置，避免拿着上一次的草稿改
  useEffect(() => {
    if (!open || !configQuery.data) return;
    const cfg: AiConfig = configQuery.data;
    setEnabled(cfg.enabled);
    setHours(cfg.hours ?? 24);
    setTokenBudget(cfg.token_budget ?? 60000);
    setReplyCharsTerse(cfg.reply_chars_terse ?? 150);
    setReplyCharsDeep(cfg.reply_chars_deep ?? 400);
    setDrafts((cfg.providers ?? []).map(toDraft));
    setActiveId(cfg.active_id ?? (cfg.providers?.[0]?.id ?? ''));
  }, [open, configQuery.data]);

  function patch(index: number, next: Partial<ProviderDraft>) {
    setDrafts((list) =>
      list.map((item, i) => (i === index ? { ...item, ...next } : item)),
    );
  }

  /**
   * 选中厂商后把「猜不出来」的字段一次填好。
   *
   * 模型名直接覆盖成第一个候选而不是保留旧值：换了厂商，旧模型名基本一定无效，
   * 留着反而会让人以为配好了却调不通。用户仍可手改。
   */
  function applyPreset(index: number, presetName: string) {
    const preset = presetByName.get(presetName);
    if (!preset) {
      patch(index, { presetName: '' });
      return;
    }
    patch(index, {
      presetName: preset.name,
      name: drafts[index]?.name?.trim() ? drafts[index].name : preset.name,
      kind: preset.kind,
      base_url: preset.base_url,
      model: preset.models[0] ?? '',
    });
  }

  const presetModelsOf = (draft: ProviderDraft): string[] =>
    draft.presetName ? presetByName.get(draft.presetName)?.models ?? [] : [];

  /** 勾选/取消一个被授权人。授权的是「用」的权限，不是「管」——对方改不了这条 */
  function toggleGrant(index: number, username: string) {
    setDrafts((list) =>
      list.map((item, i) => {
        if (i !== index) return item;
        const current = item.granted ?? [];
        return {
          ...item,
          granted: current.includes(username)
            ? current.filter((name) => name !== username)
            : [...current, username],
        };
      }),
    );
  }

  /** 必填项齐了没有。这只说明「填完了」，**不代表连得上** —— 连不连通由
      测试按钮说了算，测过的结果记在 testResults 里。 */
  const isConfigured = (item: ProviderDraft): boolean =>
    Boolean(
      item.name.trim() &&
        item.base_url.trim() &&
        item.model.trim() &&
        (item.api_key.trim() || item.api_key_set),
    );

  function addProvider() {
    setDrafts((list) => [...list, emptyDraft()]);
  }

  function removeProvider(index: number) {
    setDrafts((list) => list.filter((_, i) => i !== index));
  }

  async function test(index: number) {
    const draft = drafts[index];
    if (!draft.base_url || !draft.model) {
      toast.error(t('ai.fieldRequired'));
      return;
    }
    setTestingId(draft.id || `#${index}`);
    try {
      const reply = await aiApi.testProvider({ ...draft });
      setTestResults((prev) => ({ ...prev, [draft.id]: true }));
      // note：连得上，但有话要说（推理模型只输出了思考过程就被测试预算截断）。
      // 用 info 而不是 success —— 结论确实是「通了」，但得顺带说清为什么没看到
      // 正文，否则用户会以为测试没生效、又去折腾地址。
      if (reply.note) {
        toast.info(t('ai.testOk'), reply.note);
      } else {
        toast.success(t('ai.testOk'), reply.model || '');
      }
    } catch (err) {
      setTestResults((prev) => ({ ...prev, [draft.id]: false }));
      toast.error(t('ai.testFailed'), errorMessage(err));
    } finally {
      setTestingId('');
    }
  }

  async function save() {
    const invalid = drafts.find(
      (item) => !item.name.trim() || !item.base_url.trim() || !item.model.trim(),
    );
    if (invalid) {
      toast.error(t('ai.fieldRequired'));
      return;
    }
    setSaving(true);
    try {
      const providers = drafts
        // 平台共享项对普通用户只读：不回传（后端也会忽略，这里是不让它出现在
        // 提交里，免得界面上看起来像"我改了"其实没生效）
        .filter((item) => isAdmin || !item.readonly)
        .map((item) => ({
          id: item.id,
          name: item.name,
          kind: item.kind,
          base_url: item.base_url,
          model: item.model,
          api_key: item.api_key,
          enabled: item.enabled,
          granted: item.granted ?? [],
        }));

      if (isAdmin) {
        await aiApi.saveConfig({
          enabled,
          hours,
          token_budget: tokenBudget,
          reply_chars_terse: replyCharsTerse,
          reply_chars_deep: replyCharsDeep,
          active_id: activeId,
          providers,
        });
      } else {
        // 个人只能提交自己的；归属由后端强制盖上，这里传什么都不作数。
        // 分析窗口与 token 上限一并提交：它们记在**这个人**名下（普通用户用的是
        // 自己的模型和自己的额度），不是改平台默认值
        await aiApi.saveMyProviders({
          providers,
          hours,
          token_budget: tokenBudget,
          reply_chars_terse: replyCharsTerse,
          reply_chars_deep: replyCharsDeep,
        });
      }
      toast.success(t('ai.saved'));
      onSaved();
      onClose();
    } catch (err) {
      toast.error(t('ai.saveFailed'), errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('ai.settingsTitle')}
      description={t('ai.settingsDesc')}
      size="lg"
      /* 正文的分块节奏（间距）在这个弹窗里统一由 .ai-settings-modal 给，
         见 components.css 的「模型设置弹窗」一节 */
      className="ai-settings-modal"
      footer={
        <>
          <Button variant="secondary" onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" loading={saving} onClick={() => void save()}>
            {t('ai.save')}
          </Button>
        </>
      }
    >
      {configQuery.isLoading ? (
        <div className="flex flex-col gap-8">
          <Skeleton height={18} width="35%" />
          <Skeleton height={14} />
          <Skeleton height={14} width="60%" />
        </div>
      ) : null}

      {!configQuery.isLoading ? (
        <>
          {/* 顶部一组：范围提示 + 总开关。两者回答的都是「谁能用 / 开不开」，
              与下面的模型列表不是一个层次，所以单独成组（间距比段落小一档），
              并让总开关占一整行 —— 原先它是一个贴着提示条的孤立小控件，
              既看不出这是平台级开关，也不像可点的东西。 */}
          <div className="ai-settings-intro">
            {/* 普通用户看到的是「我的模型」：平台共享项置灰，别人配的压根不出现 */}
            {!isAdmin ? <Notice tone="info">{t('ai.myModelsOnly')}</Notice> : null}
            <div className="ai-master">
              <div className="ai-master-text">
                <span className="ai-master-title">{t('ai.enable')}</span>
                <span className="ai-master-hint">{t('ai.enableHint')}</span>
              </div>
              <Switch
                checked={enabled}
                onChange={setEnabled}
                ariaLabel={t('ai.enable')}
              />
            </div>
          </div>

          {/* 虚拟机授权策略是平台级开关（生产标签禁写、双人审批、快照保留），
              只有管理员能改。普通用户看不到这一段：他们需要知道的是「我这台
              为什么写不了」，那写在授权卡上。 */}
          {isAdmin ? <VmAiPolicyCard /> : null}

          {/* 这几项决定「一次排查跑多大、回答写多长」：分析窗口是往回看多少数据，
              token 上限是最多烧多少额度，两个字数档是回答的字数上限。放在模型列表
              **上面** —— 它们管的是整次排查而不是某一个模型。而且普通用户用的是自己
              的模型、花自己的额度（见后端 visible_providers 的说明），这些数理应由他
              自己定，不必全平台共用一个值。 */}
          {/* 预设与这四个数字是同一件事：两个字数档对普通用户来说是两个数字，不好
              判断「多少算合适」，所以给三档预设一键填好。收进同一个容器里，组内
              10px、与上下段落 18px —— 否则预设行看起来像是独立于这些数字的另一段。 */}
          <div className="ai-limits">
            <div className="ai-reply-preset">
              <span className="ai-reply-preset-label">{t('ai.replyPreset')}</span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setReplyCharsTerse(100);
                  setReplyCharsDeep(250);
                }}
              >
                {t('ai.replyPresetTerse')}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setReplyCharsTerse(150);
                  setReplyCharsDeep(400);
                }}
              >
                {t('ai.replyPresetNormal')}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setReplyCharsTerse(300);
                  setReplyCharsDeep(800);
                }}
              >
                {t('ai.replyPresetDeep')}
              </Button>
            </div>

            <div className="flex items-end gap-12 flex-wrap">
              <div style={{ maxWidth: 260, flex: '1 1 200px' }}>
                <Field label={t('ai.hours')} hint={t('ai.hoursHint')}>
                  <Input
                    type="number"
                    min={1}
                    max={168}
                    value={hours}
                    onChange={(e) => setHours(Number(e.target.value) || 24)}
                  />
                </Field>
              </div>
              <div style={{ maxWidth: 260, flex: '1 1 200px' }}>
                <Field label={t('ai.tokenBudget')} hint={t('ai.tokenBudgetHint')}>
                  <Input
                    type="number"
                    min={4000}
                    max={100000000}
                    value={tokenBudget}
                    onChange={(e) => setTokenBudget(Number(e.target.value) || 60000)}
                  />
                </Field>
              </div>
              <div style={{ maxWidth: 260, flex: '1 1 200px' }}>
                <Field label={t('ai.replyCharsTerse')} hint={t('ai.replyCharsTerseHint')}>
                  <Input
                    type="number"
                    min={50}
                    max={5000}
                    value={replyCharsTerse}
                    onChange={(e) => setReplyCharsTerse(Number(e.target.value) || 150)}
                  />
                </Field>
              </div>
              <div style={{ maxWidth: 260, flex: '1 1 200px' }}>
                <Field label={t('ai.replyCharsDeep')} hint={t('ai.replyCharsDeepHint')}>
                  <Input
                    type="number"
                    min={50}
                    max={5000}
                    value={replyCharsDeep}
                    onChange={(e) => setReplyCharsDeep(Number(e.target.value) || 400)}
                  />
                </Field>
              </div>
            </div>
          </div>

          {drafts.map((draft, index) => (
            <Card key={draft.id || index}>
              <CardHeader
                title={draft.name || t('ai.provider')}
                actions={
                  <div className="flex items-center gap-8">
                    {/* 平台共享项：普通人看得见在用哪个，但改不了也删不掉 */}
                    {draft.readonly ? (
                      <Badge variant="neutral" size="sm">
                        {t('ai.sharedModel')}
                      </Badge>
                    ) : null}
                    {/* 「当前使用」不再标在卡片上：排查时随时能在下拉里换模型，
                        默认模型这个概念已经弱化。这里更该回答的是「这条配好了没、
                        连不连得上」，而不是「谁是默认」。 */}
                    <Badge
                      variant={
                        testResults[draft.id] === false
                          ? 'danger'
                          : isConfigured(draft)
                            ? 'success'
                            : 'warning'
                      }
                      size="sm"
                      title={t('ai.badgeHint')}
                    >
                      {testResults[draft.id] === false
                        ? t('ai.connectFailed')
                        : isConfigured(draft)
                          ? t('ai.connected')
                          : t('ai.incomplete')}
                    </Badge>
                    {draft.readonly ? null : (
                      <Button
                        size="sm"
                        variant="ghost"
                        icon={<IconTrash size={14} />}
                        onClick={() => removeProvider(index)}
                        title={t('ai.removeProvider')}
                      />
                    )}
                  </div>
                }
              />
              {/* 先选厂商：Base URL 与常用模型名自动就位，管理员不用去翻文档 */}
              <Field label={t('ai.preset')} hint={t('ai.presetHint')}>
                <Select
                  value={draft.presetName ?? ''}
                  onChange={(e) => applyPreset(index, e.target.value)}
                  options={[
                    { label: t('ai.presetManual'), value: '' },
                    ...presetOptions,
                  ]}
                  disabled={Boolean(draft.readonly)}
                />
              </Field>

              <div className="form-grid-2">
                <Field label={t('ai.providerName')} hint={t('ai.providerNameHint')}>
                  <Input
                    value={draft.name}
                    onChange={(e) => patch(index, { name: e.target.value })}
                    placeholder="DeepSeek"
                    disabled={Boolean(draft.readonly)}
                  />
                </Field>
                <Field label={t('ai.providerKind')}>
                  <Select
                    value={draft.kind}
                    onChange={(e) =>
                      patch(index, { kind: e.target.value as ProviderDraft['kind'] })
                    }
                    options={[
                      { label: t('ai.kindExternal'), value: 'external' },
                      { label: t('ai.kindInternal'), value: 'internal' },
                    ]}
                    disabled={Boolean(draft.readonly)}
                  />
                </Field>
              </div>
              <Field label={t('ai.baseUrl')} hint={t('ai.baseUrlHint')}>
                <Input
                  value={draft.base_url}
                  onChange={(e) => patch(index, { base_url: e.target.value })}
                  placeholder="https://api.example.com/v1"
                  disabled={Boolean(draft.readonly)}
                />
              </Field>
              <div className="form-grid-2">
                <Field label={t('ai.model')} hint={t('ai.modelHint')}>
                  <Input
                    value={draft.model}
                    onChange={(e) => patch(index, { model: e.target.value })}
                    placeholder="deepseek-chat"
                    disabled={Boolean(draft.readonly)}
                  />
                </Field>
                <Field
                  label={t('ai.apiKey')}
                  hint={draft.api_key_set ? t('ai.apiKeyKeep') : t('ai.apiKeyHint')}
                >
                  <Input
                    type="password"
                    value={draft.api_key}
                    onChange={(e) => patch(index, { api_key: e.target.value })}
                    placeholder={t('ai.apiKeyPlaceholder')}
                    disabled={Boolean(draft.readonly)}
                  />
                </Field>
              </div>

              {/* 授权：把这条模型开放给指定的某几个人。
                  给的是「用」的权限，不是「管」—— 对方能选它排查，但改不了、
                  删不掉，也看不到密钥。只有管理员能操作这一项。 */}
              {isAdmin ? (
                <Field label={t('ai.grantTo')} hint={t('ai.grantHint')}>
                  <div className="flex flex-col gap-8">
                    {userNames.length === 0 ? (
                      <span className="fs-xs text-muted">
                        {t('ai.noOtherUsers')}
                      </span>
                    ) : (
                      <UserPicker
                        users={userNames}
                        selected={draft.granted ?? []}
                        onToggle={(name) => toggleGrant(index, name)}
                        placeholder={t('ai.searchUser')}
                        emptyText={t('ai.noMatchUser')}
                      />
                    )}
                    {/* 已选的单独列出来：下拉收起后仍看得见授权给了谁，
                        也方便点一下就撤掉 —— 撤销路径必须比授权更顺手 */}
                    {(draft.granted ?? []).length > 0 ? (
                      <div className="flex items-center gap-6 flex-wrap">
                        <span className="fs-xs text-muted">
                          {t('ai.grantedCount', {
                            n: (draft.granted ?? []).length,
                          })}
                        </span>
                        {(draft.granted ?? []).map((name) => (
                          <Button
                            key={name}
                            size="sm"
                            variant="ghost"
                            icon={<IconClose size={13} />}
                            onClick={() => toggleGrant(index, name)}
                            title={t('ai.revokeGrant', { name })}
                          >
                            {name}
                          </Button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </Field>
              ) : null}

              {/* 候选模型一键填入：敲错模型名是最常见的「配好了却调不通」 */}
              {presetModelsOf(draft).length > 0 ? (
                <div className="flex items-center gap-6 flex-wrap">
                  <span className="fs-xs text-muted">{t('ai.modelSuggest')}</span>
                  {presetModelsOf(draft).map((name) => (
                    <Button
                      key={name}
                      variant="ghost"
                      size="sm"
                      onClick={() => patch(index, { model: name })}
                    >
                      {name}
                    </Button>
                  ))}
                </div>
              ) : null}
              <div className="flex items-center gap-12 flex-wrap">
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<IconRefresh size={14} />}
                  loading={testingId === (draft.id || `#${index}`)}
                  onClick={() => void test(index)}
                >
                  {t('ai.test')}
                </Button>
                <Switch
                  checked={draft.enabled}
                  onChange={(v) => patch(index, { enabled: v })}
                  label={t('ai.provider')}
                />
              </div>
            </Card>
          ))}

          <div className="flex items-center gap-12 flex-wrap">
            <Button
              variant="secondary"
              icon={<IconPlus size={15} />}
              onClick={addProvider}
            >
              {t('ai.addProvider')}
            </Button>
          </div>

          <Notice tone="info">{t('ai.readonlyTip')}</Notice>
        </>
      ) : null}
    </Modal>
  );
}
