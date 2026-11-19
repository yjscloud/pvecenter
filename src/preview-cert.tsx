/* 临时视觉预览：证书页新布局（概览 + 单卡三视图 + 站点表）。不参与构建。 */
import "./preview-lang";
import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { I18nProvider, ensureDict } from "./i18n";
import { PageShell } from "./components/Layout";
import { Card } from "./components/ui/Card";
import { Badge } from "./components/ui/Badge";
import { Button, IconButton } from "./components/ui/Button";
import { EmptyState } from "./components/ui/EmptyState";
import {
  IconRefresh,
  IconPlus,
  IconSearch,
  IconEdit,
  IconClose,
  IconFilter,
  IconShield,
  IconDownload,
  IconTrash,
  IconUpload,
  IconChevronDown,
} from "./components/Icons";

import "./styles/theme.css";
import "./styles/global.css";
import "./styles/components.css";
import "./styles/layout.css";
import "./styles/responsive.css";

const STATS = [
  ["valid", "有效", 2],
  ["expiring", "即将过期", 1],
  ["expired", "已过期", 0],
  ["failed", "需处理", 0],
  ["paused", "已停用", 0],
] as const;

const ROWS = [
  {
    name: "pvecenter.jycloud.com",
    domain: "pvecenter.jycloud.com",
    state: "valid",
    label: "有效",
    days: "剩余 62 天",
    date: "2027-01-15",
    pct: 69,
    target: "root@192.168.1.10:/etc/nginx/ssl/…",
    sub: "pvecenter.jycloud.com.crt · pvecenter.jycloud.com.key",
    lastDeploy: "上次部署 12 天前",
  },
  {
    name: "pve.jycloud.com",
    domain: "pve.jycloud.com",
    state: "expiring",
    label: "即将过期",
    days: "剩余 7 天",
    date: "2026-11-25",
    pct: 8,
    target: "PVE 节点 pve",
    sub: "pve.fullchain.pem · pve.privkey.pem",
    lastDeploy: "上次部署 12 天前",
  },
];

const INV_ROWS = [
  {
    domain: "pvecenter.jycloud.com",
    extra: "www.pvecenter.jycloud.com",
    provider: "腾讯云 SSL",
    certId: "8Xk2pQe1Rt",
    state: "valid" as const,
    days: "剩余 62 天",
    date: "2027-01-15",
    pct: 69,
    sites: ["pvecenter.jycloud.com"],
  },
  {
    domain: "pve.jycloud.com",
    extra: "",
    provider: "托管证书",
    certId: "upload-9f2c41ab77d0",
    state: "expiring" as const,
    days: "剩余 7 天",
    date: "2026-11-25",
    pct: 8,
    sites: [],
  },
  {
    domain: "api.jycloud.com",
    extra: "",
    provider: "托管证书",
    certId: "upload-4c81be0f2d33",
    state: "expired" as const,
    days: "3 天前到期",
    date: "2026-11-15",
    pct: 0,
    sites: ["api.jycloud.com"],
  },
];

const INV_CHIPS = [
  ["", "全部渠道", 3],
  ["tencent", "腾讯云 SSL", 1],
  ["upload", "托管证书", 2],
] as const;

const TONE = { valid: "ok", expiring: "warning", expired: "danger" } as const;

function InventoryTable({ rows }: { rows: typeof INV_ROWS }) {
  return (
    <div className="table-container">
      <table className="table table-dense cert-inventory-table">
        <thead>
          <tr>
            <th className="cert-col-domains">域名</th>
            <th className="cert-col-cert">证书</th>
            <th className="cert-col-expire">到期</th>
            <th className="cert-col-sites">绑定站点</th>
            <th className="cert-col-actions">操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((item) => (
            <tr key={item.certId}>
              <td className="cert-col-domains">
                <div className="cert-inventory-domain">{item.domain}</div>
                {item.extra ? <div className="cert-sub mono">{item.extra}</div> : null}
              </td>
              <td className="cert-col-cert">
                <div className="cert-site-head">
                  <Badge variant="info" size="sm">
                    {item.provider}
                  </Badge>
                  <span className="mono cert-id">{item.certId}</span>
                </div>
              </td>
              <td className="cert-col-expire">
                <div className="cert-expiry">
                  <span className={`cert-state is-${item.state}`}>
                    <span className="cert-state-dot" aria-hidden="true" />
                    {STATE_LABEL[item.state]}
                  </span>
                  <span className={`cert-days is-${TONE[item.state]}`}>{item.days}</span>
                  <span className={`cert-bar is-${TONE[item.state]}`} role="presentation">
                    <span style={{ width: `${item.pct}%` }} />
                  </span>
                  <span className="cert-expiry-date">{item.date}</span>
                </div>
              </td>
              <td className="cert-col-sites">
                {item.sites.length ? (
                  <>
                    <div className="cert-inventory-sites">{item.sites.join("、")}</div>
                    <div className="cert-sub">{item.sites.length} 个站点</div>
                  </>
                ) : (
                  <span className="cert-inventory-unbound" title="这张证书还没有部署到任何站点">
                    未绑定站点
                  </span>
                )}
              </td>
              <td className="cert-col-actions">
                {item.sites.length === 1 ? (
                  <Button variant="ghost" size="sm">
                    部署
                  </Button>
                ) : null}
                {item.provider !== "托管证书" ? (
                  <Button variant="ghost" size="sm">
                    绑定
                  </Button>
                ) : null}
                <Button variant="ghost" size="sm" icon={<IconDownload size={14} />}>
                  导出
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SiteRow({ row }: { row: (typeof ROWS)[number] }) {
  return (
    <tr className="cert-row">
      <td className="cert-col-site">
        <div className="cert-site-head">
          <span className="cert-site-name">{row.name}</span>
          <Badge variant="success" size="sm">
            admin
          </Badge>
        </div>
        <div className="cert-site-domain mono">{row.domain}</div>
        <div className="cert-site-note">托管证书</div>
      </td>
      <td className="cert-col-expire">
        <div className="cert-expiry">
          <span className={`cert-state is-${row.state}`}>
            <span className="cert-state-dot" aria-hidden="true" />
            {row.label}
          </span>
          <span className={`cert-days is-${row.state === "valid" ? "ok" : "warning"}`}>
            {row.days}
          </span>
          <span
            className={`cert-bar is-${row.state === "valid" ? "ok" : "warning"}`}
            role="presentation"
          >
            <span style={{ width: `${row.pct}%` }} />
          </span>
          <span className="cert-expiry-date">{row.date}</span>
        </div>
      </td>
      <td className="cert-col-target">
        <div className="cert-target mono">{row.target}</div>
        <div className="cert-sub mono">{row.sub}</div>
        <div className="cert-sub">{row.lastDeploy}</div>
      </td>
      <td className="cert-col-run">
        <div className="cert-run">
          <span className="cert-run-pill is-ok">成功</span>
          <span className="cert-run-time">4 分钟前</span>
        </div>
      </td>
      <td className="cert-col-actions">
        <div className="cert-actions">
          <div className="cert-actions-group">
            <IconButton label="申请 / 续期">
              <IconShield size={15} />
            </IconButton>
            <IconButton label="立即部署">
              <IconDownload size={15} />
            </IconButton>
            <IconButton label="同步状态">
              <IconRefresh size={15} />
            </IconButton>
          </div>
          <span className="cert-actions-sep" aria-hidden="true" />
          <div className="cert-actions-group">
            <IconButton label="编辑站点">
              <IconEdit size={15} />
            </IconButton>
            <IconButton variant="danger" label="删除站点">
              <IconTrash size={15} />
            </IconButton>
          </div>
          <span className="cert-actions-sep" aria-hidden="true" />
          <button
            type="button"
            className="cert-expand"
            aria-label="展开运行详情"
            aria-expanded={false}
          >
            <IconChevronDown size={15} />
          </button>
        </div>
      </td>
    </tr>
  );
}

const STATE_LABEL: Record<string, string> = {
  valid: "有效",
  expiring: "即将过期",
  expired: "已过期",
  failed: "需处理",
  paused: "已停用",
};

const FILTER_CHIPS = [
  ["all", "全部", 3],
  ["due", "待续期", 1],
  ["failed", "需处理", 0],
  ["paused", "已停用", 0],
] as const;

function App() {
  const [, setTick] = useState(0);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [view, setView] = useState<"sites" | "inventory" | "providers">("sites");
  const [invSearch, setInvSearch] = useState("");
  const [invProvider, setInvProvider] = useState("");
  useEffect(() => {
    void ensureDict("settings").then(() => setTick((v) => v + 1));
  }, []);

  const rows = ROWS.filter((row) => {
    const kw = search.trim();
    if (kw && !`${row.name} ${row.domain}`.includes(kw)) return false;
    if (filter.startsWith("state:")) return row.state === filter.slice(6);
    if (filter === "due") return row.state === "expiring" || row.state === "expired";
    if (filter === "failed") return row.state === "failed";
    if (filter === "paused") return row.state === "paused";
    return true;
  });

  const invRows = INV_ROWS.filter((item) => {
    if (invProvider === "tencent" && item.provider !== "腾讯云 SSL") return false;
    if (invProvider === "upload" && item.provider !== "托管证书") return false;
    const kw = invSearch.trim();
    if (kw && !`${item.domain} ${item.extra} ${item.certId}`.includes(kw)) return false;
    return true;
  });

  return (
    <PageShell
      title="证书管理"
      subtitle="为站点自动签发与续期证书，部署到本机、远端主机或 PVE 节点"
      actions={
        <Button variant="secondary" icon={<IconRefresh size={15} />}>
          同步全部
        </Button>
      }
    >
      <div className="cert-overview">
        <div className="cert-overview-stats">
          {STATS.map(([state, label, value]) => {
            const body = (
              <>
                <span className="cert-stat-value">{value}</span>
                <span className="cert-stat-label">{label}</span>
              </>
            );
            return value > 0 ? (
              <button
                key={state}
                type="button"
                className={`cert-stat is-${state} has-value`}
                title="只看这一类"
                onClick={() => setFilter(`state:${state}`)}
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
          最近同步 4 分钟前
        </div>
      </div>

      <Card className="cert-card-main" collapsible={false}>
        <div className="cert-panel-bar">
          <div className="cert-tabs" role="tablist">
            {(
              [
                ["sites", "证书站点", ROWS.length],
                ["inventory", "证书清单", INV_ROWS.length],
                ["providers", "签发渠道", 1],
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
          {view === "inventory" ? (
            <div className="cert-toolbar">
              <div className="cert-search">
                <IconSearch size={14} />
                <input
                  value={invSearch}
                  placeholder="搜索域名 / 证书 ID"
                  aria-label="搜索"
                  onChange={(e) => setInvSearch(e.target.value)}
                />
                {invSearch ? (
                  <button
                    type="button"
                    className="cert-search-clear"
                    aria-label="清除搜索"
                    onClick={() => setInvSearch("")}
                  >
                    <IconClose size={12} />
                  </button>
                ) : null}
              </div>
              <div className="cert-filters" role="tablist">
                {INV_CHIPS.map(([id, label, count]) => (
                  <button
                    key={id}
                    type="button"
                    role="tab"
                    aria-selected={invProvider === id}
                    className={`cert-filter${invProvider === id ? " is-active" : ""}`}
                    onClick={() => setInvProvider(id)}
                  >
                    {label}
                    <span className="cert-filter-count">{count}</span>
                  </button>
                ))}
              </div>
              <Button variant="primary" size="sm" icon={<IconUpload size={14} />}>
                上传证书
              </Button>
            </div>
          ) : (
          <div className="cert-toolbar">
            <div className="cert-search">
              <IconSearch size={14} />
              <input
                value={search}
                placeholder="搜索站点名 / 域名"
                aria-label="搜索"
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") setSearch("");
                }}
              />
              {search ? (
                <button
                  type="button"
                  className="cert-search-clear"
                  aria-label="清除搜索"
                  onClick={() => setSearch("")}
                >
                  <IconClose size={12} />
                </button>
              ) : null}
            </div>
            <div className="cert-filters" role="tablist">
              {FILTER_CHIPS.map(([id, label, count]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={filter === id}
                  className={`cert-filter${filter === id ? " is-active" : ""}`}
                  onClick={() => setFilter(id)}
                >
                  {label}
                  <span className="cert-filter-count">{count}</span>
                </button>
              ))}
            </div>
            <Button variant="primary" size="sm" icon={<IconPlus size={14} />}>
              添加站点
            </Button>
          </div>
          )}
        </div>

        {view === "sites" ? (
          <>
        {(filter !== "all" || search) && rows.length > 0 ? (
          <div className="cert-filter-note" role="status">
            <IconFilter size={13} aria-hidden="true" />
            {filter.startsWith("state:") ? (
              <span className="cert-filter-note-state">
                只看「{STATE_LABEL[filter.slice(6)]}」
              </span>
            ) : null}
            <span className="cert-filter-note-text">
              筛出 {rows.length} / 共 {ROWS.length} 个站点
            </span>
            <button
              type="button"
              className="cert-filter-reset"
              onClick={() => {
                setFilter("all");
                setSearch("");
              }}
            >
              清除筛选
            </button>
          </div>
        ) : null}

        {rows.length > 0 ? (
          <div className="table-container">
            <table className="table table-dense cert-table">
              <thead>
                <tr>
                  <th className="cert-col-site">站点</th>
                  <th className="cert-col-expire">到期</th>
                  <th className="cert-col-target">部署位置</th>
                  <th className="cert-col-run">最近运行</th>
                  <th className="cert-col-actions">操作</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <SiteRow key={row.domain} row={row} />
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState
            icon={<IconShield size={26} />}
            title="没有匹配的站点"
            description="换个关键词，或清除下面的筛选条件。"
            action={
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setFilter("all");
                  setSearch("");
                }}
              >
                清除筛选
              </Button>
            }
          />
        )}
          </>
        ) : view === "inventory" ? (
          <InventoryTable rows={invRows} />
        ) : null}
      </Card>
    </PageShell>
  );
}

try {
  localStorage.setItem("pve_lang", "zh-CN");
} catch {
  /* 忽略 */
}

createRoot(document.getElementById("root")!).render(
  <I18nProvider>
    <App />
  </I18nProvider>,
);
