/* ==========================================================================
   证书「附加部署目标」编辑器

   第一个目标仍由站点表单里的「部署方式 / 目录」控件编辑（历史包袱，也让
   常见场景只填一处）；这里管理第 2..N 个目标 —— 同一张证书同时写到多台
   服务器 / 多个容器时用。目标之间互不影响，某个目标失败只影响它自己的
   状态（见后端 certs.deploy_site）。
   ========================================================================== */

import { Button } from "./ui/Button";
import { Input, Select } from "./ui/Input";
import { IconPlus, IconTrash } from "./Icons";
import { useT } from "../i18n";
import type { CertTarget } from "../api/types";

interface Props {
  targets: CertTarget[];
  methods: { value: string; label: string }[];
  disabled?: boolean;
  onChange: (targets: CertTarget[]) => void;
}

const BLANK: CertTarget = {
  method: "local",
  deploy_dir: "/etc/nginx/ssl",
  cert_filename: "fullchain.pem",
  key_filename: "privkey.pem",
  reload_command: "nginx -s reload",
  ssh_port: 22,
  ssh_user: "root",
  ssh_auth: "password",
  listener_port: 443,
};

/** 云负载均衡 / CDN：没有本地目录，字段与 SSH/Agent 完全不同 */
const CLOUD_METHODS = ["tencent_clb", "aliyun_slb", "tencent_cdn", "aliyun_cdn"];

function isCloudMethod(method?: string): boolean {
  return CLOUD_METHODS.includes(String(method || ""));
}

export function CertTargetsEditor({ targets, methods, disabled, onChange }: Props) {
  const t = useT();

  const update = (index: number, patch: Partial<CertTarget>) => {
    onChange(targets.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  };

  return (
    <div className="cert-targets" style={{ gridColumn: "1 / -1" }}>
      <div className="cert-targets-head">
        <div>
          <div className="field-label">{t('cert.targetsTitle')}</div>
          <div className="fs-xs text-muted">{t('cert.targetsHint')}</div>
        </div>
        <Button
          variant="ghost"
          size="sm"
          icon={<IconPlus size={14} />}
          disabled={disabled || targets.length >= 8}
          onClick={() => onChange([...targets, { ...BLANK, id: `t-${Date.now().toString(36)}` }])}
        >
          {t('cert.addTarget')}
        </Button>
      </div>

      {targets.length === 0 ? (
        <div className="fs-xs text-muted">{t('cert.targetsEmpty')}</div>
      ) : null}

      {targets.map((target, index) => (
        <div className="cert-target-card" key={target.id || index}>
          <div className="cert-target-head">
            <span className="fs-sm">{target.name || t('cert.targetN', { n: index + 2 })}</span>
            <Button
              variant="ghost"
              size="sm"
              icon={<IconTrash size={14} />}
              disabled={disabled}
              onClick={() => onChange(targets.filter((_, i) => i !== index))}
            >
              {t('common.delete')}
            </Button>
          </div>
          <div className="form-grid">
            <Input
              label={t('cert.fieldTargetName')}
              value={target.name || ""}
              placeholder={t('cert.targetNamePlaceholder')}
              disabled={disabled}
              onChange={(e) => update(index, { name: e.target.value })}
            />
            <Select
              label={t('cert.fieldDeployMethod')}
              value={target.method || "local"}
              options={methods}
              disabled={disabled}
              onChange={(e) => update(index, { method: e.target.value as CertTarget["method"] })}
            />
            {target.method !== "pve" && !isCloudMethod(target.method) ? (
              <>
                <Input
                  label={t('cert.fieldDeployDir')}
                  value={target.deploy_dir || ""}
                  placeholder="/etc/nginx/ssl"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { deploy_dir: e.target.value })}
                />
                <Input
                  label={t('cert.fieldReload')}
                  value={target.reload_command || ""}
                  placeholder="nginx -s reload"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { reload_command: e.target.value })}
                />
              </>
            ) : target.method === "pve" ? (
              <Input
                label={t('cert.fieldNode')}
                value={target.agent_node || ""}
                placeholder="pve1"
                className="mono"
                disabled={disabled}
                hint={t('cert.pveNodeHint')}
                onChange={(e) => update(index, { agent_node: e.target.value })}
              />
            ) : null}

            {target.method === "tencent_clb" ? (
              <>
                <Input
                  label={t('cert.fieldCloudRegion')}
                  value={target.cloud_region || ""}
                  placeholder="ap-guangzhou"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { cloud_region: e.target.value })}
                />
                <Input
                  label={t('cert.fieldLbId')}
                  value={target.lb_id || ""}
                  placeholder="lb-xxxxxxxx"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { lb_id: e.target.value })}
                />
                <Input
                  label={t('cert.fieldListenerId')}
                  value={target.listener_id || ""}
                  placeholder="lbl-xxxxxxxx"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { listener_id: e.target.value })}
                />
              </>
            ) : null}

            {target.method === "aliyun_slb" ? (
              <>
                <Input
                  label={t('cert.fieldLbId')}
                  value={target.lb_id || ""}
                  placeholder="lb-xxxxxxxx"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { lb_id: e.target.value })}
                />
                <Input
                  label={t('cert.fieldListenerPort')}
                  value={String(target.listener_port || 443)}
                  placeholder="443"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { listener_port: Number(e.target.value) || 443 })}
                />
              </>
            ) : null}

            {target.method === "tencent_cdn" || target.method === "aliyun_cdn" ? (
              <Input
                label={t('cert.fieldCdnDomain')}
                value={target.cdn_domain || ""}
                placeholder={t('cert.cdnDomainPlaceholder')}
                className="mono"
                disabled={disabled}
                onChange={(e) => update(index, { cdn_domain: e.target.value })}
              />
            ) : null}

            {target.method === "ssh" || target.method === "lxc" ? (
              <>
                <Input
                  label={t('cert.fieldSshHost')}
                  value={target.ssh_host || ""}
                  placeholder={target.method === "lxc" ? t('cert.nodeHostPlaceholder') : "10.0.0.10"}
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { ssh_host: e.target.value })}
                />
                <Input
                  label={t('cert.fieldSshUser')}
                  value={target.ssh_user || "root"}
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { ssh_user: e.target.value })}
                />
                <Input
                  label={t('cert.fieldSshPassword')}
                  type="password"
                  autoComplete="new-password"
                  value={target.ssh_password || ""}
                  placeholder={t('cert.secretPlaceholderSet')}
                  disabled={disabled}
                  onChange={(e) => update(index, { ssh_password: e.target.value })}
                />
              </>
            ) : null}

            {target.method === "agent" || target.method === "lxc" ? (
              <>
                <Input
                  label={t('cert.fieldNode')}
                  value={target.agent_node || ""}
                  placeholder="pve"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { agent_node: e.target.value })}
                />
                <Input
                  label={target.method === "lxc" ? t('cert.fieldCtId') : t('cert.fieldVmid')}
                  value={target.agent_vmid || ""}
                  placeholder="101"
                  className="mono"
                  disabled={disabled}
                  onChange={(e) => update(index, { agent_vmid: e.target.value })}
                />
              </>
            ) : null}
          </div>
        </div>
      ))}
    </div>
  );
}
