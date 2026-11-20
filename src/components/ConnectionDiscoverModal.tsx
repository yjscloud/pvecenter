/* ==========================================================================
   一键接入：发现 → 认证 → 完成
   ==========================================================================

   新装面板的第一次接入，用户手里通常只有「几台 PVE」这件事，没有 IP 清单，
   也没在 PVE 里建过令牌。所以这里不要求他先去 PVE 界面点一遍，而是：

     1. 扫本机所在网段，探 8006 + ``/version`` 把 PVE 认出来（只读、不带凭据）；
     2. 选一台，输一次 PVE 管理员密码；
     3. 后端在节点上建 ``pvecenter@pve``（PVEAuditor）+ 专用令牌并落库。

   和「手动添加」的分工：手动添加应对已知地址 / 跨网段；这里应对「同一内网、
   我不知道有哪些机器」。注册码那条路（见 ConnectionOnboardModal）留给跨网段
   和批量 —— 需要节点上跑 root 脚本，不适合首次接入。
   ========================================================================== */

import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { connectionsApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import type {
  DiscoveredPve,
  PveDiscoveryResult,
  PveProvisionResult,
} from '../api/types';
import { useT } from '../i18n';
import { Badge } from './ui/Badge';
import { Button } from './ui/Button';
import { Field, Input, Switch } from './ui/Input';
import { Modal } from './ui/Modal';
import { Notice } from './ui/EmptyState';
import {
  IconCheck,
  IconChevronRight,
  IconRefresh,
  IconSearch,
  IconServer,
  IconShield,
} from './Icons';

export interface ConnectionDiscoverModalProps {
  open: boolean;
  onClose: () => void;
}

type Phase = 'scan' | 'auth' | 'done';

interface AuthForm {
  username: string;
  password: string;
  name: string;
  region: string;
  storePassword: boolean;
  verifySsl: boolean;
}

function blankForm(): AuthForm {
  return {
    username: 'root@pam',
    password: '',
    name: '',
    region: '',
    storePassword: false,
    /* PVE 默认自签（CN=pve.local，证书里没有 IP SAN），首次接入按不校验证书
       走通；接进来之后可以在「编辑」里打开校验或钉一张 CA。 */
    verifySsl: false,
  };
}

export function ConnectionDiscoverModal({
  open,
  onClose,
}: ConnectionDiscoverModalProps) {
  const t = useT();
  const queryClient = useQueryClient();

  const [phase, setPhase] = useState<Phase>('scan');
  const [subnet, setSubnet] = useState('');
  const [scanning, setScanning] = useState(false);
  const [result, setResult] = useState<PveDiscoveryResult | null>(null);
  const [scanError, setScanError] = useState<string | null>(null);
  const [picked, setPicked] = useState<DiscoveredPve | null>(null);
  const [form, setForm] = useState<AuthForm>(blankForm);
  const [formError, setFormError] = useState<string | null>(null);
  const [provisioning, setProvisioning] = useState(false);
  const [provisionError, setProvisionError] = useState<string | null>(null);
  const [done, setDone] = useState<PveProvisionResult | null>(null);

  const runScan = async (cidr: string) => {
    setScanning(true);
    setScanError(null);
    setResult(null);
    setPicked(null);
    try {
      setResult(await connectionsApi.discover({ cidr: cidr.trim() }));
    } catch (err) {
      setScanError(errorMessage(err));
    } finally {
      setScanning(false);
    }
  };

  /* 只在「刚打开」时扫一次：每次 render 都扫会把刚选好的节点冲掉 */
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setPhase('scan');
      setSubnet('');
      setResult(null);
      setScanError(null);
      setPicked(null);
      setDone(null);
      setProvisionError(null);
      setForm(blankForm());
      void runScan('');
    }
    wasOpen.current = open;
    // runScan 只用到 setState，不随依赖变化
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const close = () => {
    setPhase('scan');
    setPicked(null);
    setDone(null);
    onClose();
  };

  const pick = (node: DiscoveredPve) => {
    setPicked(node);
    setPhase('auth');
    setProvisionError(null);
    setFormError(null);
    /* 名字留空就用 PVE 的节点名兜底；这里只预填区域之外的可见项 */
    setForm((prev) => ({ ...prev, name: '', password: '' }));
  };

  const submit = async () => {
    if (!picked) return;
    if (!form.username.trim()) {
      setFormError(t('conn.discoverUserRequired'));
      return;
    }
    if (!form.password) {
      setFormError(t('conn.discoverPasswordRequired'));
      return;
    }
    setProvisioning(true);
    setProvisionError(null);
    setFormError(null);
    try {
      const data = await connectionsApi.provision({
        host: picked.host,
        port: picked.port,
        username: form.username.trim(),
        password: form.password,
        name: form.name.trim(),
        region: form.region.trim(),
        verify_ssl: form.verifySsl,
        store_admin_password: form.storePassword,
      });
      setDone(data);
      setPhase('done');
      /* 连接列表、节点、健康、集群读数全变了 —— 一次刷干净 */
      void queryClient.invalidateQueries();
    } catch (err) {
      setProvisionError(errorMessage(err));
    } finally {
      setProvisioning(false);
    }
  };

  const steps = [
    t('conn.discoverStep1'),
    t('conn.discoverStep2'),
    t('conn.discoverStep3'),
  ];
  const activeStep = phase === 'scan' ? 1 : phase === 'auth' ? 2 : 3;

  const footer =
    phase === 'done' ? (
      <div className="wizard-footer">
        <span className="wizard-footer-step onboard-footer-hint">
          {t('conn.discoverDoneHint')}
        </span>
        <div className="wizard-footer-actions">
          <Button
            variant="secondary"
            onClick={() => {
              setDone(null);
              setPicked(null);
              setPhase('scan');
              void runScan(subnet);
            }}
          >
            {t('conn.discoverAgain')}
          </Button>
          <Button variant="primary" onClick={close}>
            {t('conn.discoverFinish')}
          </Button>
        </div>
      </div>
    ) : phase === 'auth' ? (
      <div className="wizard-footer">
        <span className="wizard-footer-step onboard-footer-hint">
          {t('conn.discoverOnceHint')}
        </span>
        <div className="wizard-footer-actions">
          <Button variant="ghost" onClick={() => setPhase('scan')}>
            {t('conn.discoverBack')}
          </Button>
          <Button
            variant="primary"
            loading={provisioning}
            onClick={() => void submit()}
          >
            {provisioning ? t('conn.discoverWorking') : t('conn.discoverStart')}
          </Button>
        </div>
      </div>
    ) : (
      <div className="wizard-footer">
        <span className="wizard-footer-step onboard-footer-hint">
          {t('conn.discoverScanHint')}
        </span>
        <div className="wizard-footer-actions">
          <Button
            variant="secondary"
            icon={<IconRefresh size={14} />}
            loading={scanning}
            onClick={() => void runScan(subnet)}
          >
            {t('conn.discoverRescan')}
          </Button>
          <Button variant="ghost" onClick={close}>
            {t('common.close')}
          </Button>
        </div>
      </div>
    );

  return (
    <Modal
      open={open}
      onClose={close}
      title={t('conn.discoverTitle')}
      description={t('conn.discoverDesc')}
      size="lg"
      className="discover-modal"
      footer={footer}
    >
      <ol className="onboard-steps" aria-label={t('conn.discoverStepAria')}>
        {steps.map((label, index) => {
          const num = index + 1;
          const state =
            num < activeStep ? 'done' : num === activeStep ? 'current' : 'todo';
          return (
            <li
              key={label}
              className={`onboard-step is-${state}`}
              aria-current={state === 'current' ? 'step' : undefined}
            >
              <span className="onboard-step-num">
                {state === 'done' ? <IconCheck size={11} /> : num}
              </span>
              <span className="onboard-step-label">{label}</span>
            </li>
          );
        })}
      </ol>

      {/* ---------------- 第一步：扫描 ---------------- */}
      {phase === 'scan' ? (
        <>
          <div className="discover-scanbar">
            <Input
              value={subnet}
              onChange={(e) => setSubnet(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !scanning) void runScan(subnet);
              }}
              placeholder={t('conn.discoverScanPlaceholder')}
              className="mono"
              aria-label={t('conn.discoverSubnet')}
              hint={t('conn.discoverSubnetHint')}
            />
            <Button
              variant="secondary"
              icon={<IconSearch size={14} />}
              loading={scanning}
              onClick={() => void runScan(subnet)}
            >
              {t('conn.discoverScan')}
            </Button>
          </div>

          {scanning ? (
            <div className="discover-progress" role="status" aria-live="polite">
              <span className="discover-radar" aria-hidden="true" />
              <div>
                <p className="discover-progress-title">
                  {t('conn.discoverScanning', {
                    cidr: subnet.trim() || '…',
                  })}
                </p>
                <p className="onboard-hint">{t('conn.discoverScanningHint')}</p>
              </div>
            </div>
          ) : null}

          {scanError ? (
            <Notice tone="danger" title={t('conn.discoverScanFailed')}>
              {scanError}
            </Notice>
          ) : null}

          {result && !scanning ? (
            <>
              <div className="discover-summary">
                <span>
                  {t('conn.discoverScanned', {
                    n: result.scanned,
                    ms: result.elapsed_ms,
                  })}
                </span>
                <span className="mono">{result.cidrs.join(' · ')}</span>
              </div>

              {result.truncated ? (
                <Notice tone="warning" title={t('conn.discoverTruncatedTitle')}>
                  {t('conn.discoverTruncated', { n: result.scanned })}
                </Notice>
              ) : null}

              {result.found.length === 0 ? (
                <div className="onboard-empty-state">
                  <IconServer size={22} />
                  <p className="discover-empty-title">
                    {t('conn.discoverEmptyTitle')}
                  </p>
                  <p>{t('conn.discoverEmptyBody')}</p>
                </div>
              ) : (
                <div className="discover-list">
                  {result.found.map((node) => (
                    <button
                      key={`${node.host}:${node.port}`}
                      type="button"
                      className={`discover-node${
                        node.already_added ? ' is-added' : ''
                      }`}
                      disabled={node.already_added}
                      onClick={() => pick(node)}
                    >
                      <span className="discover-node-icon" aria-hidden="true">
                        <IconServer size={16} />
                      </span>
                      <span className="discover-node-main">
                        <span className="discover-node-addr mono">
                          {node.host}:{node.port}
                        </span>
                        <span className="discover-node-meta">
                          {node.version
                            ? `PVE ${node.version}`
                            : t('conn.discoverUnverified')}
                          <span className="discover-dot" aria-hidden="true" />
                          {node.latency_ms} ms
                        </span>
                      </span>
                      {node.already_added ? (
                        <Badge variant="neutral" size="sm">
                          {t('conn.discoverAdded')}
                        </Badge>
                      ) : (
                        <IconChevronRight size={14} />
                      )}
                    </button>
                  ))}
                </div>
              )}
            </>
          ) : null}
        </>
      ) : null}

      {/* ---------------- 第二步：认证 ---------------- */}
      {phase === 'auth' && picked ? (
        <>
          <section className="onboard-card">
            <header className="onboard-card-head">
              <span className="onboard-card-icon">
                <IconServer size={16} />
              </span>
              <div className="onboard-card-text">
                <h3 className="onboard-card-title mono">
                  {picked.host}:{picked.port}
                </h3>
                <p className="onboard-card-desc">
                  {picked.version
                    ? `PVE ${picked.version}${picked.release ? ` · ${picked.release}` : ''}`
                    : t('conn.discoverUnverified')}
                </p>
              </div>
              <span className="onboard-summary">{picked.latency_ms} ms</span>
            </header>

            {provisionError ? (
              <Notice tone="danger" title={t('conn.discoverFailed')}>
                {provisionError}
              </Notice>
            ) : null}

            <div className="onboard-grid">
              <Field
                label={t('conn.discoverUser')}
                hint={t('conn.discoverUserHint')}
                error={formError && !form.username.trim() ? formError : undefined}
              >
                <Input
                  value={form.username}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, username: e.target.value }))
                  }
                  placeholder="root@pam"
                  className="mono"
                  autoComplete="off"
                  disabled={provisioning}
                />
              </Field>
              <Field
                label={t('conn.discoverPassword')}
                required
                hint={t('conn.discoverPasswordHint')}
                error={formError && form.username.trim() ? formError : undefined}
              >
                <Input
                  type="password"
                  value={form.password}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, password: e.target.value }))
                  }
                  placeholder="••••••••"
                  autoComplete="new-password"
                  disabled={provisioning}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !provisioning) void submit();
                  }}
                />
              </Field>
              <Field label={t('conn.fieldName')} hint={t('conn.discoverNameHint')}>
                <Input
                  value={form.name}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, name: e.target.value }))
                  }
                  placeholder={t('conn.namePlaceholder')}
                  autoComplete="off"
                  disabled={provisioning}
                />
              </Field>
              <Field label={t('conn.fieldRegion')} hint={t('conn.fieldRegionHint')}>
                <Input
                  value={form.region}
                  onChange={(e) =>
                    setForm((prev) => ({ ...prev, region: e.target.value }))
                  }
                  placeholder={t('conn.regionPlaceholder')}
                  autoComplete="off"
                  disabled={provisioning}
                />
              </Field>
            </div>

            <div className="discover-checks">
              <Switch
                checked={form.storePassword}
                onChange={(v) => setForm((prev) => ({ ...prev, storePassword: v }))}
                label={t('conn.discoverStorePassword')}
                hint={t('conn.discoverStorePasswordHint')}
                disabled={provisioning}
              />
              <Switch
                checked={form.verifySsl}
                onChange={(v) => setForm((prev) => ({ ...prev, verifySsl: v }))}
                label={t('conn.discoverVerifySsl')}
                hint={t('conn.discoverVerifySslHint')}
                disabled={provisioning}
              />
            </div>

            <p className="discover-assure">
              <IconShield size={14} />
              {t('conn.discoverAssure')}
            </p>
          </section>

          {provisioning ? (
            <div className="discover-progress" role="status" aria-live="polite">
              <span className="discover-radar" aria-hidden="true" />
              <div>
                <p className="discover-progress-title">
                  {t('conn.discoverWorking')}
                </p>
                <p className="onboard-hint">{t('conn.discoverWorkingHint')}</p>
              </div>
            </div>
          ) : null}
        </>
      ) : null}

      {/* ---------------- 第三步：完成 ---------------- */}
      {phase === 'done' && done ? (
        <>
          <section className="onboard-result" aria-live="polite">
            <span className="onboard-result-icon">
              <IconCheck size={18} />
            </span>
            <div className="onboard-result-text">
              <h3 className="onboard-result-title">{t('conn.discoverDoneTitle')}</h3>
              <p className="onboard-result-body">
                {t('conn.discoverDoneBody', {
                  name: done.connection.name,
                  host: done.connection.host,
                  port: done.connection.port,
                })}
              </p>
            </div>
          </section>

          <div className="discover-facts">
            <div className="discover-fact">
              <span className="discover-fact-key">{t('conn.discoverFactNode')}</span>
              <span className="discover-fact-val mono">
                {done.nodes.length > 0 ? done.nodes.join(', ') : done.node || '—'}
              </span>
            </div>
            <div className="discover-fact">
              <span className="discover-fact-key">{t('conn.discoverFactVersion')}</span>
              <span className="discover-fact-val mono">
                {done.version ? `PVE ${done.version}` : '—'}
              </span>
            </div>
            <div className="discover-fact">
              <span className="discover-fact-key">{t('conn.discoverFactAccount')}</span>
              <span className="discover-fact-val mono">{done.pve_user}</span>
            </div>
            <div className="discover-fact">
              <span className="discover-fact-key">{t('conn.discoverFactToken')}</span>
              <span className="discover-fact-val mono">{done.token_id}</span>
            </div>
          </div>

          <p className="discover-assure">
            <IconShield size={14} />
            {t('conn.discoverPermissionNote')}
          </p>
        </>
      ) : null}
    </Modal>
  );
}
