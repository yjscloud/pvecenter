/* ==========================================================================
   pvecenter — 上传第三方证书

   别处已经签好的证书（商采 / 自签 / 迁移过来）要能被面板接管：导入后进入证书
   清单，可以部署、导出、替换，和云上签发的证书走同一套管理。

   一条链路做三件事：
     1. 建一个 ``ca = upload`` 的站点（清单是按站点派生的，没有站点就没有归属）；
     2. 把 PEM/KEY 交给后端校验并落库（域名覆盖、到期时间都由后端复核）；
     3. 需要的话立刻部署一次——「上传并运行」在这里是一次点击，而不是三步。

   部署方式只放开本机与 SSH：云负载均衡 / PVE 节点 / 客户机 Agent 需要更多参数，
   放在站点编辑里补，比在这个小弹窗里塞三组字段更清楚。
   ========================================================================== */

import { useState } from "react";
import { Modal } from "./ui/Modal";
import { Button } from "./ui/Button";
import { Input, Select, Switch, Textarea } from "./ui/Input";
import { IconUpload } from "./Icons";
import { useT } from "../i18n";

export interface CertUploadPayload {
  domain: string;
  name: string;
  notes: string;
  method: "local" | "ssh";
  deployDir: string;
  certFilename: string;
  keyFilename: string;
  reloadCommand: string;
  sshHost: string;
  sshPort: number;
  sshUser: string;
  sshAuth: "password" | "key";
  sshPassword: string;
  sshKey: string;
  pem: string;
  key: string;
  deployNow: boolean;
}

function blankUpload(): CertUploadPayload {
  return {
    domain: "",
    name: "",
    notes: "",
    method: "local",
    deployDir: "/etc/nginx/ssl",
    certFilename: "fullchain.pem",
    keyFilename: "privkey.pem",
    reloadCommand: "nginx -s reload",
    sshHost: "",
    sshPort: 22,
    sshUser: "root",
    sshAuth: "password",
    sshPassword: "",
    sshKey: "",
    pem: "",
    key: "",
    deployNow: true,
  };
}

export function CertUploadDialog({
  open,
  busy,
  onClose,
  onSubmit,
}: {
  open: boolean;
  busy: boolean;
  onClose: () => void;
  /** 返回 true 表示导入成功，弹窗自己收起 */
  onSubmit: (payload: CertUploadPayload) => Promise<boolean>;
}) {
  const t = useT();
  const [form, setForm] = useState<CertUploadPayload>(blankUpload);
  const [tried, setTried] = useState(false);

  const errors: Record<string, string> = {};
  const domain = form.domain.trim();
  if (!domain) errors.domain = t("cert.errDomainRequired");
  else if (!/^(\*\.)?([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i.test(domain)) {
    errors.domain = t("cert.errDomainFormat");
  }
  if (!form.pem.includes("BEGIN CERTIFICATE")) errors.pem = t("cert.uploadPemInvalid");
  if (!/BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY/.test(form.key)) {
    errors.key = t("cert.uploadKeyInvalid");
  }
  if (!form.deployDir.trim()) errors.deployDir = t("cert.errDeployDir");
  if (form.method === "ssh") {
    if (!form.sshHost.trim()) errors.sshHost = t("cert.errSshHostRequired");
    if (!form.sshUser.trim()) errors.sshUser = t("cert.errSshUserRequired");
    if (!Number.isInteger(form.sshPort) || form.sshPort < 1 || form.sshPort > 65535) {
      errors.sshPort = t("cert.errPort");
    }
  }
  const issueCount = Object.keys(errors).length;

  async function submit() {
    if (issueCount > 0) {
      setTried(true);
      // 聚焦第一个出错的控件：PEM 区域很长，光靠红字不一定看得到
      requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(".cert-upload-form [aria-invalid='true']")?.focus();
      });
      return;
    }
    const ok = await onSubmit(form);
    if (ok) {
      setForm(blankUpload());
      setTried(false);
    }
  }

  function close() {
    onClose();
    setTried(false);
  }

  return (
    <Modal
      open={open}
      title={t("cert.uploadThirdParty")}
      description={t("cert.uploadDesc")}
      size="lg"
      onClose={close}
      footer={
        <>
          <Button variant="secondary" onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="primary"
            icon={<IconUpload size={14} />}
            loading={busy}
            onClick={() => void submit()}
          >
            {form.deployNow ? t("cert.uploadAndDeploy") : t("cert.uploadCta")}
          </Button>
        </>
      }
    >
      <div className="form-grid cert-upload-form">
        <Input
          label={t("cert.fieldDomain")}
          required
          value={form.domain}
          placeholder="example.com"
          className="mono"
          error={tried ? errors.domain : undefined}
          hint={t("cert.uploadDomainHint")}
          onChange={(e) => setForm({ ...form, domain: e.target.value })}
        />
        <Input
          label={t("cert.uploadName")}
          value={form.name}
          placeholder={t("cert.siteNamePlaceholder")}
          onChange={(e) => setForm({ ...form, name: e.target.value })}
        />
        <div style={{ gridColumn: "1 / -1" }}>
          <Textarea
            label={t("cert.fieldPem")}
            required
            mono
            rows={7}
            value={form.pem}
            placeholder={t("cert.pemPlaceholder")}
            error={tried ? errors.pem : undefined}
            hint={t("cert.uploadPemHint")}
            onChange={(e) => setForm({ ...form, pem: e.target.value })}
          />
        </div>
        <div style={{ gridColumn: "1 / -1" }}>
          <Textarea
            label={t("cert.fieldKeyPem")}
            required
            mono
            rows={5}
            value={form.key}
            placeholder={t("cert.keyPemPlaceholder")}
            error={tried ? errors.key : undefined}
            onChange={(e) => setForm({ ...form, key: e.target.value })}
          />
        </div>

        <Select
          label={t("cert.fieldDeployMethod")}
          value={form.method}
          options={[
            { value: "local", label: t("cert.uploadMethodLocal") },
            { value: "ssh", label: t("cert.uploadMethodSsh") },
          ]}
          hint={t("cert.uploadMethodNote")}
          onChange={(e) =>
            setForm({ ...form, method: e.target.value as CertUploadPayload["method"] })
          }
        />
        <Input
          label={t("cert.fieldDeployDir")}
          value={form.deployDir}
          className="mono"
          error={tried ? errors.deployDir : undefined}
          onChange={(e) => setForm({ ...form, deployDir: e.target.value })}
        />

        {form.method === "ssh" ? (
          <>
            <Input
              label={t("cert.fieldSshHost")}
              required
              value={form.sshHost}
              placeholder={t("cert.sshHostPlaceholder")}
              className="mono"
              error={tried ? errors.sshHost : undefined}
              onChange={(e) => setForm({ ...form, sshHost: e.target.value })}
            />
            <Input
              label={t("cert.fieldSshPort")}
              type="number"
              value={form.sshPort}
              error={tried ? errors.sshPort : undefined}
              onChange={(e) => setForm({ ...form, sshPort: Number(e.target.value) })}
            />
            <Input
              label={t("cert.fieldSshUser")}
              required
              value={form.sshUser}
              className="mono"
              error={tried ? errors.sshUser : undefined}
              onChange={(e) => setForm({ ...form, sshUser: e.target.value })}
            />
            <Select
              label={t("cert.fieldSshAuth")}
              value={form.sshAuth}
              options={[
                { value: "password", label: t("cert.authPassword") },
                { value: "key", label: t("cert.authKey") },
              ]}
              onChange={(e) =>
                setForm({ ...form, sshAuth: e.target.value as CertUploadPayload["sshAuth"] })
              }
            />
            {form.sshAuth === "key" ? (
              <div style={{ gridColumn: "1 / -1" }}>
                <Textarea
                  label={t("cert.fieldSshKey")}
                  rows={4}
                  mono
                  value={form.sshKey}
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY----- …"
                  onChange={(e) => setForm({ ...form, sshKey: e.target.value })}
                />
              </div>
            ) : (
              <Input
                label={t("cert.fieldSshPassword")}
                type="password"
                value={form.sshPassword}
                autoComplete="new-password"
                onChange={(e) => setForm({ ...form, sshPassword: e.target.value })}
              />
            )}
          </>
        ) : null}

        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <Switch
            checked={form.deployNow}
            label={t("cert.uploadDeployNow")}
            hint={t("cert.uploadDeployNowHint")}
            onChange={(v) => setForm({ ...form, deployNow: v })}
          />
        </div>
        <div style={{ gridColumn: "1 / -1" }} className="fs-xs text-muted">
          {t("cert.uploadOnlyHint")}
        </div>
      </div>
    </Modal>
  );
}
