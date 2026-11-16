/* ==========================================================================
   pvecenter — AI 虚拟机授权策略（管理员）

   这张卡回答的不是「谁能用 AI」，而是「AI 被允许改到哪一步」：
   · 单张票最长能开多久（到点自动失效，避免一条授权挂一整天）；
   · 高危动作要不要两个人分别点头；
   · 哪些标签的机器根本不允许被写（prod 打头的那几台）；
   · 授权前要不要自动打快照、快照留多久（回滚的兜底）。

   写策略的按钮放在卡片里而不是跟着「保存模型配置」走：这两件事的失败面完全
   不同 —— 模型配错了只是连不上，策略存错了可能让生产机器被写。分开保存，
   用户每次都知道自己刚刚动的是哪一类开关。
   ========================================================================== */

import { useCallback, useEffect, useState } from 'react';
import { errorMessage } from '../api/client';
import { aiApi, type VmAiPolicy } from '../api/endpoints';
import { useToast } from '../hooks/useToast';
import { useT } from '../i18n';
import { Button } from './ui/Button';
import { Card, CardHeader } from './ui/Card';
import { Field, Input, Switch } from './ui/Input';

export function VmAiPolicyCard() {
  const t = useT();
  const toast = useToast();
  const [policy, setPolicy] = useState<VmAiPolicy | null>(null);
  /** 标签用逗号串编辑：policy 里是数组，输入框里是文本，中间转换只做一次 */
  const [tags, setTags] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await aiApi.vmPolicy();
      setPolicy(data.policy);
      setTags((data.policy.deny_write_tags ?? []).join(', '));
      setError('');
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = (next: Partial<VmAiPolicy>) => {
    setPolicy((prev) => (prev ? { ...prev, ...next } : prev));
  };

  const save = async () => {
    if (!policy) return;
    setBusy(true);
    try {
      const data = await aiApi.saveVmPolicy({
        max_minutes: policy.max_minutes,
        double_approve: policy.double_approve,
        deny_write_tags: tags
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter(Boolean),
        auto_snapshot: policy.auto_snapshot,
        keep_snapshot_hours: policy.keep_snapshot_hours,
        allow_ops: policy.allow_ops,
      });
      setPolicy(data.policy);
      setTags((data.policy.deny_write_tags ?? []).join(', '));
      toast.success(t('ai.vmPolicy.saved'));
    } catch (err) {
      toast.error(t('ai.vmPolicy.saveFailed'), errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader
        title={t('ai.vmPolicy.title')}
        subtitle={t('ai.vmPolicy.hint')}
        actions={
          <Button
            size="sm"
            variant="secondary"
            loading={busy}
            disabled={loading || !policy}
            onClick={() => void save()}
          >
            {t('ai.vmPolicy.save')}
          </Button>
        }
      />

      {error ? (
        <div className="login-error" role="alert">
          {error}
        </div>
      ) : null}

      {loading && !policy ? (
        <span className="fs-xs text-muted">{t('ai.vmPolicy.loading')}</span>
      ) : null}

      {policy ? (
        <>
          <div className="form-grid-2">
            <Field label={t('ai.vmPolicy.maxMinutes')} hint={t('ai.vmPolicy.maxMinutesHint')}>
              <Input
                type="number"
                min={5}
                max={480}
                value={policy.max_minutes}
                onChange={(e) =>
                  patch({ max_minutes: Number(e.target.value) || policy.max_minutes })
                }
              />
            </Field>
            <Field label={t('ai.vmPolicy.keepHours')} hint={t('ai.vmPolicy.keepHoursHint')}>
              <Input
                type="number"
                min={1}
                max={720}
                value={policy.keep_snapshot_hours}
                onChange={(e) =>
                  patch({
                    keep_snapshot_hours:
                      Number(e.target.value) || policy.keep_snapshot_hours,
                  })
                }
              />
            </Field>
          </div>

          <Field label={t('ai.vmPolicy.denyTags')} hint={t('ai.vmPolicy.denyTagsHint')}>
            <Input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="prod, production"
              mono
            />
          </Field>

          <div className="flex flex-col gap-8">
            <Switch
              checked={policy.double_approve}
              onChange={(v) => patch({ double_approve: v })}
              label={t('ai.vmPolicy.doubleApprove')}
              hint={t('ai.vmPolicy.doubleApproveHint')}
            />
            <Switch
              checked={policy.auto_snapshot}
              onChange={(v) => patch({ auto_snapshot: v })}
              label={t('ai.vmPolicy.autoSnapshot')}
              hint={t('ai.vmPolicy.autoSnapshotHint')}
            />
            {/* 最宽的一档：允许签发「运维授权」整机 shell。关掉后管理员也签不出来，
                已有票据不受影响（级别在签发时已固化）。 */}
            <Switch
              checked={policy.allow_ops}
              onChange={(v) => patch({ allow_ops: v })}
              label={t('ai.vmPolicy.allowOps')}
              hint={t('ai.vmPolicy.allowOpsHint')}
            />
          </div>
        </>
      ) : null}
    </Card>
  );
}
