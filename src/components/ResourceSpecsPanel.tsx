/* ==========================================================================
   pvecenter — 资源规格（套餐）管理
   ==========================================================================

   管理员在这里定义「几核 / 几 G 内存 / 多大盘」，用户在下单页直接挑一个 ——
   不必再自己算资源，也不会出现「建到一半发现内存给多了」。

   几个刻意的取舍：

   * **内存用 GB 填**（后端与 PVE 用 MB）：日常说「2G、4G」，让人换算成 2048 会
     填错；允许小数（0.5 = 512 MB），容器的小规格也表达得了。
   * **整份覆盖保存**（与 IP 池同一契约）：本地随便改，点保存才落库；不合法的行
     由后端逐条丢弃，返回值才是真正生效的那份，保存后以返回值回显。
   * 规格**只是数字**，不参与配额判定（配额是「能下发几台」，见创建默认值那一节）。
   * **内置一组常用规格**（入门 / 通用 / 进阶 / 高配共 12 档，见后端 specs.py），
     「添加常用规格」把它们并进草稿：只并进草稿、点保存才落库，而且按
     核 / 内存 / 磁盘三元组去重，不会把手上的配置冲掉。
   ========================================================================== */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { configApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import { Card, CardHeader } from './ui/Card';
import { Button, IconButton } from './ui/Button';
import { useT, type TFunc } from '../i18n';
import { Field, Input, Select } from './ui/Input';
import { Badge } from './ui/Badge';
import { Notice } from './ui/EmptyState';
import { ScrollArea } from './ui/ScrollArea';
import { IconPlus, IconRefresh, IconSparkle, IconTrash } from './Icons';
import { useToast } from '../hooks/useToast';
import { useAuth } from '../hooks/useAuth';
import type { ResourceSpec } from '../api/types';

/** 编辑用的行：内存按 GB 存（人填的单位），提交时换算成 MB */
interface SpecDraft {
  id: string;
  name: string;
  kind: 'vm' | 'lxc' | 'both';
  cores: string;
  memoryGb: string;
  disk: string;
  description: string;
}

/* 下面两组含中文，因此做成接收 t 的工厂（模块级常量会让文案停在首次加载时的语言） */
function kindLabels(t: TFunc): Record<SpecDraft['kind'], string> {
  return {
    both: t('spec.kindBoth'),
    vm: t('spec.kindVm'),
    lxc: t('spec.kindLxc'),
  };
}

function toDraft(spec: ResourceSpec): SpecDraft {
  /* MB → GB：512 这种整数 MB 用 0.5 表示，别显示成 0.5000001 */
  const gb = spec.memory / 1024;
  return {
    id: spec.id,
    name: spec.name,
    kind: spec.kind ?? 'both',
    cores: String(spec.cores),
    memoryGb: String(Number(gb.toFixed(2))),
    disk: String(spec.disk),
    description: spec.description ?? '',
  };
}

/** 草稿 → 提交体。空/非数字的字段交给后端回落默认值，这里只做单位换算。 */
function toPayload(draft: SpecDraft): ResourceSpec {
  const gb = Number(draft.memoryGb);
  return {
    id: draft.id,
    name: draft.name.trim(),
    kind: draft.kind,
    cores: Number(draft.cores) || 0,
    memory: Math.round((Number.isFinite(gb) ? gb : 0) * 1024),
    disk: Number(draft.disk) || 0,
    description: draft.description.trim(),
  };
}

function kindOptions(
  t: TFunc,
): Array<{ value: SpecDraft['kind']; label: string }> {
  return [
    { value: 'both', label: t('spec.kindOptBoth') },
    { value: 'vm', label: t('spec.kindVm') },
    { value: 'lxc', label: t('spec.kindLxc') },
  ];
}

export function ResourceSpecsPanel() {
  const t = useT();
  const toast = useToast();
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();
  const canManage = hasPermission('settings.manage');

  const [drafts, setDrafts] = useState<SpecDraft[]>([]);
  const loadedRef = useRef(false);

  const query = useQuery({
    queryKey: ['config', 'specs'],
    queryFn: configApi.getSpecs,
    staleTime: 30_000,
    retry: false,
  });

  useEffect(() => {
    /* 只在首次拿到数据时灌进表单：之后用户改到一半被轮询覆盖会很难受 */
    if (!query.data || loadedRef.current) return;
    loadedRef.current = true;
    setDrafts(query.data.specs.map(toDraft));
  }, [query.data]);

  const saved = useMemo(
    () => (query.data?.specs ?? []).map(toDraft),
    [query.data],
  );

  const dirty = useMemo(
    () => JSON.stringify(drafts) !== JSON.stringify(saved),
    [drafts, saved],
  );

  const save = useMutation({
    mutationFn: (items: ResourceSpec[]) => configApi.saveSpecs(items),
    onSuccess: (result) => {
      /* 以服务端返回为准：不合法的行会被丢掉，直接回显这份才不会有落差 */
      setDrafts(result.specs.map(toDraft));
      queryClient.setQueryData(['config', 'specs'], result);
      queryClient.invalidateQueries({ queryKey: ['config', 'specs'] });
      toast.success(
        t('spec.saved'),
        t('spec.savedDetail', { n: result.specs.length }),
      );
    },
    onError: (err) => toast.error(t('spec.saveFailed'), errorMessage(err)),
  });

  const update = (index: number, patch: Partial<SpecDraft>) =>
    setDrafts((list) =>
      list.map((item, i) => (i === index ? { ...item, ...patch } : item)),
    );

  const addRow = () =>
    setDrafts((list) => [
      ...list,
      {
        id: '',
        name: '',
        kind: 'both',
        cores: '2',
        memoryGb: '4',
        disk: '100',
        description: '',
      },
    ]);

  const removeRow = (index: number) =>
    setDrafts((list) => list.filter((_, i) => i !== index));

  /* 判重键：核 / 内存 / 磁盘三元组。按三元组而不是按名字判 —— 管理员可能把
     4C8G 改名成「生产 A」，那仍是同一个资源组合，再加一遍没有意义。 */
  const comboKey = (draft: Pick<SpecDraft, 'cores' | 'memoryGb' | 'disk'>) =>
    `${Number(draft.cores) || 0}|${Number(draft.memoryGb) || 0}|${Number(draft.disk) || 0}`;

  /* 「添加常用规格」：把后端内置的那组并进草稿（只进草稿，点保存才落库）。
     为什么要这个入口：老部署早把规格存进 KV 了，内置表就只在「从未保存过」时
     才返回，新增的常用档位永远到不了这些实例 —— 而一条条手填十几行既慢又容易填错。 */
  const addPresets = useMutation({
    mutationFn: () => configApi.getSpecPresets(),
    onSuccess: (result) => {
      const existing = new Set(drafts.map(comboKey));
      const add = result.specs.map(toDraft).filter((d) => !existing.has(comboKey(d)));
      if (add.length === 0) {
        toast.info(t('spec.presetsNone'));
        return;
      }
      setDrafts((list) => [...list, ...add]);
      toast.success(
        t('spec.presetsAdded', { n: add.length }),
        t('spec.presetsAddedHint'),
      );
    },
    onError: (err) => toast.error(t('spec.presetsFailed'), errorMessage(err)),
  });

  return (
    <Card>
      <CardHeader
        title={t('spec.title')}
        subtitle={t('spec.subtitle')}
        actions={
          canManage ? (
            <div className="flex items-center gap-8">
              <Button
                variant="secondary"
                icon={<IconRefresh size={14} />}
                disabled={!dirty || save.isPending}
                onClick={() => setDrafts(saved)}
              >
                {t('spec.discard')}
              </Button>
              <Button
                variant="primary"
                loading={save.isPending}
                disabled={!dirty || drafts.length === 0}
                onClick={() => save.mutate(drafts.map(toPayload))}
              >
                {t('common.save')}
              </Button>
            </div>
          ) : undefined
        }
      />

      {canManage ? (
        /* 一句话说清磁盘语义：它不只是个数字，还决定一个规格能不能用于某个模板。
           真正的拦截在选择阶段（见 QuickDeployForm），这里只交代规则。 */
        <p className="fs-xs text-muted" style={{ margin: '0 0 8px' }}>
          {t('spec.diskMatchHint')}
        </p>
      ) : null}

      {query.isError ? (
        <Notice tone="warning" title={t('spec.loadFailed')}>
          {errorMessage(query.error)}
        </Notice>
      ) : null}

      {!canManage ? (
        <Notice tone="info" title={t('spec.readonly')}>
          {t('spec.readonlyBody')}
        </Notice>
      ) : null}

      {drafts.length === 0 && !query.isLoading ? (
        <Notice tone="info" title={t('spec.empty')}>
          {t('spec.emptyBody')}
        </Notice>
      ) : null}

      {/* 列表自带滚动：条数多时只在这里滚，卡片底部的「添加规格」始终在视野里。
          滚动条是鼠标移进来才淡入的那根（见 ui/ScrollArea）。 */}
      <ScrollArea className="spec-edit-list" label={t('spec.title')}>
        {drafts.map((draft, index) => (
          <div className="spec-row" key={draft.id || `new-${index}`}>
            <div className="spec-row-head">
              <Badge variant="accent" size="sm">
                {Number(draft.cores) || 0}C · {Number(draft.memoryGb) || 0}G ·{' '}
                {Number(draft.disk) || 0}G
              </Badge>
              <span className="fs-xs text-muted">
                {kindLabels(t)[draft.kind]}
              </span>
              <span className="spec-row-spacer" />
              {canManage ? (
                <IconButton
                  label={t('spec.deleteAria', {
                    name: draft.name || index + 1,
                  })}
                  variant="danger"
                  onClick={() => removeRow(index)}
                >
                  <IconTrash size={15} />
                </IconButton>
              ) : null}
            </div>

            <div className="spec-row-grid">
              <Field label={t('spec.fieldName')}>
                <Input
                  value={draft.name}
                  disabled={!canManage}
                  placeholder={t('spec.namePlaceholder')}
                  onChange={(e) => update(index, { name: e.target.value })}
                />
              </Field>
              <Field label={t('spec.fieldKind')}>
                <Select
                  value={draft.kind}
                  disabled={!canManage}
                  options={kindOptions(t)}
                  onChange={(e) =>
                    update(index, { kind: e.target.value as SpecDraft['kind'] })
                  }
                />
              </Field>
              <Field label={t('spec.fieldCores')}>
                <Input
                  value={draft.cores}
                  disabled={!canManage}
                  inputMode="numeric"
                  onChange={(e) =>
                    update(index, { cores: e.target.value.replace(/[^\d]/g, '') })
                  }
                />
              </Field>
              <Field label={t('spec.fieldMemory')}>
                <Input
                  value={draft.memoryGb}
                  disabled={!canManage}
                  inputMode="decimal"
                  onChange={(e) =>
                    update(index, {
                      memoryGb: e.target.value.replace(/[^\d.]/g, ''),
                    })
                  }
                />
              </Field>
              <Field label={t('spec.fieldDisk')}>
                <Input
                  value={draft.disk}
                  disabled={!canManage}
                  inputMode="numeric"
                  onChange={(e) =>
                    update(index, { disk: e.target.value.replace(/[^\d]/g, '') })
                  }
                />
              </Field>
              <Field label={t('spec.fieldDesc')}>
                <Input
                  value={draft.description}
                  disabled={!canManage}
                  placeholder={t('spec.descPlaceholder')}
                  onChange={(e) => update(index, { description: e.target.value })}
                />
              </Field>
            </div>
          </div>
        ))}
      </ScrollArea>

      {canManage ? (
        <div className="mt-12 flex items-center gap-8 flex-wrap">
          <Button variant="secondary" icon={<IconPlus size={14} />} onClick={addRow}>
            {t('spec.addRow')}
          </Button>
          {/* 常用规格：入门 / 通用 / 进阶 / 高配共 12 档，只并进草稿，保存后才生效 */}
          <Button
            variant="secondary"
            icon={<IconSparkle size={14} />}
            loading={addPresets.isPending}
            onClick={() => addPresets.mutate()}
          >
            {t('spec.presetsAdd')}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
