/* ==========================================================================
   pvecenter — 操作系统 logo（模板页「按系统分类」的统一标识）

   为什么要有这一个组件：
   模板页此前用三个大写字母（UBT / CEN / RCK）当系统标识。字母块只能靠家族色
   区分，隔着一屏卡片就很难一眼认出是哪个系统；而用户对自己常用的发行版，
   认得的是官方 logo，不是我们编的缩写。

   资源唯一：每个家族只对应 `src/assets/os-logos/` 下的**一份**官方 logo 文件，
   由 Vite 统一打包（带 hash，天然避免缓存旧图）。卡片、列表、分组头、筛选条
   四个位置全部走这张表，任何一处新增/替换 logo 都只改这里一行 —— 不允许在
   某个组件里另写一份路径，否则同一页会出现两套图标。

   ---------------------------------------------------------------------------
   显示规范（改样式请对照调整 src/styles/layout.css 里的 .os-logo-* 一节）
   ---------------------------------------------------------------------------
   位置            尺寸    容器                      说明
   模板卡片        26×26   .tpl-os-mark 38×38        左侧头像，卡片第一行
   分组头          16×16   无底            标题左侧，替代原来的家族色圆点
   分类筛选条      13×13   无底            chip 内文字左侧
   列表「系统」列  16×16   .os-tag-mark 22×22        与卡片头像同一套图

   硬性约束：
   1. 等比缩放：一律 `object-fit: contain`，不拉伸、不裁切。各家的 logo 画布
      比例不同（Debian 偏竖、CentOS 偏横），写死 width/height 而不管比例会
      把图标压扁。
   2. 不改色：保留官方品牌色，不加灰度/滤镜/反色；深色底会吃掉 Fedora、
      Windows 这类深蓝 logo，所以卡片与列表给它浅色 tint 底。
   3. 不旋转、不加装饰性阴影，与页面其它图标同为「静态标识」。
   4. `alt=""` + aria-hidden：logo 旁永远紧挨着系统名，读屏再念一遍是噪音。
   5. 认不出的系统（other）与资源加载失败时回退成文字缩写块（osFamilyMark），
      宁可退化也不留一个破图。
   ========================================================================== */

import { useState } from 'react';
import archLinuxLogo from '../assets/os-logos/arch.svg';
import alpineLogo from '../assets/os-logos/alpine.svg';
import almaLinuxLogo from '../assets/os-logos/alma.svg';
import centosLogo from '../assets/os-logos/centos.svg';
import coreosLogo from '../assets/os-logos/coreos.svg';
import debianLogo from '../assets/os-logos/debian.svg';
import fedoraLogo from '../assets/os-logos/fedora.svg';
import fnosLogo from '../assets/os-logos/fnos.svg';
import linuxLogo from '../assets/os-logos/linux.svg';
import opencloudosLogo from '../assets/os-logos/opencloudos.svg';
import openwrtLogo from '../assets/os-logos/openwrt.svg';
import redhatLogo from '../assets/os-logos/redhat.svg';
import rockyLogo from '../assets/os-logos/rocky.svg';
import suseLogo from '../assets/os-logos/suse.svg';
import tencentLogo from '../assets/os-logos/tencent.svg';
import ubuntuLogo from '../assets/os-logos/ubuntu.svg';
import windowsLogo from '../assets/os-logos/windows.svg';
import { osFamilyMark, type OsFamily, type OsFamilyKey } from '../utils/osFamily';

/**
 * 家族 → 官方 logo 资源的唯一映射。
 *
 * `linux`（认得出是 Linux、但认不出发行版）用 Tux —— 它本来就是 Linux 的官方
 * 标识，比随便挑一个发行版诚实。`other` 刻意**没有**图标：那代表「认不出」，
 * 给任何图形都会暗示一个并不存在的结论，交给文字缩写块回退。
 */
const OS_LOGOS: Partial<Record<OsFamilyKey, string>> = {
  windows: windowsLogo,
  ubuntu: ubuntuLogo,
  debian: debianLogo,
  redhat: redhatLogo,
  centos: centosLogo,
  rocky: rockyLogo,
  alma: almaLinuxLogo,
  fedora: fedoraLogo,
  /* TencentOS：官方标识只有位图（蓝底 #0052d9 的斜方形 + 白色 t）。这里按轮廓
     重绘成矢量（外框 4 个顶点、内圈 11 个），斜边才不会像放大位图那样出现台阶 ——
     列表里它只有 16px。**是重绘不是原图**：拿到官方 SVG 直接替换这个文件即可。 */
  tencent: tencentLogo,
  opencloudos: opencloudosLogo,
  suse: suseLogo,
  coreos: coreosLogo,
  openwrt: openwrtLogo,
  fnos: fnosLogo,
  alpine: alpineLogo,
  arch: archLinuxLogo,
  linux: linuxLogo,
};

/** 四个使用位置，尺寸与规范见文件头；CSS 类名即 `.os-logo-<size>` */
export type OsLogoSize = 'card' | 'group' | 'chip' | 'table';

/** 与 layout.css 中 .os-logo-* 的尺寸保持一致：同时写进 img 的 width/height，
    浏览器在图片解码前就按正确比例留位，列表滚动不会因 logo 加载而抖动 */
const LOGO_PX: Record<OsLogoSize, number> = {
  card: 26,
  group: 16,
  chip: 13,
  table: 16,
};

export interface OsLogoProps {
  family: OsFamily;
  size?: OsLogoSize;
  className?: string;
}

export function OsLogo({ family, size = 'card', className }: OsLogoProps) {
  /* 资源缺失/被代理拦掉时退回文字块。按实例记，不要用全局开关 ——
     一次网络抖动不该让整页都失去 logo。 */
  const [failed, setFailed] = useState(false);
  const src = OS_LOGOS[family.key];
  const extra = className ? ` ${className}` : '';

  if (!src || failed) {
    return (
      <span
        className={`os-logo os-logo-${size} os-logo-fallback${extra}`}
        aria-hidden="true"
      >
        {osFamilyMark(family)}
      </span>
    );
  }

  const px = LOGO_PX[size];
  return (
    <img
      className={`os-logo os-logo-${size}${extra}`}
      src={src}
      width={px}
      height={px}
      alt=""
      aria-hidden="true"
      draggable={false}
      decoding="async"
      /* 列表里几十张卡，logo 不进首屏关键路径；尺寸已由 width/height 占好 */
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
