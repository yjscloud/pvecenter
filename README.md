# pvecenter — Proxmox VE 8.x / 9.x 管理面板（PVE Web UI）

**简体中文 | [English](README.en.md)**

[![License](https://img.shields.io/github/license/yjscloud/pvecenter?color=blue)](LICENSE) [![Release](https://img.shields.io/github/v/release/yjscloud/pvecenter?sort=semver&color=success&label=release)](../../releases) [![CI](https://github.com/yjscloud/pvecenter/actions/workflows/ci.yml/badge.svg)](https://github.com/yjscloud/pvecenter/actions/workflows/ci.yml) [![Stars](https://img.shields.io/github/stars/yjscloud/pvecenter?style=flat)](../../stargazers) [![Forks](https://img.shields.io/github/forks/yjscloud/pvecenter?style=flat)](../../forks) [![Issues](https://img.shields.io/github/issues/yjscloud/pvecenter)](../../issues) ![Proxmox VE](https://img.shields.io/badge/Proxmox%20VE-8.x%20%2F%209.x-e57000) ![Python](https://img.shields.io/badge/python-3.11%2B-3776ab) ![React](https://img.shields.io/badge/react-19-61dafb)

**pvecenter** 是一个开源自托管的 **Proxmox VE（PVE）Web 管理面板**。一套界面管 QEMU 虚拟机、LXC 容器、cloud-init 模板、存储与网络、快照备份、内网穿透、VNC 控制台、证书和账号权限，还能同时接管多台 PVE。装成 systemd 服务或用 Docker 跑，机器重启后自己起来。

> Self-hosted **Proxmox VE (PVE) web UI** for QEMU VMs, LXC containers, cloud-init templates,
> networking, firewalls, backups, monitoring, certificates and RBAC.

[在线演示](https://pvecenter.yjscloud.com) · [界面预览](#界面预览) · [核心功能](#核心功能) ·
[快速开始](#一部署) · [常见问题](#六排查问题) · [English](README.en.md)

适合家庭实验室（homelab）、小团队，以及要把 PVE 开放给多人用的场景。

> 关键词：Proxmox VE 管理面板 · PVE 面板 · PVE Web UI · LXC 容器管理 · QEMU / KVM ·
> cloud-init 模板 · 自托管虚拟化平台 · Proxmox alternative UI · homelab · self-hosted

## 联系与反馈

遇到问题、想提建议，或者需要部署协助，扫码加微信；也可以到
[Issues](https://github.com/yjscloud/pvecenter/issues) 留言。部分功能还没经过充分测试，遇到问题直接说。

pvecenter 由个人维护。它帮你省下时间的话，欢迎自愿赞赏，用于服务器、测试环境和后续维护；不赞赏也完全不影响使用和技术支持。

<table>
  <tr>
    <td align="center" width="33%"><img src="docs/screenshots/wechat.png" alt="微信联系方式：问题反馈与部署协助" width="180" height="257"><br><sub><b>微信联系</b> · 问题反馈 / 部署协助</sub></td>
    <td align="center" width="33%"><img src="docs/screenshots/weixin.jpg" alt="微信赞赏码：自愿支持项目维护" width="180" height="257"><br><sub><b>微信赞赏</b> · 自愿支持</sub></td>
    <td align="center" width="33%"><img src="docs/screenshots/zhifubao.jpg" alt="支付宝赞赏码：自愿支持项目维护" width="180" height="257"><br><sub><b>支付宝赞赏</b> · 自愿支持</sub></td>
  </tr>
</table>

## 界面预览

> 截图统一放在 `docs/screenshots/`，建议 **1600×900**（16:9）。下面的文件名可直接替换；文件不存在时 GitHub 显示替代文字，放进去就自动显示。

<!--
截图占位清单（放入 docs/screenshots/ 后自动显示）：
vm-list.png / vm-create.png / lxc-list.png / template-pipeline.png / firewall.png /
ssh-security.png / security-baseline.png / ai-assistant.png / monitoring.png /
backup.png / console.png / audit-log.png / certificates.png / users-rbac.png
-->

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/dashboard.png" alt="pvecenter 监控仪表盘：节点状态、CPU / 内存 / 存储与最近任务"><br><sub><b>监控仪表盘</b> · 节点状态 / 资源占用 / 最近任务</sub></td>
    <td width="50%"><img src="docs/screenshots/vm-list.png" alt="Proxmox 虚拟机列表：批量开关机、迁移与配额"><br><sub><b>虚拟机列表</b> · 批量操作 / 迁移 / 配额</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/lxc-list.png" alt="LXC 容器管理：生命周期、快照、克隆与迁移"><br><sub><b>LXC 容器</b> · 生命周期 / 快照 / 迁移</sub></td>
    <td width="50%"><img src="docs/screenshots/console.png" alt="浏览器内 noVNC 控制台：无需把 PVE 8006 暴露到公网"><br><sub><b>VNC 控制台</b> · 浏览器内直接操作</sub></td>
  </tr>
</table>

## 核心功能

- **虚拟机**：四步 / 批量创建、开关机、克隆、迁移、在线改配、扩容、快照、重装系统；OVF / OVA 导入，VMDK / QCOW2 / RAW / OVA 导出
- **LXC 容器**：创建、启停、快照、克隆、迁移、rootfs 扩容、控制台
- **cloud-init 模板**：从 cloud 镜像或下载链接一键建模板，克隆时注入主机名、SSH 公钥和 IP
- **多套 PVE**：跨连接汇总节点、虚拟机、存储和任务，可只盯一套集群，也可按区域分发管理权
- **存储**：内容浏览、上传（按文件头校正后缀）、让 PVE 自己从链接下载、删卷、容量与使用率
- **网络与防火墙**：Bridge / Bond / VLAN、IP 地址池、PVE 原生防火墙、安全组、IP 集合
- **内网穿透**：内置 frpc，把虚机 / 容器端口映射到公网，端口区间、目标白名单、按人授权
- **监控与告警**：实时指标、RRD 曲线、容量预测、磁盘 SMART / ZFS；通知走邮件、飞书、Webhook 和站内信
- **备份恢复**：立即 / 定时备份、浏览备份卷、恢复到指定 VMID、防删保护、完整性核对与 PBS 校验状态；可选只读接入 PBS，看仓库容量、GC / 校验任务与磁盘健康
- **证书**：腾讯云 / 阿里云 / 华为云 / ACME / 托管证书，自动续期，部署到本机、SSH 主机、PVE 虚拟机 / 容器 / 节点
- **安全运维**：SSH 防爆破、登录审计、安全基线、端口与进程巡检、应急响应
- **AI 排查助手**：拿巡检数据做归因，只读命令白名单，写操作逐条审批
- **账号权限**：内置角色 + 自定义角色、按用户 / 按角色的额度、节点可见范围、两步验证、登录设备管理、个人 API 令牌
- **日常**：后台作业、任务队列、站内通知、`Ctrl / ⌘ + K` 命令面板、一键更新、站点品牌与入口开关

## 快速开始

装成 systemd 服务，机器重启自己起来：

```bash
git clone https://github.com/yjscloud/pvecenter.git
cd pvecenter
sudo ./deploy.sh
```

最后一屏会打印面板地址和初始口令，口令只显示这一次。想跑容器看 [Docker 部署](#docker-部署)，两种方式都写在 [一、部署](#一部署) 里。

技术栈：Python 3.11+ / FastAPI / httpx，React 19 + TypeScript + Vite，MySQL 8。

## 目录

- [界面预览](#界面预览) · [核心功能](#核心功能)
- [一、部署](#一部署)：[准备 Token](#准备-proxmox-api-token) · [授权](#授权) ·
  [裸机部署](#裸机部署推荐) · [Docker 部署](#docker-部署) · [上 HTTPS](#上-https) ·
  [服务管理](#服务管理)
- [二、功能](#二功能) · [三、配置项](#三配置项) · [四、项目结构](#四项目结构) ·
  [五、测试](#五测试) · [六、排查问题](#六排查问题) · [七、安全说明](#七安全说明) ·
  [八、规划](#八规划)

---

## 一、部署

### 准备 Proxmox API Token

面板靠 API Token 跟 Proxmox 通信。在 PVE 上执行：

```bash
pveum user add panel@pve --comment "pvecenter panel"
pveum user token add panel@pve panel --privsep 0
```

输出里的 `full-tokenid`（形如 `panel@pve!panel`）填 Token ID，`value` 填 Token Secret。

### 授权

```bash
pveum acl modify / --user panel@pve --roles PVEVMAdmin,PVEDatastoreUser,PVESDNUser,PVEAuditor,PVEVMUser
```

想按最小权限来，就照着用途拆开：

| 功能 | 角色 |
|---|---|
| 只看监控 | `PVEAuditor` |
| 开关机、控制台 | `PVEVMUser` |
| 创建 / 删除 / 改配置 / 快照 | `PVEVMAdmin` |
| 备份、ISO 与模板存储 | `PVEDatastoreUser`（要写用 `PVEDatastoreAdmin`） |
| 网桥 / VLAN | `PVESDNUser` |

> 控制台得另填一个 PVE 账号密码。Proxmox 不让 API Token 访问 `vncproxy`，只认用户密码换来的 ticket。不填只是控制台用不了，其他功能照常。

### 裸机部署（推荐）

支持 systemd 的发行版（Ubuntu / Debian / CentOS / Rocky / Alma / Fedora）都能用。装的是 systemd 服务，开机自启、进程挂了自动拉起，改完代码 `systemctl restart` 就生效。面板和数据库都跑在宿主机上，长期用最省心。

```bash
git clone https://github.com/yjscloud/pvecenter.git
cd pvecenter
sudo ./deploy.sh
```

一次装完 Python 依赖、前端产物、数据库和 systemd 服务，全程不提问：端口 `8080`、库名 `pvecenter_panel`、账号 `pvecenter`，`SECRET_KEY` 和管理员口令随机生成，最后一屏打印地址和口令。重复执行不会弄坏已有配置。

要自己指定参数就直接传，同样不提问：

```bash
sudo ./deploy.sh --port 9000 --mysql-root-password '<root 口令>' \
                 --db-name pvecenter_panel --db-user pvecenter --db-password '<库口令>'
```

常用参数：`--port` 换端口，`--service` 换服务名，`--user` 换运行用户（默认 `root`），`--skip-frontend` 复用现有 `dist/`，`--no-systemd` 只准备环境（无需 root），`--reconfigure` 逐项确认，`--help` 看全部。

> 本机构建前端要 Node.js **22.22+**（`react-router 8` 的下限，vite 8 自己也要 22.12+）。版本不够时 `deploy.sh` 会自己补：先用发行版仓库装（RHEL 9 会切 `nodejs:22` 模块流），还不行就从 npmmirror / nodejs.org 下官方二进制，解压到仓库下的 `.node/` 再构建 —— 不改系统源、不动系统里已有的 node，删目录即卸载，重复执行直接复用。想钉版本：`NODE_BOOTSTRAP_VERSION=22.22.1 sudo ./deploy.sh`。三条都走不通（Alpine、完全离线、架构不在支持列表）时脚本才会停下给手工方案：NodeSource / nvm、在别的机器 `npm run build` 后拷 `dist/` 过来加 `--skip-frontend`、或改用 Docker。宿主机一个 Node 都不装也能跑，只要 `dist/` 是新的。

> 「立即更新」要联网拉源码包，国内连 `github.com` 经常卡。0.3.2 起加了连接超时和总时长，失败直接报错而不是一直挂着。连不上就在 `systemctl edit pvecenter` 里写 `Environment=PC_UPDATE_MIRROR=https://ghfast.top/` 后重试，或者下源码包手工升级 / 改用 Docker。

> `SECRET_KEY` 生成之后别再改，换了它，库里已存的 PVE Token、SMTP 口令全都解不开。`.env` 已存在时，脚本只覆盖命令行上显式给出的键。

想前台跑着看日志（不装服务）：`npm run build && ./start-prod.sh`。`dist/` 存在时 FastAPI 会在 8080 同时提供前端页面和 `/api`，前后端同源，VNC 控制台的 WebSocket 直接可用。

### Docker 部署

不想在宿主机装 Python / Node / MySQL 就用容器，宿主机只要有 Docker：

```bash
curl -fsSLO https://raw.githubusercontent.com/yjscloud/pvecenter/main/docker-compose.yml
docker compose up -d
```

打开 `http://你的服务器IP:8080`，账号 `admin`，口令 `pvecenter@2026`。这个口令得改。

三个口令都在 `docker-compose.yml` 里，搜 `★ 改这里`。口令行长这样：

```yaml
ADMIN_PASSWORD: ${ADMIN_PASSWORD:-pvecenter@2026}
```

`:-` 是 compose 的「取默认值」记号：`.env` 或环境变量给了就用那个，没给才用后面这段。`-`、`$`、`{`、`}` 都只是语法，真正的口令是 `pvecenter@2026`，改的时候只动它：

```yaml
ADMIN_PASSWORD: ${ADMIN_PASSWORD:-MyPassw0rd2026}
```

要改的是 `ADMIN_PASSWORD`（面板登录）、`DB_PASSWORD`（连库）、`MYSQL_ROOT_PASSWORD`（MySQL 管理员）。也可以不碰这个文件，在同目录放个 `.env`，等号后直接写口令，优先级更高。

两个坑先说在前面：

- 口令只在第一次初始化时生效。`ADMIN_PASSWORD` 只在库里还没有管理员时用来建号，MySQL 也只在数据目录为空时读 `MYSQL_PASSWORD`。跑起来之后再改，要么 `docker compose down -v` 重建（数据会没），要么进库 `ALTER USER`（命令在 `docker-compose.yml` 末尾）。改登录口令请登录后在「个人中心 → 修改密码」改。
- `SECRET_KEY` 别动。它既是 JWT 签名密钥，也是库里密文（PVE Token、SMTP 口令）的加密根。写死在公开仓库等于把钥匙公示，所以容器首次启动随机生成一份存进 `panel_data` 卷；已有数据后改掉它，那些密文就都解不开了。

镜像默认构建 amd64（PVE 本身只有 x86_64），从 GHCR 拉。换 Docker Hub：`PVECENTER_IMAGE=docker.io/yjscloud/pvecenter:latest docker compose up -d`。升级：`docker compose pull && docker compose up -d`。

### 上 HTTPS

面板自己不做 TLS。要暴露到公网，用 Nginx 或 Caddy 在 443 终结，反代到 8080。WebSocket 必须一起代理，否则控制台连不上：

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;
    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;
    # 安全响应头（HSTS / CSP / nosniff / Referrer-Policy / Permissions-Policy）
    # 由面板自己下发，这里**不要再加一遍**：重复的同名头属于无效响应。
    # 反代所在层若还有别的站点，按需在此统一加，但别和面板重复。
    server_tokens off;          # 响应头只留 “Server: nginx”，不带版本号

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;        # 控制台要用的两条
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;    # 让面板知道外面已是 https
        proxy_read_timeout 3600s;                      # 控制台是长连接
        proxy_buffering off;
    }
}
```

面板这边同时打开强制 HTTPS，不然直连 8080 还是明文：

```
FORCE_HTTPS=true
FORWARDED_ALLOW_IPS=127.0.0.1
```

`FORWARDED_ALLOW_IPS` 只填反代地址，别放宽 —— 放宽之后谁都能伪造 `X-Forwarded-Proto: https` 绕过跳转。Docker 部署把这两项写进 `docker-compose.yml` 同目录的 `.env`（或直接写进 compose 的 `environment:`）。

### 公网暴露加固（强烈建议）

面板是 PVE 的全权管理入口：一个账号失守，等于整个虚拟化平台失守（开关机、删机、改密码、VNC 控制台、宿主机网络与防火墙都在里面）。FAQ 里那句「只有面板自身需要对外」说的是功能设计，不是安全边界，公网暴露时必须额外设防。

**下面这些开关都不在界面里**，改的是配置文件，路径按部署方式区分：

| 部署方式 | 要改的文件 | 说明 |
|---|---|---|
| 源码 / 裸机（`deploy.sh`、systemd） | `<安装目录>/backend/.env` | 由 `deploy.sh` 生成（模板 `backend/.env.example`）。本机部署即 `/data/pvecenter/backend/.env`；`--reconfigure` 会逐项提问并写回 |
| Docker Compose | 与 `docker-compose.yml` 同目录的 `.env` | compose 用 `${变量:-默认值}` 读取，也可以直接改 `docker-compose.yml` 的 `environment:` |
| 只装了后端（`start-prod.sh` / 自建 systemd） | 启动进程读取的同一份 `backend/.env` | 用 `systemctl cat pvecenter` 看 `EnvironmentFile=` / `WorkingDirectory=` 定位 |

改完必须重启：`systemctl restart pvecenter`，Docker 用 `docker compose up -d`。界面「设置 → 安全姿态」里同名条目的说明也是这个路径。

1. **首选收敛入口**：把面板放到 VPN / 零信任网关之后，或只允许固定办公网、跳板机访问。frp 场景在 frp 服务端做来源限制，或在反代上启用 mTLS。
2. **来源白名单（面板自己也会拦）**：配 `PANEL_ALLOW_CIDRS`，非名单来源连登录页和 `/api/openapi.json` 都看不到（HTTP 与 VNC WebSocket 一起拦）。
   ```
   PANEL_ALLOW_CIDRS=203.0.113.0/24,10.8.0.0/24,192.168.1.10
   ```
   反代部署时 `FORWARDED_ALLOW_IPS` 必须填反代的**实际地址**，否则白名单看到的是反代 IP。也可以直接在 Nginx 上做，二选一或叠加：
   ```nginx
   allow 203.0.113.0/24;
   allow 10.8.0.0/24;
   deny  all;
   ```
   前面还有 CDN / 云 WAF 的话，记得用 `set_real_ip_from` + `real_ip_header` 还原真实来源，否则 `$remote_addr` 全是 CDN 地址。
3. **管理员强制 2FA**：`TOTP_REQUIRED_ROLES=admin`。管理员没绑 TOTP 之前，除 2FA 接口外一律 403（可在「个人中心」绑定）。登录失败锁定（账号 + IP 双维度）和人机验证公网环境别关；人机验证对**登录与自助注册同时生效**，否则注册接口会被脚本拿去刷审批队列。
4. **新 IP 登录告警**：默认开（`LOGIN_IP_ALERT=true`）。某账号第一次从新 IP 登录成功时，所有管理员收到站内消息；配了 SMTP 再发一封邮件。审计里对应 `auth.new_ip`。
5. **别把 8080 裸奔到公网**：Docker 部署建议 `PANEL_BIND=127.0.0.1`，只让同机反代连。`FORWARDED_ALLOW_IPS` 绝不要填 `*` —— 客户端能伪造来源 IP，白名单、登录锁定、审计 IP 会一起失真。
6. **定期审计**：`/api/audit`、`/api/host-audit` 默认只有管理员可读。重点看登录失败、`auth.new_ip`、用户 / 权限变更，以及删机、改凭据这类敏感操作。
7. **接口文档默认关闭**：`/api/docs`、`/api/redoc`、`/api/openapi.json` 一律 404。完整 OpenAPI 等于一份全量攻击地图。排障确需查看时用 `API_DOCS_ENABLED=true` 临时打开，打开后这三个地址仍要求「已登录 + `settings.manage`」。
8. **`/api/health` 对匿名只答「活着」**：未登录只返回 `{"status":"ok"}`，不再给 PVE 版本、节点数和连接状态（版本号能直接对公开漏洞库）。登录后仍是完整读数，深度诊断走 `/api/config/diagnostics`（管理员）。
9. **安全响应头由面板统一下发**：CSP（普通页面 `script-src 'self'`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`Permissions-Policy`、`frame-ancestors 'none'`、`X-Frame-Options: DENY` 和 HSTS 都在应用层加（`backend/app/main.py` 的 `SecurityHeadersMiddleware`）。反代只做 TLS 终结，不要重复添加同名头。
10. **面板→PVE 默认不校验证书**：新建连接的 `verify_ssl` 默认 `false`（`PVE_VERIFY_SSL` 也是）。PVE 出厂是自签证书（cluster CA 签发、`CN=pve.local`、无 IP SAN），默认开着校验会在握手阶段失败，很容易误判成地址或令牌填错。要校验就别只勾开关：把 PVE 证书 PEM 填进连接的「证书 PEM」（`ca_cert`），按该证书钉扎 —— 只信这一张，比系统信任库更严，也不受那个开关影响。取证书：`openssl s_client -connect <PVE-IP>:8006 </dev/null 2>/dev/null | openssl x509`，或从任意节点 `cat /etc/pve/pve-root-ca.pem`。正规 CA 签发（或已钉扎）时再把开关打开。连接未校验时启动日志会告警。
11. **挂了反代就把后端绑回回环**：`HOST=127.0.0.1` 时 8080 不对内网开放，全部入口经 nginx（默认安装是 `0.0.0.0`，可直接用 `IP:8080`）。反代侧再配 `client_header_timeout` / `client_body_timeout` / `send_timeout` 与 `limit_conn`，抵御慢速连接耗尽 worker。
12. **API Token 有效期上限 365 天**，不支持「永不过期」：令牌是无人值守凭据，泄漏后靠到期兜底。长期自动化就在到期前重新签发（列表里能看到剩余天数）。
13. **不回显组件版本**：应用不再发 `Server` / `X-Powered-By`（`run.py` 的 `server_header=False` + 安全头中间件兜底）；nginx 侧用 `server_tokens off;` 只留 `Server: nginx`。
14. **生产不发布 sourcemap**：`vite.config.ts` 里 `build.sourcemap=false` —— `.map` 的 `sourcesContent` 就是完整 TS 源码。nginx 再对 `*.map` 返回 404 兜底，防止误传旧产物。要排障用 `npm run dev`。
15. **版本与 RBAC 清单需登录**：`/api/version`、`/api/auth/permissions` 已加登录依赖；只有 `/api/config/site`（站点名、Logo、备案号）保持匿名，供登录页与官网渲染。

需要客户端证书（mTLS）时，在 Nginx 的 server 块里加：

```nginx
ssl_client_certificate /etc/nginx/client-ca.crt;
ssl_verify_client on;   # 或 optional + 下面的 if 判断
# if ($ssl_client_verify != SUCCESS) { return 403; }
```

### 服务管理

`./deploy.sh` 装的是 systemd 服务，开机自启、进程挂了自动重启：

| 操作 | 命令 |
|---|---|
| 状态 / 启停 | `systemctl status\|start\|stop\|restart pvecenter` |
| 开机自启 | `systemctl enable pvecenter` |
| 跟随日志 | `journalctl -u pvecenter -f` |

改完代码，只动前端 `npm run build` 就够，动后端要 `systemctl restart pvecenter`。确认服务起来了：`curl http://127.0.0.1:8080/api/health`。

> Docker 部署这节可以跳过，容器本身带 `restart: unless-stopped`。

---

## 二、功能

### 虚拟机管理（QEMU / KVM）

四步创建向导（VMID、系统、磁盘与网卡、cloud-init），支持空白新建、从模板克隆、从 cloud 镜像导入。开关机（优雅关机会超时，兜底强制停止）、重启、挂起、恢复、删除；在线改 CPU / 内存 / 名称 / 标签 / 开机自启 / 保护模式，磁盘扩容、迁存储、跨节点迁移。列表能勾选多台批量执行，逐台回报，第 3 台失败不影响其余，也能只重试失败的。重置客户机用户口令优先走 Guest Agent（即时生效），没装 agent 或机器已关机就写 `cipassword` 走 cloud-init，并重新生成 config drive。

批量创建走同一个接口（`POST /api/vms/batch`），一次提交多台、逐台推进到成功或失败，失败项不自动重试。除了从模板 / 镜像创建，还能从 **OVF / OVA 导入**（上传 → 解析 OVF → 核对存储、网桥与总线，固件、控制器、EFI、串口这类不兼容项逐条提示）；**导出**走后台作业，支持 VMDK / QCOW2 / RAW / OVA，只允许导已关机的机器，产物直接流式下载；**重装系统**用 cloud-init 模板重建系统盘，保留 VMID、MAC、CPU 与磁盘拓扑，网络默认沿用原地址。

<!-- 截图占位：docs/screenshots/vm-create.png -->
![虚拟机四步创建向导：系统、磁盘与网卡、cloud-init](docs/screenshots/vm-create.png)

### LXC 容器管理

容器在 PVE 里是另一套 API 端点，面板里也是平行的一套页面。三步创建向导，模板来自节点上内容类型含 `vztmpl` 的存储（一个都没有时会给出 `pveam download` 命令）。生命周期、快照（不含内存状态）、克隆（只有全量，PVE 不支持容器链接克隆）、迁移、控制台；rootfs 和挂载点扩容、新增挂载点、迁存储；网卡增删，IP 直接写在网卡配置里，改完要重启。

<!-- 截图占位：docs/screenshots/lxc-list.png -->
![LXC 容器管理：生命周期、快照与迁移](docs/screenshots/lxc-list.png)

重置容器 root 口令走「SSH → 受管主机」。容器没有 Guest Agent，PVE 也没有「在容器里执行命令」的接口，只能借该宿主机的 SSH 凭据跑 `pct exec`。宿主没配凭据时弹窗会说明，并建议你进容器控制台自己 `passwd`。

### cloud-init 模板流水线

从 cloud 镜像一键建模板，后端按序执行、每步之间等任务完成：建虚拟机（系统盘用 `import-from=<镜像>` 让 PVE 直接导入，不再走 PVE 9.2 已移除的 `importdisk`）→ 配置启动顺序与 cloud-init 驱动 → 按需扩容 → 转模板。任何一步失败都会删掉临时虚拟机，不留半成品。建好之后用「从模板部署」批量开机，克隆时自动注入主机名、SSH 公钥和 IP。

<!-- 截图占位：docs/screenshots/template-pipeline.png -->
![cloud-init 模板流水线：从 cloud 镜像到可批量克隆的模板](docs/screenshots/template-pipeline.png)

> **镜像的扩展名得和真实格式对得上**：`.qcow2` / `.raw` / `.vmdk` / `.vdi` / `.vpc` / `.qed`。PVE 只对这几种后缀解析容器，其余一律当裸盘整块复制。典型是 Ubuntu 官方的 `*-cloudimg-amd64.img`：它其实是 qcow2（3.5 GiB 虚拟、248 MiB 实际），被当裸盘复制后，系统盘第一扇区是 `QFI\xfb` 而不是引导签名，模板建得出来、开机却停在 `Boot failed: not a bootable disk`。面板上传时会按文件头识别并改对后缀（会提示）；从 PVE 网页或命令行直接传进来的文件拦不到，请自己改名，或先 `qemu-img convert -O raw 源文件 目标.raw`。
>
> 镜像放 dir / NFS / CIFS 或块存储都行：来源只需该节点读得到，目标存储支持 `images`。
>
> 也可以不手动下载：向导里填「镜像下载链接」，面板把链接交给 **PVE 自己**下载到所选存储的 import 目录（面板不中转字节，几十 GB 的镜像也不会占内存或撑爆反向代理），下完自动选为镜像来源。文件名会校正成 PVE 认的后缀（`.img` → `.qcow2`）。

### 节点与多 PVE 连接

一套面板可以同时接多台 Proxmox：节点、虚拟机、容器、存储、任务和全局搜索都**跨连接聚合**，每行标明来自哪条连接。两条连接上的同名节点、同名存储不会被并成一条 —— 它们是两台机器上各自独立的资源。

连接可以打「区域」标签（机房 / 环境），节点列表按「区域 → 连接 → 节点」展开。顶栏的集群作用域让当前页面只看一套集群（按用户保存，链接带 `?scope=`，只有一套连接时自动隐藏）。连接配置是管理员专属页面，Token 明文不回传前端。

权限也按连接走：内置的 `region_admin` 只能看、只能管指派区域内的资源，没标区域的连接不归任何区域管理员（否则「忘了填区域」就成了谁都能管的缺口）。普通账号还能配节点可见范围：把特殊节点设成「对普通用户隐藏」，再按人 / 按角色放行。判定顺序是用户 > 角色 > 默认 > 无记录即可见，写操作在后端强校验。

节点详情里有磁盘健康卡片：SMART 属性与磨损度、故障属性高亮、ZFS 池状态与碎片。

把机器指派给别人（或收回）时，哪些数据跟着走、哪些不跟，以
[归属与安全数据边界](docs/归属与安全数据边界.md) 为准。

### 存储管理

存储页按节点列出容量与使用率，能浏览 `images` / `iso` / `import` / `vztmpl` / `backup` / `snippets` 内容，上传、删除，或者不落本地，直接填链接让 PVE 去下载。

上传直传 PVE，不占面板内存。传之前读文件头判断容器格式：`iso` 目录里放 qcow2 / vmdk 会被当场拒绝并给出两种改法；`import` 目录会自动把后缀改对（`.img` → `.qcow2`）并告诉你改成了什么 —— 不然后果就是 PVE 按裸盘整块复制，模板开机停在 `not a bootable disk`。下载链接同理（压缩包、后缀不符在开始下载前就拒绝），下载任务只在结束时给结果，所以页面显示已用时间，不编造百分比。

普通用户看到的机器卷按归属裁剪；ISO、容器模板这类共享内容不受影响。

### 网络与防火墙（Bridge / Bond / VLAN）

节点级 Linux Bridge / Bond / VLAN 的增删改，改动先进「待应用」，点「应用配置」才生效，和 Proxmox 本身一致。这样改错网桥不会当场把节点网络断掉。

<!-- 截图占位：docs/screenshots/firewall.png -->
![PVE 网络与防火墙：Bridge / Bond / VLAN、安全组与 IP 集合](docs/screenshots/firewall.png)

防火墙封装的是 Proxmox 原生防火墙，不是另起一套规则引擎，所以面板里配的规则和 PVE 界面、`pve-firewall` 看到的是同一份数据。三个作用域（集群 / 节点 / 虚拟机容器，qemu 与 lxc 自动识别），规则支持出入站、ACCEPT / DROP / REJECT、协议、端口、来源目标（IP、CIDR、`+集合名`）、PVE 预置宏、限定网卡、日志级别与排序。安全组是集群级规则集合，挂一条引用规则就能用上，改组即改所有引用方；IP 集合存可复用地址段；规则模板能一次刷到多台机器（覆盖或追加）并逐台回报，机器网卡没开 `firewall=1` 时会明确提示规则不会生效。权限分 `firewall.view` / `firewall.manage`（自己名下的虚拟机）/ `firewall.cluster`（集群级与安全组，默认仅管理员）三档。

网络上还有 **IP 地址池**：一个池 = 子网 + 可分配范围 + 网桥，已占用地址由各虚拟机的静态配置扫描得出。建机、克隆或改网卡时可以从「分给自己的池」里挑空闲地址，地址和网关自动填好。它不依赖 DHCP 或 SDN；池的增删改仍要 `network.manage`。

### 内网穿透（frp）

面板自带 frpc：填 frps 地址和 token 就能装、启停，后台作业会看着它，挂了自动拉起。规则把「某台虚机 / 容器的 `IP:端口`」映射到公网端口，端口默认限制在 40000–50000，管理员可以按 frps 实际放通的区间改（NAT / 云安全组只开了一段时尤其需要）；22 / 80 / 443 / 7000 / 7001 / 8006 等关键端口始终不可占用，跟区间无关。

越权默认拒绝：普通用户只能映射自己名下的机器，目标还要过白名单（`FRP_ALLOW_LOCAL_CIDRS`，也能按人单独放行某些地址），回环、保留地址和面板自身地址始终拒绝，默认还禁止指向面板所在网段。增删改规则要二次确认；机器改派之后，旧规则会标成 `stale`，不会继续对别人生效。合并视图只下发本人的规则并抹掉 token，日志入口只有管理员能看。

### SSH 登录安全与 fail2ban

读的是面板所在主机的 SSH 日志，集群里其他宿主机的日志读不到，页面会写明数据来自哪台。日志来源按 `/var/log/secure` → `/var/log/auth.log` → `journalctl -u ssh -u sshd` 三级回退，只统计真正的认证失败，按 sshd 的 PID 去重，健康检查和端口扫描的噪声不算。失败来源按 IP 聚合，可一键封禁、标记可信、加忽略名单；fail2ban 的 jail、封禁数、解封与手动封禁都在页面上，自定义策略只写面板自己的 `jail.d/panel-<jail>.local`，不碰 `jail.local`。告警走监控已有的飞书和邮件通道：某 IP 失败超阈值、陌生 IP 登录成功都会通知，攻击停了发恢复通知。

在「受管主机」里填地址、用户、私钥或口令（用 `SECRET_KEY` 加密落库）后，远端也能做同样的事；首次连接要确认 SSH 指纹，之后指纹变了直接拒绝。权限分 `ssh.view`（默认人人可见）与 `ssh.manage`（封禁、改策略，默认仅管理员）。

<!-- 截图占位：docs/screenshots/ssh-security.png -->
![SSH 登录安全：失败来源聚合、fail2ban 封禁与告警](docs/screenshots/ssh-security.png)

### 安全基线与端口巡检

两者共用一个架构。检测对象可以是本机（面板所在主机，直接读系统文件），也可以是受管主机（复用 SSH 通道，在远端跑一条只读命令把数据取回来本地解析）。本地和远端共用同一份判定逻辑，结论口径一致。权限是 `baseline.view` / `baseline.manage` 与 `ports.view` / `ports.manage`，后两个默认仅管理员。

<!-- 截图占位：docs/screenshots/security-baseline.png -->
![安全基线与端口巡检：评分、加固项与可疑进程](docs/screenshots/security-baseline.png)

基线检查覆盖七类：SSH 配置、口令策略、防火墙、时间同步、账号安全（空口令、UID 0）、关键内核参数。评分按严重级别加权（高危 3、中危 2、低危 1）给 A～D 等级，检测不到的项不计入分母，权限不足不会被冤枉扣分。一键加固只写面板自己命名的文件，SSH 改完立刻 `sshd -t` 校验、sysctl 改完复读 `/proc`，校验不过自动回滚。关闭 SSH 口令认证这类可能把人锁在门外的项不纳入一键加固，得逐项确认。

端口巡检用 `ss -tulpn`（老系统退回 `netstat`）列出协议、监听地址、归属进程和暴露范围；监听在非回环地址就算「对外开放」，再结合同一次采集到的防火墙状态，没有活动防火墙时风险级别整体上调。可疑进程走反弹 shell 启发式：`/dev/tcp` 重定向、`nc -e`、`socat exec:`、脚本里的 socket 与 dup2、`curl|sh` 等，再叠加上下文特征（shell 持有对外连接、可执行文件在 `/tmp`、可执行文件已被删除、已知挖矿进程名）。策略里可以配「预期端口」和「进程白名单」正则压误报。

> 得说清楚：端口巡检是启发式，不是杀毒引擎。每条命中都会列出命中了哪条规则，面板也不会据此杀进程。「对外开放」只代表监听在非回环地址，能不能真被访问还看防火墙和上游网络，所以报告里把防火墙状态一并给出，由人判断。集群里的 PVE 节点想体检或巡检，把它当一台受管主机加进来就行，PVE API 没有「在节点本机执行 shell」的接口。

### 应急响应

- 可疑虚拟机一键隔离：先取证快照 → 再断网 → 再关机 → 顺手加 VM 保护。顺序是刻意的，反过来机器一关现场就没了。每步独立执行、单独回报，取证失败不会阻止断网，但会标红。断网是逐张网卡置 `link_down=1`，在原始配置串上增删那一段，`bridge`、`tag`、`macaddr` 原样保留。
- 备份防删：把备份卷登记为受保护，并尽量给 PVE 卷打上 `protected` 旗标，面板的删除接口会拒删受保护的备份。勒索软件就算拿到管理员会话也删不掉，必须先在界面上解除保护。另外定期把登记过的备份和 PVE 实际内容对一遍，不见了或元数据变了就告警。

> 边界：真正的不可变（WORM）只能由存储侧保证（PBS retention、S3 Object Lock、只读挂载），只靠 PVE API 做不到。快照是崩溃一致性的磁盘副本，取不到内存镜像；`link_down` 只切断 PVE 虚拟网卡，PCI 直通 / SR-IOV 网卡不受控制。PVE 不返回存储内容的校验和，所以备份指纹是 `volid|size|ctime` 的哈希，能发现删除和替换，发现不了静默位翻转。

### AI 运维排查助手

把平台已有的巡检结果（安全基线 / 端口与进程 / 登录审计 / 告警 / 变更记录 / 历史指标）交给大模型做归因，回答「先修哪一条」。它不是一次性摘要，而是能追问的会话：选一台受管主机 → 先读平台数据，必要时登录主机跑只读命令 → 边查边答 → 结论落成带 `source_ref` 的报告，每条都指回原始检查项，能自己核对。

<!-- 截图占位：docs/screenshots/ai-assistant.png -->
![AI 运维排查助手：结合巡检数据归因，命令逐条审批](docs/screenshots/ai-assistant.png)

只读是默认档。工具是有限的参数化命令模板，没有「随便执行一条命令」的万能入口，参数有正则与路径白名单，不用 sudo。勾选「授权 AI 在目标主机执行命令」之后，模型才能自己组合只读命令（如 `ps aux | grep nginx`），每一段的首个可执行文件都必须在只读白名单内，且禁止 `;` `&&` `||`、重定向与命令替换，`/etc/shadow` 这类另有黑名单拦截。要改动系统的命令一律先展示、由你逐条批准才执行，`rm -rf /`、`mkfs`、`dd`、关机重启这类即便批准也直接拦下。

远程终端和它共用一个画面：浏览器内的交互式 SSH 终端（xterm.js 按需加载），你敲的命令、AI 跑的命令与输出按发生顺序排在一起，不用另开窗口对照「AI 到底在我机器上做了什么」。数据可见范围跟着权限走（普通用户只看自己名下虚拟机的告警 / 备份 / 巡检结果，SSH 登录分析要 `ssh.view`），每次运行的模型、步数、token 与逐条工具调用都落在「排查记录」里，可回看、可导出 CSV（一行 = 一次工具调用）。另有 Token 消耗入口，按 1 / 7 / 30 天看用量总额、输入输出构成与按模型明细。

需要 `ai.exec`（授权执行命令）与 `ai.terminal`（打开远程终端）两个权限，默认仅管理员。终端等于把该主机的登录凭据交给使用者，助手的执行能力也比其它页面强得多。

> 模型给的是归因建议，不是结论，每条都带 `source_ref` 指向原始检查项，请自行核对。命令白名单与黑名单由后端代码实时生成，「能力说明」里看到的就是执行时校验的同一份规则。授权只对当次会话有效，换主机或新建会话都会重置；写命令的审批有 120 秒超时，超时算拒绝。

### 监控与告警

VM 总数与运行分布、CPU / 内存 / 存储环形图、节点一览、最近任务、资源占用 Top 8；节点实时指标走 `/ws/metrics`（5 秒一推，断线自动重连并回落到 10 秒轮询）；磁盘容量按节点根分区历史线性外推，给日均增长率和预计写满天数；节点与虚拟机各有 RRD 曲线。告警规则可配阈值与持续时间，通知可走邮件、飞书群机器人、通用 Webhook 和站内信（企业微信 / 钉钉 / Slack 用 Webhook 的请求体预设，填好模板即可），触发与恢复各发一条，每个渠道还能按告警来源单独停推。告警邮件发不出去时按 [告警邮件排查手册](docs/告警邮件排查手册.md) 逐层定位，别在 SMTP 上盲试。

<!-- 截图占位：docs/screenshots/monitoring.png -->
![监控与告警：节点指标、容量预测与告警规则](docs/screenshots/monitoring.png)

### 任务队列与站内通知

任务队列跨连接汇总（按集群作用域过滤），能看到 PVE 的原始执行日志、终止还在跑的任务、清掉历史记录；每一步都带归属校验，知道 UPID 也操作不了别人的任务。

站内通知在顶栏铃铛里：未读数单独轮询，点开是独立窗口而不是下拉。资源告警、首次从新 IP 登录、证书将到期、重装完成、面板有新版本这些事件都会投递；清空消息会进审计。它和邮件、飞书并行，配置里的「按来源停推」对它们一起生效。

### 飞书机器人

把面板接进飞书群：发「列表」「状态 <名称/ID>」「开机 <名称/ID>」「创建」「快照」「快照列表」「回滚」「备份」这类指令就能操作虚拟机；关机、重启、回滚会先发一张卡片，点了确认才执行。

回调入口靠签名与 Verification Token 校验，按 `event_id` 去重防重放，3 秒内处理不完就转后台。App ID / Secret / Encrypt Key 只有管理员能看能改，接口只回 `*_set`，读配置本身也进审计。

### 命令面板与全局搜索

`Ctrl / ⌘ + K` 打开命令面板：一半是页面跳转（按当前语言匹配，跟着权限和入口开关过滤），一半是资源搜索（虚拟机、节点、存储、用户四组并发，输入 200ms 防抖，走 `/api/search`）。↑↓ 选择、Enter 打开、Esc 关闭。

### 快照、备份与恢复

快照创建（可选含内存状态）、回滚、删除，另有一个把各机器快照汇总成一张表的跨虚拟机视图；立即备份（snapshot / suspend / stop 三种模式）、浏览备份卷、恢复到指定 VMID、删除；可视化创建 PVE 定时备份任务。备份卷可登记为受保护，面板的删除接口会拒删；后台作业定期把登记过的备份和 PVE 实际内容对一遍（`volid|size|ctime` 指纹），不见了或元数据变了就告警。

### PBS 备份服务器（可选）

PVE 里把 PBS 注册成存储之后，存储页就会显示 PBS 徽标、最近备份时间、归档数与校验状态，并给出控制台入口 —— 这一步零配置。

想进一步看到 PBS 自己的仓库容量、GC 计划、节点负载、最近任务和磁盘健康，去「节点 → 连接配置 → PBS 只读接入」登记一个只读 token（`Datastore.Audit` / `Sys.Audit` 足够）。面板对 PBS **只发 GET**：prune / GC / verify / sync 都交给 PBS 控制台，凭据层面也不给写权限。没登记凭据时整块安静隐藏，不会在备份页留一个空盒子。

两条互补的巡检会走告警通道：PBS 归档 verify 未通过（`备份校验巡检`），以及 GC 失败 / 磁盘健康异常（`PBS 运行状态`）。详见 [docs/PBS监控.md](docs/PBS监控.md)。

<!-- 截图占位：docs/screenshots/backup.png -->
![备份与恢复：立即备份、定时任务、备份卷与恢复](docs/screenshots/backup.png)

### VNC 控制台

浏览器内 noVNC 图形控制台，支持自动重连、全屏、发送 Ctrl+Alt+Del。后端做 WebSocket 双向透传，PVE 的 Web 界面不用暴露到公网。

### 登录审计与操作审计

主机登录审计：`last` / `lastb` 解析出账号、终端、来源 IP、时间与状态，另有开机记录和 auth.log 里的 sudo、su 提权。事件按每台主机一个游标增量汇入 `audit_log`，和面板自身的操作在同一张表里，后台每 5 分钟跑一次，重复汇入不会重复写。读不到 VM 内部的登录日志，但能按 VM 的 IP 匹配各主机的登录记录，回答「这台 VM 有没有被用来登录宿主机」。经 frp 暴露到公网的主机想记录真实来源 IP（而不是入口机地址），见 [frp 真实 IP 接入指南](docs/frp-real-ip.md)。

<!-- 截图占位：docs/screenshots/audit-log.png -->
![登录审计与操作审计：来源 IP、动作与结果](docs/screenshots/audit-log.png)

### 证书管理（ACME / 多云 / PVE 节点）

对接腾讯云、阿里云、华为云免费证书、自有 ACME 账号（Let's Encrypt / ZeroSSL，支持泛域名与多域名）以及手动上传的托管证书。到期前自动续期，按站点部署到本机目录、SSH 远程服务器、PVE 虚拟机（Guest Agent）、LXC 容器或 PVE 节点自身的 8006 管理证书；部署后可执行重载命令，PVE 节点则由 PVE 自己落盘并重启 pveproxy。支持多站点绑定、证书清单、到期监控与失败退避重试。

<!-- 截图占位：docs/screenshots/certificates.png -->
![证书管理：签发渠道、站点列表与自动续期](docs/screenshots/certificates.png)

### 多用户、RBAC 权限与配额

| 内置角色 | 能做什么 |
|---|---|
| `admin` | 全部权限，含用户管理、连接配置、删虚拟机、集群级防火墙 |
| `operator` | 创建 / 开关机 / 控制台 / 快照 / 备份 / 克隆 / 改配置，管自己名下机器的防火墙，看 SSH 统计；不能管用户、不能删虚拟机、不能改集群级防火墙 |
| `viewer` | 只读 |
| `region_admin` | 区域管理员：只能看、只能管指派区域（连接的 `region` 标签）内的资源 |

内置角色之外还能建**自定义角色**：后端把权限按域整理成目录（`GET /api/permissions/catalog`），勾选即可；`admin` 不可改不可删，还有人在用的角色删不掉。

<!-- 截图占位：docs/screenshots/users-rbac.png -->
![多用户与 RBAC：角色、权限、配额与审批](docs/screenshots/users-rbac.png)

- 自助注册 + 管理员审批：注册后账号是 `pending`，登不了；审批时一次把角色和权限分配好。邮箱必填，审批结果发邮件（邮件是旁路，发不出去只记审计）。找回密码走一次性链接，不管账号存不存在都回同一句话，不给人试账号的机会。
- 所有写操作（含失败的）进审计日志，记用户、动作、目标、结果、详情、来源 IP，可按条件筛；结果是 `success` / `failed` / `partial` / `denied` / `accepted` 之一。普通账号即便有 `audit.view` 也只能看自己的记录，读审计本身同样留痕。
- 防呆：不能删、禁用或降级最后一个管理员，不能删当前登录账号。
- 资源额度分两层：先看用户自己有没有配，没配就继承角色默认值，两层都没配才不限制。维度是虚拟机与容器各自的台数、核数、内存、磁盘，另有单台核数 / 内存 / 磁盘封顶与快照数（总数 + 单机上限）。「继承」和「自定义但留空」不是一回事，后者表示显式不限制，会盖掉角色默认值。额度不只拦创建：改配置、加盘扩容、建快照同样校验，只守进门那一步等于没守。普通用户的读数只统计自己名下的机器，全平台已用 / 上限只在管理员和设置页展示 —— 免得把「集群还剩几台」当成「我还能建几台」，也不暴露集群规模。
- 设置里的连接配置可以在线改，存 MySQL，保存即生效不用重启，Token Secret 前端拿不到明文。「环境自检」会问 Proxmox 要当前凭据的有效权限，逐项列出 API 可达性、节点可见性、存储、虚拟机、控制台凭据是否正常，并把异常转成能直接执行的修复命令。

### 账号安全：两步验证、会话与 API 令牌

个人中心分「基本信息 / 修改密码 / 两步验证 / API 令牌 / 登录设备 / 我的权限」。两步验证绑定后给 8 张一次性恢复码，只显示一次；`TOTP_REQUIRED_ROLES` 能把某个角色设成必须先绑定才能用面板。勾了「信任设备」的浏览器 7 天内免输动态码，`TOTP_TRUST_ENABLED=false` 时每次都要过第二因子。

登录设备列出当前有效会话，能逐个踢下线，也能一键退出所有设备；管理员可以踢某个账号的全部会话，改密码同样会让其它设备上的令牌作废。同一账号最多保留 `SESSION_MAX_PER_USER` 台设备，超出的新登录挤掉最旧的一台。

个人 API 令牌用于自动化：有效期上限 365 天，不接受「永不过期」，明文只在创建时出现一次（库里存 SHA-256），签发和吊销都要求浏览器会话 + 二次确认。过期令牌由后台作业清理。

### 面板自身维护（更新 / 作业 / 品牌 / 入口开关）

- **更新**：设置页检查新版本，可以跳过某个版本，也可以改发布仓库。「立即更新」下载源码包后 `rsync` 覆盖（排除 `.git`、`backend/.env`、`data`、`dist` 等）再重启，接口立刻返回，进度看日志文件。国内拉不到 GitHub 时设 `PC_UPDATE_MIRROR` 镜像前缀。
- **后台作业**：面板自己的定时任务都列在这一页（各种巡检、告警、证书续期、归属对账、数据清理、frp 看护、版本检查等），间隔 10 秒到 30 天可调，能单独启停、立即执行并看这次的结果，也能恢复默认值。
- **站点品牌**：站点名、副标题、备案号，自定义 Logo 与登录页背景图（上传即生效，可移除回落到内置图标）。`/api/config/site` 保持匿名，登录页读它渲染。
- **入口开关**：可以逐个关闭侧边栏入口。关掉之后侧边栏、顶栏菜单、`Ctrl + K` 和直达 URL 一起失效，但**接口不因此拒绝**（入口不等于授权）；只有「系统设置」永远保留，否则就没地方恢复了。

---

## 三、配置项

`backend/.env`（Docker 部署时用同名环境变量传进去，完整示例见 `backend/.env.example`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SECRET_KEY` | 占位值 | 必填。签发登录 JWT，同时是库里密文的加密根，至少 32 位随机。留占位值或太短会拒绝启动 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / 空 | 首次启动创建的管理员。口令至少 12 位，空或弱口令会拒绝启动 |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | 监听地址。默认所有网卡，装完可直接用 `IP:8080` 访问；前面有反代时建议改回 `127.0.0.1` |
| `FORCE_HTTPS` | `false` | 明文 HTTP 一律 308 跳 https，HTTPS 响应带 HSTS，仅本机回环豁免 |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | 信任哪些来源的 `X-Forwarded-Proto` / `X-Forwarded-For`，只填反代地址；**不要填 `*`** |
| `PANEL_ALLOW_CIDRS` | 空 | 面板准入白名单（IP / CIDR，逗号分隔）。配置后非名单来源连登录页和 WebSocket 都访问不到 |
| `LOGIN_IP_ALERT` | `true` | 首次从新 IP 登录时给管理员发站内消息 + 邮件 |
| `API_DOCS_ENABLED` | `false` | 是否挂载 `/api/docs`、`/api/redoc`、`/api/openapi.json`（需管理员登录）；生产保持关闭 |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | `5` / `15` | 登录失败几次就锁定，账号和来源 IP 双计数 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | `720` / `14` | 令牌有效期，后者等于一次登录最多能用多久 |
| `SESSION_MAX_PER_USER` | `10` | 同一账号最多保留几台已登录设备，新登录挤掉最旧的一台；`0` = 不限 |
| `COOKIE_SECURE` | 跟随 `FORCE_HTTPS` | refresh Cookie 是否只走 HTTPS |
| `TOTP_REQUIRED_ROLES` | 空（`.env.example` 示例为 `admin`） | 强制开两步验证的角色 |
| `TOTP_ISSUER` | `pvecenter` | 认证器 App 里显示的签发方名称，建议写成面板的公网域名 |
| `TOTP_TRUST_ENABLED` | `true` | 是否允许勾选「信任设备」（同一台浏览器 7 天内免输动态码）。`false` 则每次登录都要过第二因子 |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_AUTH_PER_MINUTE` | `true` / `300` / `30` | 限流开关、每 IP 每分钟的普通请求与敏感入口（登录 / 注册 / 找回密码 / 刷新 / 2FA）上限 |
| `CAPTCHA_RATE_LIMIT_PER_MINUTE` | `6` | 验证码签发按来源 IP 的限流（次/分钟），防止匿名刷图放大 CPU |
| `STEP_UP_REQUIRED` / `STEP_UP_WINDOW_MINUTES` | `true` / `5` | 危险操作是否要二次确认，以及确认的有效窗口 |
| `FRP_ALLOW_LOCAL_CIDRS` / `FRP_BLOCK_PANEL_SUBNET` | 空 / `true` | 穿透目标白名单：普通用户只能指向自己名下的虚机 / 容器 IP，这里可再放行网段（也能在穿透页按人放行具体 IP）；默认还禁止指向面板自身网段 |
| `FRP_REMOTE_PORT_MIN` / `FRP_REMOTE_PORT_MAX` | `40000` / `50000` | 穿透公网端口区间的初始值 / 兜底。管理员可在页面上改成 frps 实际可用的那一段（改完即时生效并写库，以页面为准）；22 / 80 / 443 / 7000 / 7001 / 8006 始终不可占用 |
| `PVE_HOST` / `PVE_PORT` | 空 / `8006` | Proxmox 地址（不带协议）与 API 端口 |
| `PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` | 空 | 形如 `panel@pve!panel` 与 Token 的 UUID |
| `PVE_DEFAULT_NODE` | 空 | 节点作用域接口的默认节点；多节点时用于兜底 |
| `PVE_VERIFY_SSL` | `false` | 出站是否校验 PVE 证书。默认关闭：PVE 出厂自签，开着会握手失败。要安全就用连接里的「证书 PEM」（`ca_cert`）钉扎，或证书由正规 CA 签发时改成 `true` |
| `PVE_TRUST_ENV` | `false` | 是否让请求走环境变量里的 `HTTP_PROXY` / `HTTPS_PROXY` |
| `PVE_CONSOLE_USER` / `PVE_CONSOLE_PASSWORD` | 空 | 控制台用的 PVE 账号密码，留空则控制台不可用 |
| `CORS_ORIGINS` | `http://localhost:5173` | 允许的前端来源，逗号分隔 |
| `HTTP_TIMEOUT` / `TASK_POLL_INTERVAL` | `30` / `2` | PVE 请求超时（秒）与任务状态轮询间隔（秒） |
| `DEBUG` | `false` | 响应里带 `X-PVE-Calls` / `X-DB-Queries` 计数头，便于排性能问题；生产别开 |
| `METRICS_HISTORY_ENABLED` / `METRICS_SAMPLE_INTERVAL` / `METRICS_RETENTION_DAYS` | `true` / `60` / `30` | 指标采样进 `metrics_history` 的开关、周期（秒）与保留天数（`0` = 永久保留）。PVE 自带的 RRD 会被滚动覆盖，面板重启或主机重装就丢 |
| `PC_UPDATE_MIRROR` | 空 | 一键更新拉源码包的镜像前缀（直连 GitHub 失败后才用）。不在 `.env.example` 里，用 `systemctl edit pvecenter` 写 `Environment=` |
| `SSH_FORWARD_LOG_HOST` / `SSH_FORWARD_PROXIES` / `SSH_FORWARD_ROUTES` / `SSH_FORWARD_HAPROXY_CFG` | 空 | 入口机（haproxy + PROXY protocol）的来源 IP 还原：读哪台入口机的日志、可选覆盖与显式路由、haproxy 配置路径。详见 [frp 真实 IP 接入指南](docs/frp-real-ip.md) |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `127.0.0.1` / `3306` / 空 | MySQL 连接，库名必填 |
| `DB_POOL_SIZE` | `8` | 连接池上限 |

面板只支持 MySQL，建库后表结构启动时自动创建：

```sql
CREATE DATABASE pvecenter_panel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

`.env` 里的值只是初始默认值，启动后也能在「设置」页在线改，在线配置优先级更高。邮件（SMTP）配置没有环境变量，只在「设置 → 邮件通知」里填，口令用 `SECRET_KEY` 加密落库，接口只回 `password_set`。

---

## 四、项目结构

```
pvecenter/
├── Dockerfile / docker-compose.yml / docker-entrypoint.sh   容器化与一键部署
├── deploy.sh                  裸机一键部署，幂等，可重复执行
├── start.sh / start-prod.sh   开发模式（含 Vite） / 生产模式（后端托管 dist/）
├── backend/
│   ├── run.py                 启动入口
│   ├── .env.example           配置示例
│   └── app/
│       ├── main.py            FastAPI 应用、路由挂载、后台作业注册
│       ├── pve.py             Proxmox API 客户端（token + ticket 双认证、多连接）
│       ├── vmconfig.py        虚拟机 / 容器配置的构建与解析
│       ├── bulk.py            批量操作编排
│       ├── security.py        JWT 会话、RBAC、区域 / 节点可见范围、审计辅助
│       ├── quota.py           用户 / 角色两层资源额度与快照额度
│       ├── ownership.py       客户机与导入镜像的归属表（多租户隔离的基础）
│       ├── scheduler.py       面板自身的定时作业引擎
│       ├── alerting.py        告警规则、渠道（邮件 / 飞书 / Webhook / 站内信）与根因聚合
│       ├── store.py           MySQL：用户、审计日志、连接配置
│       └── routers/           每个页面对应一个模块：vms / lxc / templates / console /
│                              tasks / network / storages / firewall / backups /
│                              frp / feishu / alerts / certs / ai / baseline /
│                              portguard / isolation / ssh / hostaudit / users /
│                              roles / tokens / scheduler / update / audit / config
└── src/
    ├── api/                   axios 客户端、类型、接口封装
    ├── hooks/                 认证、toast、任务等待、WebSocket
    ├── components/ · pages/   布局、通用 UI 组件与页面
    ├── i18n/                  中英文文案
    └── styles/                浅色主题设计系统（单一主题）
```

---

## 五、测试

```bash
cd backend
../.venv/bin/python -m pip install -r requirements-dev.txt
../.venv/bin/python -m pytest        # 约 1000 个用例
```

三层测试，都不需要真实 Proxmox：纯逻辑（配置拼装、权限矩阵、字段别名、批量操作的部分失败与归属校验）；模拟 Proxmox（起一个假 HTTP 服务器，验证认证头、响应解包、任务轮询、错误到状态码的映射）；接口全链路（TestClient 打通请求到响应，覆盖 RBAC、审计、防火墙三档权限、SSH 日志解析与告警、远程主机、基线判定、端口启发式、口令重置各通道）。

> API 用例会连一个独立的 `<DB_NAME>_test` 库，连不上 MySQL 或没有建库权限时自动跳过，不碰业务库。

前端：

```bash
npm run typecheck     # tsc --noEmit
npm run build         # tsc -b && vite build
npm run verify        # 以上三步一次跑完，另含 i18n 一致性检查
```

没有 Nginx 也想验收构建产物，可以起内置静态服务器（带 `/api` 和 WebSocket 反向代理）：`npm run build && npm run serve:dist`（<http://localhost:8090>）。

对接的是 PVE 的 REST API，8.x 和 9.x 走同一套代码，没有按版本分支的功能。已在 PVE 8.4 和 9 上实测通过虚拟机、容器的创建、启停和控制台。版本号只用于展示，不需要跟着 PVE 升级改代码。

---

## 六、排查问题

**面板能登录，但虚拟机、存储列表全是空的。** 先点「设置 → 环境自检」。这是 Proxmox API Token 最容易踩的坑：在 Web 界面创建 Token 时默认勾「特权分离」，这样的 Token 一开始没有任何权限。需要特权的读取返回 403，而 `/storage` 这类接口返回 200 加空数组，看着就像面板坏了。自检会向 PVE 要该凭据的有效权限，并列出该执行哪条命令：

```bash
pveum acl modify / --tokens 'root@pam!panel' --roles PVEVMAdmin
pveum acl modify / --tokens 'root@pam!panel' --roles PVEDatastoreUser
pveum acl modify / --tokens 'root@pam!panel' --roles PVEAuditor   # 节点指标需要 Sys.Audit
```

判断特征：`pveum token list` 里 `privsep` 为 1，且 `/access/permissions` 返回空。

**连接报 502，但 ping 得通 Proxmox。** 看跑面板的机器有没有设 `HTTP_PROXY` / `HTTPS_PROXY`，面板默认不继承这些变量。确实要过代理就把 `PVE_TRUST_ENV` 设为 `true`，否则把 Proxmox 地址加进 `NO_PROXY`。

**配置是对的，但连不上集群。** 连接配置的优先级是「数据库 > `.env`」，之前用别的地址保存过的话，`.env` 里的新值不生效。去「设置」页改，或删掉 `settings` 表里的 `pve_connection` 记录再重启。Proxmox 不可达不会阻止面板启动，只在日志里留一条警告 —— 这样才有机会进界面改回来。

**接了两套以上 PVE，某个操作报 `hostname lookup 'node02' failed`。** 节点名在每台 PVE 上都是本地的，这句话通常不是 DNS 坏了，而是请求打到了**另一台** PVE，那台上没这个节点名，PVE 就把它当主机名去解析。节点作用域的接口（存储、备份、控制台、模板向导、下载任务等）会按节点所属连接自动纠正，前端调用也会带 `X-PVE-Connection`。真遇到这个报错，先看该节点属于哪条连接，必要时在「节点」页确认连接状态，或把顶栏集群作用域切到对应那套再试。

**控制台打不开**：Proxmox 的设计限制，API Token 访问不了 `vncproxy`，要在设置里补一个 PVE 账号密码（建议只给 `PVEVMUser`）。**连上但黑屏**：面板建的模板只挂 `serial0`，**不会**把 `vga` 指过去 —— 指过去 VNC 里就只剩串口输出，而没配 `console=ttyS0` 的镜像从 GRUB 之后一片全黑。要看串口日志就在控制台里切到 xterm，别改 `vga`。

**模板建好了，克隆出来的机器停在 `Boot failed: not a bootable disk`**：镜像的扩展名和真实格式对不上。Ubuntu 官方的 `*-cloudimg-amd64.img` 实际是 qcow2，PVE 认不出这种后缀，把容器按裸盘整块复制成了系统盘，第一扇区不是引导扇区。用 `qemu-img info <镜像>` 看一眼格式，转成裸盘（`qemu-img convert -O raw`）或把后缀改成 `.qcow2` 后重建模板。想看盘本身对不对：

```bash
qemu-img info <卷路径>        # 虚拟容量应是镜像的虚拟大小（Ubuntu 云镜像 3.5G），不是文件字节数
qemu-img dd -f qcow2 if=<卷路径> bs=512 count=1 of=/tmp/mbr.bin && hexdump -C /tmp/mbr.bin | tail -2
```

末两字节应当是 `55 aa`（开头 `eb 63 90` 是 GRUB 的 MBR）；出现 `51 46 49 fb` 说明整只 qcow2 容器被原样写进了盘。

**Docker 部署，容器起不来或反复重启**：多半是数据库口令对不上。`docker compose logs panel | tail -30` 找 `Access denied` / `Can't connect`，改法见 [Docker 部署](#docker-部署) 里那两条注意事项。

**面板跑在 Python 3.9 / 3.10 上，某些功能不正常（典型：告警邮件发不出、更新失败）。** 整套依赖按 Python 3.11+ 固定，最硬的限制是 Pillow 12 要求 ≥3.10 —— 3.9 上 `pip install -r backend/requirements.txt` 直接解析失败，依赖停在旧版本，一键更新也卡在装依赖这步。新版 `start-prod.sh` / `run.py` 会直接拒绝低版本解释器并给出安装命令；登录后访问 `/api/version` 能看到实际运行的 Python 版本。装好 3.11+ 后 `rm -rf .venv` 再启动即可（数据库与配置不受影响）。

**主机安全页面读不到数据**：容器里 `/var/log`、`/proc` 属于容器而不是宿主机，页面会说明，需要时按 `docker-compose.yml` 里注释的挂载项只读挂进去。

---

## 七、安全说明

密钥和口令。`SECRET_KEY` 留占位值或短于 32 位会直接拒绝启动；`ADMIN_PASSWORD` 空着、短于 12 位或是 `admin123` 这类弱口令同样拒绝启动。这两个后端会拦，别偷懒。

必须套 HTTPS。登录凭据和 API Token 在 HTTP 上是明文。打开 `FORCE_HTTPS=true`，由 Nginx / Caddy 在 443 上终结 TLS。反代要带 `X-Forwarded-Proto`，`FORWARDED_ALLOW_IPS` 只保留反代地址，否则公网请求可以自称 https 绕过跳转。面板到 PVE 的校验也补上 —— 用连接里的「证书 PEM」（`ca_cert`）钉扎，或 `PVE_VERIFY_SSL=true` —— 免得中间人截走 Token。`--privsep 1` 配合精确的 ACL 能让面板只碰该碰的资源，PVE 的 8006 也就不用对外暴露，流量全走面板后端。

密钥类配置不回传前端。`GET /api/config/connection` 只返回 `token_secret_set`，更新时留空即保持原值；SMTP 口令同样只回 `password_set`。管理员也看不到别人的 Webhook 与云密钥。

登录防护有三层。登录页的人机验证排在密码校验之前（脚本摸不到「密码对不对」这个信号），验证码没过不计入失败次数 —— 否则拿一张错图反复提交就能锁死任意账号。密码层面按账号和来源 IP 双计数，连续失败锁定若干分钟，计数落 MySQL、重启不清零。再往外是全局限流，按来源 IP 统计 `/api/*`，敏感入口单独一条更紧的线，超限返回 429。限流只信可信反代传来的 `X-Forwarded-For`，客户端自己伪造绕不过去。

会话可撤销，写操作过 CSRF。登录签发 access + refresh 两个令牌，refresh 放在 `HttpOnly` Cookie 里，JavaScript 读不到、XSS 也偷不走；登出会撤销服务端会话，当次令牌立刻失效。「个人中心 → 登录设备」能逐个踢下线，「退出所有设备」或管理员踢人会让该账号所有已发出的令牌作废，改密码同样踢掉其它设备。写请求要带 `X-CSRF-Token`（值取自同样下发但故意可读的 `panel_csrf` Cookie），跨站页面读不到它、伪造不出来；WebSocket 握手同样用 Cookie 鉴权并校验 `Origin`，令牌不再出现在 URL 里。

两步验证与二次确认。`TOTP_REQUIRED_ROLES=admin` 表示管理员必须先绑认证器才能用面板，绑定一次性给出 8 张恢复码、只显示一次，密钥用 `SECRET_KEY` 加密落库。删虚拟机、改连接凭据、增删账号、改集群级防火墙、批量下发规则、写 fail2ban 策略这些操作，即便 token 还在有效期也不够，后端返回 `403 + X-Step-Up: required`，要求重新输密码（开了两步验证还要动态码）。敏感读取也进审计：查看连接配置、邮件配置、翻阅审计日志本身都会记一条。「谁把集群 Token 抄走了」这类问题，只能靠读操作的痕迹回答。

---

## 八、规划

下一步想做的，按重要性排，不代表排期：

- SDN 网络。现在「网络」页管的是节点级 bridge / bond / VLAN 与 IP 地址池，PVE 8.x / 9.x 的 SDN（zone / vnet / subnet / controller / IPAM）还没有入口。它是集群级对象，一份配置作用于整个集群，所以底线是「读」永远可用、「写」必须有 diff 预览和二次确认，也不提供「一键重建 SDN」这类不可回滚的动作。
- 计费与用量。前置已经就绪：指标按周期落 `metrics_history`，额度按 CPU / 内存 / 磁盘统计；缺的是「按谁、按多少、值多少钱」这层计价与账单。最容易做错的是口径：PVE 给的网络流量是累计计数器，会因重启、迁移、驱动重置归零，必须靠采样差分并丢弃计数器回退的样本，否则会算出负数或暴涨的流量。
- 备份可验证的下一步是对接 PBS。现在已有「登记 + 指纹核对 + 面板层拒删 + 异常告警」，能发现删除与替换；静默位翻转得拿到 PBS 的 verification 校验和与 immutability 才看得见。
- 资源池（PVE pool）。租户隔离已经有一部分（归属表 + 区域管理员 + 额度 + 节点可见范围），缺的是池的管理界面和「按池隔离」；目前只有透传的只读 `GET /api/cluster/pools`。
- 其它候选：报表订阅（定时周报 / 月报，指标历史已经够用）、告警补 Telegram 原生通道（企业微信 / 钉钉 / Slack 现在用 Webhook 预设就行）、Prometheus 指标导出、审计外送 SIEM、LDAP / OIDC 单点登录、深色主题与主题切换、PWA。

已知的界面缺口（接口有，页面没做）：Ceph 集群状态（`GET /api/cluster/ceph/status` 已透传）；IP 池只有「从分给自己的池里挑地址」，没有「申请—审批—分配」流程。

每条的具体理由和验收标准写在 issue 里。

---

## 参与贡献

欢迎提交 [Issue](https://github.com/yjscloud/pvecenter/issues)、Fork 和 Pull Request。提交前先跑 `npm run verify` 与后端 `pytest`；功能建议附上使用场景，方便判断优先级。

如果这个项目对你有帮助，点一个 Star 是最直接的支持。

---

## 许可证

本项目以 [Apache-2.0](LICENSE) 许可发布。
