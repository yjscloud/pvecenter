/* ==========================================================================
   签发渠道

   这一块要回答四个问题：
     1. 我有哪些渠道、能不能用（状态）；
     2. 用的哪套凭据、什么时候验过（配置项与自检）；
     3. 上次失败到底报了什么（失败原因，常驻而不是只冒一次 toast）；
     4. 我能不能改（权限）。

   卡片只负责「看」：凭据的填写在弹窗里一次填完，页面上只留结果与状态——
   和 certd 的授权管理一样，列表是入口，不是表单。

   底部的签发流程是给第一次用的人看的：渠道配好之后证书是怎么走完一圈的，
   在页面上说清楚，比让人去翻文档快。

   ``bare`` 模式：嵌在证书页主卡里时用。此时不套 Card / CardHeader，
   只渲染「一行摘要 + 渠道列表」，避免一张卡里再套一张卡。
   ========================================================================== */

import { useMemo, useState } from "react";
import { Button } from "./ui/Button";
import { Card, CardHeader } from "./ui/Card";
import { IconAlert, IconCheck, IconCloud } from "./Icons";
import { CertProviderDialog, type ProviderEditorMode } from "./CertProviderDialog";
import { useT, type MessageKey } from "../i18n";
import { formatRelative } from "../utils/format";
import type { CertProviderInfo } from "../api/types";

/** 一次自检的结果（页面持有，卡片只负责展示） */
export interface ProviderTestResult {
  ok: boolean;
  detail: string;
  at: number;
}

interface Props {
  providers: CertProviderInfo[];
  canManage: boolean;
  /** 页面级忙碌键：``provider:<kind>`` / ``provider-test:<kind>`` */
  busy: string;
  /** 本次打开页面后的自检结果，按 kind 索引 */
  tests?: Record<string, ProviderTestResult>;
  /** 每个渠道下管的证书数（来自清单统计） */
  counts?: Record<string, number>;
  onSave: (kind: string, payload: Record<string, unknown>) => Promise<boolean>;
  onTest: (kind: string) => Promise<{ ok: boolean; detail: string }>;
  /** 嵌在别的卡片里：不再套一层 Card / CardHeader */
  bare?: boolean;
}

interface Editor {
  mode: ProviderEditorMode;
  kind: string;
}

/** AK ID 之类的标识不是秘密，但也没必要整串铺在列表里 */
function maskId(value: string): string {
  const text = String(value || "");
  if (text.length <= 12) return text;
  return text.slice(0, 6) + "…" + text.slice(-4);
}

export function CertProvidersCard({
  providers,
  canManage,
  busy,
  tests = {},
  counts = {},
  onSave,
  onTest,
  bare = false,
}: Props) {
  const t = useT();
  const [editor, setEditor] = useState<Editor | null>(null);

  const configured = useMemo(
    () => providers.filter((provider) => provider.configured),
    [providers],
  );
  const pending = useMemo(
    () => providers.filter((provider) => !provider.configured),
    [providers],
  );

  function openEdit(kind: string) {
    setEditor({ mode: "edit", kind });
  }

  const summary = t("cert.providerSummary", {
    n: configured.length,
    total: providers.length,
  });

  const dialog = editor ? (
    <CertProviderDialog
      mode={editor.mode}
      providers={providers}
      initialKind={editor.kind}
      canManage={canManage}
      busy={busy}
      onClose={() => setEditor(null)}
      onTest={onTest}
      onSave={async (kind, payload) => {
        const ok = await onSave(kind, payload);
        if (ok) setEditor(null);
        return ok;
      }}
    />
  ) : null;

  /* ---- 流程说明：渠道配好之后，证书怎么走完一圈 ---- */
  const flow = (
    <div className="cert-provider-flow-box">
      <span className="cert-provider-flow-title">{t("cert.providerFlowTitle")}</span>
      <ol className="cert-provider-flow">
        {(
          [
            "cert.providerFlow1",
            "cert.providerFlow2",
            "cert.providerFlow3",
            "cert.providerFlow4",
          ] as const
        ).map((key, index) => (
          <li className="cert-provider-flow-step" key={key}>
            <span className="cert-provider-flow-index" aria-hidden="true">
              {index + 1}
            </span>
            <span className="cert-provider-flow-text">{t(key)}</span>
          </li>
        ))}
      </ol>
    </div>
  );

  const body =
    providers.length === 0 ? (
      <div className="cert-provider-empty">
        <span className="cert-provider-empty-icon" aria-hidden="true">
          <IconCloud size={18} />
        </span>
        <div className="cert-provider-empty-text">
          <div className="cert-provider-empty-title">{t("cert.providerEmptyTitle")}</div>
          <div className="fs-xs text-muted">{t("cert.providerEmptyDesc")}</div>
        </div>
      </div>
    ) : (
      <>
        {configured.length === 0 ? (
          <div className="cert-provider-hint">
            <IconAlert size={14} aria-hidden="true" />
            <span>{t("cert.providerEmptyDesc")}</span>
          </div>
        ) : null}

        {configured.length > 0 ? (
          <div className="cert-provider-list">
            {configured.map((provider) => {
              const idField = provider.identity_key
                ? provider.fields.find((field) => field.key === provider.identity_key)
                : provider.fields.find((field) => !field.secret && /(^|_)id$/.test(field.key));
              const renew = provider.values?.renew_before_days;
              /* 本次打开页面后的结果优先，其次读服务端持久化的上一次自检 */
              const test = tests[provider.kind] ?? provider.last_test ?? null;
              const count = counts[provider.kind] ?? 0;
              const failed = !!test && !test.ok;
              const badgeTone = failed ? "is-failed" : "is-ready";
              const badgeText = failed
                ? t("cert.providerTestFailedShort")
                : t("cert.providerAvailable");
              return (
                <div
                  className="cert-provider-row"
                  key={provider.kind}
                  data-state={failed ? "failed" : "ready"}
                >
                  <div className="cert-provider-main">
                    <div className="cert-provider-head">
                      <span className="cert-provider-name">
                        {t(provider.label_key as MessageKey) || provider.label}
                      </span>
                      <span className={`cert-provider-badge ${badgeTone}`}>
                        {failed ? (
                          <IconAlert size={12} aria-hidden="true" />
                        ) : (
                          <IconCheck size={12} aria-hidden="true" />
                        )}
                        {badgeText}
                      </span>
                      {count > 0 ? (
                        <span className="cert-provider-count fs-xs">
                          {t("cert.providerCertCount", { n: count })}
                        </span>
                      ) : null}
                    </div>
                    <div className="cert-provider-meta">
                      <span className="cert-provider-id mono fs-xs">
                        {idField ? maskId(String(provider.values?.[idField.key] ?? "")) : ""}
                      </span>
                      {renew != null ? (
                        <span className="fs-xs">
                          {t("cert.renewDaysShort", { n: Number(renew) })}
                        </span>
                      ) : null}
                      <span className="fs-xs">
                        {test
                          ? t("cert.providerLastTest", { time: formatRelative(test.at) })
                          : t("cert.providerNeverTested")}
                      </span>
                    </div>
                    {/* 失败原因就地展开：自检报的错是排障起点，不该随 toast 一起消失 */}
                    {failed ? (
                      <div className="cert-provider-error" role="status">
                        <IconAlert size={13} aria-hidden="true" />
                        <span>{test.detail}</span>
                      </div>
                    ) : null}
                  </div>
                  <div className="cert-provider-actions">
                    {provider.test_disabled ? null : (
                      <Button
                        variant="ghost"
                        size="sm"
                        loading={busy === "provider-test:" + provider.kind}
                        disabled={!canManage}
                        onClick={() => void onTest(provider.kind)}
                      >
                        {t(
                          provider.kind === "acme"
                            ? "cert.verifyConfig"
                            : "cert.testConnection",
                        )}
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={!canManage}
                      onClick={() => openEdit(provider.kind)}
                    >
                      {t("common.edit")}
                    </Button>
                    {provider.help_url ? (
                      <a
                        className="cert-provider-help"
                        href={provider.help_url}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {t("cert.providerHelp")}
                      </a>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        {/* 未配置的渠道也列出来：用户需要知道「还能接哪些」，而不是一片空白 */}
        {pending.length > 0 ? (
          <div className="cert-provider-pending">
            <div className="cert-provider-section-title">{t("cert.providerPendingTitle")}</div>
            {pending.map((provider) => (
              <div className="cert-provider-pending-row" key={provider.kind}>
                {/* 与已配置卡片同构：左边名称 + 一行说明，右边动作。
                    三列 grid 会把说明推到卡片中间，读起来像「两个不相关的字段」。 */}
                <div className="cert-provider-pending-main">
                  <span className="cert-provider-name">
                    {t(provider.label_key as MessageKey) || provider.label}
                  </span>
                  <span className="fs-xs cert-provider-tagline">
                    {provider.tagline_key
                      ? t(provider.tagline_key as MessageKey)
                      : provider.tagline || t("cert.providerUnconfiguredHint")}
                  </span>
                </div>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={!canManage}
                  onClick={() => setEditor({ mode: "add", kind: provider.kind })}
                >
                  {t("cert.providerConfigure")}
                </Button>
              </div>
            ))}
          </div>
        ) : null}

        {flow}
      </>
    );

  /* 摘要行只陈述状态，不放「添加签发渠道」：
     下面每个未配置的渠道都有自己的「配置」，同一个弹窗开两个入口只是重复 */
  const note =
    providers.length === 0 ? null : (
      <div className="cert-provider-note">
        <div className="cert-provider-note-main">
          <span className="cert-provider-note-summary">{summary}</span>
          {/* 进度只是把「已配置几条」可视化：读屏器读一次 summary 就够 */}
          <span
            className="cert-provider-progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={providers.length}
            aria-valuenow={configured.length}
            aria-label={summary}
          >
            <span
              style={{
                width: `${
                  providers.length
                    ? Math.round((configured.length / providers.length) * 100)
                    : 0
                }%`,
              }}
            />
          </span>
        </div>
        {canManage ? (
          <span className="cert-provider-crednote">{t("cert.providerCredNote")}</span>
        ) : (
          /* 没有 cert.manage：说清楚能看不能改，比单纯把按钮置灰更明确 */
          <span className="cert-provider-readonly">{t("cert.providerReadonly")}</span>
        )}
      </div>
    );

  if (bare) {
    return (
      <div className="cert-panel-body cert-provider-panel">
        {note}
        {body}
        {dialog}
      </div>
    );
  }

  return (
    <Card className="cert-card-sub">
      <CardHeader
        title={t("cert.providersTitle")}
        subtitle={providers.length > 0 ? summary : undefined}
        icon={<IconCloud size={16} />}
      />
      <div className="cert-panel-body cert-provider-panel">
        {body}
        {dialog}
      </div>
    </Card>
  );
}
