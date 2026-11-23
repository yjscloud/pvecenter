/* ==========================================================================
   pvecenter — Input / Select / Textarea / Switch / Checkbox / RadioGroup
   ========================================================================== */

import {
  forwardRef,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type CSSProperties,
  type ForwardedRef,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../../i18n';

/* ---------------------------------------------------------------------------
   Field 外壳（label + 错误提示）
   --------------------------------------------------------------------------- */

export interface FieldProps {
  label?: ReactNode;
  /** 错误文案，非空时显示红字 */
  error?: string;
  /** 帮助文案 */
  hint?: string;
  required?: boolean;
  htmlFor?: string;
  /** 标签行右侧的附属内容（如密码框旁的「忘记密码？」链接） */
  labelExtra?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Field({
  label,
  error,
  hint,
  required,
  htmlFor,
  labelExtra,
  children,
  className,
}: FieldProps) {
  return (
    <div className={`field ${error ? 'field-error' : ''} ${className ?? ''}`}>
      {label ? (
        labelExtra ? (
          <div className="field-label-row">
            <label className="field-label" htmlFor={htmlFor}>
              {label}
              {required ? (
                <span className="field-required" aria-hidden="true">
                  *
                </span>
              ) : null}
            </label>
            <span className="field-label-extra">{labelExtra}</span>
          </div>
        ) : (
          <label className="field-label" htmlFor={htmlFor}>
            {label}
            {required ? (
              <span className="field-required" aria-hidden="true">
                *
              </span>
            ) : null}
          </label>
        )
      ) : null}
      {children}
      {error ? (
        <div className="field-message field-message-error" role="alert">
          {error}
        </div>
      ) : hint ? (
        <div className="field-message">{hint}</div>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Input
   --------------------------------------------------------------------------- */

/**
 * `prefix` is omitted from the native attributes because the DOM already has a
 * `prefix` attribute typed as `string`, and we need a ReactNode slot instead.
 * `size` is omitted too — the native one is a number, ours would conflict with
 * the CSS-driven sizing.
 */
export interface InputProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, 'prefix' | 'size'> {
  label?: ReactNode;
  error?: string;
  hint?: string;
  /** 标签行右侧的附属内容（如密码框旁的「忘记密码？」链接） */
  labelExtra?: ReactNode;
  /** 左侧图标 */
  prefix?: ReactNode;
  /** 右侧内容（按钮等）*/
  suffix?: ReactNode;
  /** 撑满宽度，默认 true */
  block?: boolean;
  /** 等宽字体（IP、路径、UUID 等）*/
  mono?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  {
    label,
    error,
    hint,
    labelExtra,
    prefix,
    suffix,
    block = true,
    mono = false,
    className,
    id,
    ...rest
  },
  ref,
) {
  const autoId = useId();
  const inputId = id ?? autoId;

  return (
    <Field
      label={label}
      error={error}
      hint={hint}
      required={rest.required}
      htmlFor={inputId}
      labelExtra={labelExtra}
    >
      <div
        className={`input-wrap ${block ? 'input-block' : ''} ${
          error ? 'is-invalid' : ''
        }`}
      >
        {prefix ? <span className="input-prefix">{prefix}</span> : null}
        <input
          ref={ref}
          id={inputId}
          className={`input ${mono ? 'mono' : ''} ${className ?? ''}`}
          aria-invalid={error ? true : undefined}
          {...rest}
        />
        {suffix ? <span className="input-suffix">{suffix}</span> : null}
      </div>
    </Field>
  );
});

/* ---------------------------------------------------------------------------
   Textarea
   --------------------------------------------------------------------------- */

export interface TextareaProps
  extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: ReactNode;
  error?: string;
  hint?: string;
  /** 等宽字体（SSH Key / JSON 等）*/
  mono?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(
  function Textarea(
    { label, error, hint, mono = false, className, id, rows = 4, ...rest },
    ref,
  ) {
    const autoId = useId();
    const inputId = id ?? autoId;

    return (
      <Field
        label={label}
        error={error}
        hint={hint}
        required={rest.required}
        htmlFor={inputId}
      >
        <textarea
          ref={ref}
          id={inputId}
          rows={rows}
          className={`textarea ${mono ? 'mono' : ''} ${
            error ? 'is-invalid' : ''
          } ${className ?? ''}`}
          aria-invalid={error ? true : undefined}
          {...rest}
        />
      </Field>
    );
  },
);

/* ---------------------------------------------------------------------------
   Select
   --------------------------------------------------------------------------- */

export interface SelectOptionItem {
  label: string;
  value: string | number;
  disabled?: boolean;
}

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: ReactNode;
  error?: string;
  hint?: string;
  /** 选项数组 */
  options?: SelectOptionItem[];
  /** 未选择时的占位（应配合 value='' 使用）*/
  placeholder?: string;
  children?: ReactNode;
  /**
   * 可搜索下拉：输入即按选项**文字**过滤（忽略大小写；空格分隔的多个词需全部
   * 命中，所以「web 101」这种「名称 + VMID」组合也能搜到）。
   *
   * 为什么按文字而不是结构化字段：这些下拉的 label 里本来就带 VMID / 节点 /
   * 连接名（例如 ``web-01（101 · pve1 · running）``），对文字做包含匹配就能同时
   * 支持「按名称搜」和「按 VMID 搜」，不必给每个调用点再传一份结构化数据。
   *
   * 只有传了 ``options`` 才生效；用 ``<option>`` 子节点的场景仍走原生下拉。
   */
  searchable?: boolean;
  /** 可搜索模式下的输入框占位 */
  searchPlaceholder?: string;
}

export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { searchable = false, searchPlaceholder, ...rest },
  ref,
) {
  if (searchable && rest.options) {
    return (
      <SearchableSelect
        {...rest}
        options={rest.options}
        searchPlaceholder={searchPlaceholder}
      />
    );
  }
  return <NativeSelect {...rest} selectRef={ref} />;
});

function NativeSelect({
  label,
  error,
  hint,
  options,
  placeholder,
  children,
  className,
  id,
  /* style 单独取出来，不跟着 rest 落到 <select> 上（原因见下） */
  style,
  selectRef,
  ...rest
}: Omit<SelectProps, 'searchable' | 'searchPlaceholder'> & {
  selectRef?: ForwardedRef<HTMLSelectElement>;
}) {
  const autoId = useId();
  const selectId = id ?? autoId;

  return (
    <Field
      label={label}
      error={error}
      hint={hint}
      required={rest.required}
      htmlFor={selectId}
    >
      {/* style 挂在这一层：调用方写 ``style={{ maxWidth: 280 }}`` 的意图是
          「这个下拉框多宽」，而右侧那个小箭头是相对这一层绝对定位的。挂到
          <select> 上只会把输入框缩窄、箭头留在原处 —— 表现出来就是箭头飘到
          框外面（框内留下一条空白）。 */}
      <div className={`select-wrap ${error ? 'is-invalid' : ''}`} style={style}>
        <select
          ref={selectRef}
          id={selectId}
          className={`select ${className ?? ''}`}
          aria-invalid={error ? true : undefined}
          {...rest}
        >
          {placeholder ? <option value="">{placeholder}</option> : null}
          {options
            ? options.map((o) => (
                <option key={String(o.value)} value={o.value} disabled={o.disabled}>
                  {o.label}
                </option>
              ))
            : children}
        </select>
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
      </div>
    </Field>
  );
}

/** 搜索列表一次最多渲染多少条：上千台的清单不可能一次铺完，靠输入缩小范围 */
const SEARCH_VISIBLE = 50;

/** 菜单浮层与输入框之间的间距、与视口边缘的最小留白 */
const SEARCH_MENU_GAP = 4;
const SEARCH_VIEW_PAD = 8;
/** 菜单的高度上限（与 .target-select-menu 的 max-height 一致）与下限 */
const SEARCH_MENU_MAX_HEIGHT = 240;
const SEARCH_MENU_MIN_HEIGHT = 120;
/** 菜单的最小宽度：窄列 / 表格单元格里也要放得下「名称（vmid · 节点 · 状态）」 */
const SEARCH_MENU_MIN_WIDTH = 240;

/** 浮层菜单的 fixed 坐标：给了 top 就是向下展开，给了 bottom 就是向上展开 */
interface SearchMenuBox {
  left: number;
  width: number;
  maxHeight: number;
  top?: number;
  bottom?: number;
}

function sameBox(prev: SearchMenuBox | null, next: SearchMenuBox): boolean {
  return (
    prev !== null &&
    prev.left === next.left &&
    prev.width === next.width &&
    prev.maxHeight === next.maxHeight &&
    prev.top === next.top &&
    prev.bottom === next.bottom
  );
}

function menuStyle(box: SearchMenuBox): CSSProperties {
  return {
    left: box.left,
    width: box.width,
    maxHeight: box.maxHeight,
    ...(box.top !== undefined
      ? { top: box.top }
      : { bottom: box.bottom }),
  };
}

/**
 * 可搜索下拉的实现。
 *
 * 交互与 ``ui/TargetSelect`` 同一套（输入即过滤、↑↓ 移动、回车确认、Esc 收起）。
 * 样式直接复用 ``target-select*`` 那组类，视觉与目标地址选择器保持一致。
 *
 * 菜单用 Portal 挂到 body + fixed 定位，而不是内联展开：
 * 这些下拉大多出现在卡片 / 表格 / Modal 里，内联展开会把所在卡片撑高（旁边一栏
 * 顿时一大片空白，页面还会跳一下），而 absolute 浮层又会被 .modal-body、
 * .table-container 的 overflow 裁掉。挂到 body 后菜单不占文档流，也不会被裁。
 */
export function SearchableSelect({
  label,
  error,
  hint,
  options,
  placeholder,
  className,
  id,
  style,
  value,
  onChange,
  disabled = false,
  required,
  searchPlaceholder,
  /* 表格 / 工具栏里已经有自己的排版时不要 Field 外壳（否则多一层 div 与原生的
     <select> 行高对不齐） */
  bare = false,
  ...rest
}: Omit<SelectProps, 'searchable' | 'children'> & {
  options: SelectOptionItem[];
  bare?: boolean;
}) {
  const t = useT();
  const autoId = useId();
  const inputId = id ?? autoId;
  const listId = `${inputId}-list`;
  /* <select> 的事件类型不能直接展开到 <input>：这里只挑调用点真正会用的几个
     属性（Firewall 等用 aria-label 而不是可见 label） */
  const ariaLabel = rest['aria-label'];
  const ariaDescribedBy = rest['aria-describedby'];
  const inputName = rest.name;
  const inputTitle = rest.title;
  const inputAutoFocus = rest.autoFocus;
  const inputTabIndex = rest.tabIndex;

  const [open, setOpen] = useState(false);
  /** null = 未在搜索（输入框显示已选值）；字符串 = 正在敲的搜索词 */
  const [keyword, setKeyword] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  /* 菜单挂在 body 下（见下面的 createPortal），所以不能靠父级的定位来摆，
     得自己按输入框的位置算 fixed 坐标；ref 用于「点菜单不算点外面」。 */
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuBox, setMenuBox] = useState<SearchMenuBox | null>(null);

  /* placeholder 与原生 <select> 一致：作为 value='' 的一项插在最前面，
     这样「清空选择」不需要额外做一个按钮 */
  const items = useMemo<SelectOptionItem[]>(
    () => (placeholder ? [{ label: placeholder, value: '' }, ...options] : options),
    [options, placeholder],
  );

  const current = items.find((item) => String(item.value) === String(value ?? ''));
  /* 值不在选项里（URL 带的旧值 / 机器已被删除）时显示值本身：显示空白会让
     人以为「没选中」，而原生 select 在这时会误导性地显示第一项 */
  const text = keyword ?? (current ? current.label : String(value ?? ''));

  const matched = useMemo(() => {
    const terms = (keyword ?? '')
      .trim()
      .toLowerCase()
      .split(/\s+/)
      .filter(Boolean);
    if (!terms.length) return items;
    return items.filter((item) => {
      const labelText = String(item.label).toLowerCase();
      return terms.every((term) => labelText.includes(term));
    });
  }, [items, keyword]);
  const shown = matched.slice(0, SEARCH_VISIBLE);
  const hidden = matched.length - shown.length;

  const close = () => {
    setOpen(false);
    setKeyword(null);
  };

  const openMenu = () => {
    if (disabled) return;
    setOpen(true);
    setKeyword(null);
    const index = items.findIndex(
      (item) => String(item.value) === String(value ?? ''),
    );
    setActive(index >= 0 && index < SEARCH_VISIBLE ? index : 0);
  };

  const choose = (item: SelectOptionItem) => {
    /* 调用方仍是 ``onChange={(e) => setX(e.target.value)}``：这里构造一个
       最小可用的 change event，免得把 8 个调用点全部改写一遍 */
    onChange?.({
      target: { value: String(item.value), name: rest.name ?? '' },
    } as ChangeEvent<HTMLSelectElement>);
    close();
    inputRef.current?.blur();
  };

  /* 菜单的 fixed 坐标：贴着输入框下方，下方放不下就翻到上方。
     滚动 / 缩放时跟着重算；输入框不可见（滚出视口 / 所在面板收起）就收起。 */
  const placeMenu = useCallback(() => {
    const anchor = rootRef.current;
    if (!anchor) return;
    const rect = anchor.getBoundingClientRect();
    if (rect.width === 0 || rect.bottom <= 0 || rect.top >= window.innerHeight) {
      setOpen(false);
      setKeyword(null);
      return;
    }
    const below = window.innerHeight - rect.bottom - SEARCH_MENU_GAP - SEARCH_VIEW_PAD;
    const above = rect.top - SEARCH_MENU_GAP - SEARCH_VIEW_PAD;
    const up = below < SEARCH_MENU_MIN_HEIGHT && above > below;
    /* 表格单元格里的下拉往往只有几十像素宽，菜单跟它等宽会把机器名截得只剩
       几个字：给一个最小宽度，再夹回视口内 */
    const width = Math.min(
      Math.max(rect.width, SEARCH_MENU_MIN_WIDTH),
      window.innerWidth - SEARCH_VIEW_PAD * 2,
    );
    const next: SearchMenuBox = {
      left: Math.max(
        SEARCH_VIEW_PAD,
        Math.min(rect.left, window.innerWidth - width - SEARCH_VIEW_PAD),
      ),
      width,
      maxHeight: Math.max(
        SEARCH_MENU_MIN_HEIGHT,
        Math.min(SEARCH_MENU_MAX_HEIGHT, up ? above : below),
      ),
      ...(up
        ? { bottom: window.innerHeight - rect.top + SEARCH_MENU_GAP }
        : { top: rect.bottom + SEARCH_MENU_GAP }),
    };
    setMenuBox((prev) => (sameBox(prev, next) ? prev : next));
  }, []);

  /* layout 阶段就量好位置：不然菜单会先在屏幕左上角画一帧再跳到输入框下 */
  useLayoutEffect(() => {
    if (!open) {
      setMenuBox(null);
      return;
    }
    placeMenu();
    window.addEventListener('resize', placeMenu);
    window.addEventListener('scroll', placeMenu, true);
    return () => {
      window.removeEventListener('resize', placeMenu);
      window.removeEventListener('scroll', placeMenu, true);
    };
  }, [open, placeMenu]);

  /* 点到组件外面（或菜单外面，菜单不在组件 DOM 里）就收起 */
  useEffect(() => {
    if (!open) return;
    const handler = (event: MouseEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      close();
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
      if (!shown.length) return;
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      setActive((prev) => (prev + delta + shown.length) % shown.length);
      return;
    }
    if (event.key === 'Enter') {
      if (!open) return;
      event.preventDefault();
      const item = shown[active];
      if (item) choose(item);
      return;
    }
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'Tab') close();
  };

  const body = (
    <div
      className={`target-select select-search ${open ? 'is-open' : ''} ${
        bare ? 'is-bare' : ''
      }`}
      ref={rootRef}
      style={style}
    >
        <div className={`input-wrap ${error ? 'is-invalid' : ''}`}>
          <span className="input-prefix" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="14" height="14">
              <circle
                cx="11"
                cy="11"
                r="6"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              />
              <path
                d="m16 16 4 4"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
              />
            </svg>
          </span>
          <input
            ref={inputRef}
            id={inputId}
            className={`input ${className ?? ''}`}
            name={inputName}
            title={inputTitle}
            autoFocus={inputAutoFocus}
            tabIndex={inputTabIndex}
            aria-label={ariaLabel}
            /* 键盘用法对读屏用户不可见，挂一段 sr-only 说明 */
            aria-describedby={
              ariaDescribedBy
                ? `${ariaDescribedBy} ${inputId}-hint`
                : `${inputId}-hint`
            }
            role="combobox"
            aria-expanded={open}
            aria-controls={listId}
            aria-autocomplete="list"
            aria-invalid={error ? true : undefined}
            aria-activedescendant={
              open && active < shown.length ? `${listId}-${active}` : undefined
            }
            autoComplete="off"
            disabled={disabled}
            value={text}
            placeholder={searchPlaceholder ?? t('common.selectSearchPlaceholder')}
            onFocus={() => {
              openMenu();
              /* 聚焦即全选：接着敲就是新搜索，不用先删掉旧值 */
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
        <span id={`${inputId}-hint`} className="sr-only">
          {t('common.selectSearchHint')}
        </span>

        {open && !disabled && menuBox
          ? createPortal(
              <div
                ref={menuRef}
                className="target-select-menu is-floating"
                role="listbox"
                id={listId}
                aria-label={
                  typeof label === 'string' ? label : t('common.selectPlaceholder')
                }
                style={menuStyle(menuBox)}
              >
                {shown.length === 0 ? (
                  <div className="target-select-empty">
                    {t('common.selectSearchNoMatch')}
                  </div>
                ) : null}

                {shown.map((item, index) => {
                  const selected = String(item.value) === String(value ?? '');
                  return (
                    <div
                      key={String(item.value)}
                      id={`${listId}-${index}`}
                      role="option"
                      aria-selected={selected}
                      className={`target-select-option ${
                        index === active ? 'is-active' : ''
                      } ${selected ? 'is-selected' : ''}`}
                      /* mousedown 而不是 click：click 之前输入框会先失焦，
                         Modal 里的焦点跳动会让列表闪一下 */
                      onMouseDown={(event) => {
                        event.preventDefault();
                        choose(item);
                      }}
                      onMouseEnter={() => setActive(index)}
                    >
                      <span className="target-select-name">{item.label}</span>
                    </div>
                  );
                })}

                {hidden > 0 ? (
                  <div className="target-select-more">
                    {t('common.selectSearchMore', { n: hidden })}
                  </div>
                ) : null}
              </div>,
              document.body,
            )
          : null}
    </div>
  );

  if (bare) return body;

  return (
    <Field label={label} error={error} hint={hint} required={required} htmlFor={inputId}>
      {body}
    </Field>
  );
}

/* ---------------------------------------------------------------------------
   Switch（开关）
   --------------------------------------------------------------------------- */

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  hint?: string;
  disabled?: boolean;
  id?: string;
  className?: string;
  /**
   * 无障碍名称。不传 label 时（例如「左侧说明文字 + 右侧开关」的设置行，
   * 文字与开关是分开的两个元素）用它保证读屏能念出这个开关是干什么的。
   */
  ariaLabel?: string;
}

export function Switch({
  checked,
  onChange,
  label,
  hint,
  disabled = false,
  id,
  className,
  ariaLabel,
}: SwitchProps) {
  const autoId = useId();
  const switchId = id ?? autoId;

  return (
    <div className={`switch-field ${disabled ? 'is-disabled' : ''} ${className ?? ''}`}>
      <button
        type="button"
        id={switchId}
        role="switch"
        aria-checked={checked}
        aria-label={ariaLabel}
        className={`switch ${checked ? 'is-on' : ''}`}
        onClick={() => !disabled && onChange(!checked)}
        disabled={disabled}
      >
        <span className="switch-thumb" aria-hidden="true" />
      </button>
      {label ? (
        <span className="switch-content">
          <label className="switch-label" htmlFor={switchId}>
            {label}
          </label>
          {hint ? <span className="switch-hint">{hint}</span> : null}
        </span>
      ) : null}
    </div>
  );
}

/* ---------------------------------------------------------------------------
   Checkbox
   --------------------------------------------------------------------------- */

export interface CheckboxProps
  extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label?: ReactNode;
  /** 中间态（批量选择）*/
  indeterminate?: boolean;
}

export function Checkbox({
  label,
  indeterminate = false,
  className,
  id,
  ...rest
}: CheckboxProps) {
  const autoId = useId();
  const cbId = id ?? autoId;

  return (
    <label className={`checkbox-field ${className ?? ''}`} htmlFor={cbId}>
      <input
        type="checkbox"
        id={cbId}
        ref={(el) => {
          if (el) el.indeterminate = indeterminate;
        }}
        className="checkbox-input"
        {...rest}
      />
      <span className="checkbox-box" aria-hidden="true">
        <svg viewBox="0 0 16 16" width="11" height="11">
          {indeterminate ? (
            <path
              d="M3.5 8h9"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.4"
              strokeLinecap="round"
            />
          ) : (
            <path
              d="M3 8.5 6.2 11.7 13 5"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          )}
        </svg>
      </span>
      {label ? <span className="checkbox-label">{label}</span> : null}
    </label>
  );
}

/* ---------------------------------------------------------------------------
   RadioGroup
   --------------------------------------------------------------------------- */

export interface RadioOption {
  label: ReactNode;
  value: string;
  hint?: string;
  disabled?: boolean;
}

export interface RadioGroupProps {
  name: string;
  value: string;
  onChange: (value: string) => void;
  options: RadioOption[];
  /** 竖向排列 */
  vertical?: boolean;
  label?: ReactNode;
}

export function RadioGroup({
  name,
  value,
  onChange,
  options,
  vertical = false,
  label,
}: RadioGroupProps) {
  return (
    <fieldset
      className={`radio-group ${vertical ? 'is-vertical' : ''}`}
      aria-label={typeof label === 'string' ? label : undefined}
    >
      {label ? <legend className="radio-legend">{label}</legend> : null}
      {options.map((o) => (
        <label
          key={o.value}
          className={`radio-field ${o.disabled ? 'is-disabled' : ''}`}
        >
          <input
            type="radio"
            name={name}
            value={o.value}
            checked={value === o.value}
            disabled={o.disabled}
            onChange={() => onChange(o.value)}
            className="radio-input"
          />
          <span className="radio-dot" aria-hidden="true" />
          <span className="radio-content">
            <span className="radio-label">{o.label}</span>
            {o.hint ? <span className="radio-hint">{o.hint}</span> : null}
          </span>
        </label>
      ))}
    </fieldset>
  );
}

/* ---------------------------------------------------------------------------
   SegmentedControl（分段切换，替代 tab）
   --------------------------------------------------------------------------- */

export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  className,
  ariaLabel,
}: {
  value: T;
  onChange: (v: T) => void;
  options: Array<{ label: ReactNode; value: T }>;
  className?: string;
  ariaLabel?: string;
}) {
  return (
    <div
      className={`segmented ${className ?? ''}`}
      role="tablist"
      aria-label={ariaLabel}
    >
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          className={`segmented-item ${value === o.value ? 'is-active' : ''}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}
