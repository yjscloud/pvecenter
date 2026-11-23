/* ==========================================================================
   pvecenter — 证书管理（多厂商 / ACME 签发、部署、自动续期）

   页面按「巡检优先」组织：顶部一条状态行回答"多少正常、多少要处理"，
   中间是站点表格（有效期进度、最近运行、重试倒计时），展开某一行可以看到
   续期流水线每一步的结果。腾讯云密钥收进折叠卡，首屏只留巡检信息。
   ========================================================================== */

import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  certWorkflowRunsApi,
  certsApi,
  connectionsApi,
  downloadCertificate,
  vmsApi,
} from "../api/endpoints";
import { PageShell } from "../components/Layout";
import { Card } from "../components/ui/Card";
import { Badge } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { Checkbox, Input, Select, Switch, Textarea } from "../components/ui/Input";
import { Modal } from "../components/ui/Modal";
import { ConfirmDialog } from "../components/ui/ConfirmDialog";
import { EmptyState, ErrorState, Notice } from "../components/ui/EmptyState";
import {
  IconShield,
  IconCloud,
  IconPlus,
  IconTrash,
  IconRefresh,
  IconSave,
  IconDownload,
  IconEdit,
  IconSearch,
  IconClose,
  IconFilter,
  IconUpload,
  IconChevronDown,
  IconTerminal,
  IconCopy,
  IconFlow,
} from "../components/Icons";
import { CertWorkflowWorkspace } from "./CertWorkflows";
import { useToast } from "../hooks/useToast";
import { useRefreshInterval } from "../hooks/useRefreshInterval";
import { useAuth } from "../hooks/useAuth";
import { PagerBar, usePaged } from "../hooks/usePaged";
import { isRunning } from "../utils/status";
import { formatDate, formatDateTime, formatRelative } from "../utils/format";
import { useT, type MessageKey, type TFunc } from "../i18n";
import { CertTargetsEditor } from "../components/CertTargetsEditor";
import { CertProvidersCard } from "../components/CertProvidersCard";
import { CertUploadDialog, type CertUploadPayload } from "../components/CertUploadDialog";
import type {
  CertDeployLog,
  CertDetail,
  CertFlowRunDetail,
  CertInventoryItem,
  CertInventoryStats,
  CertSite,
  CertTarget,
  CertTemplate,
  RemoteCertificate,
} from "../api/types";

const ACTION_LABEL: Record<string, MessageKey> = {
  apply: 'cert.act.apply',
  renew: 'cert.act.renew',
  deploy: 'cert.act.deploy',
  bind: 'cert.act.bind',
  sync: 'cert.act.sync',
  cleanup: 'cert.act.cleanup',
};

/** 流水线步骤 -> 文案。新增步骤时这里补一行，页面不用改结构。 */
const STEP_LABEL: Record<string, MessageKey> = {
  guard: 'cert.step.guard',
  prepare: 'cert.step.prepare',
  sync: 'cert.step.sync',
  decide: 'cert.step.decide',
  warn: 'cert.step.warn',
  issue: 'cert.step.issue',
  deploy: 'cert.step.deploy',
  drop: 'cert.step.drop',
  notice: 'cert.step.notice',
};

const STEP_STATUS: Record<string, MessageKey> = {
  ok: 'cert.stepStatus.ok',
  skipped: 'cert.stepStatus.skipped',
  failed: 'cert.stepStatus.failed',
  running: 'cert.stepStatus.running',
};

/* ---- 运行日志：把 last_run 的逐步结果摊成一行一条日志 -------------------
   表格里那一版是「扫一眼」的时间轴（单行、会省略），排障时要看的是完整文本，
   所以另给一份日志视图：带相对耗时、整段说明不截断、能整段复制。 */

interface RunLogLine {
  /** 相对本次运行开始的偏移，如 +0.34s */
  at: string;
  name: string;
  status: string;
  ms: string;
  detail: string;
  tone: "ok" | "skipped" | "failed";
}

function runLogLines(site: CertSite, t: TFunc): RunLogLine[] {
  return (site.last_run ?? []).map((step) => ({
    at: typeof step.at === "number" ? `+${(step.at / 1000).toFixed(2)}s` : "—",
    name: STEP_LABEL[step.step] ? t(STEP_LABEL[step.step]) : step.step,
    status: STEP_STATUS[step.status] ? t(STEP_STATUS[step.status]) : step.status,
    ms: step.ms ? `${step.ms} ms` : "",
    detail: step.detail || "",
    tone: step.status,
  }));
}

/** 复制用的纯文本日志：抬头 + 每一步 + 该步的部署日志 + 最后的错误（如果有） */
function runLogText(site: CertSite, t: TFunc, steps?: StepView[]): string {
  const head = t("cert.runLogHead", {
    site: site.name || site.domain,
    domain: site.domain,
    time: site.last_run_at ? formatDateTime(site.last_run_at) : "—",
    status: STEP_STATUS[site.last_run_status || ""]
      ? t(STEP_STATUS[site.last_run_status || ""])
      : site.last_run_status || "—",
  });
  // 有运行详情时把「该步归因到的部署日志」也带上：贴到工单里能直接看出是哪
  // 个目标失败、报的什么，而不是只有一句「部署失败」。
  const lines = steps
    ? steps.flatMap((step) => [
        [`[${step.at}]`, step.name, STEP_STATUS[step.status] ? t(STEP_STATUS[step.status]) : step.status, step.ms, step.detail]
          .filter(Boolean)
          .join(" · "),
        ...step.logs.map(
          (log) =>
            "    " +
            (ACTION_LABEL[log.action] ? t(ACTION_LABEL[log.action]) : log.action) +
            "：" +
            log.detail,
        ),
      ])
    : runLogLines(site, t).map((line) =>
        [`[${line.at}]`, line.name, line.status, line.ms, line.detail]
          .filter(Boolean)
          .join(" · "),
      );
  if (site.last_error) lines.push(`${t("cert.runLogError")}：${site.last_error}`);
  return [head, ...lines].join("\n");
}

/* ---- 运行日志弹窗的主体：每一步 + 该步产生的部署日志 ---------------------
   两种数据源合成同一份视图：
   * 有运行详情（新引擎 / 引擎开关打开）→ 用后端真实的步骤（含耗时、错误原文）；
   * 只有站点投影（旧路径）→ 用 site.last_run 的步骤，日志仍按 node_id 分组。
   日志一律取「该步骤归因到的行」，所以同一句话不会既出现在步骤 1 又出现在步骤 2。 */

interface StepView {
  key: string;
  name: string;
  status: string;
  detail: string;
  ms: string;
  at: string;
  logs: CertDeployLog[];
}

function stepViews(site: CertSite, detail: CertFlowRunDetail | null, t: TFunc): StepView[] {
  if (detail && detail.steps.length > 0) {
    return detail.steps.map((step) => ({
      key: step.node_id,
      name: STEP_LABEL[step.node_id] ? t(STEP_LABEL[step.node_id]) : step.node_id,
      status: step.status,
      detail: step.detail || "",
      ms: step.ms ? `${step.ms} ms` : "",
      at: typeof step.at_ms === "number" ? `+${(step.at_ms / 1000).toFixed(2)}s` : "—",
      logs: step.logs ?? detail.groups?.[step.node_id] ?? [],
    }));
  }
  return (site.last_run ?? []).map((step) => ({
    key: step.step,
    name: STEP_LABEL[step.step] ? t(STEP_LABEL[step.step]) : step.step,
    status: step.status,
    detail: step.detail || "",
    ms: step.ms ? `${step.ms} ms` : "",
    at: typeof step.at === "number" ? `+${(step.at / 1000).toFixed(2)}s` : "—",
    logs: detail?.groups?.[step.step] ?? [],
  }));
}

/* ---- 站点最新一次运行：拉详情 + 运行中订阅 -------------------------------
   站点上的 last_run 是**运行结束后**才投影的，正在跑的那一轮不在里面。所以
   先查最新运行记录，拿到 running 的那条就订阅 SSE，步骤与日志边跑边更新。
   引擎关闭（旧路径）时列表为空，调用方自动回退到 site.last_run。 */
function useSiteRun(siteId: string | null): {
  detail: CertFlowRunDetail | null;
  live: boolean;
} {
  const [detail, setDetail] = useState<CertFlowRunDetail | null>(null);
  const [live, setLive] = useState(false);

  useEffect(() => {
    setDetail(null);
    setLive(false);
    if (!siteId) return;

    const id = siteId;
    let alive = true;
    const controller = new AbortController();

    async function load() {
      let target = "";
      try {
        const list = await certsApi.runs(id, 1);
        if (list.items[0]) target = list.items[0].id;
      } catch {
        /* 引擎关闭 / 网络抖动：回退到站点投影 */
      }
      if (!alive || !target) return;

      try {
        const loaded = await certsApi.runDetail(target);
        if (!alive) return;
        setDetail(loaded);
        if (loaded.status !== "running") return;

        setLive(true);
        await certsApi.streamRun(
          target,
          (event) => {
            if (!alive) return;
            if (event.type === "snapshot") {
              setDetail(event.run);
            } else if (event.type === "logs") {
              // 只追加增量：后端按主键游标推，不会重复也不会乱序
              setDetail((prev) =>
                prev ? { ...prev, logs: [...prev.logs, ...event.items] } : prev,
              );
            } else {
              setLive(false);
            }
          },
          controller.signal,
        );
      } catch {
        /* 流断开：保留已加载的部分，用户可重新展开 */
      } finally {
        if (alive) setLive(false);
      }
    }

    void load();
    return () => {
      alive = false;
      controller.abort();
    };
  }, [siteId]);

  return { detail, live };
}

/** 一步一行：偏移 / 名称 / 状态 / 耗时 / 说明。弹窗与展开行共用。 */
function StepLine({ step, t }: { step: StepView; t: TFunc }) {
  return (
    <div className={`cert-runlog-line is-${step.status}`}>
      <span className="cert-runlog-at">{step.at}</span>
      <span className="cert-runlog-name">{step.name}</span>
      <span className="cert-runlog-status">
        {STEP_STATUS[step.status] ? t(STEP_STATUS[step.status]) : step.status}
      </span>
      <span className="cert-runlog-ms">{step.ms}</span>
      <span className="cert-runlog-detail">{step.detail}</span>
    </div>
  );
}

function StepLogLines({ logs, t }: { logs: CertDeployLog[]; t: TFunc }) {
  if (logs.length === 0) return null;
  return (
    <ul className="cert-step-logs">
      {logs.map((log) => (
        <li key={log.id} className={`cert-step-log is-${log.result}`}>
          <span className="cert-step-log-action">
            {ACTION_LABEL[log.action] ? t(ACTION_LABEL[log.action]) : log.action}
          </span>
          <span className="cert-step-log-detail">{log.detail}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * 状态筛选：chips 用固定的几档，「概览里的数字」用 `state:<状态>` 精确到单个
 * 状态——点「1 即将过期」就该只看到那一条，而不是把「已过期」也一并捞进来。
 */
type Filter = "all" | "due" | "failed" | "paused" | `state:${CertState}`;

function errText(err: unknown): string {
  const detail = (err as any)?.response?.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  return err instanceof Error ? err.message : String(err);
}

/** 站点编辑表单的默认值 */
function blankSite(): Partial<CertSite> {
  return {
    name: "",
    domain: "",
    deploy_method: "local",
    deploy_dir: "/etc/nginx/ssl",
    cert_filename: "fullchain.pem",
    key_filename: "privkey.pem",
    reload_command: "nginx -s reload",
    ssh_port: 22,
    ssh_user: "root",
    ssh_auth: "password",
    ssh_host: "",
    ssh_password: "",
    ssh_key: "",
    connection_id: "",
    agent_node: "",
    agent_vmid: "",
    auto_renew: true,
    auto_deploy: true,
    notify_expiring: true,
    auto_delete_old: true,
    cloud_region: "",
    lb_id: "",
    listener_id: "",
    listener_port: 443,
    cdn_domain: "",
    renew_before_days: 0,
    enabled: true,
    notes: "",
    ca: "tencent",
    domains: [],
    acme_challenge: "dns",
    acme_webroot: "",
    acme_dns_provider: "",
    targets: [],
    monitor_enabled: false,
    monitor_url: "",
  };
}

/** 把表单里的「主目标」字段与附加目标合成 targets 数组再提交。 */
function buildSitePayload(form: Partial<CertSite>): Partial<CertSite> {
  const existing = (form.targets ?? [])[0] ?? {};
  const primary: CertTarget = {
    ...existing,
    id: existing.id || "primary",
    method: (form.deploy_method || "local") as CertTarget["method"],
    deploy_dir: form.deploy_dir,
    cert_filename: form.cert_filename,
    key_filename: form.key_filename,
    reload_command: form.reload_command,
    ssh_host: form.ssh_host,
    ssh_port: form.ssh_port,
    ssh_user: form.ssh_user,
    ssh_auth: form.ssh_auth,
    ssh_password: form.ssh_password,
    ssh_key: form.ssh_key,
    connection_id: form.connection_id ?? "",
    agent_node: form.agent_node,
    agent_vmid: form.agent_vmid,
  };
  return { ...form, targets: [primary, ...(form.targets ?? []).slice(1)] };
}

/**
 * 表单校验：返回「字段 -> 文案」。
 *
 * 只拦下本地一定判得出来的错（必填、格式、端口范围），其余仍然交给后端——
 * 前端复刻一遍后端规则没有意义，但让用户为了一个空域名等一次请求往返更没有。
 * 校验是纯函数，随表单实时重算，所以「改对了红字就消失」不需要额外状态。
 */
function siteFieldErrors(form: Partial<CertSite>, t: TFunc): Record<string, string> {
  const errors: Record<string, string> = {};
  const domain = String(form.domain || "").trim();
  if (!domain) errors.domain = t("cert.errDomainRequired");
  // 允许通配符：*.example.com 是 ACME 的常规用法
  else if (!/^(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i.test(domain)) {
    errors.domain = t("cert.errDomainFormat");
  }

  const method = form.deploy_method;
  if (method === "ssh") {
    if (!String(form.ssh_host || "").trim()) errors.ssh_host = t("cert.errSshHostRequired");
    if (!String(form.ssh_user || "").trim()) errors.ssh_user = t("cert.errSshUserRequired");
    const port = Number(form.ssh_port ?? 22);
    if (!Number.isInteger(port) || port < 1 || port > 65535) errors.ssh_port = t("cert.errPort");
  }
  if (method === "tencent_clb") {
    if (!String(form.cloud_region || "").trim()) errors.cloud_region = t("cert.errCloudRegion");
    if (!String(form.lb_id || "").trim()) errors.lb_id = t("cert.errLbId");
    if (!String(form.listener_id || "").trim()) errors.listener_id = t("cert.errListenerId");
  }
  if (method === "aliyun_slb") {
    if (!String(form.lb_id || "").trim()) errors.lb_id = t("cert.errLbId");
    const port = Number(form.listener_port ?? 443);
    if (!Number.isInteger(port) || port < 1 || port > 65535) errors.listener_port = t("cert.errPort");
  }
  if (method === "tencent_cdn" || method === "aliyun_cdn") {
    if (!String(form.cdn_domain || "").trim()) errors.cdn_domain = t("cert.errCdnDomain");
  }

  const days = Number(form.renew_before_days ?? 0);
  if (!Number.isFinite(days) || days < 0 || days > 90) {
    errors.renew_before_days = t("cert.errRenewDays");
  }
  if (form.monitor_enabled && !/^https?:\/\/\S+$/i.test(String(form.monitor_url || "").trim())) {
    errors.monitor_url = t("cert.errMonitorUrl");
  }
  return errors;
}

/** 部署目标的展示文案 */
function targetText(site: CertSite, t: TFunc): string {
  const dir = site.deploy_dir || "";
  if (site.deploy_method === "ssh") {
    return `${site.ssh_user || "root"}@${site.ssh_host || "?"}:${site.ssh_port || 22}${dir}`;
  }
  if (site.deploy_method === "agent") {
    return `VM ${site.agent_vmid || "?"}@${site.agent_node || "?"}${dir}`;
  }
  if (site.deploy_method === "pve") {
    return t('cert.targetPve', { node: site.agent_node || "?" });
  }
  return t('cert.targetLocal', { dir });
}

/**
 * 证书状态。视觉、文案、筛选、排序都从这里派生，不再各自判断一遍。
 *
 * 关键区分：**已过期**（left < 0）与**即将过期**（left <= 阈值）以前同属
 * warning 一色，页面上分不出「快到期」和「已经过期」，而这恰恰是运维最先要
 * 认出来的两件事。
 */
type CertState = "valid" | "expiring" | "expired" | "failed" | "paused" | "revoked";

const STATE_TONE: Record<CertState, "ok" | "warning" | "danger" | "muted"> = {
  valid: "ok",
  expiring: "warning",
  expired: "danger",
  failed: "danger",
  paused: "muted",
  revoked: "muted",
};

const STATE_LABEL: Record<CertState, MessageKey> = {
  valid: 'cert.state.valid',
  expiring: 'cert.state.expiring',
  expired: 'cert.state.expired',
  failed: 'cert.state.failed',
  paused: 'cert.state.paused',
  revoked: 'cert.state.revoked',
};

function siteState(site: CertSite): CertState {
  // 已吊销是一个终态：即使还剩天数/上次运行成功，也不能显示成「有效」
  if ((site.cert_status ?? -1) === 10) return "revoked";
  if (!site.enabled) return "paused";
  // 「处理失败」优先于「快到期」：已经出错的站点比还有几天余量的更需要人看
  if (site.last_run_status === "failed" || (site.attempt_count ?? 0) > 0) return "failed";
  const left = site.days_left;
  if (left !== null && left !== undefined) {
    if (left < 0) return "expired";
    if (left <= (site.renew_before_days_effective ?? 15)) return "expiring";
  }
  return "valid";
}

/** 站点健康度：一条规则决定排序、筛选与色带，避免三处各写一遍。 */
function siteTone(site: CertSite): "danger" | "warning" | "ok" | "muted" {
  return STATE_TONE[siteState(site)];
}

function isDue(site: CertSite): boolean {
  const left = site.days_left;
  if (left === null || left === undefined) return false;
  return left <= (site.renew_before_days_effective ?? 15);
}

export function Certificates() {
  const t = useT();
  const qc = useQueryClient();
  const toast = useToast();
  const [searchParams] = useSearchParams();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("cert.manage");
  // 私钥导出是独立权限：查看证书 ≠ 拿走证书私钥
  const canExport = hasPermission("cert.export");

  /** 多站点绑定：清单里选中的证书 + 勾选的站点 */
  const [bindCert, setBindCert] = useState<CertInventoryItem | null>(null);
  const [bindSites, setBindSites] = useState<string[]>([]);
  const [importTarget, setImportTarget] = useState<CertSite | null>(null);
  /** 证书导出：选中的证书 + 格式 + 可选口令 */
  const [exportCert, setExportCert] = useState<CertInventoryItem | null>(null);
  const [exportFormat, setExportFormat] = useState<"pem" | "pfx">("pem");
  const [exportPassword, setExportPassword] = useState("");
  const [exportAck, setExportAck] = useState(false);
  const [importPem, setImportPem] = useState("");
  const [importKey, setImportKey] = useState("");
  const [form, setForm] = useState<Partial<CertSite> | null>(null);
  const [isNew, setIsNew] = useState(false);
  /** 点过「保存」之后才显示字段错误：新开的空表单不该一上来就满屏红字 */
  const [submitTried, setSubmitTried] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<CertSite | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<CertSite | null>(null);
  /** 证书详情弹窗（链 / SAN / 指纹 / 线上体检） */
  const [detailSite, setDetailSite] = useState<CertSite | null>(null);
  /** 批量操作：勾选的站点 id；revoke 需要二次确认，单独用 bulkRevoke 标记 */
  const [picked, setPicked] = useState<string[]>([]);
  const [bulkRevoke, setBulkRevoke] = useState(false);

  const [pickerSite, setPickerSite] = useState<CertSite | null>(null);
  const [remote, setRemote] = useState<RemoteCertificate[] | null>(null);
  const [remoteSearch, setRemoteSearch] = useState("");
  const [busy, setBusy] = useState("");
  /** 展开的行：一次只展开一个，避免表格被切成碎片 */
  const [expanded, setExpanded] = useState("");
  /** 打开「运行日志」弹窗的站点（null = 关闭） */
  const [runLogSite, setRunLogSite] = useState<CertSite | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  /** 证书清单的筛选：站点与清单是两套数据，各用各的搜索/渠道 */
  const [invSearch, setInvSearch] = useState("");
  const [invProvider, setInvProvider] = useState("");
  /** 上传第三方证书弹窗 */
  const [uploadOpen, setUploadOpen] = useState(false);
  /** 渠道自检结果：自动自检与手动自检共用，让结果停在卡片上而不是随 toast 消失 */
  const [providerTests, setProviderTests] = useState<
    Record<string, { ok: boolean; detail: string; at: number }>
  >({});
  /** 主体四视图：站点 / 证书清单 / 签发渠道 / 工作流。合成一张卡，标题与工具栏只出现一次 */
  const [view, setView] = useState<
    "sites" | "inventory" | "providers" | "workflows"
  >(() => (searchParams.get("view") === "workflows" ? "workflows" : "sites"));
  /** 从证书行发起流程：切到工作流视图，并把站点带进发起弹窗 */
  const [wfLaunchSite, setWfLaunchSite] = useState<CertSite | null>(null);
  /**
   * 工作流视图工具栏的挂载点。
   *
   * 「运行记录 / 工作流定义」这层切换器和「发起流程」属于工作流自己的状态，
   * 但它们应该和站点、清单的搜索框、筛选项出现在同一条工具栏上——否则工作流
   * 视图会多出一条带分隔线的标题栏，和另外三个子视图不是一套结构。这里留一个
   * 空容器，工作流组件用 portal 把自己的工具栏渲染进来。
   */
  const [wfToolbarSlot, setWfToolbarSlot] = useState<HTMLDivElement | null>(null);

  /* 展开行与弹窗各自订阅自己站点的运行：同一个 hook 两个实例，互不影响 */
  const { detail: runDetail, live: runLive } = useSiteRun(runLogSite?.id ?? null);
  const { detail: expandedDetail, live: expandedLive } = useSiteRun(expanded || null);

  /* 轮询间隔跟「设置 → 面板设置 → 数据刷新间隔」走（见 useRefreshInterval） */
  const refresh = useRefreshInterval();
  const query = useQuery({
    queryKey: ["certs"],
    queryFn: certsApi.get,
    refetchInterval: refresh,
    retry: false,
  });
  const detailQuery = useQuery({
    queryKey: ["cert-detail", detailSite?.id],
    queryFn: () => certsApi.detail(String(detailSite?.id)),
    enabled: !!detailSite,
    retry: false,
  });
  /* 与工作流面板共用同一份缓存：站点行能显示待处理流程数，切换标签时也不再请求一次 */
  const workflowRunsQuery = useQuery({
    queryKey: ["cert-workflow-runs"],
    queryFn: () => certWorkflowRunsApi.list({ limit: 100 }),
    refetchInterval: refresh,
  });
  const activeRunsBySite = useMemo(() => {
    const map = new Map<string, number>();
    for (const run of workflowRunsQuery.data?.items ?? []) {
      if (run.status !== "pending" && run.status !== "running") continue;
      if (!run.site_id) continue;
      map.set(run.site_id, (map.get(run.site_id) ?? 0) + 1);
    }
    return map;
  }, [workflowRunsQuery.data]);

  /* 校验结果随表单实时重算：改对了红字自动消失，不需要额外的「已修正」状态 */
  const formErrors = useMemo(() => (form ? siteFieldErrors(form, t) : {}), [form, t]);
  const formIssueCount = Object.keys(formErrors).length;

  const needsAgent = form?.deploy_method === "agent";
  /** 云负载均衡 / CDN 没有本地目录，主表单要按方法切换字段 */
  const isCloudDeploy = (method?: string) =>
    ["tencent_clb", "aliyun_slb", "tencent_cdn", "aliyun_cdn"].includes(
      String(method || ""),
    );
  const vmsQuery = useQuery({
    queryKey: ["vms", "all"],
    queryFn: () => vmsApi.list(),
    staleTime: 30_000,
    enabled: needsAgent,
  });

  /* 节点 / 机器所在的 PVE：证书最终要写进那台机器，节点名只在它所属的集群上
     成立。多套集群都叫 pve1 时，只给 node 会让后端在「当前连接」上找 ——
     agent 方式甚至会在错机器的客户机里执行命令。 */
  const connectionsQuery = useQuery({
    queryKey: ["connections"],
    queryFn: connectionsApi.list,
    staleTime: 300_000,
    retry: false,
    enabled: ["agent", "lxc", "pve"].includes(String(form?.deploy_method || "")),
  });
  const connectionOptions = useMemo(
    () =>
      (connectionsQuery.data ?? []).map((c) => ({
        label: c.name || c.id,
        value: c.id,
      })),
    [connectionsQuery.data],
  );
  const multiConnection = connectionOptions.length > 1;

  // Guest Agent 部署只能选运行中的虚拟机（虚拟机没开机时 Agent 无法工作）
  const agentOptions = useMemo(() => {
    /* 「先选目标 PVE 集群、再选目标虚拟机」：选定了集群就只列那套 PVE 上的机器，
       否则下拉里混着别的集群的同名节点 / 同号机器，选错就写到另一台上。 */
    const scope = form?.connection_id || "";
    const vms = (vmsQuery.data ?? [])
      .filter((vm) => isRunning(vm.status) && !vm.template)
      .filter((vm) => !scope || (vm.connection_id ?? "") === scope)
      .map((vm) => ({
        label: `${t('cert.agentOption', { name: vm.name, vmid: vm.vmid, node: vm.node })}${
          vm.connection_name ? ` · ${vm.connection_name}` : ''
        }`,
        // 值里带连接：两台 PVE 上同名的节点 + 同号机器是两台不同的机器
        value: `${vm.connection_id ?? ''}|${vm.node}|${vm.vmid}`,
      }));
    return vms;
  }, [vmsQuery.data, t, form?.connection_id]);

  const agentValue = useMemo(() => {
    if (!form?.agent_node || !form?.agent_vmid) return "";
    const identity = `${form.agent_node}|${form.agent_vmid}`;
    const exact = `${form.connection_id ?? ''}|${identity}`;
    if (agentOptions.some((o) => o.value === exact)) return exact;
    /* 旧站点没存 connection_id：按 node+vmid 后缀认一次，显示上不至于变成
       「这台机器不在运行」；用户重新选一次就会把连接补上。 */
    return agentOptions.find((o) => o.value.endsWith(`|${identity}`))?.value ?? exact;
  }, [form?.agent_node, form?.agent_vmid, form?.connection_id, agentOptions]);

  const agentPickerOptions = useMemo(() => {
    if (agentValue && !agentOptions.some((o) => o.value === agentValue)) {
      return [
        { label: t('cert.agentNotRunning', { value: agentValue }), value: agentValue },
        ...agentOptions,
      ];
    }
    return agentOptions;
  }, [agentOptions, agentValue, t]);

  async function run(key: string, fn: () => Promise<string>, okTitle: string): Promise<boolean> {
    setBusy(key);
    try {
      const note = await fn();
      await qc.invalidateQueries({ queryKey: ["certs"] });
      toast.success(okTitle, note || undefined);
      return true;
    } catch (err) {
      toast.error(t('common.opFailed'), errText(err));
      return false;
    } finally {
      setBusy("");
    }
  }

  const sites: CertSite[] = query.data?.sites ?? [];
  const options = query.data?.options;
  const providers = query.data?.providers ?? [];
  const inventory: CertInventoryItem[] = query.data?.inventory ?? [];
  /** 到期分布统计（后端随清单一起下发，避免二次请求） */
  const inventoryStats: CertInventoryStats | undefined = query.data?.inventory_stats;
  // 证书按用户隔离：腾讯云密钥、站点、日志都归属到使用者；管理员可见全部
  const ownUsername = String(query.data?.own_username ?? "");
  const isAdmin = !!query.data?.is_admin;
  /** 别人的站点：管理员可查看但只读（操作一律用站点归属者的密钥） */
  const isForeign = (site: CertSite) =>
    isAdmin && !!site.username && String(site.username) !== ownUsername;

  /* ---- 巡检快照 ---- */
  const stats = useMemo(() => {
    const tally: Record<CertState, number> = {
      valid: 0,
      expiring: 0,
      expired: 0,
      failed: 0,
      paused: 0,
      revoked: 0,
    };
    for (const site of sites) tally[siteState(site)] += 1;
    return tally;
  }, [sites]);

  const lastSynced = useMemo(() => {
    const stamps = sites.map((s) => Number(s.last_check_at || 0)).filter(Boolean);
    return stamps.length ? Math.max(...stamps) : 0;
  }, [sites]);

  /** 列表顺序即优先级：要处理的在前，其次快到期的 */
  const visible = useMemo(() => {
    const kw = search.trim().toLowerCase();
    const matches = sites.filter((site) => {
      if (kw) {
        // 环境 / 集群也进搜索：`prod`、`pve-a` 是最常被拿来收缩范围的词
        const hay =
          `${site.name} ${site.domain} ${site.username || ""} ${site.environment || ""} ${site.cluster || ""}`.toLowerCase();
        if (!hay.includes(kw)) return false;
      }
      if (filter.startsWith("state:")) return siteState(site) === filter.slice(6);
      if (filter === "due") return site.enabled && isDue(site);
      // 「需处理」= 续期失败；「到期」筛选负责 expiring + expired 两种状态
      if (filter === "failed") return siteState(site) === "failed";
      if (filter === "paused") return !site.enabled;
      return true;
    });
    const rank = { danger: 0, warning: 1, ok: 2, muted: 3 } as const;
    return matches.sort((a, b) => {
      const diff = rank[siteTone(a)] - rank[siteTone(b)];
      if (diff !== 0) return diff;
      const la = a.days_left ?? Number.MAX_SAFE_INTEGER;
      const lb = b.days_left ?? Number.MAX_SAFE_INTEGER;
      return la - lb;
    });
  }, [sites, search, filter]);

  /* ---- 分页：三张表各自分页，筛选条件一变就回到第 1 页 ---- */
  const sitePager = usePaged(visible, `${filter}|${search}`);

  /** 清单筛选：关键词打域名 / 证书 ID / 归属者，再按渠道收窄 */
  const filteredInventory = useMemo(() => {
    const kw = invSearch.trim().toLowerCase();
    return inventory.filter((item) => {
      if (invProvider && item.provider !== invProvider) return false;
      if (!kw) return true;
      const hay = [
        item.cert_id,
        item.provider_label,
        item.username || "",
        ...item.domains,
      ]
        .join(" ")
        .toLowerCase();
      return hay.includes(kw);
    });
  }, [inventory, invSearch, invProvider]);

  const inventoryPager = usePaged(
    filteredInventory,
    `inventory|${invSearch}|${invProvider}`,
  );

  /** 每个渠道下有多少张证书：渠道卡片上要给出「这个渠道在管什么」 */
  const providerCertCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const row of inventoryStats?.by_ca ?? []) counts[row.provider] = row.count;
    return counts;
  }, [inventoryStats]);

  /* 弹窗里的步骤视图：优先用运行详情（含每步日志），退回站点投影 */
  const runSteps = useMemo(
    () => (runLogSite ? stepViews(runLogSite, runDetail, t) : []),
    [runLogSite, runDetail, t],
  );

  /* 展开行的步骤视图：同一个合成函数，数据跟着 expanded 站点走 */
  const expandedSteps = useMemo(() => {
    const site = sites.find((item) => item.id === expanded);
    return site ? stepViews(site, expandedDetail, t) : [];
  }, [sites, expanded, expandedDetail, t]);

  /* ---- 签发渠道凭据（腾讯云 / 阿里云 / 华为云，同一套接口） ---- */
  /** 记一次自检结果：卡片上要能一直看到「上次自检成功 / 失败 + 原因」 */
  function noteProviderTest(kind: string, ok: boolean, detail: string) {
    setProviderTests((prev) => ({ ...prev, [kind]: { ok, detail, at: Date.now() } }));
  }

  async function saveProvider(
    kind: string,
    payload: Record<string, unknown>,
  ): Promise<boolean> {
    const ok = await run(
      "provider:" + kind,
      async () => {
        await certsApi.saveProvider(kind, payload);
        return t('cert.providerSaved');
      },
      t('cert.configSaved'),
    );
    if (!ok) return false;

    /* ACME 没有可自检的接口（要知道证书能不能签，得真去申请一次），跳过 */
    if (providers.find((item) => item.kind === kind)?.test_disabled) return true;

    /* 保存后立刻自检一次：凭据错在添加时就说清楚，不要等第一次申请证书才炸 */
    const toastId = toast.loading(t('cert.testingConnection'));
    try {
      const result = await certsApi.testProvider(kind);
      noteProviderTest(kind, true, result.detail);
      toast.update(toastId, {
        type: "success",
        title: t('cert.connectionOk'),
        message: result.detail,
        persistent: false,
        loading: false,
      });
    } catch (err) {
      noteProviderTest(kind, false, errText(err));
      toast.update(toastId, {
        type: "warning",
        title: t('cert.testFailed'),
        message: errText(err),
        persistent: false,
        loading: false,
      });
    }
    return true;
  }

  /** 手动自检：结果同时回给卡片，让「上次自检」常驻显示而不是只冒一次 toast */
  async function testProvider(kind: string): Promise<{ ok: boolean; detail: string }> {
    setBusy("provider-test:" + kind);
    const toastId = toast.loading(t('cert.testingConnection'));
    try {
      const result = await certsApi.testProvider(kind);
      noteProviderTest(kind, true, result.detail);
      toast.update(toastId, {
        type: "success",
        title: t('cert.connectionOk'),
        message: result.detail,
        persistent: false,
        loading: false,
      });
      return { ok: true, detail: result.detail };
    } catch (err) {
      const detail = errText(err);
      noteProviderTest(kind, false, detail);
      toast.update(toastId, {
        type: "warning",
        title: t('cert.testFailed'),
        message: detail,
        persistent: false,
        loading: false,
      });
      return { ok: false, detail };
    } finally {
      setBusy("");
    }
  }

  /* ---- 多站点绑定：一张证书一次写到多个站点 ---- */
  function openBind(cert: CertInventoryItem) {
    setBindCert(cert);
    setBindSites(cert.sites.map((site) => site.id));
  }

  function submitBind() {
    if (!bindCert) return;
    void run(
      "inventory-bind",
      async () => {
        const result = await certsApi.bindInventory({
          provider: bindCert.provider,
          cert_id: bindCert.cert_id,
          site_ids: bindSites,
        });
        setBindCert(null);
        setBindSites([]);
        return result.skipped.length
          ? t('cert.inventoryBoundSkipped', {
              n: result.bound,
              skipped: result.skipped.length,
            })
          : result.detail;
      },
      t('cert.inventoryBindOk'),
    );
  }

  /* ---- 证书导出：PEM / PFX ---- */
  async function submitExport() {
    if (!exportCert || !canExport || !exportAck) return;
    // 清单是「按证书 ID 聚合」的，同一张证书内容在各站点一致，取第一个站点即可
    const siteId = exportCert.sites[0]?.id;
    if (!siteId) return;
    await run(
      "export",
      async () => {
        await downloadCertificate(siteId, exportFormat, exportPassword);
        setExportCert(null);
        setExportPassword("");
        setExportAck(false);
        return "";
      },
      t('cert.exportConfirm'),
    );
  }

  /* ---- 站点 ---- */
  function openCreate() {
    setForm(blankSite());
    setIsNew(true);
    setSubmitTried(false);
  }

  function openEdit(site: CertSite) {
    setForm({ ...site });
    setIsNew(false);
    setSubmitTried(false);
  }

  /** 筛选生效时给一条「看得到、也退得出去」的提示 */
  const filtering = filter !== "all" || search.trim().length > 0;
  function resetFilters() {
    setFilter("all");
    setSearch("");
  }

  /** 整行可点：看运行详情不必再去够最右边那个箭头 */
  function toggleRow(id: string) {
    setExpanded((cur) => (cur === id ? "" : id));
  }

  function saveSite() {
    if (!form) return;
    if (formIssueCount > 0) {
      setSubmitTried(true);
      toast.error(t('cert.formInvalid', { n: formIssueCount }), Object.values(formErrors)[0]);
      // 表单很长，红字可能在视口外：直接滚到并聚焦第一个出错的输入框
      requestAnimationFrame(() => {
        const el = document.querySelector<HTMLElement>('.form-grid [aria-invalid="true"]');
        el?.scrollIntoView({ block: 'center' });
        el?.focus({ preventScroll: true });
      });
      return;
    }
    void run(
      "site",
      async () => {
        const payload = buildSitePayload(form);
        if (isNew) {
          await certsApi.createSite(payload);
        } else if (form.id) {
          await certsApi.updateSite(form.id, payload);
        }
        setForm(null);
        return t('cert.siteSaved');
      },
      t('cert.saveOk'),
    );
  }

  function applyCert(site: CertSite) {
    void run(
      "apply:" + site.id,
      async () => (await certsApi.apply(site.id)).detail,
      t('cert.applySubmitted'),
    );
  }

  function deployById(siteId: string) {
    void run(
      "deploy:" + siteId,
      async () => (await certsApi.deploy(siteId)).detail,
      t('cert.deployDone'),
    );
  }

  function deployCert(site: CertSite) {
    deployById(site.id);
  }

  /* ---- 上传第三方证书：建站点 → 导入 PEM/KEY → 可选立即部署 ---- */
  async function submitUpload(payload: CertUploadPayload): Promise<boolean> {
    return run(
      "upload",
      async () => {
        const created = await certsApi.createSite({
          name: payload.name.trim() || payload.domain.trim(),
          domain: payload.domain.trim(),
          notes: payload.notes,
          ca: "upload",
          // 托管证书没有可续期的来源：自动续期、自动部署都关掉，避免定时任务空转
          auto_renew: false,
          auto_deploy: false,
          deploy_method: payload.method,
          deploy_dir: payload.deployDir,
          cert_filename: payload.certFilename,
          key_filename: payload.keyFilename,
          reload_command: payload.reloadCommand,
          ssh_host: payload.sshHost,
          ssh_port: payload.sshPort,
          ssh_user: payload.sshUser,
          ssh_auth: payload.sshAuth,
          ssh_password: payload.sshPassword,
          ssh_key: payload.sshKey,
        } as Partial<CertSite>);
        const siteId = created.site.id;
        try {
          await certsApi.importCertificate(siteId, payload.pem, payload.key);
        } catch (err) {
          // 导入失败就把刚建的站点撤回：清单里留一个没有证书的空壳只会更乱
          await certsApi.removeSite(siteId).catch(() => undefined);
          throw err;
        }
        if (payload.deployNow) {
          try {
            await certsApi.deploy(siteId);
          } catch (err) {
            // 证书已经在库里了，部署失败只报不撤
            toast.error(t('cert.deployFailed'), errText(err));
          }
        }
        setUploadOpen(false);
        setView("inventory");
        return t('cert.uploadDone');
      },
      t('cert.uploadOk'),
    );
  }

  function syncCert(site: CertSite) {
    void run(
      "sync:" + site.id,
      async () => {
        const s = (await certsApi.sync(site.id)).site;
        return t('cert.syncStatus', { status: s.cert_status_text || t('common.unknown') }) +
          (s.days_left !== null && s.days_left !== undefined
            ? t('cert.syncDays', { n: s.days_left })
            : "");
      },
      t('cert.synced'),
    );
  }

  /* ---- 托管证书（上传自己签好的 PEM/KEY） ---- */
  function submitImport() {
    if (!importTarget) return;
    void run(
      "import:" + importTarget.id,
      async () => {
        const result = await certsApi.importCertificate(
          importTarget.id,
          importPem,
          importKey,
        );
        setImportTarget(null);
        setImportPem("");
        setImportKey("");
        return result.detail;
      },
      t('cert.importDone'),
    );
  }

  /* ---- 站点模板 ---- */
  function applyTemplate(templateId: string) {
    const template = (query.data?.templates ?? []).find((item) => item.id === templateId);
    if (!template || !form) return;
    const { id: _id, name: _name, ...rest } = template as CertTemplate;
    setForm({ ...form, ...rest });
  }

  /** 站点用哪家厂商的证书列表：非云厂商（ACME / 托管）回落到腾讯云，仅用于选择已有证书 */
  function cloudKind(site: CertSite | null): string {
    const kinds = query.data?.options?.provider_kinds ?? [];
    const kind = String(site?.ca || "");
    return kinds.includes(kind) ? kind : kinds[0] || "tencent";
  }

  function loadRemote() {
    const kind = cloudKind(pickerSite);
    void run(
      "remote",
      async () => {
        const r = await certsApi.remoteCerts(kind, remoteSearch);
        setRemote(r.certificates);
        return t('cert.fetchedCerts', { n: r.count });
      },
      t('cert.remoteListTitle'),
    );
  }

  function bindRemote(cert: RemoteCertificate) {
    if (!pickerSite) return;
    const kind = cloudKind(pickerSite);
    void run(
      "bind",
      async () => {
        await certsApi.bind(pickerSite.id, cert.cert_id, kind);
        setPickerSite(null);
        setRemote(null);
        return t('cert.boundCert', { id: cert.cert_id });
      },
      t('cert.bindDone'),
    );
  }

  /* ---- 批量操作 / 主动轮换 / 详情 ---- */
  function togglePick(id: string) {
    setPicked((prev) => (prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]));
  }

  async function runBulk(action: "apply" | "sync" | "deploy" | "rotate") {
    const ids = [...picked];
    if (!ids.length) return;
    await run(
      "bulk",
      async () => {
        const result = await certsApi.bulk(action, ids);
        setPicked([]);
        return t('cert.bulkResult', { ok: result.ok, failed: result.failed });
      },
      t(`cert.bulk${action[0].toUpperCase()}${action.slice(1)}` as MessageKey),
    );
  }

  async function runBulkRevoke() {
    const ids = [...picked];
    setBulkRevoke(false);
    if (!ids.length) return;
    await run(
      "bulk",
      async () => {
        const result = await certsApi.bulk("revoke", ids);
        setPicked([]);
        return t('cert.bulkResult', { ok: result.ok, failed: result.failed });
      },
      t('cert.bulkRevoke'),
    );
  }

  function rotateOne(site: CertSite) {
    void run(
      `rotate:${site.id}`,
      async () => (await certsApi.rotate(site.id)).detail,
      t('cert.rotateDone'),
    );
  }

  const pickable = canManage
    ? visible.filter((site) => !isForeign(site) && site.enabled)
    : [];
  const allPicked = pickable.length > 0 && pickable.every((site) => picked.includes(site.id));

  const FILTERS: { id: Filter; label: MessageKey; count: number }[] = [
    { id: "all", label: 'cert.filterAll', count: sites.length },
    { id: "due", label: 'cert.filterDue', count: stats.expiring + stats.expired },
    { id: "failed", label: 'cert.filterFailed', count: stats.failed },
    { id: "paused", label: 'cert.filterPaused', count: stats.paused },
  ];

  /* 把日志整段塞进剪贴板：排障时更多人是要贴到工单 / 群里，而不是逐行抄 */
  async function copyRunLog() {
    if (!runLogSite) return;
    try {
      await navigator.clipboard.writeText(runLogText(runLogSite, t, runSteps));
      toast.success(t('cert.runLogCopied'));
    } catch {
      toast.error(t('common.opFailed'), t('cert.runLogCopy'));
    }
  }

  return (
    <PageShell
      title={t('cert.title')}
      subtitle={t('cert.subtitle')}
      actions={
        view !== "workflows" ? (
          <Button
            variant="secondary"
            icon={<IconRefresh size={15} />}
            loading={busy === "syncAll"}
            disabled={!canManage || sites.length === 0}
            onClick={() =>
              void run(
                "syncAll",
                async () => {
                  const r = await certsApi.syncAll();
                  return t('cert.syncedSites', { n: r.sites.length });
                },
                t('cert.syncAllDone'),
              )
            }
          >
            {t('cert.syncAll')}
          </Button>
        ) : null
      }
    >
      {query.isError ? (
        /* 报错要给出路：带上「重试」，否则用户只能刷新整页 */
        <ErrorState
          title={t('cert.loadFailed')}
          message={errText(query.error)}
          onRetry={() => void query.refetch()}
        />
      ) : null}

      {/* ---------------- 健康概览：先回答「多少正常、多少要处理」 ----------------
          数字在上、标签在下：扫一眼先拿到数量，再确认它代表什么。五种状态各占
          一格，有值的异常态（即将过期 / 已过期 / 需处理）才点亮，避免满屏彩色。
          四个子视图（站点 / 清单 / 渠道 / 工作流）都显示这一条：它讲的是整页证书
          的健康度，不是某个视图的局部数据，藏掉工作流那一份会让页面突然少一截，
          和其它三个子页面对不上。 */}
      <div className="cert-overview">
        <div className="cert-overview-stats">
          {(
            [
              ["valid", stats.valid, 'cert.state.valid'],
              ["expiring", stats.expiring, 'cert.state.expiring'],
              ["expired", stats.expired, 'cert.state.expired'],
              ["failed", stats.failed, 'cert.state.failed'],
              ["paused", stats.paused, 'cert.state.paused'],
            ] as const
          ).map(([state, value, label]) => {
            /* 不再放圆点：左侧 2px 状态色条已经说明了颜色，数字后面再跟一个
               圆点会读成「2 · 有效」的列表感，反而分散注意力 */
            const body = (
              <>
                <span className="cert-stat-value">{query.isLoading ? "—" : value}</span>
                <span className="cert-stat-label">{t(label)}</span>
              </>
            );
            /* 有数量的那一格变成入口：点「1 即将过期」直接筛出那一条，
               不必先看一眼数字、再回工具栏找对应的 chip。这一条在四个子视图
               里都显示，所以点的时候要顺手切回站点视图 —— 否则在清单、渠道、
               工作流上点下去，筛选是生效了，但眼前的内容纹丝不动。 */
            const clickable = value > 0 && !query.isLoading;
            return clickable ? (
              <button
                key={state}
                type="button"
                className={`cert-stat is-${state} has-value`}
                title={t('cert.statFilterHint')}
                onClick={() => {
                  setView("sites");
                  setFilter(`state:${state}`);
                  setSearch("");
                }}
              >
                {body}
              </button>
            ) : (
              <div key={state} className={`cert-stat is-${state}`}>
                {body}
              </div>
            );
          })}
        </div>
        <div className="cert-overview-meta">
          <IconRefresh size={13} aria-hidden="true" />
          {lastSynced ? t('cert.statSynced', { time: formatRelative(lastSynced) }) : ""}
        </div>
      </div>

      {/* ---------------- 证书站点：页面主体，不折叠 ---------------- */}
      <Card className="cert-card-main" collapsible={false}>
        {/* 一条工具栏同时承担「切视图」和「筛选当前视图」：
            原来三块各有一张卡、各有一个标题栏和分页，页面被切成三段；
            合并成一张卡后标题只出现一次，视觉重量集中到表格本身。 */}
        <div className="cert-panel-bar">
          <div className="cert-tabs" role="tablist" aria-label={t('cert.title')}>
            {(
              [
                ["sites", t('cert.sitesTitle'), sites.length],
                ["inventory", t('cert.inventoryTitle'), inventory.length],
                [
                  "providers",
                  t('cert.providersTitle'),
                  providers.filter((item) => item.configured).length,
                ],
                [
                  "workflows",
                  t('workflow.tab'),
                  workflowRunsQuery.data?.stats.active ?? 0,
                ],
              ] as const
            ).map(([id, label, count]) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={view === id}
                className={`cert-tab${view === id ? " is-active" : ""}`}
                onClick={() => setView(id)}
              >
                {label}
                <span className="cert-tab-count">{count}</span>
              </button>
            ))}
          </div>

          {view === "sites" ? (
            <div className="cert-toolbar">
              <div className="cert-search">
                <IconSearch size={14} />
                <input
                  value={search}
                  placeholder={t('cert.searchPlaceholder')}
                  onChange={(e) => setSearch(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape" && search) {
                      e.preventDefault();
                      setSearch("");
                    }
                  }}
                  aria-label={t('cert.searchPlaceholder')}
                />
                {/* 有内容才出现清除键：搜出来的结果不对时，退回去只要一次点击 */}
                {search ? (
                  <button
                    type="button"
                    className="cert-search-clear"
                    aria-label={t('cert.clearSearch')}
                    title={t('cert.clearSearch')}
                    onClick={() => setSearch("")}
                  >
                    <IconClose size={12} />
                  </button>
                ) : null}
              </div>
              <div className="cert-filters" role="tablist">
                {FILTERS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    role="tab"
                    aria-selected={filter === item.id}
                    className={`cert-filter${filter === item.id ? " is-active" : ""}`}
                    onClick={() => setFilter(item.id)}
                  >
                    {t(item.label)}
                    <span className="cert-filter-count">{item.count}</span>
                  </button>
                ))}
              </div>
              {/* 主操作跟着它作用的卡片走：站点视图的「新增」就落在站点列表这一栏，
                  与清单视图的「上传证书」同位；页头只留页面级的「同步全部」 */}
              <Button
                variant="primary"
                size="sm"
                icon={<IconPlus size={14} />}
                disabled={!canManage}
                onClick={openCreate}
              >
                {t('cert.addSite')}
              </Button>
            </div>
          ) : view === "inventory" ? (
            /* 清单的工具栏：搜索域名 / 证书 ID、按签发渠道收窄、上传第三方证书。
               三个入口同在一行，是因为它们回答的是同一个问题——「我这张证在哪」 */
            <div className="cert-toolbar">
              <div className="cert-search">
                <IconSearch size={14} />
                <input
                  value={invSearch}
                  placeholder={t('cert.inventorySearch')}
                  onChange={(e) => setInvSearch(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape" && invSearch) {
                      e.preventDefault();
                      setInvSearch("");
                    }
                  }}
                  aria-label={t('cert.inventorySearch')}
                />
                {invSearch ? (
                  <button
                    type="button"
                    className="cert-search-clear"
                    aria-label={t('cert.clearSearch')}
                    title={t('cert.clearSearch')}
                    onClick={() => setInvSearch("")}
                  >
                    <IconClose size={12} />
                  </button>
                ) : null}
              </div>
              <div className="cert-filters" role="tablist">
                <button
                  type="button"
                  role="tab"
                  aria-selected={invProvider === ""}
                  className={`cert-filter${invProvider === "" ? " is-active" : ""}`}
                  onClick={() => setInvProvider("")}
                >
                  {t('cert.inventoryAllProviders')}
                  <span className="cert-filter-count">{inventory.length}</span>
                </button>
                {(inventoryStats?.by_ca ?? []).map((item) => (
                  <button
                    key={item.provider}
                    type="button"
                    role="tab"
                    aria-selected={invProvider === item.provider}
                    className={`cert-filter${invProvider === item.provider ? " is-active" : ""}`}
                    onClick={() => setInvProvider(item.provider)}
                  >
                    {item.label}
                    <span className="cert-filter-count">{item.count}</span>
                  </button>
                ))}
              </div>
              <Button
                variant="primary"
                size="sm"
                icon={<IconUpload size={14} />}
                disabled={!canManage}
                onClick={() => setUploadOpen(true)}
              >
                {t('cert.inventoryUpload')}
              </Button>
            </div>
          ) : view === "workflows" ? (
            /* 工作流视图的工具栏由 CertWorkflowWorkspace 通过 portal 渲染到这里，
               与上面两个分支共用同一条工具栏、同一套控件 */
            <div className="cert-toolbar" ref={setWfToolbarSlot} />
          ) : null}
        </div>

        {view === "workflows" ? (
          <CertWorkflowWorkspace
            sites={sites}
            canManage={canManage}
            launchSite={wfLaunchSite}
            onLaunchConsumed={() => setWfLaunchSite(null)}
            toolbarSlot={wfToolbarSlot}
          />
        ) : view === "sites" ? (
          <>
        {/* 筛选生效时补一条结果行：现在「为什么列表变短了」和「怎么退回去」
            都在同一个地方回答，不用去猜是哪个 chip 在起作用 */}
        {/* 结果为零时交给空态说话：两处都报「筛出 0 条」只是重复 */}
        {filtering && visible.length > 0 ? (
          <div className="cert-filter-note" role="status">
            <IconFilter size={13} aria-hidden="true" />
            {/* 从概览下钻时不落在任何一个 chip 上，这里得说清楚「现在是按什么筛的」 */}
            {filter.startsWith("state:") ? (
              <span className="cert-filter-note-state">
                {t('cert.onlyState', {
                  state: t(STATE_LABEL[filter.slice(6) as CertState]),
                })}
              </span>
            ) : null}
            <span className="cert-filter-note-text">
              {t('cert.filterResult', { shown: visible.length, total: sites.length })}
            </span>
            <button type="button" className="cert-filter-reset" onClick={resetFilters}>
              {t('cert.resetFilters')}
            </button>
          </div>
        ) : null}
        {query.isLoading ? (
          /* 骨架屏按真实行的节奏铺 5 行：比一个居中转圈更能说明「马上就有内容」，
             也避免数据到位时整张卡片高度跳变 */
          <div className="cert-skeleton" aria-hidden="true">
            {Array.from({ length: 5 }).map((_, index) => (
              <div key={index} className="cert-skeleton-row" />
            ))}
          </div>
        ) : visible.length > 0 ? (
          <>
            {canManage && picked.length > 0 ? (
              <div className="cert-bulk-bar">
                <span className="cert-bulk-count">
                  {t('cert.bulkPicked', { n: picked.length })}
                </span>
                <Button
                  size="sm"
                  variant="secondary"
                  loading={busy === "bulk"}
                  onClick={() => runBulk("sync")}
                >
                  {t('cert.bulkSync')}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  loading={busy === "bulk"}
                  onClick={() => runBulk("deploy")}
                >
                  {t('cert.bulkDeploy')}
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  loading={busy === "bulk"}
                  onClick={() => runBulk("rotate")}
                >
                  {t('cert.bulkRotate')}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  loading={busy === "bulk"}
                  onClick={() => setBulkRevoke(true)}
                >
                  {t('cert.bulkRevoke')}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setPicked([])}>
                  {t('cert.bulkClear')}
                </Button>
              </div>
            ) : null}
          <div className="table-container">
            <table className="table table-dense cert-table">
              <thead>
                <tr>
                  {canManage ? (
                    <th className="cert-col-pick">
                      <Checkbox
                        aria-label={t('cert.bulkSelectAll')}
                        checked={allPicked}
                        onChange={(e) => setPicked(e.target.checked ? pickable.map((s) => s.id) : [])}
                      />
                    </th>
                  ) : null}
                  <th className="cert-col-site">{t('cert.colSite')}</th>
                  <th className="cert-col-expire">{t('cert.colExpire')}</th>
                  <th className="cert-col-target">{t('cert.colTarget')}</th>
                  <th className="cert-col-run">{t('cert.colRun')}</th>
                  <th className="cert-col-actions">{t('cert.colAction')}</th>
                </tr>
              </thead>
              <tbody>
                {sitePager.rows.map((site) => {
                  const left = site.days_left;
                  const state = siteState(site);
                  const tone = STATE_TONE[state];
                  const readonly = isForeign(site);
                  const open = expanded === site.id;
                  const pct =
                    left === null || left === undefined
                      ? null
                      : Math.max(0, Math.min(100, Math.round((left / 90) * 100)));
                  const nextAt = Number(site.next_attempt_at || 0);
                  const nowSec = Math.floor(Date.now() / 1000);
                  const retryIn =
                    nextAt > nowSec ? Math.max(1, Math.round((nextAt - nowSec) / 60)) : 0;
                  const runStatus = site.last_run_status;
                  const siteWorkflows = activeRunsBySite.get(site.id) ?? 0;
                  return (
                    <Fragment key={site.id}>
                      <tr
                        className={`cert-row${open ? " is-expanded" : ""}`}
                        onClick={(e) => {
                          // 整个操作列都不参与「点行展开」：按钮、分隔线和箭头
                          // 各有各的语义，点它们却把行收起来会很别扭
                          if (
                            (e.target as HTMLElement).closest(
                              ".cert-actions, button, a, input, select, textarea, label",
                            )
                          ) {
                            return;
                          }
                          if (String(window.getSelection()?.toString() ?? "").length > 0) return;
                          toggleRow(site.id);
                        }}
                      >
                        {canManage ? (
                          <td className="cert-col-pick">
                            <Checkbox
                              aria-label={t('cert.bulkPickOne', { name: site.name || site.domain })}
                              checked={picked.includes(site.id)}
                              disabled={readonly || !site.enabled}
                              onChange={() => togglePick(site.id)}
                            />
                          </td>
                        ) : null}
                        <td className="cert-col-site">
                          <div className="cert-site-head">
                            <span className="cert-site-name">{site.name || site.domain}</span>
                            {isAdmin ? (
                              <Badge variant={readonly ? "neutral" : "success"} size="sm">
                                {site.username || ownUsername || "-"}
                              </Badge>
                            ) : null}
                            {/* 只标异常：正常站点不需要每行挂一枚「已开启」 */}
                            {!site.enabled ? (
                              <Badge variant="neutral" size="sm">
                                {t('cert.disabledSite')}
                              </Badge>
                            ) : null}
                            {!site.auto_renew ? (
                              <Badge variant="warning" size="sm" dot>
                                {t('cert.autoRenewOff')}
                              </Badge>
                            ) : null}
                            {/* 体检异常只标在站点名旁边：正常站点不需要每行挂一枚绿标 */}
                            {site.health_status === "bad" ? (
                              <Badge variant="danger" size="sm" dot>
                                {t('cert.healthBad')}
                              </Badge>
                            ) : site.health_status === "warn" ? (
                              <Badge variant="warning" size="sm" dot>
                                {t('cert.healthWarn')}
                              </Badge>
                            ) : null}
                          </div>
                          <div className="cert-site-domain mono" title={site.domain}>
                            {site.domain}
                          </div>
                          {site.notes ? (
                            <div className="cert-site-note" title={site.notes}>
                              {site.notes}
                            </div>
                          ) : null}
                        </td>
                        <td className="cert-col-expire">
                          <div className="cert-expiry">
                            {left === null || left === undefined ? (
                              /* 没有到期时间 = 还没签发 / 已解除绑定。此时不挂状态
                                 胶囊，否则一个空证书站点会被标成「有效」。 */
                              <span className="cert-expiry-date">—</span>
                            ) : (
                              <>
                                {/* 状态胶囊放在最前：同一列里给出「结论」和「依据」
                                    （剩余天数 / 进度条 / 到期日），扫视先看到结论 */}
                                <span className={`cert-state is-${state}`}>
                                  <span className="cert-state-dot" aria-hidden="true" />
                                  {t(STATE_LABEL[state])}
                                </span>
                                <span className={`cert-days is-${tone}`}>
                                  {left < 0
                                    ? t('cert.expiredDays', { n: -left })
                                    : t('cert.daysLeft', { n: left })}
                                </span>
                                {pct !== null ? (
                                  <span
                                    className={`cert-bar is-${tone}`}
                                    role="progressbar"
                                    aria-valuenow={left}
                                    aria-valuemin={0}
                                    aria-valuemax={90}
                                  >
                                    <span style={{ width: `${pct}%` }} />
                                  </span>
                                ) : null}
                                <span className="cert-expiry-date">
                                  {formatDate(site.expire_at)}
                                </span>
                              </>
                            )}
                          </div>
                        </td>
                        <td className="cert-col-target">
                          <div className="cert-target mono">{targetText(site, t)}</div>
                          <div className="cert-sub mono">
                            {site.cert_filename} · {site.key_filename}
                          </div>
                          {site.last_deploy_at ? (
                            <div className="cert-sub">
                              {t('cert.lastDeploy', {
                                time: formatRelative(site.last_deploy_at),
                              })}
                            </div>
                          ) : null}
                        </td>
                        <td className="cert-col-run">
                          <div className="cert-run">
                            <span className={`cert-run-pill is-${runStatus || "none"}`}>
                              {runStatus === "failed"
                                ? t('cert.stepStatus.failed')
                                : runStatus === "ok"
                                  ? t('cert.stepStatus.ok')
                                  : "—"}
                            </span>
                            <span className="cert-run-time">
                              {site.last_run_at ? formatRelative(site.last_run_at) : ""}
                            </span>
                          </div>
                          {retryIn > 0 ? (
                            <div className="cert-run-note is-warning">
                              {t('cert.retryInMin', { n: retryIn })}
                            </div>
                          ) : null}
                          {(site.attempt_count ?? 0) > 0 ? (
                            <div className="cert-run-note is-danger">
                              {t('cert.attempts', { n: site.attempt_count ?? 0 })}
                            </div>
                          ) : null}
                          {siteWorkflows > 0 ? (
                            <div className="cert-run-note is-workflow">
                              <IconFlow size={11} />
                              {t('cert.activeWorkflows', { n: siteWorkflows })}
                            </div>
                          ) : null}
                        </td>
                        <td className="cert-col-actions">
                          <div className="cert-actions">
                            <div className="cert-actions-group">
                            <IconButton
                              label={
                                readonly
                                  ? t('cert.readonlyForeign')
                                  : site.ca === "upload"
                                    ? t('cert.uploadOnly')
                                    : t('cert.applyOrRenew')
                              }
                              disabled={
                                !canManage ||
                                readonly ||
                                site.ca === "upload" ||
                                busy === "apply:" + site.id
                              }
                              onClick={() => applyCert(site)}
                            >
                              <IconShield size={15} />
                            </IconButton>
                            <IconButton
                              label={readonly ? t('cert.readonlyForeign') : t('cert.deployNow')}
                              disabled={!canManage || readonly || busy === "deploy:" + site.id}
                              onClick={() => deployCert(site)}
                            >
                              <IconDownload size={15} />
                            </IconButton>
                            <IconButton
                              label={readonly ? t('cert.readonlyForeign') : t('cert.syncStatusBtn')}
                              disabled={!canManage || readonly || busy === "sync:" + site.id}
                              onClick={() => syncCert(site)}
                            >
                              <IconRefresh size={15} />
                            </IconButton>
                            <IconButton
                              label={
                                readonly
                                  ? t('cert.readonlyForeign')
                                  : t('workflow.run.launchForSite')
                              }
                              disabled={!canManage || readonly}
                              onClick={() => {
                                setWfLaunchSite(site);
                                setView("workflows");
                              }}
                            >
                              <IconFlow size={15} />
                            </IconButton>
                            </div>
                            <span className="cert-actions-sep" aria-hidden="true" />
                            <div className="cert-actions-group">
                              <IconButton
                                label={readonly ? t('cert.readonlyForeign') : t('cert.editSite')}
                                disabled={!canManage || readonly}
                                onClick={() => openEdit(site)}
                              >
                                <IconEdit size={15} />
                              </IconButton>
                              <IconButton
                                variant="danger"
                                label={
                                  readonly ? t('cert.readonlyForeign') : t('cert.deleteSite')
                                }
                                disabled={!canManage || readonly}
                                onClick={() => setDeleteTarget(site)}
                              >
                                <IconTrash size={15} />
                              </IconButton>
                            </div>
                            <span className="cert-actions-sep" aria-hidden="true" />
                            <button
                              type="button"
                              className={`cert-expand${open ? " is-open" : ""}`}
                              aria-expanded={open}
                              aria-label={open ? t('cert.collapseRun') : t('cert.expandRun')}
                              title={t('cert.expandHint')}
                              onClick={() => toggleRow(site.id)}
                            >
                              <IconChevronDown size={15} />
                            </button>
                          </div>
                        </td>
                      </tr>
                      {open ? (
                        <tr className="cert-detail-row">
                          <td colSpan={canManage ? 6 : 5}>
                            <div className="cert-detail">
                              <div className="cert-detail-main">
                                <div className="cert-detail-head">
                                  <span className="cert-detail-title">{t('cert.runTitle')}</span>
                                  {expandedLive ? (
                                    <span className="cert-run-live">
                                      <span className="cert-step-dot" aria-hidden="true" />
                                      {t('cert.runLive')}
                                    </span>
                                  ) : null}
                                  <span className="fs-xs text-muted">
                                    {expandedDetail?.started_at || site.last_run_at
                                      ? t('cert.runAt', {
                                          time: formatDateTime(
                                            expandedDetail?.started_at || site.last_run_at || 0,
                                          ),
                                        })
                                      : ""}
                                  </span>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className="cert-runlog-btn"
                                    icon={<IconTerminal size={14} />}
                                    onClick={() => setRunLogSite(site)}
                                  >
                                    {t('cert.runLog')}
                                  </Button>
                                </div>
                                {expandedSteps.length > 0 ? (
                                  <ol className="cert-runlog-steps">
                                    {expandedSteps.map((step) => (
                                      <li key={step.key} className={`cert-step is-${step.status}`}>
                                        <StepLine step={step} t={t} />
                                        {/* 这一步归因到的部署日志：谁能看到「哪一步打了什么」 */}
                                        <StepLogLines logs={step.logs} t={t} />
                                      </li>
                                    ))}
                                  </ol>
                                ) : (
                                  <div className="fs-xs text-muted">{t('cert.runEmpty')}</div>
                                )}
                              </div>

                              <aside className="cert-detail-side">
                                <div className="cert-kv">
                                  <span>{t('cert.fieldCa')}</span>
                                  <span>{site.ca_label || site.ca || "tencent"}</span>
                                </div>
                                <div className="cert-kv">
                                  <span>{t('cert.colAutoRenew')}</span>
                                  <span>
                                    {site.auto_renew
                                      ? t('cert.autoRenewOn')
                                      : t('cert.autoRenewOff')}
                                  </span>
                                </div>
                                <div className="cert-kv">
                                  <span>{t('cert.certIds')}</span>
                                  <span className="mono">
                                    {site.cert_id || t('cert.notApplied')}
                                  </span>
                                </div>
                                {site.pending_cert_id ? (
                                  <div className="cert-kv">
                                    <span>{t('cert.pendingId')}</span>
                                    <span className="mono text-accent">
                                      {site.pending_cert_id}
                                    </span>
                                  </div>
                                ) : null}
                                <div className="cert-kv">
                                  <span>{t('cert.deployTo')}</span>
                                  <span className="mono">{targetText(site, t)}</span>
                                </div>
                                {nextAt > 0 && nextAt <= nowSec ? (
                                  <div className="cert-kv">
                                    <span>{t('cert.colRun')}</span>
                                    <span className="text-warning">{t('cert.retryInMin', { n: 1 })}</span>
                                  </div>
                                ) : null}
                                {site.last_error ? (
                                  <div className="cert-detail-error">{site.last_error}</div>
                                ) : null}
                                {/* 窄屏放不下行内动作图标，操作收进展开区 */}
                                <div className="cert-detail-actions cert-detail-actions-touch">
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconShield size={14} />}
                                    disabled={
                                      !canManage ||
                                      readonly ||
                                      site.ca === "upload" ||
                                      busy === "apply:" + site.id
                                    }
                                    onClick={() => applyCert(site)}
                                  >
                                    {t('cert.applyOrRenew')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconDownload size={14} />}
                                    disabled={!canManage || readonly}
                                    onClick={() => deployCert(site)}
                                  >
                                    {t('cert.deployNow')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconRefresh size={14} />}
                                    disabled={!canManage || readonly}
                                    onClick={() => syncCert(site)}
                                  >
                                    {t('cert.syncStatusBtn')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconFlow size={14} />}
                                    disabled={!canManage || readonly}
                                    onClick={() => {
                                      setWfLaunchSite(site);
                                      setView("workflows");
                                    }}
                                  >
                                    {t('workflow.run.new')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconSearch size={14} />}
                                    onClick={() => setDetailSite(site)}
                                  >
                                    {t('cert.detailBtn')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconRefresh size={14} />}
                                    disabled={
                                      !canManage ||
                                      readonly ||
                                      site.ca === "upload" ||
                                      !site.cert_id ||
                                      busy === `rotate:${site.id}`
                                    }
                                    loading={busy === `rotate:${site.id}`}
                                    onClick={() => rotateOne(site)}
                                    title={
                                      site.ca === "upload"
                                        ? t('cert.rotateUploadHint')
                                        : t('cert.rotateHint')
                                    }
                                  >
                                    {t('cert.rotateBtn')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<IconEdit size={14} />}
                                    disabled={!canManage || readonly}
                                    onClick={() => openEdit(site)}
                                  >
                                    {t('cert.editSite')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className="cert-danger-text"
                                    disabled={
                                      !canManage ||
                                      readonly ||
                                      (!site.cert_id && site.ca !== "acme")
                                    }
                                    onClick={() => setRevokeTarget(site)}
                                  >
                                    {t('cert.revokeCert')}
                                  </Button>
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    className="cert-danger-text"
                                    icon={<IconTrash size={14} />}
                                    disabled={!canManage || readonly}
                                    onClick={() => setDeleteTarget(site)}
                                  >
                                    {t('cert.deleteSite')}
                                  </Button>
                                </div>
                                {site.ca === "upload" ? (
                                  <div className="cert-detail-actions">
                                    <Button
                                      variant="ghost"
                                      size="sm"
                                      icon={<IconDownload size={14} />}
                                      disabled={!canManage || readonly}
                                      onClick={() => {
                                        setImportTarget(site);
                                        setImportPem("");
                                        setImportKey("");
                                      }}
                                    >
                                      {t('cert.uploadCert')}
                                    </Button>
                                  </div>
                                ) : null}
                              </aside>
                            </div>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
          </>
        ) : (
          /* 两种「空」是两件事：一条都没有时要建站点，筛不出来时要退回去。
             空态里直接放上那一步，用户不用再回工具栏找。 */
          <EmptyState
            icon={<IconShield size={26} />}
            title={sites.length > 0 ? t('cert.emptyMatchTitle') : t('cert.emptyTitle')}
            description={sites.length > 0 ? t('cert.emptyMatchHint') : t('cert.noSites')}
            action={
              sites.length > 0 ? (
                <Button variant="secondary" size="sm" onClick={resetFilters}>
                  {t('cert.resetFilters')}
                </Button>
              ) : canManage ? (
                <Button
                  variant="primary"
                  size="sm"
                  icon={<IconPlus size={14} />}
                  onClick={openCreate}
                >
                  {t('cert.addSite')}
                </Button>
              ) : null
            }
          />
        )}
        {visible.length > 0 ? <PagerBar pager={sitePager} /> : null}
        </>
        ) : view === "inventory" ? (
          <>
          {/* 清单的分布原本是 7 个大色块统计条，与顶部概览语义重复、又占掉
              一整行高度。改成一行 chips：只在需要时扫一眼，不抢表格的注意力。 */}
          {inventoryStats ? (
            <div className="cert-inline-stats">
              {(
                [
                  ["total", inventoryStats.total, "cert.statsTotal"],
                  ["expired", inventoryStats.buckets.expired, "cert.statsExpired"],
                  ["d7", inventoryStats.buckets.d7, "cert.statsD7"],
                  ["d30", inventoryStats.buckets.d30, "cert.statsD30"],
                  ["d90", inventoryStats.buckets.d90, "cert.statsD90"],
                  ["d90plus", inventoryStats.buckets.d90plus, "cert.statsD90plus"],
                  ["unknown", inventoryStats.buckets.unknown, "cert.statsUnknown"],
                ] as const
              ).map(([key, value, labelKey]) => (
                <span
                  key={key}
                  className={`cert-inline-stat${key === "total" || key === "expired" || key === "d7" ? " is-strong" : ""}`}
                >
                  <b>{value}</b>
                  {t(labelKey)}
                </span>
              ))}
            </div>
          ) : null}
          {filteredInventory.length ? (
            <div className="table-container">
              <table className="table table-dense cert-inventory-table">
                <thead>
                  <tr>
                    <th className="cert-col-domains">{t('cert.inventoryColDomains')}</th>
                    <th className="cert-col-cert">{t('cert.inventoryColCert')}</th>
                    <th className="cert-col-expire">{t('cert.colExpire')}</th>
                    <th className="cert-col-sites">{t('cert.inventoryColSites')}</th>
                    <th className="cert-col-actions">{t('cert.colAction')}</th>
                  </tr>
                </thead>
                <tbody>
                  {inventoryPager.rows.map((item) => {
                    const left = item.days_left;
                    const state = item.pending
                      ? "pending"
                      : left === null || left === undefined
                        ? "muted"
                        : left < 0
                          ? "expired"
                          : left <= 15
                            ? "expiring"
                            : "valid";
                    const tone =
                      state === "expired"
                        ? "danger"
                        : state === "expiring"
                          ? "warning"
                          : state === "valid"
                            ? "ok"
                            : "muted";
                    const pct =
                      left === null || left === undefined
                        ? null
                        : Math.max(0, Math.min(100, Math.round((left / 90) * 100)));
                    const readonlyCert =
                      isAdmin && !!item.username && item.username !== ownUsername;
                    return (
                      <tr key={`${item.username || ""}:${item.provider}:${item.cert_id}`}>
                        {/* 域名在前：用户找的是「我那张 example.com 的证书」，证书 ID 是佐证 */}
                        <td className="cert-col-domains">
                          <div className="cert-inventory-domain" title={item.domains.join(", ")}>
                            {item.domains[0] || "—"}
                          </div>
                          {item.domains.length > 1 ? (
                            <div
                              className="cert-sub mono"
                              title={item.domains.join(", ")}
                            >
                              {item.domains.slice(1).join(", ")}
                            </div>
                          ) : null}
                        </td>
                        <td className="cert-col-cert">
                          <div className="cert-site-head">
                            <Badge variant="info" size="sm">
                              {item.provider_label}
                            </Badge>
                            <span className="mono cert-id">{item.cert_id}</span>
                          </div>
                        </td>
                        <td className="cert-col-expire">
                          <div className="cert-expiry">
                            {state === "pending" ? (
                              <span className="cert-state is-pending">
                                <span className="cert-state-dot" aria-hidden="true" />
                                {t('cert.inventoryPending')}
                              </span>
                            ) : left === null || left === undefined ? (
                              <span className="cert-state is-paused">
                                <span className="cert-state-dot" aria-hidden="true" />
                                {item.status_text || "—"}
                              </span>
                            ) : (
                              <>
                                <span className={`cert-state is-${state}`}>
                                  <span className="cert-state-dot" aria-hidden="true" />
                                  {t(STATE_LABEL[state as CertState])}
                                </span>
                                <span className={`cert-days is-${tone}`}>
                                  {left < 0
                                    ? t('cert.expiredDays', { n: -left })
                                    : t('cert.daysLeft', { n: left })}
                                </span>
                                {pct !== null ? (
                                  <span
                                    className={`cert-bar is-${tone}`}
                                    role="progressbar"
                                    aria-valuenow={left}
                                    aria-valuemin={0}
                                    aria-valuemax={90}
                                  >
                                    <span style={{ width: `${pct}%` }} />
                                  </span>
                                ) : null}
                                <span className="cert-expiry-date">
                                  {item.expire_at ? formatDate(item.expire_at) : "—"}
                                </span>
                              </>
                            )}
                          </div>
                        </td>
                        <td className="cert-col-sites">
                          {item.sites.length ? (
                            <>
                              <div className="cert-inventory-sites">
                                {item.sites.map((site) => site.name || site.domain).join("、")}
                              </div>
                              <div className="cert-sub">
                                {t('cert.inventorySiteCount', { n: item.sites.length })}
                              </div>
                            </>
                          ) : (
                            <span
                              className="cert-inventory-unbound"
                              title={t('cert.inventoryUnboundHint')}
                            >
                              {t('cert.inventoryUnbound')}
                            </span>
                          )}
                        </td>
                        <td className="cert-col-actions">
                          {/* 部署需要具体站点：只有一张证挂在一个站点上时才能一键下发 */}
                          {item.sites.length === 1 ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              loading={busy === "deploy:" + item.sites[0].id}
                              disabled={!canManage || item.pending || readonlyCert}
                              onClick={() => deployById(item.sites[0].id)}
                            >
                              {t('cert.inventoryDeploy')}
                            </Button>
                          ) : null}
                          {/* 托管证书不属于任何厂商账号，没法「从厂商绑定到别的站点」 */}
                          {item.provider !== "upload" ? (
                            <Button
                              variant="ghost"
                              size="sm"
                              disabled={!canManage || item.pending}
                              onClick={() => openBind(item)}
                            >
                              {t('cert.inventoryBind')}
                            </Button>
                          ) : null}
                          <Button
                            variant="ghost"
                            size="sm"
                            icon={<IconDownload size={14} />}
                            disabled={item.pending || readonlyCert || !canExport}
                            title={canExport ? undefined : t('cert.exportNoPerm')}
                            onClick={() => {
                              setExportCert(item);
                              setExportFormat("pem");
                              setExportPassword("");
                              setExportAck(false);
                            }}
                          >
                            {t('cert.exportTitle')}
                          </Button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              icon={<IconShield size={26} />}
              title={
                inventory.length > 0
                  ? t('cert.emptyMatchTitle')
                  : t('cert.inventoryEmptyTitle')
              }
              description={
                inventory.length > 0
                  ? t('cert.emptyMatchHint')
                  : t('cert.inventoryEmptyDesc')
              }
              action={
                inventory.length > 0 ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setInvSearch("");
                      setInvProvider("");
                    }}
                  >
                    {t('cert.resetFilters')}
                  </Button>
                ) : canManage ? (
                  <Button
                    variant="primary"
                    size="sm"
                    icon={<IconUpload size={14} />}
                    onClick={() => setUploadOpen(true)}
                  >
                    {t('cert.inventoryUpload')}
                  </Button>
                ) : null
              }
            />
          )}
          {filteredInventory.length ? <PagerBar pager={inventoryPager} /> : null}
          </>
        ) : (
          /* 签发渠道（字段由后端 schema 驱动）：同一张卡里的一块，不再单独成卡 */
          <CertProvidersCard
            bare
            providers={providers}
            canManage={canManage}
            busy={busy}
            tests={providerTests}
            counts={providerCertCounts}
            onSave={saveProvider}
            onTest={testProvider}
          />
        )}
      </Card>

      {/* ---------------- 运行日志（逐步结果 → 可复制的完整日志） ---------------- */}
      <Modal
        open={runLogSite !== null}
        title={t('cert.runLog')}
        description={
          runLogSite
            ? t('cert.runLogHead', {
                site: runLogSite.name || runLogSite.domain,
                domain: runLogSite.domain,
                time: runLogSite.last_run_at
                  ? formatDateTime(runLogSite.last_run_at)
                  : '—',
                status: STEP_STATUS[runLogSite.last_run_status || '']
                  ? t(STEP_STATUS[runLogSite.last_run_status || ''])
                  : runLogSite.last_run_status || '—',
              })
            : undefined
        }
        size="lg"
        onClose={() => setRunLogSite(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setRunLogSite(null)}>
              {t('common.close')}
            </Button>
            <Button
              variant="primary"
              icon={<IconCopy size={14} />}
              disabled={runSteps.length === 0}
              onClick={copyRunLog}
            >
              {t('cert.runLogCopy')}
            </Button>
          </>
        }
      >
        {runSteps.length > 0 ? (
          <>
            {runLive ? (
              <div className="cert-run-live">
                <span className="cert-step-dot" aria-hidden="true" />
                {t('cert.runLive')}
              </div>
            ) : null}
            <ol className="cert-runlog-steps">
              {runSteps.map((step) => (
                <li key={step.key} className={`cert-step is-${step.status}`}>
                  <StepLine step={step} t={t} />
                  {/* 该步骤归因到的部署日志：签发 / 同步 / 每个部署目标各一行 */}
                  <StepLogLines logs={step.logs} t={t} />
                </li>
              ))}
            </ol>
            {/* 失败步骤的说明里通常已经带着同一段错误，不再重复一遍 */}
            {runLogSite?.last_error &&
            !runSteps.some((step) =>
              step.detail.includes(runLogSite.last_error as string),
            ) ? (
              <div className="cert-detail-error mt-8">{runLogSite.last_error}</div>
            ) : null}
          </>
        ) : (
          <div className="fs-sm text-secondary">{t('cert.runEmpty')}</div>
        )}
      </Modal>

      {/* ---------------- 站点编辑 ---------------- */}
      <Modal
        open={form !== null}
        title={isNew ? t('cert.addSiteTitle') : t('cert.editSiteTitle')}
        description={t('cert.editDesc')}
        size="lg"
        onClose={() => setForm(null)}
        footer={
          <>
            {!isNew && form?.id ? (
              <Button
                variant="ghost"
                icon={<IconCloud size={14} />}
                onClick={() => {
                  const site = form as CertSite;
                  setPickerSite(site);
                  setRemote(null);
                }}
              >
                {t('cert.chooseExisting')}
              </Button>
            ) : null}
            <Button variant="secondary" onClick={() => setForm(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              icon={<IconSave size={14} />}
              loading={busy === "site"}
              onClick={saveSite}
            >
              {t('common.save')}
            </Button>
          </>
        }
      >
        {form ? (
          <div className="form-grid">
            {/* 字段错误就地标红，这里只汇总一句「还有几处」：表单很长，
                在底部按保存的人看不到顶部哪一格有问题 */}
            {submitTried && formIssueCount > 0 ? (
              <div style={{ gridColumn: "1 / -1" }}>
                <Notice tone="danger">{t('cert.formInvalid', { n: formIssueCount })}</Notice>
              </div>
            ) : null}
            {isNew && (query.data?.templates ?? []).length > 0 ? (
              <Select
                label={t('cert.fieldTemplate')}
                value=""
                options={[
                  { value: "", label: t('cert.templateNone') },
                  ...(query.data?.templates ?? []).map((template) => ({
                    value: template.id,
                    label: template.name,
                  })),
                ]}
                hint={t('cert.templateHint')}
                onChange={(e) => applyTemplate(e.target.value)}
              />
            ) : null}
            <Input
              label={t('cert.fieldSiteName')}
              value={form.name || ""}
              placeholder={t('cert.siteNamePlaceholder')}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <Input
              label={t('cert.fieldDomain')}
              required
              value={form.domain || ""}
              placeholder="blog.example.com"
              className="mono"
              error={submitTried ? formErrors.domain : undefined}
              hint={t('cert.domainHint')}
              onChange={(e) => setForm({ ...form, domain: e.target.value })}
            />
            <Select
              label={t('cert.fieldCa')}
              value={form.ca || "tencent"}
              options={options?.cas ?? []}
              hint={
                form.ca === "upload"
                  ? t('cert.caHintUpload')
                  : form.ca === "acme"
                    ? t('cert.caHintAcme')
                    : form.ca === "tencent"
                      ? t('cert.caHintTencent')
                      : t('cert.caHintProvider')
              }
              onChange={(e) => setForm({ ...form, ca: e.target.value as CertSite["ca"] })}
            />
            {form.ca === "acme" ? (
              <>
                <Input
                  label={t('cert.fieldExtraDomains')}
                  value={(form.domains ?? []).join(", ")}
                  placeholder="www.example.com, *.example.com"
                  className="mono"
                  hint={t('cert.extraDomainsHint')}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      domains: e.target.value
                        .split(/[,;\s]+/)
                        .map((v) => v.trim())
                        .filter(Boolean),
                    })
                  }
                />
                <Select
                  label={t('cert.fieldAcmeChallenge')}
                  value={form.acme_challenge || "dns"}
                  options={options?.acme_challenges ?? []}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      acme_challenge: e.target.value as CertSite["acme_challenge"],
                    })
                  }
                />
                {form.acme_challenge === "http" ? (
                  <Input
                    label={t('cert.fieldAcmeWebroot')}
                    value={form.acme_webroot || ""}
                    placeholder="/var/www/html"
                    className="mono"
                    hint={t('cert.acmeWebrootHint')}
                    onChange={(e) => setForm({ ...form, acme_webroot: e.target.value })}
                  />
                ) : (
                  <Select
                    label={t('cert.fieldAcmeDnsProvider')}
                    value={form.acme_dns_provider || ""}
                    options={[
                      { value: "", label: t('cert.acmeFollowAccount') },
                      ...(options?.dns_providers ?? []),
                    ]}
                    hint={t('cert.acmeDnsProviderHint')}
                    onChange={(e) =>
                      setForm({ ...form, acme_dns_provider: e.target.value })
                    }
                  />
                )}
              </>
            ) : null}
            <Select
              label={t('cert.fieldDeployMethod')}
              value={form.deploy_method || "local"}
              options={options?.deploy_methods ?? []}
              hint={
                form.deploy_method === "local"
                  ? t('cert.deployHintLocal')
                  : form.deploy_method === "ssh"
                    ? t('cert.deployHintSsh')
                    : form.deploy_method === "agent"
                      ? t('cert.deployHintAgent')
                      : form.deploy_method === "lxc"
                        ? t('cert.deployHintLxc')
                        : t('cert.deployHintPve')
              }
              onChange={(e) =>
                setForm({ ...form, deploy_method: e.target.value as CertSite["deploy_method"] })
              }
            />
            {form.deploy_method === "local" && query.data?.local_deploy_hint ? (
              <div style={{ gridColumn: "1 / -1" }}>
                <Notice tone="warning">{query.data.local_deploy_hint}</Notice>
              </div>
            ) : null}
            {form.deploy_method === "ssh" ? (
              <>
                <Input
                  label={t('cert.fieldSshHost')}
                  required
                  value={form.ssh_host || ""}
                  placeholder={t('cert.sshHostPlaceholder')}
                  className="mono"
                  error={submitTried ? formErrors.ssh_host : undefined}
                  hint={t('cert.sshHostHint')}
                  onChange={(e) => setForm({ ...form, ssh_host: e.target.value })}
                />
                <Input
                  label={t('cert.fieldSshPort')}
                  type="number"
                  value={form.ssh_port ?? 22}
                  error={submitTried ? formErrors.ssh_port : undefined}
                  onChange={(e) => setForm({ ...form, ssh_port: Number(e.target.value) })}
                />
                <Input
                  label={t('cert.fieldSshUser')}
                  required
                  value={form.ssh_user || ""}
                  placeholder="root"
                  className="mono"
                  error={submitTried ? formErrors.ssh_user : undefined}
                  hint={t('cert.sshUserHint')}
                  onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                />
                <Select
                  label={t('cert.fieldSshAuth')}
                  value={form.ssh_auth || "password"}
                  options={[
                    { value: "password", label: t('cert.authPassword') },
                    { value: "key", label: t('cert.authKey') },
                  ]}
                  onChange={(e) => setForm({ ...form, ssh_auth: e.target.value })}
                />
                {form.ssh_auth === "key" ? (
                  <div style={{ gridColumn: "1 / -1" }}>
                    <Textarea
                      label={t('cert.fieldSshKey')}
                      rows={5}
                      mono
                      value={form.ssh_key || ""}
                      placeholder={
                        form.ssh_key_set
                          ? t('cert.sshKeyPlaceholderSet')
                          : "-----BEGIN OPENSSH PRIVATE KEY----- …"
                      }
                      hint={t('cert.sshKeyHint')}
                      onChange={(e) => setForm({ ...form, ssh_key: e.target.value })}
                    />
                  </div>
                ) : (
                  <Input
                    label={t('cert.fieldSshPassword')}
                    type="password"
                    value={form.ssh_password || ""}
                    placeholder={
                      form.ssh_password_set
                        ? t('cert.secretPlaceholderSet')
                        : t('cert.sshPasswordPlaceholder')
                    }
                    autoComplete="new-password"
                    hint={t('cert.sshPasswordHint')}
                    onChange={(e) => setForm({ ...form, ssh_password: e.target.value })}
                  />
                )}
                <div className="field">
                  <div className="field-label">{t('cert.fieldHostKey')}</div>
                  <div className="fs-xs text-muted mono" style={{ wordBreak: "break-all" }}>
                    {form.ssh_host_key || t('cert.hostKeyPlaceholder')}
                  </div>
                </div>
              </>
            ) : null}
            {multiConnection &&
            ["agent", "lxc", "pve"].includes(String(form.deploy_method || "")) ? (
              <Select
                label={t('cert.fieldPveConnection')}
                value={form.connection_id || ""}
                options={[
                  { label: t('cert.connectionAuto'), value: "" },
                  ...connectionOptions,
                ]}
                hint={t('cert.pveConnectionHint')}
                onChange={(e) => {
                  const connectionId = e.target.value;
                  /* 先选集群、再选机器：换了集群，原来那台不一定还在这套 PVE 上。
                     不清掉的话表单里会留着一条跨集群的 node/vmid，提交后写到另一套
                     集群里同名节点上的同号机器（agent 方式还会在那台客户机里执行命令）。 */
                  const identity = `${form.agent_node}|${form.agent_vmid}`;
                  const keepsVm =
                    Boolean(form.agent_node && form.agent_vmid) &&
                    (vmsQuery.data ?? []).some(
                      (vm) =>
                        isRunning(vm.status) &&
                        !vm.template &&
                        `${vm.node}|${vm.vmid}` === identity &&
                        (!connectionId || (vm.connection_id ?? "") === connectionId),
                    );
                  setForm({
                    ...form,
                    connection_id: connectionId,
                    ...(keepsVm ? {} : { agent_node: "", agent_vmid: "" }),
                  });
                }}
              />
            ) : null}
            {form.deploy_method === "agent" ? (
              <Select
                label={t('cert.fieldAgentVm')}
                value={agentValue}
                placeholder={t('cert.agentVmPlaceholder')}
                options={agentPickerOptions}
                hint={t('cert.agentVmHint')}
                /* 机器一多逐个按键找太慢：输入即按「名称 / VMID」过滤（label 里
                   本来就带 VMID 与节点，见 cert.agentOption） */
                searchable
                onChange={(e) => {
                  // 值形如「连接|节点|VMID」：选择机器时把连接一并记下来
                  const [connectionId = "", node = "", vmid = ""] = String(
                    e.target.value,
                  ).split("|");
                  setForm({
                    ...form,
                    connection_id: connectionId,
                    agent_node: node || "",
                    agent_vmid: vmid || "",
                  });
                }}
              />
            ) : null}
            {form.deploy_method === "lxc" ? (
              <>
                <Input
                  label={t('cert.fieldNode')}
                  required
                  value={form.agent_node || ""}
                  placeholder="pve"
                  className="mono"
                  onChange={(e) => setForm({ ...form, agent_node: e.target.value })}
                />
                <Input
                  label={t('cert.fieldCtId')}
                  required
                  value={form.agent_vmid || ""}
                  placeholder="101"
                  className="mono"
                  onChange={(e) => setForm({ ...form, agent_vmid: e.target.value })}
                />
                <Input
                  label={t('cert.fieldSshHost')}
                  required
                  value={form.ssh_host || ""}
                  placeholder="10.0.0.10"
                  className="mono"
                  hint={t('cert.nodeHostHint')}
                  onChange={(e) => setForm({ ...form, ssh_host: e.target.value })}
                />
                <Input
                  label={t('cert.fieldSshUser')}
                  value={form.ssh_user || "root"}
                  className="mono"
                  onChange={(e) => setForm({ ...form, ssh_user: e.target.value })}
                />
                <Input
                  label={t('cert.fieldSshPassword')}
                  type="password"
                  autoComplete="new-password"
                  value={form.ssh_password || ""}
                  placeholder={form.ssh_password_set ? t('cert.secretPlaceholderSet') : ""}
                  onChange={(e) => setForm({ ...form, ssh_password: e.target.value })}
                />
              </>
            ) : null}
            {form.deploy_method === "pve" ? (
              <Input
                label={t('cert.fieldNode')}
                required
                value={form.agent_node || ""}
                placeholder="pve1"
                className="mono"
                hint={t('cert.pveNodeHint')}
                onChange={(e) => setForm({ ...form, agent_node: e.target.value })}
              />
            ) : null}
            {form.deploy_method !== "pve" && !isCloudDeploy(form.deploy_method) ? (
              <>
            <Input
              label={
                form.deploy_method === "local"
                  ? t('cert.fieldDeployDir')
                  : t('cert.fieldTargetDeployDir')
              }
              required
              value={form.deploy_dir || ""}
              placeholder={t('cert.deployDirPlaceholder')}
              className="mono"
              hint={
                form.deploy_method === "ssh"
                  ? t('cert.deployDirHintSsh')
                  : form.deploy_method === "agent"
                    ? t('cert.deployDirHintAgent')
                    : t('cert.deployDirHintLocal')
              }
              onChange={(e) => setForm({ ...form, deploy_dir: e.target.value })}
            />
            <Input
              label={t('cert.fieldCertFile')}
              value={form.cert_filename || ""}
              placeholder="fullchain.pem"
              className="mono"
              hint={t('cert.certFileHint')}
              onChange={(e) => setForm({ ...form, cert_filename: e.target.value })}
            />
            <Input
              label={t('cert.fieldKeyFile')}
              value={form.key_filename || ""}
              placeholder="privkey.pem"
              className="mono"
              hint={t('cert.keyFileHint')}
              onChange={(e) => setForm({ ...form, key_filename: e.target.value })}
            />
            <Input
              label={t('cert.fieldReloadCmd')}
              value={form.reload_command || ""}
              placeholder="nginx -s reload"
              className="mono"
              hint={
                form.deploy_method === "ssh"
                  ? t('cert.reloadCmdHintSsh')
                  : form.deploy_method === "agent"
                    ? t('cert.reloadCmdHintAgent')
                    : t('cert.reloadCmdHintLocal')
              }
              onChange={(e) => setForm({ ...form, reload_command: e.target.value })}
            />
              </>
            ) : null}
            {isCloudDeploy(form.deploy_method) ? (
              <>
                {form.deploy_method === "tencent_clb" ? (
                  <>
                    <Input
                      label={t('cert.fieldCloudRegion')}
                      value={form.cloud_region || ""}
                      placeholder="ap-guangzhou"
                      className="mono"
                      error={submitTried ? formErrors.cloud_region : undefined}
                      onChange={(e) => setForm({ ...form, cloud_region: e.target.value })}
                    />
                    <Input
                      label={t('cert.fieldLbId')}
                      value={form.lb_id || ""}
                      placeholder="lb-xxxxxxxx"
                      className="mono"
                      error={submitTried ? formErrors.lb_id : undefined}
                      onChange={(e) => setForm({ ...form, lb_id: e.target.value })}
                    />
                    <Input
                      label={t('cert.fieldListenerId')}
                      value={form.listener_id || ""}
                      placeholder="lbl-xxxxxxxx"
                      className="mono"
                      error={submitTried ? formErrors.listener_id : undefined}
                      onChange={(e) => setForm({ ...form, listener_id: e.target.value })}
                    />
                  </>
                ) : null}
                {form.deploy_method === "aliyun_slb" ? (
                  <>
                    <Input
                      label={t('cert.fieldLbId')}
                      value={form.lb_id || ""}
                      placeholder="lb-xxxxxxxx"
                      className="mono"
                      error={submitTried ? formErrors.lb_id : undefined}
                      onChange={(e) => setForm({ ...form, lb_id: e.target.value })}
                    />
                    <Input
                      label={t('cert.fieldListenerPort')}
                      value={String(form.listener_port ?? 443)}
                      placeholder="443"
                      className="mono"
                      error={submitTried ? formErrors.listener_port : undefined}
                      onChange={(e) =>
                        setForm({ ...form, listener_port: Number(e.target.value) })
                      }
                    />
                  </>
                ) : null}
                {form.deploy_method === "tencent_cdn" ||
                form.deploy_method === "aliyun_cdn" ? (
                  <Input
                    label={t('cert.fieldCdnDomain')}
                    value={form.cdn_domain || ""}
                    placeholder={t('cert.cdnDomainPlaceholder')}
                    className="mono"
                    error={submitTried ? formErrors.cdn_domain : undefined}
                    onChange={(e) => setForm({ ...form, cdn_domain: e.target.value })}
                  />
                ) : null}
              </>
            ) : null}
            <div className="cert-form-pair">
              <Input
                label={t('cert.fieldEnvironment')}
                value={form.environment || ""}
                placeholder={t('cert.environmentPlaceholder')}
                onChange={(e) => setForm({ ...form, environment: e.target.value })}
              />
              <Input
                label={t('cert.fieldCluster')}
                value={form.cluster || ""}
                placeholder={t('cert.clusterPlaceholder')}
                onChange={(e) => setForm({ ...form, cluster: e.target.value })}
              />
            </div>
            <Input
              label={t('cert.fieldRenewDays')}
              type="number"
              value={form.renew_before_days ?? 0}
              error={submitTried ? formErrors.renew_before_days : undefined}
              hint={t('cert.renewDaysHint')}
              onChange={(e) =>
                setForm({ ...form, renew_before_days: Number(e.target.value) })
              }
            />
            <div className="field">
              <Switch
                checked={form.auto_rotate === true}
                label={t('cert.autoRotateSwitch')}
                hint={t('cert.autoRotateSwitchHint')}
                onChange={(v) => setForm({ ...form, auto_rotate: v })}
              />
            </div>
            {form.auto_rotate ? (
              <Input
                label={t('cert.fieldRotateDays')}
                type="number"
                min={0}
                max={90}
                value={form.rotate_before_days ?? 0}
                hint={t('cert.rotateDaysHint')}
                onChange={(e) =>
                  setForm({ ...form, rotate_before_days: Number(e.target.value) })
                }
              />
            ) : null}
            <Input
              label={t('incident.fieldNote')}
              value={form.notes || ""}
              placeholder={t('cert.notesPlaceholder')}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
            />
            <div className="field">
              <Switch
                checked={form.auto_renew !== false}
                label={t('cert.autoRenewSwitch')}
                hint={t('cert.autoRenewSwitchHint')}
                onChange={(v) => setForm({ ...form, auto_renew: v })}
              />
            </div>
            <div className="field">
              <Switch
                checked={form.auto_deploy !== false}
                label={t('cert.autoDeploySwitch')}
                hint={t('cert.autoDeploySwitchHint')}
                onChange={(v) => setForm({ ...form, auto_deploy: v })}
              />
            </div>
            <div className="field">
              <Switch
                checked={form.notify_expiring !== false}
                label={t('cert.notifyExpiringSwitch')}
                hint={t('cert.notifyExpiringSwitchHint')}
                onChange={(v) => setForm({ ...form, notify_expiring: v })}
              />
            </div>
            <div className="field">
              <Switch
                checked={form.auto_delete_old !== false}
                label={t('cert.autoDeleteOld')}
                hint={t('cert.autoDeleteOldHint')}
                onChange={(v) => setForm({ ...form, auto_delete_old: v })}
              />
            </div>
            <div className="field">
              <Switch
                checked={form.enabled !== false}
                label={t('cert.enabledSwitch')}
                hint={t('cert.enabledSwitchHint')}
                onChange={(v) => setForm({ ...form, enabled: v })}
              />
            </div>
            <CertTargetsEditor
              targets={(form.targets ?? []).slice(1)}
              methods={options?.deploy_methods ?? []}
              disabled={!canManage}
              onChange={(extra) =>
                setForm({
                  ...form,
                  targets: [(form.targets ?? [])[0] ?? {}, ...extra],
                })
              }
            />
            <div className="field">
              <Switch
                checked={!!form.monitor_enabled}
                label={t('cert.monitorSwitch')}
                hint={t('cert.monitorSwitchHint')}
                onChange={(v) => setForm({ ...form, monitor_enabled: v })}
              />
            </div>
            {form.monitor_enabled ? (
              <Input
                label={t('cert.fieldMonitorUrl')}
                value={form.monitor_url || ""}
                placeholder={`https://${form.domain || "example.com"}`}
                className="mono"
                error={submitTried ? formErrors.monitor_url : undefined}
                hint={t('cert.monitorUrlHint')}
                onChange={(e) => setForm({ ...form, monitor_url: e.target.value })}
              />
            ) : null}
          </div>
        ) : null}
      </Modal>

      {/* ---------------- 腾讯云证书选择 ---------------- */}
      <Modal
        open={pickerSite !== null}
        title={t('cert.pickerTitle')}
        description={
          pickerSite ? t('cert.pickerDesc', { name: pickerSite.name }) : undefined
        }
        size="lg"
        onClose={() => setPickerSite(null)}
        footer={
          <Button variant="secondary" onClick={() => setPickerSite(null)}>
            {t('common.close')}
          </Button>
        }
      >
        <div className="flex items-center gap-8">
          <Input
            value={remoteSearch}
            placeholder={t('cert.remoteSearchPlaceholder')}
            onChange={(e) => setRemoteSearch(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") loadRemote();
            }}
          />
          <Button
            variant="secondary"
            icon={<IconSearch size={14} />}
            loading={busy === "remote"}
            onClick={loadRemote}
          >
            {t('cert.query')}
          </Button>
        </div>
        {remote && remote.length > 0 ? (
          <div className="table-container" style={{ marginTop: 12 }}>
            <table className="table table-dense">
              <thead>
                <tr>
                  <th>{t('cert.colDomain')}</th>
                  <th>{t('cert.colCertId')}</th>
                  <th>{t('common.status')}</th>
                  <th>{t('cert.colExpire')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {remote.map((cert) => (
                  <tr key={cert.cert_id}>
                    <td>
                      <div className="mono">{cert.domain}</div>
                      <div className="fs-xs text-muted">{cert.alias}</div>
                    </td>
                    <td className="mono fs-sm">{cert.cert_id}</td>
                    <td>
                      <Badge
                        variant={cert.status === 1 ? "success" : "warning"}
                        size="sm"
                        dot
                      >
                        {cert.status_text}
                      </Badge>
                    </td>
                    <td className="fs-sm text-secondary">
                      {cert.expire_at ? formatDate(cert.expire_at) : "—"}
                    </td>
                    <td>
                      <Button
                        variant="ghost"
                        size="sm"
                        loading={busy === "bind"}
                        disabled={cert.status !== 1}
                        onClick={() => bindRemote(cert)}
                      >
                        {t('cert.bind')}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-secondary fs-sm" style={{ marginTop: 12 }}>
            {remote ? t('cert.noMatchCerts') : t('cert.clickQuery')}
          </div>
        )}
      </Modal>

      {/* ---------------- 上传第三方证书：建站点 + 导入 + 可选部署 ---------------- */}
      <CertUploadDialog
        open={uploadOpen}
        busy={busy === "upload"}
        onClose={() => setUploadOpen(false)}
        onSubmit={submitUpload}
      />

      {/* ---------------- 上传托管证书（PEM / 私钥） ---------------- */}
      <Modal
        open={importTarget !== null}
        title={t('cert.importTitle')}
        description={t('cert.importDesc')}
        size="lg"
        onClose={() => setImportTarget(null)}
        footer={
          <>
            <Button variant="secondary" onClick={() => setImportTarget(null)}>
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              icon={<IconDownload size={14} />}
              loading={
                importTarget !== null && busy === "import:" + importTarget.id
              }
              disabled={!importPem.trim() || !importKey.trim()}
              onClick={submitImport}
            >
              {t('cert.uploadCert')}
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <Notice tone="warning">{t('cert.importKeyWarning')}</Notice>
          <Textarea
            label={t('cert.fieldPem')}
            required
            mono
            rows={8}
            value={importPem}
            placeholder={t('cert.pemPlaceholder')}
            hint={t('cert.importHint')}
            onChange={(e) => setImportPem(e.target.value)}
          />
          <Textarea
            label={t('cert.fieldKeyPem')}
            required
            mono
            rows={6}
            value={importKey}
            placeholder={t('cert.keyPemPlaceholder')}
            onChange={(e) => setImportKey(e.target.value)}
          />
        </div>
      </Modal>

      {/* ---------------- 多站点绑定（一张证书绑到多个站点） ---------------- */}
      <Modal
        open={bindCert !== null}
        title={t('cert.inventoryBindTitle')}
        description={
          bindCert ? t('cert.inventoryBindDesc', { id: bindCert.cert_id }) : undefined
        }
        size="lg"
        onClose={() => {
          setBindCert(null);
          setBindSites([]);
        }}
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setBindCert(null);
                setBindSites([]);
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              loading={busy === "inventory-bind"}
              disabled={bindSites.length === 0}
              onClick={submitBind}
            >
              {t('cert.inventoryBindConfirm')}
            </Button>
          </>
        }
      >
        <div className="fs-xs text-muted" style={{ marginBottom: 8 }}>
          {t('cert.inventoryBindHint')}
        </div>
        {bindCert?.domains?.length ? (
          <div className="fs-xs text-muted mono" style={{ marginBottom: 8 }}>
            {t('cert.inventoryBindCovered', { domains: bindCert.domains.join(", ") })}
          </div>
        ) : null}
        <div style={{ maxHeight: 320, overflowY: "auto" }}>
          {sites
            .filter((site) => !isForeign(site))
            .map((site) => (
              <label
                key={site.id}
                className="flex items-center gap-8 fs-sm"
                style={{ padding: "6px 0" }}
              >
                <input
                  type="checkbox"
                  checked={bindSites.includes(site.id)}
                  onChange={(e) =>
                    setBindSites(
                      e.target.checked
                        ? [...bindSites, site.id]
                        : bindSites.filter((id) => id !== site.id),
                    )
                  }
                />
                <span>{site.name || site.domain}</span>
                <span className="mono fs-xs text-muted">{site.domain}</span>
                {site.cert_id && site.cert_id !== bindCert?.cert_id ? (
                  <span className="fs-xs text-warning">
                    {t('cert.inventoryBindReplace')}
                  </span>
                ) : null}
              </label>
            ))}
        </div>
      </Modal>

      {/* ---------------- 证书详情（链 / SAN / 指纹 / 体检） ---------------- */}
      <Modal
        open={detailSite !== null}
        title={t('cert.detailTitle', { name: detailSite?.name || detailSite?.domain || "" })}
        description={t('cert.detailHint')}
        onClose={() => setDetailSite(null)}
        footer={
          <Button variant="secondary" onClick={() => setDetailSite(null)}>
            {t('common.close')}
          </Button>
        }
      >
        {detailQuery.isLoading ? (
          <div className="text-secondary fs-sm">{t('common.loading')}</div>
        ) : detailQuery.data ? (
          <CertDetailBody detail={detailQuery.data} />
        ) : detailQuery.error ? (
          <Notice tone="danger">{errText(detailQuery.error)}</Notice>
        ) : null}
      </Modal>

      {/* ---------------- 批量吊销确认 ---------------- */}
      <ConfirmDialog
        open={bulkRevoke}
        danger
        title={t('cert.bulkRevokeTitle')}
        confirmText={t('cert.bulkRevoke')}
        message={t('cert.bulkRevokeMessage', { n: picked.length })}
        onCancel={() => setBulkRevoke(false)}
        onConfirm={runBulkRevoke}
      />

      {/* ---------------- 证书导出（PEM / PFX） ---------------- */}
      <Modal
        open={exportCert !== null}
        title={t('cert.exportTitle')}
        description={t('cert.exportHint')}
        onClose={() => {
          setExportCert(null);
          setExportPassword("");
        }}
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                setExportCert(null);
                setExportPassword("");
              }}
            >
              {t('common.cancel')}
            </Button>
            <Button
              variant="primary"
              loading={busy === "export"}
              disabled={!exportCert || !canExport || !exportAck}
              onClick={submitExport}
            >
              {t('cert.exportConfirm')}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          <Notice tone="warning">{t('cert.exportWarning')}</Notice>
          <Select
            label={t('cert.exportFormat')}
            value={exportFormat}
            options={[
              { value: "pem", label: t('cert.exportPem') },
              { value: "pfx", label: t('cert.exportPfx') },
            ]}
            onChange={(e) => setExportFormat(e.target.value as "pem" | "pfx")}
          />
          <Input
            label={t('cert.exportPassword')}
            type="password"
            value={exportPassword}
            hint={t('cert.exportPasswordHint')}
            onChange={(e) => setExportPassword(e.target.value)}
          />
          <Checkbox
            label={t('cert.exportAck')}
            checked={exportAck}
            onChange={(e) => setExportAck(e.target.checked)}
          />
        </div>
      </Modal>

      {/* ---------------- 删除确认 ---------------- */}
      <ConfirmDialog
        open={deleteTarget !== null}
        danger
        title={t('cert.deleteTitle')}
        confirmText={t('common.delete')}
        message={t('cert.deleteMessage', {
          name: deleteTarget?.name ?? "",
          domain: deleteTarget?.domain ?? "",
        })}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={async () => {
          const ok = await run(
            "delete",
            async () => {
              if (!deleteTarget) return "";
              const r = await certsApi.removeSite(deleteTarget.id);
              return r.detail;
            },
            t('cert.siteDeleted'),
          );
          if (ok) setDeleteTarget(null);
        }}
      />

      {/* ---------------- 吊销确认 ---------------- */}
      <ConfirmDialog
        open={revokeTarget !== null}
        danger
        title={t('cert.revokeTitle')}
        confirmText={t('cert.revokeCert')}
        message={t('cert.revokeMessage', {
          name: revokeTarget?.name ?? "",
          domain: revokeTarget?.domain ?? "",
        })}
        onCancel={() => setRevokeTarget(null)}
        onConfirm={async () => {
          const ok = await run(
            "revoke",
            async () => {
              if (!revokeTarget) return "";
              const r = await certsApi.revoke(revokeTarget.id);
              return r.detail;
            },
            t('cert.revoked'),
          );
          if (ok) setRevokeTarget(null);
        }}
      />

    </PageShell>
  );
}

/* ---------------------------------------------------------------- 证书详情 */

const HEALTH_ISSUE_LABEL: Record<string, MessageKey> = {
  expired: 'cert.issueExpired',
  expiring: 'cert.issueExpiring',
  weak_protocol: 'cert.issueWeakProtocol',
  hostname_mismatch: 'cert.issueHostname',
  chain_untrusted: 'cert.issueChain',
  weak_key: 'cert.issueWeakKey',
  weak_signature: 'cert.issueWeakSignature',
};

function DetailField({
  label,
  value,
  mono,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="cert-detail-field">
      <span className="cert-detail-label">{label}</span>
      <span className={`cert-detail-value${mono ? " mono" : ""}`}>{value || "—"}</span>
    </div>
  );
}

/** 详情弹窗正文：先给结论（体检），再给证据（叶证书 / 链 / 部署目标）。 */
function CertDetailBody({ detail }: { detail: CertDetail }) {
  const t = useT();
  const leaf = detail.leaf || {};
  const issues = detail.health.issues || [];
  const healthTone =
    detail.health.status === "ok"
      ? "success"
      : detail.health.status === "bad"
        ? "danger"
        : "warning";

  return (
    <div className="cert-detail-body">
      <div className="cert-detail-grid">
        <DetailField label={t('cert.detailDomain')} value={detail.domain} mono />
        <DetailField label={t('cert.detailCa')} value={detail.ca} />
        <DetailField label={t('cert.detailStatus')} value={detail.status_text} />
        <DetailField
          label={t('cert.detailExpire')}
          value={detail.expire_at ? formatDateTime(detail.expire_at) : "—"}
        />
        <DetailField
          label={t('cert.detailDays')}
          value={detail.days_left === null || detail.days_left === undefined ? "—" : detail.days_left}
        />
        <DetailField
          label={t('cert.detailEnv')}
          value={[detail.environment, detail.cluster].filter(Boolean).join(" / ")}
        />
      </div>

      <div className="cert-detail-section">
        <h4>{t('cert.detailHealth')}</h4>
        <div className="cert-detail-health">
          <Badge variant={healthTone} size="sm" dot>
            {detail.health.status || t('cert.healthUnknown')}
          </Badge>
          <span className="fs-xs text-muted">
            {t('cert.detailProtocol')}: {detail.health.protocol || "—"}
          </span>
          <span className="fs-xs text-muted">
            {t('cert.detailTrusted')}: {detail.health.trusted ? t('common.yes') : t('common.no')}
          </span>
          <span className="fs-xs text-muted">
            {t('cert.detailHostname')}: {detail.health.hostname_ok ? t('common.yes') : t('common.no')}
          </span>
        </div>
        {issues.length ? (
          <div className="cert-detail-issues">
            {issues.map((issue) => (
              <Badge key={issue} variant="warning" size="sm">
                {t(HEALTH_ISSUE_LABEL[issue] ?? 'cert.issueOther')}
              </Badge>
            ))}
          </div>
        ) : null}
      </div>

      {leaf.subject ? (
        <div className="cert-detail-section">
          <h4>{t('cert.detailLeaf')}</h4>
          <div className="cert-detail-grid">
            <DetailField label={t('cert.detailSubject')} value={leaf.subject} mono />
            <DetailField label={t('cert.detailIssuer')} value={leaf.issuer} mono />
            <DetailField label={t('cert.detailSerial')} value={leaf.serial} mono />
            <DetailField
              label={t('cert.detailFingerprint')}
              value={leaf.fingerprint_sha256}
              mono
            />
            <DetailField
              label={t('cert.detailKey')}
              value={leaf.key ? `${leaf.key.type} ${leaf.key.bits || ""}`.trim() : "—"}
            />
            <DetailField label={t('cert.detailSig')} value={leaf.signature_hash} />
          </div>
          <div className="cert-detail-san mono">{(leaf.san || []).join(", ") || "—"}</div>
        </div>
      ) : (
        <Notice tone="warning">{t('cert.detailNoCert')}</Notice>
      )}

      {detail.chain.length ? (
        <div className="cert-detail-section">
          <h4>{t('cert.detailChain', { n: detail.chain.length })}</h4>
          {detail.chain.map((item) => (
            <div key={item.fingerprint_sha256} className="cert-detail-chain">
              <span className="mono">{item.subject}</span>
              <span className="fs-xs text-muted">
                {formatDateTime(item.not_after)}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {detail.targets.length ? (
        <div className="cert-detail-section">
          <h4>{t('cert.detailTargets', { n: detail.targets.length })}</h4>
          {detail.targets.map((target) => (
            <div key={target.id || target.name} className="cert-detail-chain">
              <span>{target.name || target.method}</span>
              <span className="fs-xs text-muted">
                {target.last_error
                  ? target.last_error
                  : target.last_deploy_at
                    ? formatDateTime(target.last_deploy_at)
                    : "—"}
              </span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}
