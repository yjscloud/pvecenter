/* ==========================================================================
   pvecenter — PBS 只读接入（连接配置页）
   --------------------------------------------------------------------------
   与 PVE 连接管理（ConnectionManager）并列在同一页，**视觉与交互按同一套规范**：

   * 列表用 `.set-conn-grid` 卡片网格（名称 / 地址 / 芯片 / 底部动作条），
     以前是把整张表单平铺在页面里，几行配置就把页面顶掉一屏；
   * 新增与编辑走 Modal（与 PVE 同一个表单骨架 `dyn-row` + 选项块 + 测试结果），
     页面常态只剩「连接卡片」，信息密度回到和 PVE 一致；
   * 每个卡片自带「测试」按钮，结果同时落到卡片状态条与提示里；
   * 授权说明收进折叠块：需要时展开，不需要时不占版面。

   与 PVE 的差异只在凭据体系（PBSAPIToken）与能力探测（PBS 没有只读 system
   角色），这些差异用文案与能力徽章表达，而不是另起一套版式。
   ========================================================================== */

import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { pbsApi } from '../api/endpoints';
import type { PbsConfigItem, PbsTestResult } from '../api/types';
import { errorMessage } from '../api/client';
import { useToast } from '../hooks/useToast';
import { useT } from '../i18n';
import { Card, CardHeader } from './ui/Card';
import { Badge } from './ui/Badge';
import { Button, IconButton } from './ui/Button';
import { Field, Input, Switch, Textarea } from './ui/Input';
import { Modal } from './ui/Modal';
import { CollapsibleCard, EmptyState, Notice } from './ui/EmptyState';
import {
  IconBackup,
  IconCheck,
  IconClose,
  IconGlobe,
  IconInfo,
  IconKey,
  IconLayers,
  IconPlus,
  IconRefresh,
  IconServer,
  IconShield,
  IconTrash,
} from './Icons';

function blank(): PbsConfigItem {
  return {
    id: '',
    name: '',
    /* 区域 / 机房与 PVE 连接表单同字段同语义：纯分组标签，留空 = 未分区 */
    region: '',
    host: '',
    port: 8007,
    token_id: '',
    token_secret: '',
    /* 新建默认**不校验证书**，与 PVE 连接表单、后端默认（PVE_VERIFY_SSL=false）
       保持一致：PBS 出厂自签，开校验直接连不上，容易被误判成地址填错。
       需要安全性就填下面的「CA 证书 / 证书 PEM」做钉扎 —— 填了它只认那张证书，
       与这个开关无关。 */
    verify_ssl: false,
    ca_cert: '',
    node: '',
  };
}

/** 一条连接在页面上的「可读状态」：来自手动测试，未测过就是 unknown。 */
interface RowState {
  ok: boolean;
  message?: string;
}

export function PbsConnectionCard({
  viewMode = 'card',
}: {
  /** 与 PVE 连接卡共用一个视图偏好，避免上半页列表、下半页卡片 */
  viewMode?: 'card' | 'list';
}) {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: ['pbs', 'config'],
    queryFn: pbsApi.config,
    // 管理凭据：只有 pbs.manage 才调这个接口
    staleTime: 60_000,
    retry: false,
  });

  const rows = query.data ?? [];

  const [formOpen, setFormOpen] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<PbsConfigItem>(blank());
  /* 后端不回传 secret，只告知「是否已设置」—— 用它决定 secret 是否必填 */
  const [secretSet, setSecretSet] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [busyRow, setBusyRow] = useState('');
  /* 暂停 / 恢复正在提交的那一行：只锁它自己，别把整张卡片网格都锁住 */
  const [pausingId, setPausingId] = useState('');
  const [testResult, setTestResult] = useState<PbsTestResult | null>(null);
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  const configuredCount = rows.filter(
    (item) => item.configured ?? Boolean(item.host && item.token_id),
  ).length;

  /* ---- 表单 ---- */

  const patch = (key: keyof PbsConfigItem, value: PbsConfigItem[keyof PbsConfigItem]) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    /* 字段一改，上次的测试结论就不再对应当前表单 */
    setTestResult(null);
    setErrors((prev) => {
      if (!prev[key as string]) return prev;
      const next = { ...prev };
      delete next[key as string];
      return next;
    });
  };

  const validate = (): boolean => {
    const next: Record<string, string> = {};

    const host = form.host.trim();
    if (!host) next.host = t('pbs.errHostRequired');
    else if (!/^[a-zA-Z0-9.-]+$/.test(host) && !/^\[?[0-9a-fA-F:]+\]?$/.test(host)) {
      next.host = t('pbs.errHostFormat');
    }

    const port = Number(form.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      next.port = t('pbs.errPort');
    }

    const tokenId = form.token_id.trim();
    if (!tokenId) next.token_id = t('pbs.errTokenIdRequired');
    else if (!tokenId.includes('!')) next.token_id = t('pbs.errTokenId');

    /* 已存过 secret 时留空合法（表示沿用）；新建时必须填 */
    if (!(form.token_secret ?? '').trim() && !secretSet) {
      next.token_secret = t('pbs.errTokenSecret');
    }

    const caCert = (form.ca_cert ?? '').trim();
    if (caCert && !caCert.includes('-----BEGIN CERTIFICATE-----')) {
      next.ca_cert = t('pbs.errCaCert');
    }

    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const startNew = () => {
    setEditingId(null);
    setForm(blank());
    setSecretSet(false);
    setErrors({});
    setTestResult(null);
    setFormOpen(true);
  };

  const startEdit = (item: PbsConfigItem) => {
    setEditingId(item.id);
    // 密钥永不回填：留空 = 沿用
    setForm({
      ...blank(),
      ...item,
      token_secret: '',
      /* 老记录没有 verify_ssl 字段时不能让它回填成 undefined（否则开关显示
         成「开」、保存时又把 undefined 传下去），按新建连接同一套默认走 */
      verify_ssl: item.verify_ssl ?? false,
    });
    setSecretSet(Boolean(item.token_secret_set));
    setErrors({});
    setTestResult(null);
    setFormOpen(true);
  };

  const closeForm = () => {
    setFormOpen(false);
    setTestResult(null);
    setErrors({});
  };

  /* ---- 测试 ---- */

  /** 连上了但不一定有全部能力：把缺的那项说清楚，别让人以为「都正常」。 */
  const capabilityNote = (result: PbsTestResult): string => {
    const caps = result.capabilities;
    if (caps && !caps.datastores) return t('pbs.testNoDatastorePerm');
    /* 端点能通、返回空列表：PBS 对无权限的 token 就是这么回应的，
       所以这不是「没有仓库」，而是 ACL 没落在 /datastore 上 */
    if (result.probe_errors?.datastores === 'no_datastore_audit') {
      return t('pbs.testNoDatastoreAudit');
    }
    /* 看得到仓库 ≠ 看得到容量：容量走 /admin/datastore/{store}/status，
       这一步失败时概览里只有仓库名没有使用率，必须单独说清楚 */
    if (caps && caps.datastore_status === false) {
      return t('pbs.testNoCapacityPerm', {
        code: result.probe_errors?.datastore_status ?? 'unknown',
      });
    }
    if (caps && !caps.node_status) return t('pbs.testPartialHint');
    return result.version ? `PBS ${result.version}` : '';
  };

  /** 401 时按「凭据长什么样」给出更具体的一句。 */
  const failureHint = (result: PbsTestResult): string => {
    if (result.code === 'pbs_auth_failed') {
      const d = result.diagnostics;
      if (d?.secret_looks_like_token_id) return t('pbs.secretLooksLikeTokenId');
      if (d && !d.secret_uuid_like && !d.secret_hex_like) {
        return t('pbs.secretNotUuid');
      }
      return t('pbs.authFailedHint');
    }
    if (result.code === 'pbs_permission_denied') {
      return t('pbs.permissionDeniedHint');
    }
    return result.error || '';
  };

  /** 把一行配置发去探测。`fromModal` 决定结果落在弹窗里还是卡片上。 */
  const runTest = async (item: PbsConfigItem, fromModal: boolean) => {
    if (fromModal && !validate()) {
      toast.error(t('conn.fixFormErrors'));
      return;
    }
    const target: PbsConfigItem = {
      ...item,
      host: item.host.trim(),
      token_id: item.token_id.trim(),
      token_secret: (item.token_secret ?? '').trim(),
      ca_cert: (item.ca_cert ?? '').trim(),
    };

    if (fromModal) {
      setTesting(true);
      setTestResult(null);
    } else {
      setBusyRow(item.id);
    }

    try {
      const result = await pbsApi.test(target);
      if (fromModal) setTestResult(result);

      if (result.ok) {
        toast.success(t('pbs.testOk'), capabilityNote(result));
        if (!fromModal) {
          setRowState((prev) => ({ ...prev, [item.id]: { ok: true } }));
        }
      } else {
        const hint = failureHint(result);
        toast.error(
          result.code === 'pbs_auth_failed' ? t('pbs.authFailed') : t('pbs.testFailed'),
          hint,
        );
        if (!fromModal) {
          setRowState((prev) => ({
            ...prev,
            [item.id]: { ok: false, message: hint },
          }));
        }
      }
    } catch (err) {
      const message = errorMessage(err);
      if (fromModal) setTestResult({ ok: false, error: message });
      toast.error(t('pbs.testFailed'), message);
      if (!fromModal) {
        setRowState((prev) => ({ ...prev, [item.id]: { ok: false, message } }));
      }
    } finally {
      setTesting(false);
      setBusyRow('');
    }
  };

  /* ---- 保存 / 删除 ---- */

  /* 后端是「整份列表保存」：这里把单条改动合并进当前列表再提交。
     卡片上的编辑 / 删除因此都是立即生效的，与 PVE 连接的逐条操作手感一致。 */
  const persist = async (next: PbsConfigItem[]) => {
    const saved = await pbsApi.saveConfig(next);
    queryClient.setQueryData(['pbs', 'config'], saved);
    void queryClient.invalidateQueries({ queryKey: ['pbs'] });
    return saved;
  };

  const save = async () => {
    if (!validate()) {
      toast.error(t('conn.fixFormErrors'));
      return;
    }
    setSaving(true);
    try {
      const item: PbsConfigItem = {
        ...form,
        name: form.name.trim() || form.host.trim(),
        region: (form.region ?? '').trim(),
        host: form.host.trim(),
        port: Number(form.port),
        token_id: form.token_id.trim(),
        token_secret: (form.token_secret ?? '').trim(),
        ca_cert: (form.ca_cert ?? '').trim(),
        node: form.node.trim(),
      };
      const next = editingId
        ? rows.map((row) =>
            row.id === editingId ? { ...row, ...item, id: editingId } : row,
          )
        : [...rows, item];

      await persist(next);
      toast.success(t('pbs.configSaved'), t('pbs.configSavedHint'));
      /* 填完即走：留在弹窗里继续点「保存」只会让人怀疑存没存 */
      closeForm();
    } catch (err) {
      toast.error(t('pbs.configSaveFailed'), errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  /* ---- 暂停 / 恢复 ----
     走专用端点而不是 saveConfig：整份保存会覆盖所有连接，开关状态容易被
     别人那份旧数据带回旧值。返回的整份列表直接写回缓存，省一次往返。 */
  const togglePause = async (item: PbsConfigItem, paused: boolean) => {
    setPausingId(item.id);
    try {
      const saved = await pbsApi.pauseConnection(item.id, paused);
      queryClient.setQueryData(['pbs', 'config'], saved);
      /* 概览与状态条读的是另一份缓存：暂停会改变在线率，必须一起刷新 */
      void queryClient.invalidateQueries({ queryKey: ['pbs', 'overview'] });
      toast.success(
        paused ? t('pbs.paused') : t('pbs.resumed'),
        item.name || item.host,
      );
    } catch (err) {
      toast.error(t('pbs.pauseFailed'), errorMessage(err));
    } finally {
      setPausingId('');
    }
  };

  const remove = async (item: PbsConfigItem) => {
    try {
      await persist(rows.filter((row) => row.id !== item.id));
      setRowState((prev) => {
        const next = { ...prev };
        delete next[item.id];
        return next;
      });
      toast.success(t('pbs.deleted'), item.name || item.host);
    } catch (err) {
      toast.error(t('pbs.configSaveFailed'), errorMessage(err));
    }
  };

  /* ---- 渲染 ---- */

  const caps = testResult?.capabilities;
  const missingCaps = Boolean(caps && (!caps.datastores || !caps.node_status));

  const capabilityChip = (label: string, ok: boolean, code?: string) => (
    <span className="set-conn-tag" key={label} title={code || undefined}>
      {ok ? <IconCheck size={12} /> : <IconClose size={12} />}
      {label}
      <span className="text-muted">
        {ok ? t('pbs.capOk') : t('pbs.capMissing')}
        {!ok && code ? ` · ${code}` : ''}
      </span>
    </span>
  );

  return (
    <>
      <Card className={`conn-view-${viewMode}`}>
        <CardHeader
          title={t('pbs.configTitle')}
          subtitle={t('pbs.configSubtitle')}
          icon={<IconBackup size={17} />}
          actions={
            <>
              {/* 接入指引：常驻的小按钮，正文只放「怎么配」，不占列表版面 */}
              <Button
                variant="ghost"
                size="sm"
                className="pbs-guide-btn"
                icon={<IconInfo size={14} />}
                title={t('pbs.guideButton')}
                aria-label={t('pbs.guideButton')}
                onClick={() => setGuideOpen(true)}
              >
                {t('pbs.guideButton')}
              </Button>
              <Badge
                variant={configuredCount > 0 ? 'success' : 'warning'}
                dot
                size="sm"
              >
                {configuredCount > 0
                  ? t('pbs.configuredCount', { n: configuredCount })
                  : t('pbs.notConfigured')}
              </Badge>
              <Button
                variant="secondary"
                size="sm"
                icon={<IconPlus size={14} />}
                onClick={startNew}
              >
                {t('pbs.add')}
              </Button>
            </>
          }
        />

        {rows.length === 0 ? (
          <EmptyState
            title={t('pbs.noConnection')}
            description={t('pbs.configEmpty')}
            icon={<IconBackup size={30} />}
            action={
              <Button
                variant="primary"
                icon={<IconPlus size={15} />}
                onClick={startNew}
              >
                {t('pbs.add')}
              </Button>
            }
          />
        ) : (
          <div className="set-conn-grid">
            {rows.map((item) => {
              const configured =
                item.configured ?? Boolean(item.host && item.token_id);
              const state = rowState[item.id];
              /* 已暂停优先于「测过 / 没测过」：暂停的连接根本没发过请求，
                 画成红的「不可达」会让人以为机器坏了 */
              const tone = item.paused
                ? 'paused'
                : state
                  ? state.ok
                    ? 'ok'
                    : 'down'
                  : 'unknown';
              const busy = busyRow === item.id;
              const pausing = pausingId === item.id;
              return (
                <div key={item.id} className={`set-conn-card is-${tone}`}>
                  <div className="set-conn-top">
                    <span className="set-conn-icon" aria-hidden="true">
                      <IconBackup size={17} />
                    </span>
                    <div className="set-conn-id">
                      <span className="set-conn-name" title={item.name || item.host}>
                        {item.name || item.host || t('pbs.configNew')}
                      </span>
                      <span className="set-conn-addr mono">
                        {item.host}:{item.port}
                      </span>
                    </div>
                    {item.paused ? (
                      <Badge variant="neutral" size="sm" dot>
                        {t('pbs.paused')}
                      </Badge>
                    ) : state ? (
                      state.ok ? (
                        <Badge variant="success" size="sm" dot>
                          {t('pbs.connected')}
                        </Badge>
                      ) : (
                        <Badge
                          variant="danger"
                          size="sm"
                          dot
                          title={state.message || t('pbs.testFailed')}
                        >
                          {t('pbs.disconnected')}
                        </Badge>
                      )
                    ) : configured ? (
                      /* 没测过就是「未测试」，不能画成绿的 —— 与上方 PVE 卡的
                         「检测中…」同理：不知道的事不能显示成已知的好结果 */
                      <Badge variant="neutral" size="sm" title={t('pbs.untestedHint')}>
                        {t('pbs.untested')}
                      </Badge>
                    ) : (
                      <Badge variant="warning" size="sm">
                        {t('pbs.badgeIncomplete')}
                      </Badge>
                    )}
                  </div>

                  <div className="set-conn-tags">
                    {/* 区域和 PVE 节点树里的区域是同一个概念：填了才显示，
                        空着就是「未分区」，不额外占一枚灰芯片 */}
                    {item.region ? (
                      <span className="set-conn-tag" title={t('pbs.fieldRegion')}>
                        <IconGlobe size={12} />
                        {item.region}
                      </span>
                    ) : null}
                    {/* 顺序与上方 PVE 卡逐项对齐：区域 → 节点 → 凭据 → 密钥。
                        PBS 的「节点」是读取目标（单节点，不可展开），PVE 是节点数
                        （可展开），两者位置与图标保持一致，差异只体现在内容上。 */}
                    <span className="set-conn-tag" title={t('pbs.fieldNode')}>
                      <IconLayers size={12} />
                      {item.node || t('pbs.fieldNodeAuto')}
                    </span>
                    <span
                      className="set-conn-tag"
                      title={item.token_id || t('pbs.fieldTokenId')}
                    >
                      <IconKey size={12} />
                      {item.token_id || '—'}
                    </span>
                    {item.token_secret_set ? (
                      <span className="set-conn-tag" title={t('pbs.secretSavedHint')}>
                        <IconShield size={12} />
                        {t('pbs.secretSaved')}
                      </span>
                    ) : null}
                  </div>

                  <div className="set-conn-actions">
                    <div className="set-conn-actions-main">
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => startEdit(item)}
                      >
                        {t('common.edit')}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        icon={<IconRefresh size={14} />}
                        loading={busy}
                        onClick={() => void runTest(item, false)}
                      >
                        {t('pbs.test')}
                      </Button>
                      {/* 暂停 / 恢复：配置与凭据都留着，随时能接回来 */}
                      <Button
                        size="sm"
                        variant="ghost"
                        loading={pausing}
                        title={item.paused ? t('pbs.resumeHint') : t('pbs.pauseHint')}
                        onClick={() => void togglePause(item, !item.paused)}
                      >
                        {item.paused ? t('pbs.resume') : t('pbs.pause')}
                      </Button>
                    </div>
                    <IconButton
                      label={t('pbs.deleteAria', { name: item.name || item.host })}
                      variant="danger"
                      onClick={() => void remove(item)}
                    >
                      <IconTrash size={15} />
                    </IconButton>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* ---- 新增 / 编辑：与 PVE 连接同一个表单骨架 ---- */}
      <Modal
        open={formOpen}
        onClose={closeForm}
        title={editingId ? t('pbs.editTitle') : t('pbs.addTitle')}
        description={editingId ? t('pbs.editDesc') : t('pbs.addDesc')}
        size="lg"
        footer={
          <div className="wizard-footer">
            <span className="wizard-footer-step">
              {t('pbs.footerHint')}
            </span>
            <div className="wizard-footer-actions">
              <Button variant="secondary" onClick={closeForm}>
                {t('pbs.cancel')}
              </Button>
              <Button
                variant="secondary"
                icon={<IconRefresh size={15} />}
                loading={testing}
                onClick={() => void runTest(form, true)}
              >
                {t('pbs.testConn')}
              </Button>
              <Button
                variant="primary"
                loading={saving}
                onClick={() => void save()}
              >
                {t('pbs.save')}
              </Button>
            </div>
          </div>
        }
      >
        <div className="dyn-list">
          {/* 名称 + 区域：与 PVE 连接表单第一行完全同款（同一个 Field 骨架、
              同一个 hint 措辞），两套连接看的是同一张表单 */}
          <div className="dyn-row">
            <Field label={t('pbs.fieldName')} hint={t('pbs.fieldNameHint')}>
              <Input
                value={form.name}
                placeholder={t('pbs.namePlaceholder')}
                autoComplete="off"
                onChange={(e) => patch('name', e.target.value)}
              />
            </Field>
            <Field label={t('pbs.fieldRegion')} hint={t('pbs.fieldRegionHint')}>
              <Input
                value={form.region ?? ''}
                placeholder={t('pbs.regionPlaceholder')}
                autoComplete="off"
                onChange={(e) => patch('region', e.target.value)}
              />
            </Field>
          </div>

          <div className="dyn-row">
            <Field
              label={t('pbs.fieldHost')}
              required
              error={errors.host}
            >
              <Input
                value={form.host}
                placeholder="10.0.0.5"
                className="mono"
                autoComplete="off"
                onChange={(e) => patch('host', e.target.value)}
              />
            </Field>
            <Field label={t('pbs.fieldPort')} required error={errors.port}>
              <Input
                type="number"
                min={1}
                max={65535}
                value={String(form.port)}
                className="mono"
                onChange={(e) => patch('port', Number(e.target.value))}
              />
            </Field>
          </div>

          <div className="dyn-row">
            <Field
              label={t('pbs.fieldTokenId')}
              required
              error={errors.token_id}
              hint={t('pbs.fieldTokenIdHint')}
            >
              <Input
                value={form.token_id}
                placeholder="pvecenter@pbs!readonly"
                className="mono"
                autoComplete="off"
                onChange={(e) => patch('token_id', e.target.value)}
              />
            </Field>
            <Field
              label={t('pbs.fieldTokenSecret')}
              required={!secretSet}
              error={errors.token_secret}
              hint={
                secretSet
                  ? t('pbs.fieldTokenSecretKeep')
                  : t('pbs.fieldTokenSecretHint')
              }
            >
              <Input
                type="password"
                value={form.token_secret ?? ''}
                autoComplete="new-password"
                placeholder={secretSet ? '••••••••' : ''}
                onChange={(e) => patch('token_secret', e.target.value)}
              />
            </Field>
          </div>

          <div className="set-form-block-head">
            <span className="set-form-block-icon">
              <IconShield size={15} />
            </span>
            <span className="set-form-block-title">{t('pbs.optionsBlock')}</span>
            <span className="set-form-block-hint">{t('pbs.optionsBlockHint')}</span>
          </div>

          {/* 读取节点：只在多节点 PBS 上才需要选，跟着「连接选项」走，
              不占上面那三行必填项的视觉权重 */}
          <div className="dyn-row">
            <Field label={t('pbs.fieldNode')} hint={t('pbs.fieldNodeHint')}>
              <Input
                value={form.node}
                placeholder={t('pbs.fieldNodeAuto')}
                autoComplete="off"
                onChange={(e) => patch('node', e.target.value)}
              />
            </Field>
          </div>

          <div className="dyn-row">
            <Switch
              checked={form.verify_ssl ?? false}
              onChange={(value) => patch('verify_ssl', value)}
              label={t('pbs.fieldVerifySsl')}
              hint={t('pbs.fieldVerifySslHint')}
            />
          </div>

          <Field
            label={t('pbs.fieldCaCert')}
            hint={t('pbs.fieldCaCertHint')}
            error={errors.ca_cert}
          >
            <Textarea
              rows={3}
              value={form.ca_cert}
              className="mono"
              placeholder="-----BEGIN CERTIFICATE-----"
              onChange={(e) => patch('ca_cert', e.target.value)}
            />
          </Field>

          {/* 测试结果：成功但缺权限时用 warning，别让「连上了」看起来像「都能看」 */}
          {testResult ? (
            <Notice
              tone={testResult.ok ? (missingCaps ? 'warning' : 'success') : 'danger'}
              title={
                testResult.ok ? t('pbs.testResultTitle') : t('pbs.testFailed')
              }
              icon={
                testResult.ok ? <IconCheck size={16} /> : <IconClose size={16} />
              }
            >
              {testResult.ok ? (
                <div className="desc-list">
                  <div className="desc-item">
                    <div className="desc-label">{t('conn.version')}</div>
                    <div className="desc-value mono">
                      {testResult.version ? `PBS ${testResult.version}` : t('conn.unknown')}
                    </div>
                  </div>
                  {/* 老后端不返回 capabilities：那就只报版本，别把三项都显示成「不可读」 */}
                  {caps ? (
                    <div className="desc-item">
                      <div className="desc-label">{t('pbs.capTitle')}</div>
                      <div className="desc-value">
                        <div className="set-conn-tags">
                          {capabilityChip(t('pbs.capDatastores'), caps.datastores)}
                          {caps.datastore_status !== undefined ? (
                            capabilityChip(
                              t('pbs.capDatastoreStatus'),
                              caps.datastore_status,
                              testResult?.probe_errors?.datastore_status,
                            )
                          ) : null}
                          {capabilityChip(t('pbs.capTasks'), caps.tasks)}
                          {capabilityChip(t('pbs.capNodeStatus'), caps.node_status)}
                        </div>
                      </div>
                    </div>
                  ) : null}
                </div>
              ) : (
                failureHint(testResult)
              )}
            </Notice>
          ) : null}

          {/* 授权说明默认收起：只在需要时展开，不占常态版面 */}
          <CollapsibleCard
            title={t('pbs.permissionGuideTitle')}
            icon={<IconKey size={15} />}
          >
            <p className="fs-sm text-muted">{t('pbs.permissionGuideBody')}</p>
          </CollapsibleCard>
        </div>
      </Modal>

      {/* ---- 接入指引：分步说明 ----
          正文放在 `.modal-body` 里滚动（该容器自带 overflow-y），小屏也不会把
          页脚按钮挤出视口；页脚另给一个「知道了」的关闭入口，与右上角 X 并存。 */}
      <Modal
        open={guideOpen}
        onClose={() => setGuideOpen(false)}
        title={t('pbs.guideTitle')}
        description={t('pbs.guideDesc')}
        size="lg"
        footer={
          <Button variant="primary" onClick={() => setGuideOpen(false)}>
            {t('pbs.guideClose')}
          </Button>
        }
      >
        <div className="pbs-guide">
          <section className="pbs-guide-step">
            <div className="set-form-block-head">
              <span className="set-form-block-icon">
                <IconKey size={15} />
              </span>
              <span className="set-form-block-title">{t('pbs.guideStep1')}</span>
            </div>
            <p className="fs-sm text-muted">{t('pbs.guideStep1Body')}</p>
            <pre className="code-block">{t('pbs.guideStep1Code')}</pre>
          </section>

          <section className="pbs-guide-step">
            <div className="set-form-block-head">
              <span className="set-form-block-icon">
                <IconShield size={15} />
              </span>
              <span className="set-form-block-title">{t('pbs.guideStep2')}</span>
            </div>
            <p className="fs-sm text-muted">{t('pbs.guideStep2Body')}</p>
            <pre className="code-block">{t('pbs.guideStep2Code')}</pre>
            {/* 这条坑踩过就别再踩：/system 授 Admin 看起来“权限全有了”，
                但仓库列表照样是空的，且 PBS 不会报错 */}
            <Notice tone="warning" title={t('pbs.guideStep2WarnTitle')}>
              {t('pbs.guideStep2Warn')}
            </Notice>
          </section>

          <section className="pbs-guide-step">
            <div className="set-form-block-head">
              <span className="set-form-block-icon">
                <IconServer size={15} />
              </span>
              <span className="set-form-block-title">{t('pbs.guideStep3')}</span>
            </div>
            <div className="desc-list">
              <div className="desc-item">
                <div className="desc-label">{t('pbs.guideAddrLabel')}</div>
                <div className="desc-value">{t('pbs.guideAddrValue')}</div>
              </div>
              <div className="desc-item">
                <div className="desc-label">{t('pbs.guideTokenIdLabel')}</div>
                <div className="desc-value mono">{t('pbs.guideTokenIdValue')}</div>
              </div>
              <div className="desc-item">
                <div className="desc-label">{t('pbs.guideSecretLabel')}</div>
                <div className="desc-value">{t('pbs.guideSecretValue')}</div>
              </div>
              <div className="desc-item">
                <div className="desc-label">{t('pbs.guideNodeLabel')}</div>
                <div className="desc-value">{t('pbs.guideNodeValue')}</div>
              </div>
            </div>
          </section>

          <section className="pbs-guide-step">
            <div className="set-form-block-head">
              <span className="set-form-block-icon">
                <IconCheck size={15} />
              </span>
              <span className="set-form-block-title">{t('pbs.guideStep4')}</span>
            </div>
            <p className="fs-sm text-muted">{t('pbs.guideStep4Body')}</p>
          </section>

          <Notice tone="info" title={t('pbs.guideTroubleTitle')}>
            <ul className="desc-list" style={{ paddingLeft: 18, margin: 0 }}>
              <li className="fs-sm">{t('pbs.guideTrouble401')}</li>
              <li className="fs-sm">{t('pbs.guideTrouble403')}</li>
              <li className="fs-sm">{t('pbs.guideTroubleAcl')}</li>
              <li className="fs-sm">{t('pbs.guideTroubleNet')}</li>
            </ul>
          </Notice>
        </div>
      </Modal>
    </>
  );
}
