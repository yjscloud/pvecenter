/* ==========================================================================
   pvecenter — 复制到剪贴板（带非安全上下文的回退）

   为什么不能只用 navigator.clipboard：它要求**安全上下文**（https 或
   localhost）。面板常以 `http://<内网 IP>:<端口>` 部署 —— 那种页面里
   `navigator.clipboard` 直接是 undefined，「复制命令」于是永远报「复制失败」，
   而用户只是点了一下复制。

   回退走老办法：临时 textarea + `document.execCommand('copy')`。它在新标准里
   已标记废弃，但恰恰是**非安全上下文里唯一能用**的路径 —— 两条路合起来才覆盖
   「https 面板」与「http 内网面板」两种部署。
   ========================================================================== */

/** 复制文本，成功返回 true；失败返回 false（调用方决定怎么提示）。 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false;

  /* 首选异步剪贴板：不受焦点影响，也不会闪一下临时元素 */
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 权限被拒 / 文档失去焦点：继续走回退，别直接判失败 */
    }
  }

  return legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  const area = document.createElement('textarea');
  area.value = text;
  /* 仍要可选中：display:none、visibility:hidden 选不中，复制会静默失败。
     所以放在视口外 */
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.top = '0';
  area.style.left = '-9999px';
  area.style.opacity = '0';

  const active = document.activeElement as HTMLElement | null;
  document.body.appendChild(area);
  try {
    area.select();
    /* iOS Safari 上 select() 会选中整个节点，setSelectionRange 才能锁定内容 */
    area.setSelectionRange(0, area.value.length);
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
    /* 焦点还给原来的元素：复制完用户可能接着按键盘 */
    active?.focus?.();
  }
}
