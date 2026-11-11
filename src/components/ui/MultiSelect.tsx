/* ==========================================================================
   pvecenter — MultiSelect（下拉多选）
   --------------------------------------------------------------------------
   受控的多选下拉：触发器显示已选摘要，展开后是一个带「全选」的勾选列表。
   用于「一条计划备份哪些机器」这类需要一次勾多台的场景，省得在输入框里
   手打一长串逗号分隔的 ID —— 手抄少一位就是一个备份不到的对象，而且用户
   自己也看不出来。

   列表**内联展开**而不是绝对定位的浮层：这个控件会出现在 Modal 里，而
   .modal-body 是 overflow-y:auto —— 浮层会被裁掉一半。内联展开顶多把弹窗
   撑高，交给 Modal 自己滚动，稳得多（与 UserSelect 同一处理）。
   ========================================================================== */

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { Field } from './Input';
import { useT } from '../../i18n';

export interface MultiSelectOption {
  label: string;
  value: string;
  /** 次级说明（例如所在节点），跟在标签右侧淡显 */
  hint?: string;
  disabled?: boolean;
}

export interface MultiSelectProps {
  label?: ReactNode;
  error?: string;
  hint?: string;
  required?: boolean;
  /** 已选中的值（受控） */
  value: string[];
  onChange: (value: string[]) => void;
  options: MultiSelectOption[];
  placeholder?: string;
  /** 「全选」行的文案；不传则不显示全选 */
  selectAllLabel?: string;
  emptyText?: string;
  disabled?: boolean;
  /** 摘要里最多直接列出几个名字，超出只显示「已选 N 项」 */
  maxSummary?: number;
  id?: string;
  className?: string;
}

export function MultiSelect({
  label,
  error,
  hint,
  required,
  value,
  onChange,
  options,
  placeholder,
  selectAllLabel,
  emptyText,
  disabled = false,
  maxSummary = 2,
  id,
  className,
}: MultiSelectProps) {
  const t = useT();
  const autoId = useId();
  const btnId = id ?? autoId;
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  /* 点到组件外面就收起。内联展开不用管滚动 —— 菜单跟着内容走，位置不会歪；
     反倒不能把「在菜单里滚动选项」误判成页面滚动而把菜单收起来。 */
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  /* 变灰禁用时别继续挂着一个能点的浮层 */
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const selected = useMemo(() => new Set(value), [value]);
  const selectable = useMemo(
    () => options.filter((o) => !o.disabled),
    [options],
  );
  const allSelected =
    selectable.length > 0 && selectable.every((o) => selected.has(o.value));
  const someSelected = selectable.some((o) => selected.has(o.value));

  const toggleOne = (optionValue: string) => {
    if (selected.has(optionValue)) {
      onChange(value.filter((v) => v !== optionValue));
    } else {
      /* 追加到末尾：保留用户勾选的先后顺序，提交出去的 vmid 串更可预期 */
      onChange([...value, optionValue]);
    }
  };

  const toggleAll = () => {
    if (allSelected) {
      /* 取消全选只清掉「当前列表里可选的」，不动列表外的既有值 */
      const drop = new Set(selectable.map((o) => o.value));
      onChange(value.filter((v) => !drop.has(v)));
    } else {
      const next = new Set(value);
      for (const o of selectable) next.add(o.value);
      onChange([...next]);
    }
  };

  const summary = useMemo(() => {
    if (!value.length) return '';
    const labels = options
      .filter((o) => selected.has(o.value))
      .map((o) => o.label);
    /* 选中的项可能来自旧数据、已不在当前列表里 —— 仍算进总数，别少报 */
    const count = Math.max(labels.length, value.length);
    if (labels.length > 0 && labels.length === count && count <= maxSummary) {
      return labels.join(t('common.listSeparator'));
    }
    return t('common.selectedCount', { count });
  }, [value, options, selected, maxSummary, t]);

  return (
    <Field
      label={label}
      error={error}
      hint={hint}
      required={required}
      htmlFor={btnId}
      className={className}
    >
      <div
        ref={rootRef}
        className={`multiselect ${error ? 'is-invalid' : ''} ${
          disabled ? 'is-disabled' : ''
        }`}
      >
        <button
          type="button"
          id={btnId}
          className="multiselect-trigger"
          onClick={() => !disabled && setOpen((prev) => !prev)}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-invalid={error ? true : undefined}
          disabled={disabled}
        >
          <span className={`multiselect-value ${value.length ? '' : 'is-empty'}`}>
            {summary || placeholder || t('common.selectPlaceholder')}
          </span>
          <span className="select-arrow" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="14" height="14">
              <path
                d="m6 9 6 6 6-6"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
        </button>

        {open ? (
          <div
            className="multiselect-menu"
            role="listbox"
            aria-multiselectable="true"
          >
            {selectAllLabel && selectable.length > 0 ? (
              <label className="multiselect-item multiselect-all">
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = !allSelected && someSelected;
                  }}
                  onChange={toggleAll}
                />
                <span className="multiselect-item-label">{selectAllLabel}</span>
                <span className="multiselect-item-hint">{selectable.length}</span>
              </label>
            ) : null}

            {options.length === 0 ? (
              <div className="multiselect-empty">
                {emptyText || t('common.noOptions')}
              </div>
            ) : (
              options.map((o) => (
                <label
                  key={o.value}
                  className={`multiselect-item ${
                    o.disabled ? 'is-disabled' : ''
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={selected.has(o.value)}
                    disabled={o.disabled}
                    onChange={() => toggleOne(o.value)}
                  />
                  <span className="multiselect-item-label">{o.label}</span>
                  {o.hint ? (
                    <span className="multiselect-item-hint">{o.hint}</span>
                  ) : null}
                </label>
              ))
            )}
          </div>
        ) : null}
      </div>
    </Field>
  );
}
