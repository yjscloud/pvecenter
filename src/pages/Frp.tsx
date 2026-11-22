/* ==========================================================================
   pvecenter — 内网穿透（frp 客户端）
   --------------------------------------------------------------------------
   按职责拆成两块：
   - 服务端：frps 地址 / token / 启停 / 安装 —— 仅 admin 可写（settings.manage）
   - 规则：每条带 username 归属 —— frp.manage 用户能增删改自己的规则，
     普通用户看不到别人的规则
   运行时 server + 全部规则合并渲染成 frpc.toml（见后端 effective_config）
   ========================================================================== */

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { frpApi } from "../api/endpoints";
import { errorMessage } from "../api/client";
import { PageShell } from "../components/Layout";
import { Card, CardHeader } from "../components/ui/Card";
import { Badge } from "../components/ui/Badge";
import { Button, IconButton } from "../components/ui/Button";
import { Notice } from "../components/ui/EmptyState";
import { Field, Input, Select, Switch } from "../components/ui/Input";
import { TargetSelect, type TargetChoice } from "../components/ui/TargetSelect";
import { Modal } from "../components/ui/Modal";
import {
  IconNetwork,
  /* 页面标题用与侧边栏同一枚隧道字形：同一个功能在导航与页头是同一个标志 */
  IconFrp,
  IconPlay,
  IconStop,
  IconPlus,
  IconTrash,
  IconRefresh,
  IconDownload,
  IconServer,
  IconUsers,
  IconCopy,
  IconCheck,
  IconSettings,
  IconSave,
  IconChevronDown,
} from "../components/Icons";
import type {
  FrpGrant,
  FrpPolicy,
  FrpRule,
  FrpRuleInput,
  FrpRulePatch,
} from "../api/types";
import { useAuth } from "../hooks/useAuth";
import { PagerBar, usePaged } from "../hooks/usePaged";
import { useRefreshInterval } from "../hooks/useRefreshInterval";
import { useToast } from "../hooks/useToast";
import { useT } from "../i18n";

export function Frp() {
  const t = useT();
  const { isAdmin } = useAuth();
  return (
    <PageShell
      title={
        <>
          <IconFrp size={20} />
          {t('frp.title')}
        </>
      }
      subtitle={t('frp.subtitle')}
      actions={<HeaderActions />}
    >
      <StatusRow />

      <div className="grid grid-2">
        <ServerCard />
        <ProcessCard />
      </div>

      <RulesCard />
      <GrantsCard />
      {/* frpc 进程日志**仅超级管理员**可见：日志里带着 frps 地址、连接失败的真实
          原因和每条代理的起停顺序，普通用户需要的信息（我这条隧道通不通）在规则
          列表与生效配置里已经足够。后端 /api/frp/logs 同样是 require_admin，
          这里不渲染卡片，顺带省掉一个必然 403 的轮询。 */}
      {isAdmin ? <LogCard /> : null}
    </PageShell>
  );
}

/* ---------------------------------------------------------------------------
   顶部右侧动作：手动启停 frpc
   --------------------------------------------------------------------------- */
function HeaderActions() {
  const t = useT();
  const { hasPermission } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  /* 轮询间隔跟「设置 → 面板设置 → 数据刷新间隔」走（见 useRefreshInterval） */
  const refresh = useRefreshInterval();
  const [busy, setBusy] = useState(false);

  const statusQuery = useQuery({
    queryKey: ["frp", "status"],
    queryFn: frpApi.status,
    refetchInterval: refresh,
  });
  const running = statusQuery.data?.running ?? false;
  const canManage = hasPermission("settings.manage");

  return (
    <Button
      variant={running ? "danger" : "primary"}
      icon={running ? <IconStop size={15} /> : <IconPlay size={15} />}
      loading={busy}
      disabled={!canManage || statusQuery.isLoading}
      onClick={async () => {
        if (!canManage) {
          toast.error(t('frp.noPermission'), t('frp.noPermissionDetail'));
          return;
        }
        setBusy(true);
        try {
          if (running) {
            await frpApi.server.stop();
            toast.success(t('frp.stopped'), t('frp.stoppedDetail'));
          } else {
            await frpApi.server.start();
            toast.success(t('frp.started'));
          }
          void qc.invalidateQueries({ queryKey: ["frp"] });
        } catch (err) {
          toast.error(t('common.opFailed'), errorMessage(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      {running ? t('frp.stopTunnel') : t('frp.startTunnel')}
    </Button>
  );
}

/* ---------------------------------------------------------------------------
   状态行：运行状态 / frpc 二进制 / 规则数
   --------------------------------------------------------------------------- */
function StatusRow() {
  const t = useT();
  const { hasPermission } = useAuth();
  const refresh = useRefreshInterval();
  const statusQuery = useQuery({
    queryKey: ["frp", "status"],
    queryFn: frpApi.status,
    refetchInterval: refresh,
  });
  const rulesQuery = useQuery({
    queryKey: ["frp", "rules"],
    queryFn: frpApi.rules.list,
    refetchInterval: refresh,
  });
  const serverQuery = useQuery({
    queryKey: ["frp", "server"],
    queryFn: frpApi.server.get,
    refetchInterval: refresh,
  });

  const running = statusQuery.data?.running ?? false;
  const available = statusQuery.data?.available ?? false;
  /* 装的是哪一版，取自二进制本身（后端跑一次 frpc -v 并缓存） */
  const version = (statusQuery.data?.version ?? "").trim();
  const ruleCount = rulesQuery.data?.rules.length ?? 0;
  /* 服务端地址只有管理员能拿到（见 /api/frp/server）：普通用户看到的是
     「由管理员配置」，而不是一个看起来像「还没配」的空地址。 */
  const canManage = hasPermission("settings.manage");
  const serverAddr = serverQuery.data?.server_addr ?? "";
  const serverPort = serverQuery.data?.server_port ?? 7000;
  const tokenSet = serverQuery.data?.token_set ?? false;

  return (
    <Card collapsible={false}>
      <CardHeader title={t('frp.statusTitle')} icon={<IconNetwork size={16} />} />
      <div className="grid grid-4">
        <Stat label={t('frp.statProcess')}>
          <Badge variant={running ? "success" : "neutral"} dot pulse={running} size="sm">
            {running ? t('frp.running') : t('frp.stoppedState')}
          </Badge>
        </Stat>
        <Stat label={t('frp.statClient')}>
          <Badge variant={available ? "success" : "warning"} size="sm">
            {available
              ? version
                ? t('frp.installedWithVersion', { version })
                : t('frp.installed')
              : t('frp.notInstalled')}
          </Badge>
        </Stat>
        <Stat label={t('frp.statServer')}>
          {serverAddr ? (
            <span className="mono fs-sm">
              {serverAddr}:{serverPort}
              {tokenSet ? <span className="text-success"> · token</span> : null}
            </span>
          ) : canManage ? (
            <span className="text-warning fs-sm">{t('frp.notConfigured')}</span>
          ) : (
            <span className="text-secondary fs-sm">
              {t('frp.serverManagedByAdmin')}
            </span>
          )}
        </Stat>
        <Stat label={t('frp.statRules')}>
          <span className="mono">{t('frp.ruleCount', { n: ruleCount })}</span>
        </Stat>
      </div>
    </Card>
  );
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-6">
      <span className="fs-xs text-muted">{label}</span>
      <div>{children}</div>
    </div>
  );
}

/* ---------------------------------------------------------------------------
   服务端配置（admin 可写，其他人只读）
   --------------------------------------------------------------------------- */
const EMPTY_SERVER = {
  server_addr: "",
  server_port: 7000,
  public_addr: "",
  token: "",
};

type ServerDraft = typeof EMPTY_SERVER;
type ServerField = keyof ServerDraft;

/**
 * 地址类字段只接受「主机」形态：域名、IPv4 或 IPv6 字面量。
 *
 * 服务端在这里填的是 frps 的主机名 / IP，不是 URL —— 带着 ``http://``、端口或
 * 路径粘进来是最常见的错（从浏览器地址栏直接复制），后端会把它当成主机名去解析，
 * 失败时只报「连不上」。所以这里先拦一道，并在提示里说清该填什么。
 * 允许 ``:`` 与 ``[]`` 是为了 IPv6（``fe80::1`` / ``[::1]``）。
 */
const HOST_LIKE = /^[A-Za-z0-9._:[\]-]+$/;

function isHostLike(value: string): boolean {
  const v = value.trim();
  return v.length > 0 && HOST_LIKE.test(v) && !v.includes("//");
}

function ServerCard() {
  const t = useT();
  const { hasPermission } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const canManage = hasPermission("settings.manage");
  const refresh = useRefreshInterval();

  const serverQuery = useQuery({
    queryKey: ["frp", "server"],
    queryFn: frpApi.server.get,
    refetchInterval: refresh,
  });

  const [draft, setDraft] = useState<ServerDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [tokenDraft, setTokenDraft] = useState("");
  /* 高级选项默认收起；已经配过对外地址的展开着 —— 折叠不该藏住已生效的配置。 */
  const [advancedOpen, setAdvancedOpen] = useState(false);
  /* 校验只在「碰过这个字段」或「点过一次保存」之后才显示：
     一进页面就满屏红字，骂的是用户还没做的事。 */
  const [touched, setTouched] = useState<Partial<Record<ServerField, boolean>>>({});
  const [attempted, setAttempted] = useState(false);

  useEffect(() => {
    if (serverQuery.data && draft === null) {
      const initial: ServerDraft = {
        server_addr: serverQuery.data.server_addr,
        server_port: serverQuery.data.server_port,
        public_addr: serverQuery.data.public_addr ?? "",
        token: "",
      };
      setDraft(initial);
      setAdvancedOpen(Boolean(initial.public_addr.trim()));
    }
  }, [serverQuery.data, draft]);

  const dirty =
    draft !== null &&
    serverQuery.data !== undefined &&
    (draft.server_addr !== serverQuery.data.server_addr ||
      draft.server_port !== serverQuery.data.server_port ||
      draft.public_addr.trim() !== (serverQuery.data.public_addr ?? "") ||
      Boolean(tokenDraft));

  const errors = useMemo(() => {
    const out: Partial<Record<ServerField, string>> = {};
    if (!draft) return out;
    const addr = draft.server_addr.trim();
    if (!addr) {
      out.server_addr = t('frp.errAddrRequired');
    } else if (!isHostLike(addr)) {
      out.server_addr = t('frp.errAddrShape');
    }
    /* 0 = 输入框被清空（Number("") 的结果），与「超出范围」分开提示：
       用户删光一个数字和填了 70000 想改的东西并不一样 */
    if (!draft.server_port) {
      out.server_port = t('frp.errPortRequired');
    } else if (
      !Number.isInteger(draft.server_port) ||
      draft.server_port < 1 ||
      draft.server_port > 65535
    ) {
      out.server_port = t('frp.errPortRange');
    }
    const pub = draft.public_addr.trim();
    if (pub && !isHostLike(pub)) {
      out.public_addr = t('frp.errAddrShape');
    }
    return out;
    /* t 参与依赖：切语言时错误文案要跟着换 */
  }, [draft, t]);

  const errorFor = (field: ServerField): string | undefined =>
    attempted || touched[field] ? errors[field] : undefined;

  const touch = (field: ServerField) =>
    setTouched((prev) => (prev[field] ? prev : { ...prev, [field]: true }));

  /** 放弃改动：回到服务端当前值（含 token 草稿与校验状态）。 */
  function discard() {
    if (!serverQuery.data) return;
    setDraft({
      server_addr: serverQuery.data.server_addr,
      server_port: serverQuery.data.server_port,
      public_addr: serverQuery.data.public_addr ?? "",
      token: "",
    });
    setTokenDraft("");
    setTouched({});
    setAttempted(false);
  }

  async function save() {
    if (!draft) return;
    /* 地址 / 端口错了就不该发出去：后端拒的是「连不上」，用户拿到的是
       一句笼统的失败，还得回来猜是哪个字段的问题。 */
    const firstError = Object.keys(errors)[0] as ServerField | undefined;
    if (firstError) {
      setAttempted(true);
      /* 错误在折叠区里时先把那一段展开，否则提示红字在页面上根本找不到 */
      if (errors.public_addr) setAdvancedOpen(true);
      toast.error(t('frp.fixErrorsFirst'), errors[firstError]);
      return;
    }
    setBusy(true);
    try {
      const payload: {
        server_addr: string;
        server_port: number;
        public_addr: string;
        token?: string;
        token_clear?: boolean;
      } = {
        server_addr: draft.server_addr.trim(),
        server_port: draft.server_port,
        public_addr: draft.public_addr.trim(),
      };
      if (tokenDraft) {
        payload.token = tokenDraft;
      } else if (serverQuery.data?.token_set === false) {
        // 从未设置过但还是空字符串 —— 不主动发 token_clear
      }
      const result = await frpApi.server.save(payload);
      if (result.restart_error) {
        toast.warning(
          t('frp.savedTitle'),
          t('frp.savedRestartFailedDetail', { error: result.restart_error }),
        );
      } else if (result.restarted) {
        toast.success(t('frp.savedRestarted'));
      } else {
        toast.success(t('frp.saved'));
      }
      /* 用「真正发出去的值」回填草稿：后端会 trim，若草稿里还留着那个空格，
         重新拉回来的数据就与草稿不相等，「未保存」徽章会永远亮着。 */
      setDraft({
        server_addr: payload.server_addr,
        server_port: payload.server_port,
        public_addr: payload.public_addr,
        token: "",
      });
      setTokenDraft("");
      setAttempted(false);
      setTouched({});
      void qc.invalidateQueries({ queryKey: ["frp"] });
    } catch (err) {
      toast.error(t('frp.saveFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function clearToken() {
    if (!draft) return;
    setBusy(true);
    try {
      await frpApi.server.save({
        server_addr: draft.server_addr,
        server_port: draft.server_port,
        /* 后端按「未传即重置」规整服务端配置，漏传会把对外地址一起清掉 */
        public_addr: draft.public_addr.trim(),
        token_clear: true,
      });
      toast.success(t('frp.tokenCleared'));
      void qc.invalidateQueries({ queryKey: ["frp"] });
    } catch (err) {
      toast.error(t('frp.clearFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card collapsible={false} className="frp-server-card">
      <CardHeader
        title={t('frp.serverTitle')}
        icon={<IconServer size={16} />}
        subtitle={
          canManage ? t('frp.serverSubtitleAdmin') : t('frp.serverSubtitleUser')
        }
        actions={
          canManage ? (
            <>
              {/* 改动是否落盘必须一眼可见：地址栏改了两个字符和「根本没动过」
                  长得一样，而这张卡的保存是手动的，没有这个标记就只能靠记忆。 */}
              {dirty ? (
                <Badge variant="warning" size="sm" dot>
                  {t('frp.unsaved')}
                </Badge>
              ) : null}
              {dirty ? (
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={busy}
                  onClick={discard}
                >
                  {t('frp.discard')}
                </Button>
              ) : null}
              <Button
                variant="primary"
                size="sm"
                icon={<IconSave size={14} />}
                loading={busy}
                disabled={!dirty}
                /* 置灰时说明为什么点不动：没有改动 vs 有错要改，
                   两种原因给的提示不一样，否则用户只会以为按钮坏了 */
                title={dirty ? undefined : t('frp.saveDisabledHint')}
                onClick={() => void save()}
              >
                {t('frp.saveChanges')}
              </Button>
            </>
          ) : null
        }
      />
      {!canManage ? (
        /* 服务端配置只对管理员有意义：地址与 token 都不下发（见 /api/frp/server），
           普通用户看到一句说明，而不是几个禁用状态的空输入框。 */
        <p className="fs-sm text-secondary">{t('frp.serverManagedByAdmin')}</p>
      ) : draft ? (
        <div className="frp-form">
          {/* 基础参数：决定「连不连得上」，两项都必填 */}
          <section className="frp-form-section">
            <div className="frp-form-legend">
              <h4 className="frp-form-legend-title">{t('frp.sectionBasic')}</h4>
              <span className="frp-form-legend-hint">
                {t('frp.sectionBasicHint')}
              </span>
            </div>
            {/* .form-grid 是 auto-fit：这张卡在桌面上常常只占半栏（约 490px），
                写死两列会把两条说明挤成三行；宽的时候它自己并排，窄的时候自己叠。 */}
            <div className="form-grid">
              <Field
                label={t('frp.fieldServerAddr')}
                required
                htmlFor="frp-server-addr"
                error={errorFor('server_addr')}
                hint={t('frp.serverAddrHint')}
              >
                <Input
                  id="frp-server-addr"
                  mono
                  value={draft.server_addr}
                  placeholder={t('frp.serverAddrPlaceholder')}
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={errorFor('server_addr') ? true : undefined}
                  disabled={!canManage}
                  onChange={(e) =>
                    setDraft({ ...draft, server_addr: e.target.value })
                  }
                  onBlur={() => touch('server_addr')}
                />
              </Field>
              <Field
                label={t('frp.fieldServerPort')}
                required
                htmlFor="frp-server-port"
                error={errorFor('server_port')}
                hint={t('frp.serverPortHint')}
              >
                <Input
                  id="frp-server-port"
                  mono
                  type="number"
                  inputMode="numeric"
                  min={1}
                  max={65535}
                  value={draft.server_port || ""}
                  placeholder="7000"
                  aria-invalid={errorFor('server_port') ? true : undefined}
                  disabled={!canManage}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      server_port: Number(e.target.value) || 0,
                    })
                  }
                  onBlur={() => touch('server_port')}
                />
              </Field>
            </div>
          </section>

          {/* 连接参数：决定「不被服务端拒绝」，token 只认服务端那一份 */}
          <section className="frp-form-section">
            <div className="frp-form-legend">
              <h4 className="frp-form-legend-title">{t('frp.sectionAuth')}</h4>
              <span className="frp-form-legend-hint">
                {t('frp.sectionAuthHint')}
              </span>
            </div>
            <Field
              /* 状态徽章跟着标签走，不甩到行尾：这是一条「读一眼就知道」的状态，
                 隔着半张卡的距离去找它，反而不如紧挨着标签 */
              label={
                <span className="frp-field-label-state">
                  {t('frp.fieldToken')}
                  <Badge
                    variant={serverQuery.data?.token_set ? 'success' : 'neutral'}
                    size="sm"
                  >
                    {serverQuery.data?.token_set
                      ? t('frp.tokenSet')
                      : t('frp.tokenUnset')}
                  </Badge>
                </span>
              }
              htmlFor="frp-token"
              hint={
                serverQuery.data?.token_set
                  ? t('frp.tokenHintSet')
                  : t('frp.tokenHintUnset')
              }
            >
              <div className="frp-token-row">
                <Input
                  id="frp-token"
                  type="password"
                  value={tokenDraft}
                  placeholder={
                    serverQuery.data?.token_set
                      ? t('frp.tokenPlaceholder')
                      : t('frp.tokenPlaceholderNew')
                  }
                  autoComplete="new-password"
                  disabled={!canManage}
                  onChange={(e) => setTokenDraft(e.target.value)}
                />
                {canManage && serverQuery.data?.token_set ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    loading={busy}
                    onClick={() => void clearToken()}
                  >
                    {t('frp.clear')}
                  </Button>
                ) : null}
              </div>
            </Field>
          </section>

          {/* 高级选项：多数部署用不到，默认收起；已经配过的展开着，
              免得「配了但看不见」 */}
          <details
            className="frp-advanced"
            open={advancedOpen}
            onToggle={(e) => setAdvancedOpen(e.currentTarget.open)}
          >
            <summary>
              <span className="frp-advanced-chevron" aria-hidden="true">
                <IconChevronDown size={14} />
              </span>
              {t('frp.sectionAdvanced')}
              <span className="frp-advanced-state">
                {draft.public_addr.trim() || t('frp.advancedUnset')}
              </span>
            </summary>
            <div className="frp-advanced-body">
              <p className="fs-xs text-muted">{t('frp.sectionAdvancedHint')}</p>
              <Field
                label={t('frp.fieldPublicAddr')}
                htmlFor="frp-public-addr"
                error={errorFor('public_addr')}
                hint={t('frp.publicAddrHint')}
              >
                <Input
                  id="frp-public-addr"
                  mono
                  value={draft.public_addr}
                  placeholder={t('frp.publicAddrPlaceholder')}
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={errorFor('public_addr') ? true : undefined}
                  disabled={!canManage}
                  onChange={(e) =>
                    setDraft({ ...draft, public_addr: e.target.value })
                  }
                  onBlur={() => touch('public_addr')}
                />
              </Field>
            </div>
          </details>

          {canManage ? (
            <p className="frp-form-footnote">{t('frp.restartNote')}</p>
          ) : null}
        </div>
      ) : (
        <div className="skeleton skeleton-text" />
      )}
    </Card>
  );
}

/* ---------------------------------------------------------------------------
   进程控制：安装 frpc 二进制（admin only）
   --------------------------------------------------------------------------- */
function ProcessCard() {
  const t = useT();
  const { hasPermission } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const canManage = hasPermission("settings.manage");
  const refresh = useRefreshInterval();

  const statusQuery = useQuery({
    queryKey: ["frp", "status"],
    queryFn: frpApi.status,
    refetchInterval: refresh,
  });
  const available = statusQuery.data?.available ?? false;
  const version = (statusQuery.data?.version ?? "").trim();

  const [busy, setBusy] = useState(false);

  return (
    <Card collapsible={false}>
      <CardHeader title={t('frp.clientTitle')} icon={<IconNetwork size={16} />} />
      <div className="flex flex-col gap-12">
        {/* 刻意不显示 frpc 的安装路径：路径是面板的实现细节，对用户没有用处，
            对探测者却是一条「这台机器上装了什么、目录怎么摆」的情报（V2-04）。 */}
        <div className="flex items-center justify-between">
          <span className="fs-sm text-secondary">{t('frp.clientVersion')}</span>
          {/* 显示二进制自己报的版本号（后端 frpc -v），而不是「已就绪」这种
              等于没说的状态：排障时要知道装的到底是哪一版。 */}
          <span className="mono fs-sm">
            {available
              ? version
                ? `v${version}`
                : t('frp.versionUnknown')
              : t('frp.missing')}
          </span>
        </div>
        {!available ? (
          <Notice tone="warning" title={t('frp.notInstalledTitle')}>
            {t('frp.notInstalledBody')}
          </Notice>
        ) : null}
        {canManage && !available ? (
          <Button
            variant="primary"
            icon={<IconDownload size={14} />}
            loading={busy}
            onClick={async () => {
              setBusy(true);
              try {
                const res = await frpApi.server.install();
                toast.success(t('frp.installDone'), res.binary);
                void qc.invalidateQueries({ queryKey: ["frp"] });
              } catch (err) {
                toast.error(t('frp.installFailed'), errorMessage(err));
              } finally {
                setBusy(false);
              }
            }}
          >
            {t('frp.installBtn')}
          </Button>
        ) : null}
        {canManage ? (
          <div className="flex items-center justify-between gap-16">
            <div className="flex flex-col gap-2">
              <span className="fs-sm">{t('frp.autoRestartLabel')}</span>
              <span className="fs-xs text-muted">
                {t('frp.autoRestartHint')}
              </span>
            </div>
            <Switch
              checked={statusQuery.data?.auto_restart ?? false}
              disabled={busy || statusQuery.isLoading || !available}
              ariaLabel={t('frp.autoRestartAria')}
              onChange={async (next) => {
                setBusy(true);
                try {
                  const res = await frpApi.server.autoRestart(next);
                  if (res.start_error) {
                    toast.warning(
                      t('frp.autoRestartOn'),
                      t('frp.autoRestartStartFailed', { error: res.start_error }),
                    );
                  } else if (res.started) {
                    toast.success(t('frp.autoRestartOn'), t('frp.autoRestartStarted'));
                  } else {
                    toast.success(
                      next ? t('frp.autoRestartOn') : t('frp.autoRestartOff'),
                      next ? t('frp.autoRestartOnDetail') : undefined,
                    );
                  }
                  void qc.invalidateQueries({ queryKey: ["frp"] });
                } catch (err) {
                  toast.error(t('common.opFailed'), errorMessage(err));
                } finally {
                  setBusy(false);
                }
              }}
            />
          </div>
        ) : null}
        {canManage ? (
          <p className="fs-xs text-muted">
            {t('frp.installNextStep')}
          </p>
        ) : null}
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------------------
   规则列表（按用户归属过滤）
   --------------------------------------------------------------------------- */
/* 目标地址刻意留空：旧的默认值 127.0.0.1 正是被修复的那个问题 ——
   普通用户不该被引导去指向面板本机。公网端口由策略接口给出一个空闲最小值。 */
const EMPTY_RULE: FrpRuleInput = {
  name: "tunnel",
  type: "tcp",
  local_ip: "",
  local_port: 8080,
  remote_port: 40000,
};

/** 拼「公网地址:端口」。IPv6 必须加方括号，否则 host:port 会被当成别的东西。 */
function endpoint(host: string, port: number): string {
  const text = (host || "").trim();
  if (!text) return "";
  const hostPart = text.startsWith("[") || !text.includes(":") ? text : `[${text}]`;
  return `${hostPart}:${port}`;
}

function RulesCard() {
  const t = useT();
  const { hasPermission } = useAuth();
  const toast = useToast();
  const qc = useQueryClient();
  const canManage = hasPermission("frp.manage");
  /** 策略类设置（面板网段拦截）与规则增删是两种权限：前者 settings.manage */
  const canAdmin = hasPermission("settings.manage");
  const refresh = useRefreshInterval();
  /** 新建规则走弹窗，填完再保存 —— 避免先落库一条空规则再反复重启 frpc */
  const [creating, setCreating] = useState(false);
  const [blockBusy, setBlockBusy] = useState(false);
  /** 存量规则补关联（管理员动作，逐人解析 PVE 地址，可能慢一点） */
  const [relinking, setRelinking] = useState(false);

  const listQuery = useQuery({
    queryKey: ["frp", "rules"],
    queryFn: frpApi.rules.list,
    refetchInterval: refresh,
  });
  const effectiveQuery = useQuery({
    queryKey: ["frp", "effective"],
    queryFn: frpApi.effective,
    refetchInterval: refresh,
  });
  /* 策略快照：允许的目标 / 端口区间 / 已占用端口。
     刻意**不轮询** —— 算一次要逐条连接读 PVE 的客户机清单，按页面刷新间隔
     反复跑等于把 PVE 当数据库用。它只在进入页面（且已过期）与规则增删改后
     （invalidateQueries）重新取一次，足够跟上「新建虚机 / 别人占了端口」。 */
  const policyQuery = useQuery({
    queryKey: ["frp", "policy"],
    queryFn: () => frpApi.policy(),
    staleTime: 60_000,
  });
  const policy = policyQuery.data;
  /* 用户访问穿透服务的地址（public_host:remote_port）。规则列表自带一份，
     策略接口也给一份 —— 任一个到了就能显示，不必等两个请求都完成。 */
  const publicHost = listQuery.data?.public_host || policy?.public_host || "";

  const rules = listQuery.data?.rules ?? [];
  const isAdmin = listQuery.data?.is_admin ?? false;
  const ownUsername = listQuery.data?.own_username ?? "";

  /* 当前生效（合并后）的代理名集合 —— 用于标记「已生效 / 未应用」。
     比的是**渲染名**（proxy_name，带归属前缀），不是规则名：合并后的 frpc
     配置里每条代理都叫 `<用户>_<规则名>`，用规则名永远对不上。 */
  const effectiveNames = useMemo(() => {
    const set = new Set<string>();
    for (const r of effectiveQuery.data?.config.proxies ?? []) set.add(r.name);
    return set;
  }, [effectiveQuery.data]);

  /* 同一个人名下的重名规则。跨用户同名不算问题：渲染时名字带归属前缀
     （alice_tunnel-1 / bob_tunnel-1），所以这里按「用户名 + 规则名」成对去重；
     同人重名则会被自动加序号，提示用户改名是为了让隧道名字可预期。 */
  const dupNames = useMemo(() => {
    const seen = new Set<string>();
    const dups = new Set<string>();
    for (const r of rules) {
      const key = r.username + ":" + r.name;
      if (seen.has(key)) dups.add(r.name);
      else seen.add(key);
    }
    return [...dups];
  }, [rules]);

  /* 目标机器已不归规则作者的条数：规则本身还在，只是不进转发配置
     （判定见后端 frp.annotate_rules）。管理员视角是「有多少条被暂停」，
     用户视角是「我的哪几条为什么不通」。 */
  const staleCount = useMemo(() => rules.filter((r) => r.stale).length, [rules]);

  /* 规则会越积越多（每台内网服务一条），和管理员那边其它列表一样分页。
     用统一的分页 hook：页码越界自动夹回最后一页，并提供「每页 N 条」下拉。
     resetKey 取「看的范围」（管理员看全部 / 用户看自己的）—— 这一页没有筛选框，
     换个范围才是真的换了一份列表，那时该回到第 1 页。 */
  const rulePage = usePaged(rules, isAdmin ? 'all' : ownUsername || 'self');

  async function create(payload: FrpRuleInput) {
    await frpApi.rules.create(payload);
  }

  /**
   * 给升级前创建的规则补上「指向哪台机器」。
   *
   * 补上之后它们才会被改派复核覆盖；补不上的（拿不到机器地址）逐条返回，
   * 由用户自己把目标改成名下地址。只补空字段，不覆盖已有值。
   */
  async function relink() {
    setRelinking(true);
    try {
      const res = await frpApi.rules.relink();
      const rest = res.unlinked.length
        ? t('frp.relinkUnlinked', { n: res.unlinked.length })
        : undefined;
      if (res.linked) {
        toast.success(t('frp.relinkDone', { n: res.linked }), rest);
      } else if (rest) {
        toast.error(t('frp.relinkNone'), rest);
      } else {
        toast.success(t('frp.relinkNone'));
      }
      void qc.invalidateQueries({ queryKey: ["frp"] });
    } catch (err) {
      toast.error(t('frp.relinkFailed'), errorMessage(err));
    } finally {
      setRelinking(false);
    }
  }

  async function update(id: string, payload: FrpRulePatch) {
    await frpApi.rules.update(id, payload);
  }

  async function remove(id: string) {
    await frpApi.rules.remove(id);
  }

  function nextName(): string {
    const used = new Set(rules.map((r) => r.name));
    for (let i = 1; i <= 999; i += 1) {
      const candidate = `tunnel-${i}`;
      if (!used.has(candidate)) return candidate;
    }
    return `tunnel-${Date.now()}`;
  }

  /** 开关「阻止把穿透目标指向面板所在网段」。关掉会放大攻击面，用 warning
     级 toast 把风险说清楚；失败时重取一次策略，让开关回到真实状态。 */
  async function toggleBlockPanelSubnet(next: boolean) {
    setBlockBusy(true);
    try {
      await frpApi.setBlockPanelSubnet(next);
      await qc.invalidateQueries({ queryKey: ["frp"] });
      if (next) {
        toast.success(t('frp.blockPanelOn'));
      } else {
        toast.warning(t('frp.blockPanelOff'), t('frp.blockPanelOffDetail'));
      }
    } catch (err) {
      toast.error(t('frp.blockPanelToggleFailed'), errorMessage(err));
      void qc.invalidateQueries({ queryKey: ["frp"] });
    } finally {
      setBlockBusy(false);
    }
  }

  return (
    <Card className="frp-rules-card">
      <CardHeader
        title={t('frp.rulesTitle')}
        icon={<IconNetwork size={16} />}
        subtitle={
          isAdmin
            ? t('frp.rulesSubtitleAdmin')
            : t('frp.rulesSubtitleUser', { user: ownUsername || t('frp.currentUser') })
        }
        actions={
          canManage || canAdmin ? (
            <div className="flex items-center gap-4">
              {/* 存量规则补关联：只有补上 guest_ref，改派复核才管得到它们 */}
              {canAdmin ? (
                <Button
                  variant="secondary"
                  size="sm"
                  icon={<IconRefresh size={14} />}
                  loading={relinking}
                  title={t('frp.relinkHint')}
                  onClick={() => void relink()}
                >
                  {t('frp.relink')}
                </Button>
              ) : null}
              {canManage ? (
                <Button
                  variant="primary"
                  size="sm"
                  icon={<IconPlus size={14} />}
                  onClick={() => setCreating(true)}
                >
                  {t('frp.newRule')}
                </Button>
              ) : null}
            </div>
          ) : null
        }
      />

      {policy ? (
        canAdmin ? (
          /* 管理员：策略集中成一块带边框的面板（标题栏 + 若干设置行），
             与下面的规则表分开。原先三行说明文字直接摊在卡片里、控件甩到
             最右侧，既看不出它们属于同一组，也读不出谁控制谁。 */
          <div className="frp-policy">
            <div className="frp-policy-head">
              <IconSettings size={13} />
              <span className="frp-policy-title">{t('frp.policyPanelTitle')}</span>
              <Badge variant="neutral" size="sm">
                {t('frp.policyAdminTarget')}
              </Badge>
            </div>
            {/* 两项策略并排：原先上下两行、每行右边一大片空白，既浪费高度，
                也把「保存区间」推到离标题很远的地方 */}
            <div className="frp-policy-body">
              <PortRangeEditor policy={policy} />
              <div className="frp-policy-item">
                <div className="frp-policy-item-head">
                  <span className="frp-policy-label">{t('frp.blockPanelLabel')}</span>
                  <div className="frp-policy-control">
                    <Switch
                      checked={policy.block_panel_subnet ?? true}
                      disabled={blockBusy}
                      ariaLabel={t('frp.blockPanelLabel')}
                      onChange={(next) => void toggleBlockPanelSubnet(next)}
                    />
                  </div>
                </div>
                <span className="frp-policy-hint">{t('frp.blockPanelHint')}</span>
              </div>
            </div>
          </div>
        ) : (
          <RuleTargetHint policy={policy} />
        )
      ) : null}

      {/* 没有对外访问地址时，规则建了也不知道从哪儿连 —— 直接把话说清楚 */}
      {!publicHost ? (
        <Notice tone="info" title={t('frp.publicNotConfiguredTitle')}>
          {isAdmin
            ? t('frp.publicNotConfiguredAdmin')
            : t('frp.publicNotConfiguredUser')}
        </Notice>
      ) : null}

      {dupNames.length > 0 ? (
        <Notice tone="danger" title={t('frp.dupTitle')}>
          {t('frp.dupBody', { names: dupNames.join(t('incident.evidenceSeparator')) })}
        </Notice>
      ) : null}

      {/* 目标机器改派 / 收回后规则会自动失效：这条提示就是「你的隧道怎么不通了」
          的答案。机器回到原主人名下会自动恢复，所以措辞是「失效」而不是「出错」。 */}
      {staleCount > 0 ? (
        <Notice tone="warning" title={t('frp.staleBadge')}>
          {isAdmin
            ? t('frp.staleNoticeAdmin', { n: staleCount })
            : t('frp.staleNotice', { n: staleCount })}
        </Notice>
      ) : null}

      {rules.length === 0 ? (
        <div className="frp-rules-empty">
          <IconNetwork size={20} />
          <p className="fs-sm text-secondary">{t('frp.noRules')}</p>
        </div>
      ) : (
        <>
          <div className="table-container frp-rules-table-wrap">
            <table className="table table-dense frp-rules-table">
              <thead>
                <tr>
                  <th>{t('frp.colOwner')}</th>
                  <th>{t('frp.colName')}</th>
                  <th>{t('frp.colLocalIp')}</th>
                  <th>{t('frp.colLocalPort')}</th>
                  <th>{t('frp.colRemotePort')}</th>
                  <th>{t('frp.colPublic')}</th>
                  <th>{t('common.status')}</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rulePage.rows.map((r) => (
                  <RuleRow
                    key={r.id}
                    rule={r}
                    publicHost={publicHost}
                    portMin={policy?.remote_port_min}
                    portMax={policy?.remote_port_max}
                    canManageRow={canManage && (isAdmin || r.username === ownUsername)}
                    isEffective={effectiveNames.has(r.proxy_name ?? r.name)}
                    onUpdate={async (patch) => {
                      try {
                        await update(r.id, patch);
                        void qc.invalidateQueries({ queryKey: ["frp"] });
                      } catch (err) {
                        toast.error(t('frp.saveFailed'), errorMessage(err));
                      }
                    }}
                    onDelete={async () => {
                      try {
                        await remove(r.id);
                        void qc.invalidateQueries({ queryKey: ["frp"] });
                        toast.success(t('frp.deleted'));
                      } catch (err) {
                        toast.error(t('frp.deleteFailed'), errorMessage(err));
                      }
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
          {/* 分页条接在表格下沿，成为同一张卡的页脚（见 .pagination 的样式） */}
          <PagerBar pager={rulePage} />
        </>
      )}

      <RuleDialog
        open={creating}
        initialName={nextName()}
        policy={policy}
        onClose={() => setCreating(false)}
        onSave={async (payload) => {
          await create(payload);
          void qc.invalidateQueries({ queryKey: ["frp"] });
        }}
      />
    </Card>
  );
}

/* ---------------------------------------------------------------------------
   策略提示：普通用户能指向哪些地址、公网端口只能用哪一段
   —— 这些限制以前只体现在后端校验的报错里，用户要先撞一次墙才知道。
   --------------------------------------------------------------------------- */
function RuleTargetHint({ policy }: { policy: FrpPolicy }) {
  const t = useT();
  const portRange = t('frp.policyPortRange', {
    min: policy.remote_port_min,
    max: policy.remote_port_max,
  });

  if (!policy.restricted) {
    return (
      <div className="fs-xs text-muted">
        {t('frp.policyAdminTarget')} · {portRange}
      </div>
    );
  }

  if (policy.own_ips.length === 0 && policy.granted.length === 0) {
    return (
      <div className="flex flex-col gap-4">
        <Notice tone="warning" title={t('frp.policyNoTargetTitle')}>
          {policy.allow_cidrs.length > 0
            ? t('frp.policyNoTargetWithCidrs', {
                cidrs: policy.allow_cidrs.join('、'),
              })
            : t('frp.policyNoTargetBody')}
        </Notice>
        {policy.own_ips_error ? (
          <div className="fs-xs text-warning">
            {t('frp.policyTargetLookupFailed', { error: policy.own_ips_error })}
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {policy.own_ips.length > 0 ? (
        <div className="flex flex-wrap items-center gap-6 fs-xs text-muted">
          <span>{t('frp.policyOwnIps')}</span>
          {policy.own_ips.slice(0, 8).map((ip) => (
            <Badge key={ip} variant="neutral" size="sm">
              <span className="mono">{ip}</span>
            </Badge>
          ))}
          {policy.own_ips.length > 8 ? (
            <span>{t('frp.policyMoreIps', { n: policy.own_ips.length - 8 })}</span>
          ) : null}
        </div>
      ) : null}
      {policy.granted.length > 0 ? (
        <div className="flex flex-wrap items-center gap-6 fs-xs text-muted">
          <span>{t('frp.policyGranted')}</span>
          {policy.granted.map((value) => (
            <Badge key={value} variant="info" size="sm">
              <span className="mono">{value}</span>
            </Badge>
          ))}
        </div>
      ) : null}
      {policy.allow_cidrs.length > 0 ? (
        <div className="fs-xs text-muted">
          {t('frp.policyAllowCidrs', { cidrs: policy.allow_cidrs.join('、') })}
        </div>
      ) : null}
      <div className="fs-xs text-muted">{portRange}</div>
      {/* 常显：机器不在下拉里时，用户最需要的是「为什么没有、该找谁」 */}
      <div className="fs-xs text-muted">{t('frp.targetIpHint')}</div>
    </div>
  );
}

/* ---------------------------------------------------------------------------
   公网端口区间（仅管理员）：原先写死 40000–50000。frps 前面有 NAT / 云安全组
   时，能映射出去的往往只有很窄的一段端口，写死的区间会让规则「建得出来但
   连不上」。这里让管理员按实际可用范围设定，普通用户建规则时只能填这一段。

   关键端口黑名单与区间无关、始终生效（区间放到 1–65535 也不会放出 22 / 80 /
   443 / 8006）。收窄区间不会让已有规则失效，只是它们下次修改时必须落到
   区间内 —— 保存后把这类端口列出来提醒。
   --------------------------------------------------------------------------- */
function PortRangeEditor({ policy }: { policy: FrpPolicy }) {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const [low, setLow] = useState(String(policy.remote_port_min));
  const [high, setHigh] = useState(String(policy.remote_port_max));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  /* 策略快照不轮询，只在保存成功 / 别处改动后重取；那时把输入框同步成服务端值。
     草稿由用户主动编辑，不会被这条同步冲掉。 */
  useEffect(() => {
    setLow(String(policy.remote_port_min));
    setHigh(String(policy.remote_port_max));
  }, [policy.remote_port_min, policy.remote_port_max]);

  const min = Number(low);
  const max = Number(high);
  const valid =
    Number.isInteger(min) &&
    Number.isInteger(max) &&
    min >= 1 &&
    max <= 65535 &&
    min <= max;
  const dirty = min !== policy.remote_port_min || max !== policy.remote_port_max;

  async function save() {
    if (!valid) {
      setError(t('frp.portRangeInvalid'));
      return;
    }
    setError("");
    setBusy(true);
    try {
      const res = await frpApi.setPortRange(min, max);
      await qc.invalidateQueries({ queryKey: ["frp"] });
      if (res.out_of_range_ports.length > 0) {
        toast.warning(
          t('frp.portRangeSaved'),
          t('frp.portRangeOutOfRange', {
            ports: res.out_of_range_ports.join('、'),
          }),
        );
      } else {
        toast.success(t('frp.portRangeSaved'));
      }
    } catch (err) {
      toast.error(t('frp.portRangeSaveFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="frp-policy-item">
      <div className="frp-policy-item-head">
        <span className="frp-policy-label">{t('frp.portRangeLabel')}</span>
        <div className="frp-policy-control">
          {/* 起止端口收在同一个框里：一眼就是「一个区间」，不是两个无关的字段 */}
          <div className={`frp-port-range${error ? ' is-invalid' : ''}`}>
            <input
              type="number"
              inputMode="numeric"
              min={1}
              max={65535}
              value={low}
              disabled={busy}
              aria-label={t('frp.portRangeMinLabel')}
              onChange={(e) => {
                setLow(e.target.value);
                setError("");
              }}
            />
            <span className="frp-port-range-sep" aria-hidden="true">
              –
            </span>
            <input
              type="number"
              inputMode="numeric"
              min={1}
              max={65535}
              value={high}
              disabled={busy}
              aria-label={t('frp.portRangeMaxLabel')}
              onChange={(e) => {
                setHigh(e.target.value);
                setError("");
              }}
            />
          </div>
          {/* 没有改动时退成次级按钮：一个常亮的蓝色主按钮放在这里是纯噪音 */}
          <Button
            size="sm"
            variant={dirty ? "primary" : "secondary"}
            loading={busy}
            disabled={!dirty}
            title={dirty ? undefined : t('frp.portRangeUnchanged')}
            onClick={() => void save()}
          >
            {t('frp.portRangeSave')}
          </Button>
        </div>
      </div>
      <span className="frp-policy-hint">{t('frp.portRangeHint')}</span>
      {error ? (
        <span className="frp-policy-error" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------------------
   前端侧的轻量校验（权威判定在后端：归属要查 PVE、白名单是管理员配置）
   --------------------------------------------------------------------------- */
function isIpLiteral(value: string): boolean {
  const text = value.trim().replace(/^\[/, "").replace(/\]$/, "");
  if (!text) return false;
  if (text.includes(":")) {
    // IPv6 只做「字符合法 + 至少两段」的粗判，具体由后端 ipaddress 校验
    return /^[0-9A-Fa-f:.]+$/.test(text) && text.split(":").length >= 3;
  }
  return text.split(".").length === 4 && text.split(".").every(
    (part) => /^\d{1,3}$/.test(part) && Number(part) <= 255,
  );
}

function ipv4ToInt(ip: string): number | null {
  const parts = ip.trim().split(".");
  if (parts.length !== 4) return null;
  let value = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    value = value * 256 + octet;
  }
  return value;
}

/** ``cidr`` 是否覆盖 ``ip``（只处理 IPv4；其余交给后端判定）。 */
function cidrContains(cidr: string, ip: string): boolean {
  const [base, bitsRaw] = cidr.trim().split("/");
  const target = ipv4ToInt(ip);
  const network = ipv4ToInt(base);
  if (target === null || network === null) return false;
  const bits = bitsRaw === undefined || bitsRaw === "" ? 32 : Number(bitsRaw);
  if (!Number.isInteger(bits) || bits < 0 || bits > 32) return false;
  if (bits === 0) return true;
  const mask = bits === 32 ? 0xffffffff : (0xffffffff << (32 - bits)) >>> 0;
  return ((target & mask) >>> 0) === ((network & mask) >>> 0);
}

/* ---------------------------------------------------------------------------
   新建规则弹窗：填完再保存
   —— 原先是「点一下就落库一条空规则」，随后每次失焦又各触发一次 frpc 重启，
   既容易卡顿，也会把没填完的规则写进配置。
   --------------------------------------------------------------------------- */
function RuleDialog({
  open,
  initialName,
  policy,
  onClose,
  onSave,
}: {
  open: boolean;
  initialName: string;
  policy?: FrpPolicy;
  onClose: () => void;
  onSave: (payload: FrpRuleInput) => Promise<void>;
}) {
  const t = useT();
  const toast = useToast();
  const [form, setForm] = useState<FrpRuleInput>({ ...EMPTY_RULE, name: initialName });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  /* 普通用户的目标只能从「自己名下的虚机 / 容器 + 管理员授权给他的地址」里
     选：下拉的选项就是 /api/frp/policy 的 targets / granted，后端用同一份清单
     判定 —— 界面上根本没有「填一个别人的 IP 试试」这条路。只有放行过网段时，
     才多给一个手动输入的入口（输入后仍然按网段校验）。 */
  const restricted = policy?.restricted ?? false;
  /* 管理员的可选目标要逐条连接读 PVE（离线连接会一直等到超时），所以不跟页面
     一起拉：只在弹窗打开时单独请求一次。普通用户那份随 policy 一起到 —— 他们
     名下没有归属机器时后端根本不会碰 PVE，很便宜。 */
  const targetsQuery = useQuery({
    queryKey: ["frp", "policy", "targets"],
    queryFn: () => frpApi.policy({ withTargets: true }),
    enabled: open && !restricted,
    staleTime: 60_000,
  });
  const targets = restricted
    ? policy?.targets ?? []
    : targetsQuery.data?.targets ?? [];
  /* 读 PVE 失败（连接不通等）：下拉里只剩「自定义」，得说清为什么 */
  const targetsError = restricted ? "" : targetsQuery.data?.own_ips_error ?? "";
  const targetsLoading = !restricted && targetsQuery.isLoading;
  const granted = policy?.granted ?? [];
  const grantedIps = granted.filter((item) => !item.includes('/'));
  const grantedCidrs = granted.filter((item) => item.includes('/'));
  const manualCidrs = [...(policy?.allow_cidrs ?? []), ...grantedCidrs];
  const allowManual = restricted && manualCidrs.length > 0;
  /* 下拉的候选：虚机 / 容器（带主机名、VMID）+ 管理员单独放行的 IP（没有主机名，
     只标一句「管理员授权」）。两种都支持按主机名 / IP / VMID 搜索。 */
  const choices = useMemo<TargetChoice[]>(() => {
    const guests: TargetChoice[] = targets.map((item) => ({
      ip: item.ip,
      name: item.name,
      vmid: item.vmid,
      node: item.node,
      type: item.type,
    }));
    const grantedOnly: TargetChoice[] = grantedIps
      .filter((ip) => !targets.some((item) => item.ip === ip))
      .map((ip) => ({ ip, note: t('frp.targetGrantedName') }));
    return [...guests, ...grantedOnly];
  }, [targets, grantedIps, t]);
  const [manual, setManual] = useState(false);
  const noTarget = restricted && choices.length === 0 && !allowManual;

  /* 策略与默认名每秒都在轮询 / 重算，如果它们进了依赖，弹窗开着的时候也会
     把用户正在填的内容冲掉。只在「打开」这一刻取一次初值。 */
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const initialNameRef = useRef(initialName);
  initialNameRef.current = initialName;

  useEffect(() => {
    if (!open) return;
    const current = policyRef.current;
    const candidates = current?.targets ?? [];
    const firstGranted = (current?.granted ?? []).find(
      (item) => !item.includes('/'),
    );
    setForm({
      ...EMPTY_RULE,
      name: initialNameRef.current,
      /* 普通用户只有「自己名下」这一小组地址，直接选中第一个省一次点击；
         管理员面对的是全平台清单（见 /api/frp/policy 的 targets），预选第一个
         等于替他把公网端口指向一台随机的机器 —— 留空让他自己选。 */
      local_ip: current?.restricted ? candidates[0]?.ip ?? firstGranted ?? "" : "",
      remote_port: current?.default_remote_port ?? EMPTY_RULE.remote_port,
    });
    setManual(false);
    setErrors({});
    setBusy(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  function patch<K extends keyof FrpRuleInput>(key: K, value: FrpRuleInput[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function validate(): boolean {
    const next: Record<string, string> = {};
    const name = form.name.trim();
    if (!name) next.name = t('frp.ruleNameRequired');
    else if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(name))
      next.name = t('frp.ruleNameInvalid');

    const localIp = (form.local_ip ?? "").trim();
    if (!localIp) {
      next.local_ip = t('frp.localIpRequired');
    } else if (!isIpLiteral(localIp)) {
      next.local_ip = t('frp.localIpInvalid');
    } else if (restricted) {
      /* 后端才是权威（归属 / 白名单要查 PVE），这里先按同一份候选拦一遍：
         下拉本来选不出别人的地址，只有「手动输入」那条路需要这一层。 */
      const own =
        targets.some((item) => item.ip === localIp) || grantedIps.includes(localIp);
      const allowed =
        manual && manualCidrs.some((cidr) => cidrContains(cidr, localIp));
      if (!own && !allowed) next.local_ip = t('frp.localIpNotAllowed');
    }

    if (!(form.local_port > 0 && form.local_port <= 65535))
      next.local_port = t('frp.portRange');

    const min = policy?.remote_port_min ?? 40000;
    const max = policy?.remote_port_max ?? 50000;
    if (!(form.remote_port >= min && form.remote_port <= max)) {
      next.remote_port = t('frp.policyPortRange', { min, max });
    } else if (policy?.forbidden_ports.includes(form.remote_port)) {
      next.remote_port = t('frp.policyForbiddenPort', { port: form.remote_port });
    } else if (policy?.used_remote_ports.includes(form.remote_port)) {
      next.remote_port = t('frp.policyPortInUse', { port: form.remote_port });
    }

    setErrors(next);
    return Object.keys(next).length === 0;
  }

  async function submit() {
    if (!validate()) return;
    setBusy(true);
    try {
      await onSave({ ...form, name: form.name.trim() });
      toast.success(t('frp.ruleCreated'), t('frp.ruleCreatedDetail'));
      onClose();
    } catch (err) {
      toast.error(t('frp.createFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={t('frp.dialogTitle')}
      description={t('frp.dialogDesc')}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t('common.cancel')}
          </Button>
          <Button
            variant="primary"
            icon={<IconCheck size={14} />}
            loading={busy}
            /* 没有任何可指向的目标时，保存按钮直接不可用 —— 让用户先看到
               「去准备虚机 / 找管理员」这句，而不是点一下吃一个 403。 */
            disabled={noTarget}
            onClick={() => void submit()}
          >
            {t('common.save')}
          </Button>
        </>
      }
    >
      {/* .modal-body 本身不带子元素间距（见 components.css 的说明），
          这个弹窗叠了多个字段 + 提示条，统一在这里隔开 —— 否则上一个字段的
          说明文字会和下一个字段的标签粘在一起，整张表看着又挤又密。 */}
      <div className="flex flex-col gap-16">
      <Input
        label={t('frp.fieldRuleName')}
        required
        value={form.name}
        error={errors.name}
        hint={t('frp.ruleNameHint')}
        onChange={(e) => patch("name", e.target.value)}
      />
      {restricted ? (
        <>
          {/* 普通用户：目标只能从自己名下虚机 / 容器的地址里选。选项由后端按
              归属 + PVE 实况算出（/api/frp/policy 的 targets），不是前端自己
              拼的列表 —— 列表和判定永远一致。机器多了就在框里搜（主机名 / IP /
              VMID），规则见 utils/targetSearch.ts。 */}
          <TargetSelect
            label={t('frp.fieldLocalIp')}
            required
            choices={choices}
            value={manual ? '' : form.local_ip}
            manual={manual}
            manualLabel={t('frp.targetManualOption')}
            onSelect={(choice) => {
              setManual(false);
              patch('local_ip', choice.ip);
            }}
            onManual={
              allowManual
                ? (text) => {
                    setManual(true);
                    patch('local_ip', text);
                  }
                : undefined
            }
            disabled={noTarget}
            placeholder={
              noTarget
                ? t('frp.targetSelectEmpty')
                : t('frp.targetSearchPlaceholder')
            }
            error={manual ? undefined : errors.local_ip}
            hint={
              noTarget
                ? undefined
                : allowManual
                  ? t('frp.targetSelectHintManual', {
                      cidrs: manualCidrs.join('、'),
                    })
                  : t('frp.targetSelectHint', { n: choices.length })
            }
          />
          {manual ? (
            <Input
              label={t('frp.fieldLocalIpManual')}
              className="mono"
              required
              autoFocus
              placeholder={t('frp.localIpPlaceholder')}
              value={form.local_ip}
              error={manual ? errors.local_ip : undefined}
              hint={t('frp.policyAllowCidrs', { cidrs: manualCidrs.join('、') })}
              onChange={(e) => patch('local_ip', e.target.value)}
            />
          ) : null}
          {noTarget ? (
            <Notice tone="warning" title={t('frp.policyNoTargetTitle')}>
              {t('frp.policyNoTargetBody')}
            </Notice>
          ) : null}
        </>
      ) : (
        <>
          {/* 管理员不受目标白名单限制，所以这个选择器只是「预设地址」的快捷入口 ——
              候选是 /api/frp/policy 的 targets（全部虚机 / 容器的地址，与普通用户
              那份同一套生成逻辑），列表末尾单独一行切到「自定义 IP」手动填写。
              默认不预选：这份清单是全平台的，预选第一个等于替他把公网端口指向
              一台随机的机器，让他自己选（或切到自定义）。 */}
          <TargetSelect
            label={t('frp.fieldLocalIp')}
            required
            choices={choices}
            value={manual ? '' : form.local_ip}
            manual={manual}
            manualLabel={t('frp.targetManualOptionAdmin')}
            onSelect={(choice) => {
              setManual(false);
              patch('local_ip', choice.ip);
            }}
            onManual={(text) => {
              setManual(true);
              patch('local_ip', text);
            }}
            loading={targetsLoading}
            placeholder={t('frp.targetSearchPlaceholder')}
            error={manual ? undefined : errors.local_ip}
            hint={
              manual
                ? undefined
                : targetsLoading
                  ? t('frp.targetSelectLoading')
                  : targetsError
                    ? t('frp.policyTargetLookupFailed', { error: targetsError })
                    : choices.length > 0
                      ? t('frp.targetSelectHintAdmin', { n: choices.length })
                      : t('frp.targetSelectAdminNoPreset')
            }
          />
          {manual ? (
            <Input
              label={t('frp.fieldLocalIpManualAdmin')}
              className="mono"
              required
              autoFocus
              placeholder={t('frp.localIpPlaceholder')}
              value={form.local_ip}
              error={manual ? errors.local_ip : undefined}
              hint={t('frp.localIpHint')}
              onChange={(e) => patch('local_ip', e.target.value)}
            />
          ) : null}
        </>
      )}
      <div className="form-grid-2">
        <Input
          label={t('frp.fieldLocalPort')}
          type="number"
          min={1}
          max={65535}
          value={String(form.local_port)}
          error={errors.local_port}
          onChange={(e) => patch("local_port", Number(e.target.value))}
        />
        <Input
          label={t('frp.fieldRemotePort')}
          type="number"
          min={policy?.remote_port_min ?? 40000}
          max={policy?.remote_port_max ?? 50000}
          value={String(form.remote_port)}
          error={errors.remote_port}
          hint={t('frp.remotePortHint', {
            min: policy?.remote_port_min ?? 40000,
            max: policy?.remote_port_max ?? 50000,
          })}
          onChange={(e) => patch("remote_port", Number(e.target.value))}
        />
      </div>
      </div>
    </Modal>
  );
}

function RuleRow({
  rule,
  publicHost,
  portMin,
  portMax,
  canManageRow,
  isEffective,
  onUpdate,
  onDelete,
}: {
  rule: FrpRule;
  /** 公网访问地址（空串 = 管理员还没配，显示占位） */
  publicHost: string;
  /** 当前生效的公网端口区间（管理员可调），用于输入框提示与校验范围 */
  portMin?: number;
  portMax?: number;
  canManageRow: boolean;
  isEffective: boolean;
  onUpdate: (patch: FrpRulePatch) => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const t = useT();
  const toast = useToast();
  const [draft, setDraft] = useState(rule);
  const [busy, setBusy] = useState(false);
  /** 复制成功的短暂反馈：按钮图标换成对勾，1.5 秒后还原 */
  const [copied, setCopied] = useState(false);
  const isDirty = JSON.stringify(draft) !== JSON.stringify(rule);

  /* 只在服务端值真的变化时才同步草稿：列表每 8 秒轮询一次，
     不能因为拿到新对象就把用户正在输入的内容覆盖掉 */
  useEffect(() => {
    setDraft(rule);
  }, [rule.id, rule.name, rule.local_ip, rule.local_port, rule.remote_port]);

  /* 显式保存：一次改动只提交一次请求，也就只重启一次 frpc。
     失败（例如把目标改成了不属于自己的 IP、端口撞车）要当场说出来，
     否则行内编辑看起来「点了没反应」。 */
  async function save() {
    if (!isDirty) return;
    setBusy(true);
    try {
      await onUpdate({
        name: draft.name,
        local_ip: draft.local_ip,
        local_port: draft.local_port,
        remote_port: draft.remote_port,
      });
    } catch (err) {
      toast.error(t('frp.saveFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    try {
      await onDelete();
    } catch (err) {
      toast.error(t('frp.deleteFailed'), errorMessage(err));
    }
  }

  /** 公网地址是给人拿去连的，能一键复制比手抄靠谱（IPv6 尤其）。 */
  async function copyEndpoint() {
    try {
      await navigator.clipboard.writeText(endpoint(publicHost, draft.remote_port));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
      toast.success(t('common.copied'));
    } catch {
      toast.error(t('common.copyFailed'), t('common.clipboardDenied'));
    }
  }

  const locked = !canManageRow || busy;

  return (
    <tr>
      <td>
        <Badge variant={rule.username === "" ? "warning" : "neutral"} size="sm">
          {rule.username || t('frp.ownerless')}
        </Badge>
      </td>
      <td>
        <div className="input-wrap">
          <input
            className="input"
            value={draft.name}
            disabled={locked}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
        </div>
      </td>
      <td>
        <div className="input-wrap">
          <input
            className="input mono"
            value={draft.local_ip}
            disabled={locked}
            onChange={(e) => setDraft({ ...draft, local_ip: e.target.value })}
          />
        </div>
      </td>
      <td>
        <div className="input-wrap">
          <input
            className="input"
            type="number"
            value={draft.local_port}
            disabled={locked}
            onChange={(e) => setDraft({ ...draft, local_port: Number(e.target.value) })}
          />
        </div>
      </td>
      <td>
        <div className="input-wrap">
          <input
            className="input"
            type="number"
            min={portMin ?? 40000}
            max={portMax ?? 50000}
            value={draft.remote_port}
            disabled={locked}
            title={t('frp.remotePortHint', {
              min: portMin ?? 40000,
              max: portMax ?? 50000,
            })}
            onChange={(e) => setDraft({ ...draft, remote_port: Number(e.target.value) })}
          />
        </div>
      </td>
      <td>
        {publicHost ? (
          /* 与同行的其它输入框同一套外框（.input-wrap）：既填满单元格的空白，
             风格也一致。文字可整体选中，复制按钮在框内右侧。 */
          <div className="input-wrap input-block copy-cell">
            <span className="copy-cell-text">
              {endpoint(publicHost, draft.remote_port)}
            </span>
            <IconButton
              label={t('frp.copyPublic', {
                addr: endpoint(publicHost, draft.remote_port),
              })}
              onClick={() => void copyEndpoint()}
            >
              {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
            </IconButton>
          </div>
        ) : (
          <span className="fs-xs text-muted">{t('frp.publicUnknown')}</span>
        )}
      </td>
      <td>
        <div className="flex flex-wrap items-center gap-4">
          {rule.stale ? (
            /* 失效优先于「已生效 / 未应用」：它不是「还没应用」，而是被主动排除 */
            <Badge variant="warning" size="sm" dot>
              {t('frp.staleBadge')}
            </Badge>
          ) : (
            <Badge variant={isEffective ? "success" : "neutral"} size="sm" dot pulse={isEffective}>
              {isEffective ? t('frp.effective') : t('frp.notApplied')}
            </Badge>
          )}
          {isDirty ? (
            <Badge variant={busy ? "info" : "warning"} size="sm" dot={busy} pulse={busy}>
              {busy ? t('frp.saving') : t('frp.unsaved')}
            </Badge>
          ) : null}
        </div>
        {rule.stale ? (
          <div className="fs-xs text-muted">
            {rule.stale_owner
              ? t('frp.staleAssignedTo', { name: rule.stale_owner })
              : t('frp.staleUnowned')}
          </div>
        ) : null}
      </td>
      <td>
        <div className="flex items-center gap-4">
          {isDirty ? (
            <>
              <IconButton
                label={t('frp.saveRow')}
                variant="primary"
                disabled={busy}
                onClick={() => void save()}
              >
                <IconCheck size={15} />
              </IconButton>
              <IconButton label={t('frp.revertRow')} disabled={busy} onClick={() => setDraft(rule)}>
                <IconRefresh size={15} />
              </IconButton>
            </>
          ) : (
            <IconButton
              label={t('frp.deleteRule', { name: rule.name })}
              variant="danger"
              disabled={busy}
              onClick={() => void remove()}
            >
              <IconTrash size={15} />
            </IconButton>
          )}
        </div>
      </td>
    </tr>
  );
}

/* ---------------------------------------------------------------------------
   用户授权（仅管理员）：给某个用户额外放行可穿透的目标 IP / 网段
   —— 「自己名下虚机」拿不到 IP 时（没装 Guest Agent / 没开机 / 没静态地址），
   用户并不是无路可走：管理员在这里按人放行一两个具体地址即可。比在全球
   配置里开一整个网段精确得多，改完立刻反映到对方的「目标地址」下拉里。

   交互：添加区是「选人 → 填目标 → 添加」一行，回车就能提交，还能一次粘贴
   多个目标；列表按用户分组，每个人的授权是一组可单条撤销的 chip —— 原先
   是「一人一条」的表格，同一个人的多条授权散在各行，扫一眼看不出「这个人
   到底被放行了什么」。
   --------------------------------------------------------------------------- */

/** 一次可以粘贴多个目标：逗号 / 分号 / 空白（含换行）都当分隔符。 */
function splitGrantInput(raw: string): string[] {
  return raw
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * ``frpguard.parse_grant`` 的轻量镜像：只看形状（IP / 网段）。
 * 保留地址、面板自身地址这些判定仍以后端为准。
 */
function grantKind(value: string): 'ip' | 'cidr' | null {
  const text = value.trim();
  if (!text) return null;
  if (!text.includes('/')) return isIpLiteral(text) ? 'ip' : null;
  const [base, bitsRaw] = text.split('/');
  if (!isIpLiteral(base)) return null;
  const bits = Number(bitsRaw);
  const max = base.includes(':') ? 128 : 32;
  /* /0 等于关掉这道门，后端同样拒绝 —— 这里先挡一次，省得白跑一趟 */
  if (!Number.isInteger(bits) || bits < 1 || bits > max) return null;
  return 'cidr';
}

function GrantsCard() {
  const t = useT();
  const toast = useToast();
  const qc = useQueryClient();
  const { hasPermission } = useAuth();
  const canManage = hasPermission("settings.manage");
  const query = useQuery({
    queryKey: ["frp", "grants"],
    queryFn: frpApi.grants.list,
    enabled: canManage,
  });

  const [username, setUsername] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState("");
  const [error, setError] = useState("");

  const users = query.data?.users ?? [];
  const items = query.data?.items ?? [];

  /* 按用户分组：同一个人的多条授权收成一组。 */
  const groups = useMemo(() => {
    const map = new Map<string, FrpGrant[]>();
    for (const item of items) {
      const list = map.get(item.username) ?? [];
      list.push(item);
      map.set(item.username, list);
    }
    return [...map.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [items]);

  const tokens = useMemo(() => splitGrantInput(value), [value]);
  const invalidTokens = useMemo(
    () => tokens.filter((token) => grantKind(token) === null),
    [tokens],
  );

  if (!canManage) return null;

  async function add() {
    const targetUser = username.trim();
    if (!targetUser) {
      setError(t('frp.grantUserRequired'));
      return;
    }
    const wanted = [...new Set(tokens)];
    if (wanted.length === 0) {
      setError(t('frp.grantValueRequired'));
      return;
    }
    if (invalidTokens.length > 0) {
      setError(t('frp.grantInvalid', { values: invalidTokens.join('、') }));
      return;
    }

    setError("");
    setBusy(true);
    let ok = 0;
    let firstError = "";
    try {
      /* 逐条提交：授权接口要二次确认，串行能保证只弹一次框，也便于汇总失败项 */
      for (const token of wanted) {
        try {
          await frpApi.grants.add(targetUser, token);
          ok += 1;
        } catch (err) {
          firstError = firstError || errorMessage(err);
        }
      }
      await qc.invalidateQueries({ queryKey: ["frp"] });
      if (ok > 0) setValue("");
      if (ok > 0 && !firstError) {
        toast.success(
          t('frp.grantAdded'),
          wanted.length > 1
            ? t('frp.grantAddedCount', { n: ok, user: targetUser })
            : t('frp.grantAddedDetail', { user: targetUser }),
        );
      } else if (ok > 0) {
        toast.warning(
          t('frp.grantAddPartial', { ok, failed: wanted.length - ok }),
          firstError,
        );
      } else {
        toast.error(t('frp.grantAddFailed'), firstError);
      }
    } finally {
      setBusy(false);
    }
  }

  async function remove(item: FrpGrant) {
    const key = `${item.username}:${item.value}`;
    setRemoving(key);
    try {
      await frpApi.grants.remove(item.username, item.value);
      await qc.invalidateQueries({ queryKey: ["frp"] });
      toast.success(
        t('frp.grantRemoved'),
        t('frp.grantRemovedDetail', { value: item.value }),
      );
    } catch (err) {
      toast.error(t('frp.grantRemoveFailed'), errorMessage(err));
    } finally {
      setRemoving("");
    }
  }

  /* 授权表是「读 → 改 → 写」的整份 JSON：增删并发会互相覆盖，所以任何一次
     写操作进行中，另一侧的按钮都先禁用 */
  const locked = busy || Boolean(removing);

  return (
    <Card>
      <CardHeader
        title={t('frp.grantsTitle')}
        icon={<IconUsers size={16} />}
        subtitle={t('frp.grantsSubtitle')}
      />
      <div className="flex flex-col gap-16">
        {/* 添加区：选人 / 填目标 / 添加在同一行，回车即可提交 */}
        <div>
          <div className="grant-compose">
            <div className="grant-compose-user">
              <Select
                aria-label={t('frp.grantUser')}
                value={username}
                placeholder={
                  users.length === 0
                    ? t('frp.grantNoUsers')
                    : t('frp.grantUserPlaceholder')
                }
                options={users.map((name) => ({ value: name, label: name }))}
                disabled={locked || users.length === 0}
                onChange={(e) => {
                  setUsername(e.target.value);
                  if (error) setError("");
                }}
              />
            </div>
            <div className="grant-compose-value">
              <Input
                aria-label={t('frp.grantValue')}
                className="mono"
                placeholder={t('frp.grantValuePlaceholder')}
                value={value}
                error={error || undefined}
                disabled={busy}
                onChange={(e) => {
                  setValue(e.target.value);
                  if (error) setError("");
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    if (!locked) void add();
                  }
                }}
              />
            </div>
            <div className="grant-compose-actions">
              <Button
                variant="primary"
                icon={<IconPlus size={14} />}
                loading={busy}
                disabled={locked || !username || tokens.length === 0}
                onClick={() => void add()}
              >
                {t('frp.grantAdd')}
              </Button>
            </div>
          </div>
          {error ? null : (
            <p className="fs-xs text-muted grant-compose-hint">
              {t('frp.grantBatchHint')}
            </p>
          )}
        </div>

        {/* 已授权：按用户分组，一眼看清「谁被放行了什么」 */}
        {groups.length === 0 ? (
          <div className="grant-empty">
            <p className="fs-sm text-secondary">{t('frp.grantsEmpty')}</p>
            <p className="fs-xs text-muted">{t('frp.grantsEmptyHint')}</p>
          </div>
        ) : (
          <div className="grant-groups">
            {groups.map(([name, list]) => (
              <div className="grant-group" key={name}>
                <div className="grant-group-head">
                  <span className="grant-group-user mono">{name}</span>
                  <span className="fs-xs text-muted">
                    {t('frp.grantGroupCount', { n: list.length })}
                  </span>
                </div>
                <div className="grant-chips">
                  {list.map((item) => {
                    const key = `${item.username}:${item.value}`;
                    return (
                      <span
                        key={key}
                        className={`grant-chip ${removing === key ? 'is-busy' : ''}`}
                      >
                        <span className="mono">{item.value}</span>
                        <span className="grant-chip-kind">
                          {item.kind === 'cidr'
                            ? t('frp.grantKindCidr')
                            : t('frp.grantKindIp')}
                        </span>
                        <button
                          type="button"
                          className="grant-chip-remove"
                          title={t('frp.grantRemove', { value: item.value })}
                          aria-label={t('frp.grantRemove', { value: item.value })}
                          disabled={locked}
                          onClick={() => void remove(item)}
                        >
                          ×
                        </button>
                      </span>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="fs-xs text-muted">{t('frp.grantsNote')}</p>
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------------------------
   日志
   --------------------------------------------------------------------------- */
function LogCard() {
  const t = useT();
  const refresh = useRefreshInterval();
  const logsQuery = useQuery({
    queryKey: ["frp", "logs"],
    queryFn: frpApi.logs,
    refetchInterval: refresh,
  });
  const logs = logsQuery.data?.logs ?? [];

  return (
    <Card>
      <CardHeader
        title={t('frp.logsTitle')}
        icon={<IconNetwork size={16} />}
        actions={
          <Button
            variant="ghost"
            size="sm"
            icon={<IconRefresh size={14} />}
            onClick={() => void logsQuery.refetch()}
          >
            {t('frp.refreshLogs')}
          </Button>
        }
      />
      {logs.length > 0 ? (
        <div className="log-viewer">
          {logs.map((line, i) => (
            <div className="log-line" key={i}>
              <span className="log-line-num">{i + 1}</span>
              <span className="log-line-text">{line}</span>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-secondary fs-sm">{t('frp.noLogs')}</p>
      )}
    </Card>
  );
}