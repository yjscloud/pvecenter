/* ==========================================================================
   pvecenter — 内联 SVG 图标集
   统一 24x24 viewBox，继承 currentColor
   ========================================================================== */

import type { SVGProps } from 'react';

export interface IconProps extends SVGProps<SVGSVGElement> {
  size?: number;
}

function Icon({ size = 18, children, ...rest }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...rest}
    >
      {children}
    </svg>
  );
}

/* --------------------------------------------------------------------------- */

/** 地球：语言切换。面板里唯一的「非功能性」图标，故单独放在最前面。 */
export const IconGlobe = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <ellipse cx="12" cy="12" rx="4" ry="9" />
  </Icon>
);

export const IconDashboard = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="3" width="7.5" height="7.5" rx="1.5" />
    <rect x="13.5" y="3" width="7.5" height="4.5" rx="1.5" />
    <rect x="13.5" y="10.5" width="7.5" height="10.5" rx="1.5" />
    <rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5" />
  </Icon>
);

export const IconServer = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="3.5" width="18" height="7" rx="1.8" />
    <rect x="3" y="13.5" width="18" height="7" rx="1.8" />
    <path d="M7 7h.01M7 17h.01" />
    <path d="M11 7h5M11 17h5" />
  </Icon>
);

export const IconVm = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="4" width="18" height="13" rx="2" />
    <path d="M8 21h8M12 17v4" />
    <path d="M7.5 9.5 10 11.5l-2.5 2M13 13.5h3.5" />
  </Icon>
);

/** 侧栏「虚拟机」栏目标志：两个错位叠放、各带一处开口的方框，取
 *  「一台机器上叠着多台」的意思。改用 24 视口 + stroke 描边绘制，线宽与颜色
 *  和侧栏其余图标完全一致（stroke currentColor + 1.8），选中态一起变强调色。
 *
 *  只给导航入口用：页面里的 IconVm 说的是「具体这一台虚拟机」（基本信息、
 *  列表标题、固件卡片等几十处），换成栏目标志反而不合适。 */
export const IconVmStacked = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6.6 16.7H4.4A1.9 1.9 0 0 1 2.5 14.8V5.3A1.9 1.9 0 0 1 4.4 3.4H13.9A1.9 1.9 0 0 1 15.8 5.3V14.8A1.9 1.9 0 0 1 13.9 16.7H11.8" />
    <path d="M17.5 7.9H19.6A1.9 1.9 0 0 1 21.5 9.8V19.3A1.9 1.9 0 0 1 19.6 21.2H10.1A1.9 1.9 0 0 1 8.2 19.3V9.8A1.9 1.9 0 0 1 10.1 7.9H12.3" />
  </Icon>
);

export const IconTemplate = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="3" width="18" height="18" rx="2" />
    <path d="M3 8.5h18M8.5 8.5V21" />
    <path d="M12 12.5h5M12 16h5" />
  </Icon>
);

export const IconStorage = (p: IconProps) => (
  <Icon {...p}>
    <ellipse cx="12" cy="6" rx="8" ry="3" />
    <path d="M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6" />
    <path d="M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" />
  </Icon>
);

export const IconNetwork = (p: IconProps) => (
  <Icon {...p}>
    <rect x="9" y="2.5" width="6" height="6" rx="1.5" />
    <rect x="2.5" y="15.5" width="6" height="6" rx="1.5" />
    <rect x="15.5" y="15.5" width="6" height="6" rx="1.5" />
    <path d="M12 8.5v4M5.5 15.5v-2.5h13v2.5" />
  </Icon>
);

export const IconBackup = (p: IconProps) => (
  <Icon {...p}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 4v5h-5" />
    <path d="M12 8v4l3 2" />
  </Icon>
);

export const IconSnapshot = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 7.5A2.5 2.5 0 0 1 5.5 5h2.2l1.4-2h5.8l1.4 2h2.2A2.5 2.5 0 0 1 21 7.5v9A2.5 2.5 0 0 1 18.5 19h-13A2.5 2.5 0 0 1 3 16.5z" />
    <circle cx="12" cy="12" r="3.5" />
  </Icon>
);

export const IconTasks = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 5h11M9 12h11M9 19h11" />
    <path d="M4 5.5 5.5 7 7.5 4.5M4 12.5 5.5 14l2-2.5M4 19.5 5.5 21l2-2.5" />
  </Icon>
);

export const IconUsers = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="9" cy="8" r="3.5" />
    <path d="M2.5 20c0-3.3 2.9-5.5 6.5-5.5s6.5 2.2 6.5 5.5" />
    <path d="M16.5 5.2a3.5 3.5 0 0 1 0 6.6M18 14.8c2.1.6 3.5 2.2 3.5 4.2" />
  </Icon>
);

export const IconAudit = (p: IconProps) => (
  <Icon {...p}>
    <path d="M14 2.5H6.5A1.5 1.5 0 0 0 5 4v16a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 20V7.5z" />
    <path d="M14 2.5V8h5" />
    <path d="M8.5 12.5h7M8.5 16h4.5" />
  </Icon>
);

export const IconSettings = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1.08-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6h.08A1.65 1.65 0 0 0 10.6 3.1V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9v.08a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
  </Icon>
);

export const IconConsole = (p: IconProps) => (
  <Icon {...p}>
    <rect x="2.5" y="4" width="19" height="14" rx="2" />
    <path d="M7 9.5 9.5 12 7 14.5M12 14.5h5" />
    <path d="M8 21.5h8" />
  </Icon>
);

/* ---- 操作类 ---- */

export const IconPlay = (p: IconProps) => (
  <Icon {...p}>
    <path d="M7 4.5v15l12-7.5z" fill="currentColor" stroke="none" />
  </Icon>
);

export const IconPower = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3v9" />
    <path d="M18.4 6.6a9 9 0 1 1-12.8 0" />
  </Icon>
);

export const IconStop = (p: IconProps) => (
  <Icon {...p}>
    <rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor" stroke="none" />
  </Icon>
);

export const IconRestart = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20.5 12a8.5 8.5 0 1 1-2.4-5.9" />
    <path d="M20.5 3.5V9H15" />
  </Icon>
);

export const IconPause = (p: IconProps) => (
  <Icon {...p}>
    <rect x="6.5" y="5" width="4" height="14" rx="1.2" fill="currentColor" stroke="none" />
    <rect x="13.5" y="5" width="4" height="14" rx="1.2" fill="currentColor" stroke="none" />
  </Icon>
);

export const IconPlus = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 5v14M5 12h14" />
  </Icon>
);

export const IconRefresh = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20.5 11A8.5 8.5 0 0 0 5.6 6.2L3.5 8" />
    <path d="M3.5 13a8.5 8.5 0 0 0 14.9 4.8l2.1-1.8" />
    <path d="M3.5 4v4h4M20.5 20v-4h-4" />
  </Icon>
);

export const IconSearch = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m16.5 16.5 4 4" />
  </Icon>
);

/* AI 排查助手：四角星 + 一点闪光，是「智能」的通用符号 */
export const IconSparkle = (p: IconProps) => (
  <Icon {...p}>
    <path d="M11 3.5 12.6 8.4 17.5 10 12.6 11.6 11 16.5 9.4 11.6 4.5 10 9.4 8.4z" />
    <path d="M18.5 14v3.5M20.25 15.75h-3.5" />
  </Icon>
);

export const IconClose = (p: IconProps) => (
  <Icon {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </Icon>
);

export const IconCheck = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20 6 9 17l-5-5" />
  </Icon>
);

export const IconTrash = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 7h16M10 11v6M14 11v6" />
    <path d="M6 7l1 12.5A1.5 1.5 0 0 0 8.5 21h7a1.5 1.5 0 0 0 1.5-1.5L18 7" />
    <path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7" />
  </Icon>
);

export const IconEdit = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" />
  </Icon>
);

export const IconEye = (p: IconProps) => (
  <Icon {...p}>
    <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" />
    <circle cx="12" cy="12" r="3" />
  </Icon>
);

export const IconEyeOff = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9.9 5.7A9.4 9.4 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.3 4.2" />
    <path d="M6.4 7.4A17 17 0 0 0 2.5 12S6 18.5 12 18.5c1.6 0 3-.4 4.3-1" />
    <path d="M10 10a3 3 0 0 0 4 4" />
    <path d="M3 3l18 18" />
  </Icon>
);

export const IconMore = (p: IconProps) => (
  <Icon {...p} strokeWidth="2.4">
    <circle cx="5" cy="12" r="1.2" fill="currentColor" />
    <circle cx="12" cy="12" r="1.2" fill="currentColor" />
    <circle cx="19" cy="12" r="1.2" fill="currentColor" />
  </Icon>
);

/* 拖动把手：两列各三点。仪表盘布局编辑时提示「这块可以拖」，
   与 IconMore 的横排三点区分开，不会被误读成「更多操作」。 */
export const IconGrip = (p: IconProps) => (
  <Icon {...p} strokeWidth="0">
    <circle cx="9" cy="6" r="1.4" fill="currentColor" />
    <circle cx="15" cy="6" r="1.4" fill="currentColor" />
    <circle cx="9" cy="12" r="1.4" fill="currentColor" />
    <circle cx="15" cy="12" r="1.4" fill="currentColor" />
    <circle cx="9" cy="18" r="1.4" fill="currentColor" />
    <circle cx="15" cy="18" r="1.4" fill="currentColor" />
  </Icon>
);

/* 布局编辑：四宫格里有一块被「拿起来」。用于仪表盘的「编辑布局」入口。 */
export const IconLayout = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="3" width="8" height="8" rx="1.5" />
    <rect x="15" y="3" width="6" height="8" rx="1.5" strokeDasharray="2.5 2.5" />
    <rect x="3" y="15" width="8" height="6" rx="1.5" />
    <rect x="15" y="15" width="6" height="6" rx="1.5" />
  </Icon>
);

export const IconChevronDown = (p: IconProps) => (
  <Icon {...p}>
    <path d="m6 9 6 6 6-6" />
  </Icon>
);

export const IconChevronRight = (p: IconProps) => (
  <Icon {...p}>
    <path d="m9 6 6 6-6 6" />
  </Icon>
);

export const IconChevronLeft = (p: IconProps) => (
  <Icon {...p}>
    <path d="m15 6-6 6 6 6" />
  </Icon>
);

export const IconChevronUp = (p: IconProps) => (
  <Icon {...p}>
    <path d="m6 15 6-6 6 6" />
  </Icon>
);

export const IconLogout = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
    <path d="m16 17 5-5-5-5M21 12H9" />
  </Icon>
);

export const IconMenu = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 6h16M4 12h16M4 18h16" />
  </Icon>
);

export const IconAlert = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3.5 21 19H3z" />
    <path d="M12 9.5v4M12 16.5h.01" />
  </Icon>
);

export const IconInfo = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 11v5M12 8h.01" />
  </Icon>
);

export const IconClock = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5.2l3.2 1.9" />
  </Icon>
);

export const IconCpu = (p: IconProps) => (
  <Icon {...p}>
    <rect x="6" y="6" width="12" height="12" rx="2" />
    <rect x="9.5" y="9.5" width="5" height="5" rx="1" />
    <path d="M9 2.5v3M15 2.5v3M9 18.5v3M15 18.5v3M2.5 9h3M2.5 15h3M18.5 9h3M18.5 15h3" />
  </Icon>
);

export const IconMemory = (p: IconProps) => (
  <Icon {...p}>
    <rect x="2.5" y="7" width="19" height="10" rx="2" />
    <path d="M6 17v3M10 17v3M14 17v3M18 17v3" />
    <path d="M6.5 10.5v3M11 10.5v3M15.5 10.5v3" />
  </Icon>
);

export const IconDisk = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="12" r="9" />
    <circle cx="12" cy="12" r="2.6" />
    <path d="m14.2 14.2 3.8 3.8" />
  </Icon>
);

export const IconActivity = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 12h4l2.5-7 4 14 2.5-7h5" />
  </Icon>
);

export const IconUpload = (p: IconProps) => (
  <Icon {...p}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="m7.5 8.5 4.5-5 4.5 5M12 3.5v12" />
  </Icon>
);

export const IconDownload = (p: IconProps) => (
  <Icon {...p}>
    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
    <path d="m7.5 11 4.5 5 4.5-5M12 16V3.5" />
  </Icon>
);

export const IconCopy = (p: IconProps) => (
  <Icon {...p}>
    <rect x="9" y="9" width="12" height="12" rx="2" />
    <path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1" />
  </Icon>
);

export const IconExpand = (p: IconProps) => (
  <Icon {...p}>
    <path d="M8 3H4a1 1 0 0 0-1 1v4M16 3h4a1 1 0 0 1 1 1v4M8 21H4a1 1 0 0 1-1-1v-4M16 21h4a1 1 0 0 0 1-1v-4" />
  </Icon>
);

export const IconShrink = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 8h4a1 1 0 0 0 1-1V3M21 8h-4a1 1 0 0 1-1-1V3M3 16h4a1 1 0 0 1 1 1v4M21 16h-4a1 1 0 0 0-1 1v4" />
  </Icon>
);

export const IconFilter = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3 5h18l-7 8v6l-4 2v-8z" />
  </Icon>
);

export const IconLink = (p: IconProps) => (
  <Icon {...p}>
    <path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" />
    <path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" />
  </Icon>
);

export const IconShield = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 3 4.5 6v6c0 4.5 3.2 8.3 7.5 9.5 4.3-1.2 7.5-5 7.5-9.5V6z" />
    <path d="m9 12 2 2 4-4" />
  </Icon>
);

/** 归属对账（iconfont 1024 视口）：一个人形 + 下方的循环箭头，取「核对备份
 *  归档里的 [owner:x] 是否与现状一致」的意思。仅给备份页的「归属对账」入口用。 */
export const IconOwnerDrift = ({ size = 18, ...rest }: IconProps) => (
  <svg
    viewBox="0 0 1024 1024"
    width={size}
    height={size}
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    {...rest}
  >
    <path d="M741.44 339.008a240.96 240.96 0 0 0-241.28-233.6 240.96 240.96 0 0 0-137.216 439.04l7.232 4.736a391.68 391.68 0 0 0-261.568 369.408H168.832a331.264 331.264 0 0 1 331.328-331.328l14.72-0.384a240.96 240.96 0 0 0 226.56-247.872z m-422.016 7.36a180.736 180.736 0 1 1 361.408 0 180.736 180.736 0 0 1-361.408 0z m523.52 235.392h-71.552l40.384 80.768H567.68v64h347.648l-72.32-144.768z m-202.88 336.832h71.616l-40.448-80.832h244.16v-64H567.68l72.448 144.832z" />
  </svg>
);

/** 盾牌 + 对勾的实心字形（iconfont 1024 视口）：用于「安全基线」菜单。
 *  与描边版 IconShield 区分开 —— 后者在告警、防火墙、备份等几十处都在用，
 *  这里只给安全基线换上这枚更「体检通过」意味的标志。 */
export const IconShieldCheck = ({ size = 18, ...rest }: IconProps) => (
  <svg
    viewBox="0 0 1024 1024"
    width={size}
    height={size}
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    {...rest}
  >
    <path d="M867.584 160.192c-149.632-16.928-262.208-57.408-334.592-120.352l-19.04-16.544-20.544 14.656C379.968 118.944 267.776 160 160 160H128v448c0 137.344 121.088 261.92 370.208 380.864l13.088 6.24 13.344-5.728C771.072 883.52 896 755.232 896 608V163.424l-28.416-3.232zM832 608c0 116.8-107.392 223.36-319.328 316.8C299.872 821.024 192 714.464 192 608V222.976c104.672-6.784 211.584-46.688 318.496-118.944C587.232 162.528 695.168 201.536 832 220.256V608z" />
    <path d="M359.776 468.672a32 32 0 1 0-47.968 42.4l121.792 137.824c12.608 14.24 30.176 21.568 47.904 21.568a64.384 64.384 0 0 0 49.696-23.52l197.6-242.72a32 32 0 0 0-49.632-40.416l-197.6 242.688-121.792-137.824z" />
  </svg>
);

/** 证书字形（iconfont 1024 视口）：一张带文字行的证书 + 右下角的蜡封徽章。
 *  比通用盾牌更能说明这一页管的是 HTTPS 证书的签发与续期。 */
export const IconCertificate = ({ size = 18, ...rest }: IconProps) => (
  <svg
    viewBox="0 0 1024 1024"
    width={size}
    height={size}
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    {...rest}
  >
    <path d="M886.4 132.288c38.4 0 69.312 31.552 69.312 70.4v554.624c0 38.848-30.976 70.4-69.312 70.4l-122.752-0.064 0.064 100.352a27.712 27.712 0 0 1-29.824 27.52l-5.376-0.832-4.928-1.92-70.08-37.888-75.776 37.952a27.84 27.84 0 0 1-39.616-19.584L537.6 928v-100.352l-400 0.064a69.76 69.76 0 0 1-68.928-62.72l-0.384-7.68V202.688c0-38.848 30.976-70.4 69.312-70.4h748.8z m-178.176 621.632l-8.448 3.008c-7.488 2.24-15.36 3.904-23.616 5.12l-12.672 1.28-7.36 0.32-7.296 0.064a176 176 0 0 1-46.336-6.208l-9.472-3.072v128.64l45.248-22.528a27.264 27.264 0 0 1 14.272-2.816l5.12 0.832 5.376 1.92 45.184 22.656v-129.28z m178.176-566.208H137.6c-7.552 0-13.888 6.592-13.888 14.976v554.624c0 8.32 6.336 14.976 13.888 14.976h400.192l1.6-55.36a155.264 155.264 0 0 1-42.752-86.08l-1.344-12.416-0.384-10.432a155.712 155.712 0 0 1 311.488 0c0 41.28-16.128 80-42.624 104.512l-0.128 59.776h122.752a14.08 14.08 0 0 0 13.376-10.944l0.512-4.032V202.688c0-8.32-6.336-14.976-13.888-14.976z m-235.712 320a100.288 100.288 0 0 0-8.96 200.192l11.712 0.32h1.536a100.288 100.288 0 0 0-4.288-200.512zM352 601.6a27.712 27.712 0 1 1 0 55.488H266.688a27.712 27.712 0 0 1 0-55.488H352z m42.688-149.312a27.712 27.712 0 1 1 0 55.424h-128a27.712 27.712 0 1 1 0-55.424h128z m384-149.376a27.712 27.712 0 0 1 0 55.488h-512a27.712 27.712 0 1 1 0-55.488h512z" />
  </svg>
);

/* 处置中心：一把扳手 —— 语义是「动手修」，与只读的体检 / 分析页面区分开 */
export const IconWrench = (p: IconProps) => (
  <Icon {...p}>
    <path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />
  </Icon>
);

export const IconUser = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="12" cy="8" r="4" />
    <path d="M4.5 21c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5" />
  </Icon>
);

export const IconTerminal = (p: IconProps) => (
  <Icon {...p}>
    <rect x="2.5" y="4" width="19" height="16" rx="2" />
    <path d="m6.5 9 3 3-3 3M13 15h4.5" />
  </Icon>
);

export const IconMonitor = (p: IconProps) => (
  <Icon {...p}>
    <rect x="2.5" y="3.5" width="19" height="13" rx="2" />
    <path d="M8 20.5h8M12 16.5v4" />
  </Icon>
);

export const IconKey = (p: IconProps) => (
  <Icon {...p}>
    <circle cx="7.5" cy="15.5" r="4" />
    <path d="m10.5 12.5 8-8M16 5l3 3M14 7l3 3" />
  </Icon>
);

export const IconPlug = (p: IconProps) => (
  <Icon {...p}>
    <path d="M9 2.5v6M15 2.5v6" />
    <path d="M6 8.5h12v3a6 6 0 0 1-6 6 6 6 0 0 1-6-6z" />
    <path d="M12 17.5v4" />
  </Icon>
);

export const IconLayers = (p: IconProps) => (
  <Icon {...p}>
    <path d="m12 2.5 9 5-9 5-9-5z" />
    <path d="m3 12.5 9 5 9-5M3 17l9 5 9-5" />
  </Icon>
);

/* 容器（LXC）：六边箱体。与 IconVm 的「显示器」形成一眼可辨的对照，
   侧边栏与页面标题共用同一个符号。 */
export const IconBox = (p: IconProps) => (
  <Icon {...p}>
    <path d="M12 2.8 20.5 7v10L12 21.2 3.5 17V7z" />
    <path d="M3.5 7 12 11.4 20.5 7" />
    <path d="M12 11.4v9.8" />
  </Icon>
);

/** 内网穿透（frp）字形（iconfont 1024 视口）：一条左右贯穿的隧道 + 两侧端点。
 *  与「连接管理」用的插头 IconPlug 区分开 —— 插头讲的是「接上」，这里讲的是「穿出去」。
 *
 *  关于视口：原始字形（路径实占 72.7~951.2 × 160~885.4，中心 511.95/522.7）在
 *  0 0 1024 1024 里只占 86% 宽、71% 高，而本站其余图标画在 24 视口里、描边后约
 *  占 80% —— 同尺寸摆进侧边栏时这一枚会明显「缩一圈」。
 *
 *  所以这里不放大 size（那会让它和相邻图标对不齐），而是把视口裁到字形的等距
 *  外框（930×930，四周留白一致）：宽占 94.5%、高占 78%，与描边图标的视觉大小
 *  对齐。视口仍是正方形且原样保留宽高比（preserveAspectRatio 默认 xMidYMid
 *  meet），任何尺寸下都只做等比缩放，不会被压扁或拉长。 */
export const IconFrp = ({ size = 18, ...rest }: IconProps) => (
  <svg
    viewBox="47 58 930 930"
    width={size}
    height={size}
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    {...rest}
  >
    <path d="M586.7 160v330.7h242L717.4 379.4l45.2-45.3 188.6 188.6-188.6 188.5-45.2-45.2 111.3-111.3h-242v330.7H288V661.3h64v160h170.7V554.7h-450v-64h450V224H352v159.1h-64V160z" />
  </svg>
);

export const IconCloud = (p: IconProps) => (
  <Icon {...p}>
    <path d="M17.5 18.5h-11A4 4 0 0 1 6 10.6a5.5 5.5 0 0 1 10.4-1.5 4.5 4.5 0 0 1 1.1 9.4z" />
  </Icon>
);

export const IconSave = (p: IconProps) => (
  <Icon {...p}>
    <path d="M4 4.5h11l5 5v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1v-14a1 1 0 0 1 1-1z" />
    <path d="M8 4.5v5h6v-5M8 20.5v-6h8v6" />
  </Icon>
);

export const IconFolder = (p: IconProps) => (
  <Icon {...p}>
    <path d="M3.5 6.5a1 1 0 0 1 1-1h4.2l1.8 2.2h8a1 1 0 0 1 1 1v9.3a1 1 0 0 1-1 1h-14a1 1 0 0 1-1-1z" />
  </Icon>
);

export const IconLock = (p: IconProps) => (
  <Icon {...p}>
    <rect x="4.5" y="10.5" width="15" height="10.5" rx="2" />
    <path d="M8 10.5V7.5a4 4 0 0 1 8 0v3" />
    <path d="M12 15v2.5" />
  </Icon>
);

/** 铃铛：用于「监控告警」，与三角警示 IconAlert 区分开 */
export const IconBell = (p: IconProps) => (
  <Icon {...p}>
    <path d="M18 8.5a6 6 0 1 0-12 0c0 6.5-2.5 8.5-2.5 8.5h17S18 15 18 8.5" />
    <path d="M13.7 20.5a2 2 0 0 1-3.4 0" />
  </Icon>
);

/** 对话气泡：用于「飞书机器人」这类会话式入口 */
export const IconChat = (p: IconProps) => (
  <Icon {...p}>
    <path d="M20.5 14.5a2 2 0 0 1-2 2H8l-4.5 4V5.5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z" />
    <path d="M8.5 8.5h8M8.5 12h5" />
  </Icon>
);

/** 飞书（Lark）标志：直接采用官方图标字形（iconfont 1024 视口的实心路径）。
 *
 *  侧栏的「飞书 Bot」是面板唯一的外部 IM 通道，通用聊天气泡看不出接的是
 *  飞书还是别家。字形是实心的，尺寸仍按 18px 走，fill 用 currentColor，
 *  深浅主题下与相邻菜单图标同色。
 */
export const IconFeishu = ({ size = 18, ...rest }: IconProps) => (
  <svg
    viewBox="0 0 1024 1024"
    width={size}
    height={size}
    fill="currentColor"
    aria-hidden="true"
    focusable="false"
    {...rest}
  >
    <path d="M428.141714 367.908571q67.510857 72.411429 106.496 143.872-77.494857 70.326857-171.958857 70.326858c-0.987429 0-1.974857 0-2.925714 0.109714-101.558857-77.165714-191.085714-173.641143-239.250286-243.712a42.678857 42.678857 0 0 0-71.753143 46.153143v387.913143l13.348572 10.971428q60.635429 49.956571 139.958857 83.785143 102.217143 43.593143 203.154286 43.885714 186.075429 0.585143 322.486857-115.346285 15.469714-13.165714 29.513143-27.721143c3.657143-1.828571 7.241143-3.803429 10.825142-5.851429 11.849143-6.912 19.163429-18.578286 20.918858-31.195428q20.992-27.684571 37.668571-59.355429 22.016-41.691429 48.493714-120.393143 20.772571-61.696 33.901715-85.577143 18.907429-34.267429 47.177142-49.664l64-34.925714-66.267428-30.427429-3.84-1.755428q-80.676571-37.302857-147.821714-27.721143-27.904 3.949714-55.917715 14.994286-48.201143-137.508571-133.156571-214.089143l-10.459429-9.435429H159.963429l61.330285 62.244572q20.297143 20.589714 73.947429 67.620571 88.429714 77.531429 132.900571 125.257143z m303.725715 317.44c-2.267429 0.804571-4.461714 1.828571-6.582858 3.072-38.253714 22.125714-85.430857 25.782857-140.873142 12.288-42.678857-10.386286-88.283429-30.537143-134.4-57.563428q96.768-27.904 175.177142-120.100572 99.364571-116.845714 187.392-129.353142 25.088-3.584 54.857143 3.291428-12.324571 15.104-22.454857 33.536-16.310857 29.586286-39.168 97.499429-24.649143 73.142857-43.885714 109.604571-13.385143 25.417143-30.061714 47.689143z m-126.756572 105.910858q-89.453714 47.177143-199.68 46.811428-86.089143-0.256-174.665143-38.034286-61.001143-25.965714-108.873143-62.390857v-260.388571c63.963429 72.009143 148.443429 149.942857 238.336 210.176 65.024 43.593143 135.058286 79.396571 204.032 96.182857 13.531429 3.291429 27.209143 5.888 40.850286 7.68z m75.995429-419.657143q-46.957714 32.219429-94.208 84.224-41.179429-69.12-105.216-137.801143-47.030857-50.468571-138.24-130.413714l-1.938286-1.718858H574.171429q67.291429 66.377143 106.934857 185.709715z" />
  </svg>
);

/** 信封：邮件通道（在此之前邮件相关的地方都在借铃铛用） */
export const IconMail = (p: IconProps) => (
  <Icon {...p}>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <path d="m4.2 7.6 6.9 4.8a1.6 1.6 0 0 0 1.8 0l6.9-4.8" />
  </Icon>
);
