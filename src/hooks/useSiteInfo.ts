/* ==========================================================================
   pvecenter — 站点信息
   品牌名 / 副标题 / 版权 / 备案号 / 友情链接 / 自定义 Logo，
   由后端 settings 表提供，可在「设置 → 站点信息」中修改。
   ========================================================================== */

import { useEffect, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { siteApi } from '../api/endpoints';
import { useT } from '../i18n';
import type { SiteInfo } from '../api/types';

/**
 * 后端不可用时的兜底值：与后端 site.py 的内置默认值保持一致，
 * 保证接口失败时界面仍显示完整的品牌信息，而不是空白标题。
 * 备案号与友链的默认值都是「空」，即不展示。
 *
 * 副标题与版权留空：它们的默认文案要随界面语言变，由 useSiteInfo()
 * 在运行时用词条补齐（模块级常量拿不到当前语言）。
 */
export const DEFAULT_SITE_INFO: SiteInfo = {
  name: 'pvecenter',
  subtitle: '',
  copyright: '',
  icp: '',
  links: [],
  logo_url: null,
  login_bg_url: null,
};

/**
 * 入口页的品牌信息：后端在返回 index.html 时把站点名 / 副标题 / Logo 写进
 * <meta name="site-*">（backend/app/main.py 的 _render_index）。
 *
 * 这里读出来当 React Query 的 placeholderData —— 否则首帧只能拿 DEFAULT_SITE_INFO，
 * 侧边栏、登录页会先闪一下兜底的 "pvecenter"，等接口回来才换成真实站点名。
 *
 * 刻意**不用 initialData**：initialData 会写进缓存，而它只带品牌三项，版权 /
 * 备案号 / 友链都是空的；设置页如果把这份缓存当表单初值，「保存」就会拿空字段
 * 覆盖掉真实配置。placeholderData 只在没数据时参与渲染，不进缓存。
 *
 * 单独跑 Vite 或把构建产物静态托管时，meta 里是构建时的兜底值；与默认值相同就
 * 当作「没有注入」，交给接口自己取，避免把兜底值当成用户配置。
 */
function readInjectedSiteInfo(): SiteInfo | undefined {
  if (typeof document === 'undefined') return undefined;
  const meta = (key: string): string => {
    const el = document.querySelector<HTMLMetaElement>(`meta[name="${key}"]`);
    return el?.content?.trim() ?? '';
  };

  const name = meta('site-name');
  if (!name || name === DEFAULT_SITE_INFO.name) return undefined;

  return {
    ...DEFAULT_SITE_INFO,
    name,
    subtitle: meta('site-subtitle'),
    logo_url: meta('site-logo') || null,
  };
}

/** 模块加载时读一次即可：入口页的 meta 在整个会话里不会变。 */
const INJECTED_SITE_INFO = readInjectedSiteInfo();

export function useSiteInfo(): SiteInfo {
  const t = useT();
  const query = useQuery({
    queryKey: ['config', 'site'],
    queryFn: siteApi.get,
    // 只作占位：接口一回来就用真实数据替换（未注入时是 undefined，等于没设）
    placeholderData: INJECTED_SITE_INFO,
    staleTime: 5 * 60_000,
    retry: 1,
  });

  const info = query.data ?? DEFAULT_SITE_INFO;

  const resolved = useMemo<SiteInfo>(
    () => ({
      ...info,
      subtitle: info.subtitle || t('site.defaultSubtitle'),
      copyright: info.copyright || t('site.defaultCopyright'),
    }),
    [info, t],
  );

  /* 浏览器标题跟随站点名：放在这里，登录页与落地页也能生效。
   *
   * 接口没回来之前不写：入口页的 <title> 已经由后端替换成真实站点名了
   * （backend/app/main.py 的 _render_index）。这里若拿 DEFAULT_SITE_INFO 的
   * 兜底名再覆盖一次，标签页会先闪回 "pvecenter"，等接口回来才变回去。
   *
   * 服务端没注入的场景（前端单独部署 / 静态托管）也照样成立：标题保持构建
   * 时的兜底值，接口回来后一次性改成站点名，不会多一次跳变。 */
  useEffect(() => {
    if (!query.data) return;
    document.title = t('site.documentTitle', { name: resolved.name });
  }, [query.data, resolved.name, t]);

  /* 上传了自定义 Logo 就一并用作浏览器标签页图标 */
  useEffect(() => {
    if (!info.logo_url) return;
    let link = document.querySelector<HTMLLinkElement>("link[rel='icon']");
    if (!link) {
      link = document.createElement('link');
      link.rel = 'icon';
      document.head.appendChild(link);
    }
    link.href = info.logo_url;
  }, [info.logo_url]);

  return resolved;
}
