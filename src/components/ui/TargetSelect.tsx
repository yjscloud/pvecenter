/* ==========================================================================
   pvecenter — TargetSelect（可搜索的「目标地址」选择器）

   为什么不用原生 <select>：一个集群几十上百台虚机 / 容器，原生下拉只能按键
   逐项跳，找一台机器得按几十下；而让人手打 IP 又回到「打错一个数字，规则指向
   了另一台机器」的老问题。所以这里是个 combobox：输入即过滤（主机名 / IP /
   VMID 三个维度，规则见 utils/targetSearch.ts），上下键选择、回车确认。

   列表**内联展开**而不是绝对定位的浮层：这个控件只在 Modal 里用，而
   .modal-body 是 overflow-y:auto —— 浮层会被裁掉一半。内联展开顶多把弹窗
   撑高，交给 Modal 自己滚动。

   「自定义 IP」放在列表末尾单独一行（有分隔线）：预设与手填是两种意图，
   混在同一个视觉层级里，用户会以为手填的地址也要先被搜出来。
   ========================================================================== */

import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { useT } from '../../i18n';
import { MAX_RESULTS, searchTargets, type SearchableTarget } from '../../utils/targetSearch';
import { Field } from './Input';

export type TargetChoice = SearchableTarget;

export interface TargetSelectProps {
  /** 候选目标（顺序由后端给出，空查询时原样展示） */
  choices: TargetChoice[];
  /** 当前选中的地址（空串 = 未选） */
  value: string;
  /** 选中一个预设目标 */
  onSelect: (choice: TargetChoice) => void;
  /**
   * 切到「手动填写」。**不传就没有这个入口**（普通用户没被放行网段时只能选
   * 列表里的地址）。参数是搜索框里已经敲进去的文字 —— 搜不到时点「手动填写」
   * 不该让人再敲一遍。
   */
  onManual?: (prefill: string) => void;
  /** 是否正处在手动填写模式（此时输入框显示 manualLabel） */
  manual?: boolean;
  manualLabel?: string;
  label?: ReactNode;
  required?: boolean;
  error?: string;
  hint?: string;
  placeholder?: string;
  disabled?: boolean;
  /** 候选清单正在读取（管理员侧是打开弹窗时按需拉取的） */
  loading?: boolean;
  id?: string;
}

export function TargetSelect({
  choices,
  value,
  onSelect,
  onManual,
  manual = false,
  manualLabel,
  label,
  required,
  error,
  hint,
  placeholder,
  disabled = false,
  loading = false,
  id,
}: TargetSelectProps) {
  const t = useT();
  const autoId = useId();
  const inputId = id ?? autoId;
  const listId = `${inputId}-list`;

  const [open, setOpen] = useState(false);
  /** null = 不在搜索（显示已选值）；字符串 = 正在敲的搜索词 */
  const [keyword, setKeyword] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const matched = useMemo(
    () => searchTargets(choices, keyword ?? ''),
    [choices, keyword],
  );
  const shown = matched.slice(0, MAX_RESULTS);
  const hidden = matched.length - shown.length;

  const selected = useMemo(
    () => (value ? choices.find((item) => item.ip === value) : undefined),
    [choices, value],
  );

  /* 自定义行占一个位置：键盘上下键要能走到它上面 */
  const customIndex = onManual ? shown.length : -1;
  const itemCount = shown.length + (onManual ? 1 : 0);

  const committed = manual
    ? manualLabel ?? ''
    : selected
      ? displayText(selected)
      : '';
  const text = keyword ?? committed;

  const close = () => {
    setOpen(false);
    setKeyword(null);
  };

  const openMenu = () => {
    setOpen(true);
    setKeyword(null);
    const index = shown.findIndex((item) => item.ip === value);
    setActive(index >= 0 ? index : 0);
  };

  const choose = (choice: TargetChoice) => {
    onSelect(choice);
    close();
    inputRef.current?.blur();
  };

  const chooseManual = () => {
    if (!onManual) return;
    onManual((keyword ?? '').trim());
    close();
    inputRef.current?.blur();
  };

  /* 点到组件外面就收起 */
  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
        setKeyword(null);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!open) {
        openMenu();
        return;
      }
      if (!itemCount) return;
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      setActive((prev) => (prev + delta + itemCount) % itemCount);
      return;
    }
    if (event.key === 'Enter') {
      if (!open) return;
      event.preventDefault();
      if (active === customIndex && customIndex >= 0) {
        chooseManual();
        return;
      }
      const item = shown[active];
      if (item) choose(item);
      /* 一个都没匹配上时回车 = 「就按我敲的这个地址手填」 */
      else if (onManual && (keyword ?? '').trim()) chooseManual();
      return;
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'Tab') close();
  };

  return (
    <Field label={label} required={required} error={error} hint={hint} htmlFor={inputId}>
      <div className={`target-select ${open ? 'is-open' : ''}`} ref={rootRef}>
        <div className="input-wrap">
          <input
            ref={inputRef}
            id={inputId}
            className="input"
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={
              open && active < itemCount ? `${listId}-${active}` : undefined
            }
            autoComplete="off"
            disabled={disabled}
            value={text}
            placeholder={placeholder ?? t('frp.targetSearchPlaceholder')}
            onFocus={() => {
              openMenu();
              /* 聚焦即全选：接着敲字就是新搜索，不必先删掉旧值 */
              window.setTimeout(() => inputRef.current?.select(), 0);
            }}
            onChange={(event) => {
              setKeyword(event.target.value);
              setOpen(true);
              setActive(0);
            }}
            onKeyDown={onKeyDown}
          />
        </div>

        {open && !disabled ? (
          <div
            className="target-select-menu"
            role="listbox"
            id={listId}
            aria-label={t('frp.targetSearchListAria')}
          >
            {loading ? (
              <div className="target-select-empty">{t('frp.targetSelectLoading')}</div>
            ) : null}

            {!loading && shown.length === 0 ? (
              <div className="target-select-empty">
                <span>
                  {(keyword ?? '').trim()
                    ? t('frp.targetSearchNoMatch', { query: (keyword ?? '').trim() })
                    : t('frp.targetSelectEmpty')}
                </span>
                {/* 搜不到又允许手填：直接把已敲的内容带过去，省一次重复输入 */}
                {onManual && (keyword ?? '').trim() ? (
                  <button
                    type="button"
                    className="target-select-action"
                    onMouseDown={(event) => {
                      event.preventDefault();
                      chooseManual();
                    }}
                  >
                    {t('frp.targetSearchManual')}
                  </button>
                ) : null}
              </div>
            ) : null}

            {shown.map((choice, index) => {
              const isSelected = choice.ip === value && !manual;
              return (
                <div
                  key={choice.ip}
                  id={`${listId}-${index}`}
                  role="option"
                  aria-selected={isSelected}
                  className={`target-select-option ${index === active ? 'is-active' : ''} ${
                    isSelected ? 'is-selected' : ''
                  }`}
                  /* mousedown 而不是 click：click 之前输入框会先失焦，
                     Modal 里的焦点跳动会让列表闪一下 */
                  onMouseDown={(event) => {
                    event.preventDefault();
                    choose(choice);
                  }}
                  onMouseEnter={() => setActive(index)}
                >
                  <span className="target-select-name">
                    {choice.name || choice.note || choice.ip}
                  </span>
                  {choice.vmid !== undefined ? (
                    <span className="target-select-meta">
                      {choice.type === 'lxc' ? t('frp.kindLxc') : t('frp.kindQemu')}{' '}
                      {choice.vmid}
                    </span>
                  ) : choice.note && choice.name ? (
                    <span className="target-select-meta">{choice.note}</span>
                  ) : null}
                  <span className="target-select-ip mono">{choice.ip}</span>
                </div>
              );
            })}

            {hidden > 0 ? (
              <div className="target-select-more">
                {t('frp.targetSearchMore', { n: hidden })}
              </div>
            ) : null}

            {onManual ? (
              <>
                <div className="target-select-sep" />
                <div
                  id={`${listId}-${customIndex}`}
                  role="option"
                  aria-selected={manual}
                  className={`target-select-option target-select-custom ${
                    active === customIndex ? 'is-active' : ''
                  } ${manual ? 'is-selected' : ''}`}
                  onMouseDown={(event) => {
                    event.preventDefault();
                    chooseManual();
                  }}
                  onMouseEnter={() => setActive(customIndex)}
                >
                  <span className="target-select-name">{manualLabel}</span>
                  {(keyword ?? '').trim() ? (
                    <span className="target-select-ip mono">{(keyword ?? '').trim()}</span>
                  ) : null}
                </div>
              </>
            ) : null}
          </div>
        ) : null}
      </div>
    </Field>
  );
}

/** 收起时输入框里显示的文字：主机名 · IP（没有名字就「说明 · IP」） */
function displayText(choice: TargetChoice): string {
  const head = choice.name || choice.note;
  return head ? `${head} · ${choice.ip}` : choice.ip;
}
