/* ==========================================================================
   pvecenter — 工作流节点的参数表单

   表单完全由后端节点契约（`/cert-workflows/node-types`）驱动：
   `fields[].type` 决定控件，`show_if` 决定联动显隐，`choices` 决定下拉项。
   新增节点类型时不需要改这个文件。
   ========================================================================== */

import { useState } from 'react';
import { Field, Input, Select, Switch, Textarea } from '../ui/Input';
import { useT, type MessageKey } from '../../i18n';
import type { CertWorkflowFieldSpec, CertWorkflowNodeSpec } from '../../api/types';

type OptionValue = unknown;

function isVisible(field: CertWorkflowFieldSpec, options: Record<string, OptionValue>): boolean {
  const condition = field.show_if;
  if (!condition) return true;
  const actual = options[condition.key];
  if (condition.in) return condition.in.includes(String(actual ?? ''));
  if (condition.contains) {
    if (Array.isArray(actual)) return actual.map(String).includes(condition.contains);
    if (typeof actual === 'string') {
      return actual
        .split(/[,，\n]/)
        .map((item) => item.trim())
        .includes(condition.contains);
    }
    return false;
  }
  return true;
}

/**
 * 列表输入保留用户的原始文本（换行 / 逗号 / 行尾逗号），只把解析结果回传。
 * 直接用 `array.join('\n')` 受控的话，用户敲下逗号的瞬间它就会被重排成换行，
 * 输入体验很差。
 */
function ListTextInput({
  value,
  onChange,
  label,
  hint,
  placeholder,
  required,
}: {
  value: unknown;
  onChange: (value: string[]) => void;
  label: string;
  hint?: string;
  placeholder?: string;
  required?: boolean;
}) {
  const [draft, setDraft] = useState(() =>
    Array.isArray(value) ? value.map(String).join('\n') : String(value ?? ''),
  );
  return (
    <Field label={label} hint={hint} required={required}>
      <Textarea
        rows={3}
        value={draft}
        placeholder={placeholder}
        onChange={(event) => {
          const text = event.target.value;
          setDraft(text);
          onChange(
            text
              .split(/[,，\n]/)
              .map((item) => item.trim())
              .filter(Boolean),
          );
        }}
      />
    </Field>
  );
}

function MultiSelectInput({
  field,
  value,
  onChange,
}: {
  field: CertWorkflowFieldSpec;
  value: unknown;
  onChange: (value: string[]) => void;
}) {
  const t = useT();
  const selected = Array.isArray(value) ? value.map(String) : [];
  return (
    <Field
      label={t(field.label_key as MessageKey)}
      hint={field.hint_key ? t(field.hint_key as MessageKey) : undefined}
      required={field.required}
    >
      <div className="wf-checks">
        {(field.choices ?? []).map((choice) => {
          const checked = selected.includes(choice.value);
          return (
            <label key={choice.value} className={`wf-check ${checked ? 'is-on' : ''}`}>
              <input
                type="checkbox"
                checked={checked}
                onChange={() =>
                  onChange(
                    checked
                      ? selected.filter((item) => item !== choice.value)
                      : [...selected, choice.value],
                  )
                }
              />
              <span>{t(choice.label_key as MessageKey)}</span>
            </label>
          );
        })}
      </div>
    </Field>
  );
}

export function NodeOptionsForm({
  spec,
  nodeId,
  options,
  onChange,
  disabled = false,
}: {
  spec: CertWorkflowNodeSpec;
  /** 只用于给列表输入做 key：切换节点时重置草稿，同一节点内不重挂 */
  nodeId: string;
  options: Record<string, OptionValue>;
  onChange: (key: string, value: OptionValue) => void;
  disabled?: boolean;
}) {
  const t = useT();
  return (
    <div className="wf-options-form">
      {spec.fields.length === 0 ? (
        <div className="text-secondary">{t('workflow.form.optionsEmpty')}</div>
      ) : null}
      {spec.fields.map((field) => {
        if (!isVisible(field, options)) return null;
        const label = t(field.label_key as MessageKey);
        const hint = field.hint_key ? t(field.hint_key as MessageKey) : undefined;
        const value = options[field.key];

        if (field.type === 'switch') {
          return (
            <Switch
              key={field.key}
              checked={Boolean(value)}
              disabled={disabled}
              label={label}
              hint={hint}
              onChange={(checked) => onChange(field.key, checked)}
            />
          );
        }

        if (field.type === 'select') {
          return (
            <Select
              key={field.key}
              label={label}
              hint={hint}
              required={field.required}
              value={String(value ?? '')}
              disabled={disabled}
              options={(field.choices ?? []).map((choice) => ({
                value: choice.value,
                label: t(choice.label_key as MessageKey),
              }))}
              onChange={(event) => onChange(field.key, event.target.value)}
            />
          );
        }

        if (field.type === 'multiselect') {
          return (
            <MultiSelectInput
              key={field.key}
              field={field}
              value={value}
              onChange={(next) => onChange(field.key, next)}
            />
          );
        }

        if (field.type === 'stringlist') {
          return (
            <ListTextInput
              key={`${nodeId}:${field.key}`}
              label={label}
              hint={hint}
              required={field.required}
              placeholder={field.placeholder}
              value={value}
              onChange={(next) => onChange(field.key, next)}
            />
          );
        }

        if (field.type === 'number') {
          return (
            <Input
              key={field.key}
              type="number"
              label={label}
              hint={hint}
              required={field.required}
              min={field.min}
              max={field.max}
              value={value === '' || value == null ? '' : String(value)}
              disabled={disabled}
              onChange={(event) =>
                onChange(
                  field.key,
                  event.target.value === '' ? '' : Number(event.target.value),
                )
              }
            />
          );
        }

        return (
          <Input
            key={field.key}
            label={label}
            hint={hint}
            required={field.required}
            placeholder={field.placeholder}
            value={String(value ?? '')}
            disabled={disabled}
            onChange={(event) => onChange(field.key, event.target.value)}
          />
        );
      })}
    </div>
  );
}
