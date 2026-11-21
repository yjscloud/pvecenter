/* ==========================================================================
   ScrollArea — 自带「悬停才现身」滚动条的滚动区

   为什么不用原生滚动条：
   * 各平台长得完全不一样（Windows 是常驻的 15px 灰条，macOS 是滚动时才浮出来的
     细条，Linux 两副面孔），和这套控制台的整体风格对不上；
   * 原生覆盖式滚动条**鼠标划过时不会出现**，只能靠「先滚一下」才看见，长列表
     第一眼不知道自己还能不能往下翻。

   所以这里自己画一根：鼠标移进区域时淡入（`opacity` 过渡），按住滑块拖、在轨道上
   拖动/点击都由我们接管；滚轮、触控板、键盘方向键仍交给浏览器原生滚动（用的是
   同一个 `overflow-y: auto`），惯性与回弹都保持系统手感。

   几条纪律：
   * **轨道不吃鼠标事件**（`pointer-events: none`）：否则右边缘的表单输入框会被
     一条看不见的 12px 条挡住，点了没反应 —— 那种 bug 极难查。
   * 只有滑块 `pointer-events: auto`，且拖动时 `preventDefault`，不会抢走输入框焦点。
   * 内容放得下时整根条不渲染（不是隐藏）：没有可滚的东西就不该有拖动把手。
   * 触屏（`hover: none`）不画条：手指没有「悬停」，条反而挡住内容。
   ========================================================================== */

import { useCallback, useEffect, useRef, useState } from 'react';

/** 轨道上下留白 / 滑块最小高度（像素） */
const PAD = 4;
const MIN_THUMB = 28;

export interface ScrollAreaProps {
  /** 外层容器（控制尺寸的那个） */
  className?: string;
  /** 滚动视口 */
  viewClassName?: string;
  children: React.ReactNode;
  /** 给读屏用的区域名 */
  label?: string;
}

export function ScrollArea({
  className = '',
  viewClassName = '',
  children,
  label,
}: ScrollAreaProps) {
  const viewRef = useRef<HTMLDivElement>(null);
  /** 按下滑块那一刻的鼠标位置与滚动量，拖动时都以它为原点 */
  const dragOrigin = useRef({ y: 0, top: 0 });
  const [thumb, setThumb] = useState<{ top: number; height: number } | null>(
    null,
  );
  const [dragging, setDragging] = useState(false);

  /* 滑块的位置与高度按「视口 / 内容」比例算。内容没超出就置空（不渲染）。 */
  const sync = useCallback(() => {
    const el = viewRef.current;
    if (!el) return;
    const view = el.clientHeight;
    const content = el.scrollHeight;
    if (view <= 0 || content <= view + 1) {
      setThumb((prev) => (prev ? null : prev));
      return;
    }
    const track = Math.max(view - PAD * 2, 0);
    const height = Math.max(MIN_THUMB, Math.min(track, (view / content) * track));
    const maxScroll = content - view;
    const top =
      PAD + (maxScroll > 0 ? (el.scrollTop / maxScroll) * (track - height) : 0);
    setThumb((prev) =>
      prev && Math.abs(prev.top - top) < 0.5 && Math.abs(prev.height - height) < 0.5
        ? prev
        : { top, height },
    );
  }, []);

  /* 尺寸变化（窗口缩放、字体变化）与内容增删（加一删一行）都要重算 */
  useEffect(() => {
    const el = viewRef.current;
    if (!el) return;
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    for (const child of Array.from(el.children)) observer.observe(child);
    const mutations = new MutationObserver(sync);
    mutations.observe(el, { childList: true });
    window.addEventListener('resize', sync);
    return () => {
      observer.disconnect();
      mutations.disconnect();
      window.removeEventListener('resize', sync);
    };
  }, [sync]);

  /* 拖动：按「滑块走了多少像素」换算成「内容滚了多少像素」 */
  useEffect(() => {
    if (!dragging) return;
    const el = viewRef.current;
    if (!el) return;
    const { y: startY, top: startTop } = dragOrigin.current;

    const onMove = (event: PointerEvent) => {
      const view = el.clientHeight;
      const maxScroll = el.scrollHeight - view;
      const track = Math.max(view - PAD * 2, 0);
      const usable = track - (thumb?.height ?? 0);
      if (usable <= 0 || maxScroll <= 0) return;
      el.scrollTop = startTop + ((event.clientY - startY) / usable) * maxScroll;
    };
    const stop = () => setDragging(false);
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
    };
  }, [dragging, thumb?.height]);

  const startDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const el = viewRef.current;
    if (!el || event.button !== 0) return;
    /* 不 preventDefault 的话，浏览器会把这次按下当成「点空白」，焦点可能从
       正在编辑的输入框上掉下来 */
    event.preventDefault();
    dragOrigin.current = { y: event.clientY, top: el.scrollTop };
    setDragging(true);
  };

  return (
    <div className={`scrollarea ${className}`.trim()}>
      <div
        ref={viewRef}
        className={`scrollarea-view ${viewClassName}`.trim()}
        onScroll={sync}
        tabIndex={0}
        role="group"
        aria-label={label}
      >
        {children}
      </div>

      {thumb ? (
        <div
          className="scrollarea-bar"
          aria-hidden="true"
          data-dragging={dragging ? 'true' : undefined}
        >
          <div
            className="scrollarea-thumb"
            style={{ height: `${thumb.height}px`, top: `${thumb.top}px` }}
            onPointerDown={startDrag}
          />
        </div>
      ) : null}
    </div>
  );
}
