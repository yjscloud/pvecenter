/* ==========================================================================
   pvecenter — 主题偏好
   三档：跟随系统 / 浅色 / 深色。存 localStorage，只影响本机；不写服务端，
   因为这是「这台设备上的观看习惯」，不是账号配置。
   ========================================================================== */

export type ThemeMode = 'light' | 'dark' | 'system';

const THEME_KEY = 'ui.theme';
const DARK_MEDIA = '(prefers-color-scheme: dark)';
const THEME_EVENT = 'pvecenter:theme';

export function getThemeMode(): ThemeMode {
  try {
    const value = window.localStorage.getItem(THEME_KEY);
    if (value === 'light' || value === 'dark' || value === 'system') return value;
  } catch {
    /* 隐私模式 / 禁用存储：按跟随系统处理 */
  }
  return 'system';
}

/** 把三档偏好解析成实际生效的浅/深 */
export function resolveTheme(mode: ThemeMode): 'light' | 'dark' {
  if (mode === 'system') {
    return window.matchMedia?.(DARK_MEDIA).matches ? 'dark' : 'light';
  }
  return mode;
}

/** 应用到 <html data-theme="…">：所有颜色变量都在 theme.css 里按它切换 */
export function applyTheme(mode: ThemeMode = getThemeMode()): void {
  const resolved = resolveTheme(mode);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.style.colorScheme = resolved;
}

export function setThemeMode(mode: ThemeMode): void {
  try {
    window.localStorage.setItem(THEME_KEY, mode);
  } catch {
    /* 存不下也不影响本次生效 */
  }
  applyTheme(mode);
  window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: mode }));
}

/** 订阅偏好变化（用户切换、或系统在「跟随系统」下切了深浅） */
export function subscribeTheme(listener: () => void): () => void {
  window.addEventListener(THEME_EVENT, listener);
  const media = window.matchMedia?.(DARK_MEDIA);
  media?.addEventListener?.('change', listener);
  return () => {
    window.removeEventListener(THEME_EVENT, listener);
    media?.removeEventListener?.('change', listener);
  };
}
