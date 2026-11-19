/* ==========================================================================
   pvecenter — 证书工作流节点视觉元数据

   只放「类型 → 图标 / 色调 / 摘要」这类纯展示映射。参数表单完全由后端的
   `/cert-workflows/node-types` 描述驱动（见 CertWorkflowEditor），所以新增
   节点类型时这里只需要补一个图标；即使忘了补，也会回退到通用图标。
   ========================================================================== */

import type { ReactNode } from 'react';
import {
  IconActivity,
  IconBell,
  IconBox,
  IconCertificate,
  IconUpload,
} from '../Icons';
import type { MessageKey, TFunc } from '../../i18n';
import type { CertWorkflowNode, CertWorkflowNodeSpec } from '../../api/types';

interface Visual {
  tone: 'teal' | 'blue' | 'violet' | 'amber';
  icon: (size: number) => ReactNode;
}

const VISUALS: Record<string, Visual> = {
  domain_monitor: { tone: 'teal', icon: (size) => <IconActivity size={size} /> },
  cert_apply: { tone: 'blue', icon: (size) => <IconCertificate size={size} /> },
  deploy_upload: { tone: 'violet', icon: (size) => <IconUpload size={size} /> },
  notification: { tone: 'amber', icon: (size) => <IconBell size={size} /> },
};

const FALLBACK: Visual = { tone: 'blue', icon: (size) => <IconBox size={size} /> };

export function nodeVisual(type: string): Visual {
  return VISUALS[type] ?? FALLBACK;
}

/** 节点显示名：优先用户填的 title，其次类型的本地化名称。 */
export function nodeLabel(
  node: Pick<CertWorkflowNode, 'type' | 'title'>,
  t: TFunc,
  specs: Map<string, CertWorkflowNodeSpec>,
): string {
  if (node.title) return node.title;
  const spec = specs.get(node.type);
  return spec ? t(spec.label_key as MessageKey) : node.type;
}

/** 节点卡片上的一行参数摘要，帮助在不点开的情况下认出这个节点。 */
export function nodeSummary(
  node: CertWorkflowNode,
  t: TFunc,
  specs: Map<string, CertWorkflowNodeSpec>,
): string {
  const options = node.options || {};
  if (node.type === 'domain_monitor') {
    const domain = String(options.domain || '');
    const port = String(options.port ?? '');
    if (!domain) return t('workflow.form.optionsEmpty');
    return port && port !== '443' ? `${domain}:${port}` : domain;
  }
  if (node.type === 'cert_apply') {
    const domains = Array.isArray(options.domains)
      ? options.domains.map(String)
      : String(options.domains || '')
          .split(/[,，\n]/)
          .map((item) => item.trim())
          .filter(Boolean);
    const provider = String(options.provider || '');
    const providerLabel = provider
      ? t(`workflow.option.provider.${provider}` as MessageKey)
      : '';
    return [domains.join(', '), providerLabel].filter(Boolean).join(' · ');
  }
  if (node.type === 'deploy_upload') {
    const target = String(options.target_type || '');
    const targetLabel = target
      ? t(`workflow.option.target.${target}` as MessageKey)
      : '';
    const place = String(options.path || options.host || '');
    return [targetLabel, place].filter(Boolean).join(' · ');
  }
  if (node.type === 'notification') {
    const channels = Array.isArray(options.channels)
      ? options.channels.map(String)
      : [];
    const labels = channels.map((value) =>
      t(`workflow.option.channel.${value}` as MessageKey),
    );
    return labels.join(' · ');
  }
  const spec = specs.get(node.type);
  return spec ? t(spec.desc_key as MessageKey) : '';
}

/** 来源徽章文案。 */
export function originLabel(origin: string, t: TFunc): string {
  const known: Record<string, MessageKey> = {
    template: 'workflow.origin.template',
    blank: 'workflow.origin.blank',
    import: 'workflow.origin.import',
    editor: 'workflow.origin.editor',
  };
  return known[origin] ? t(known[origin]) : origin;
}
