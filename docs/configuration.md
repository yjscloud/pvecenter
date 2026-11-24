# 配置项

配置都在 `backend/.env`。Docker 部署用同名环境变量传进去，完整示例见 `backend/.env.example`。下面这些是最常改的，其余照着示例文件填。

> 返回 [README](../README.md) · 相关：[部署](deployment.md) · [公网加固](hardening.md)

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
| `CONNECTION_TEST_ALLOW_TARGETS` | 空 | 「测试连接」的出站目标白名单（IP / CIDR / 主机名，逗号分隔）。默认只允许测试已保存连接里登记的 host:port，新增连接需先保存再测；填这里可在保存前放行。私网与回环需登记或白名单，链路本地（含云元数据）、组播、保留地址始终拒绝 |
| `PVE_HOST` / `PVE_PORT` | 空 / `8006` | Proxmox 地址（不带协议）与 API 端口 |
| `PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` | 空 | 形如 `panel@pve!panel` 与 Token 的 UUID |
| `PVE_DEFAULT_NODE` | 空 | 节点作用域接口的默认节点；多节点时用于兜底 |
| `PVE_VERIFY_SSL` | `false` | 出站是否校验 PVE 证书。默认关闭：PVE 出厂自签，开着会握手失败。要安全就用连接里的「证书 PEM」（`ca_cert`）钉扎，或证书由正规 CA 签发时改成 `true` |
| `PVE_TRUST_ENV` | `false` | 是否让请求走环境变量里的 `HTTP_PROXY` / `HTTPS_PROXY` |
| `PVE_CONSOLE_USER` / `PVE_CONSOLE_PASSWORD` | 空 | 控制台用的 PVE 账号密码，留空则控制台不可用 |
| `CORS_ORIGINS` | `http://localhost:5173` | 允许的前端来源，逗号分隔 |
| `HTTP_TIMEOUT` / `TASK_POLL_INTERVAL` | `30` / `2` | PVE 请求超时（秒）与任务状态轮询间隔（秒）。轮询是自适应的：从 0.2 秒起逐步翻倍，最长到这个值 |
| `BATCH_CONCURRENCY` | `3` | 批量下发的并发台数（仅 `stop_on_error=0` 时生效，设 `1` 回到串行）。每台都要等 PVE 任务结束，并发能把总耗时压到接近「最慢一台 × 轮数」 |
| `DEBUG` | `false` | 响应里带 `X-PVE-Calls` / `X-DB-Queries` 计数头，便于排性能问题；生产别开 |
| `METRICS_HISTORY_ENABLED` / `METRICS_SAMPLE_INTERVAL` / `METRICS_RETENTION_DAYS` | `true` / `60` / `30` | 指标采样进 `metrics_history` 的开关、周期（秒）与保留天数（`0` = 永久保留）。PVE 自带的 RRD 会被滚动覆盖，面板重启或主机重装就丢 |
| `PC_UPDATE_MIRROR` | 空 | 一键更新的镜像前缀（直连 GitHub 失败后才用）：源码包安装拿它拼下载地址，git 安装拿它当远端拉目标 tag。不在 `.env.example` 里，用 `systemctl edit pvecenter` 写 `Environment=` |
| `SSH_FORWARD_LOG_HOST` / `SSH_FORWARD_PROXIES` / `SSH_FORWARD_ROUTES` / `SSH_FORWARD_HAPROXY_CFG` | 空 | 入口机（haproxy + PROXY protocol）的来源 IP 还原：读哪台入口机的日志、可选覆盖与显式路由、haproxy 配置路径。详见 [frp 真实 IP 接入指南](frp-real-ip.md) |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `127.0.0.1` / `3306` / 空 | MySQL 连接，库名必填 |
| `DB_POOL_SIZE` | `8` | 连接池上限 |

面板只支持 MySQL，建库后表结构启动时自动创建：

```sql
CREATE DATABASE pvecenter_panel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

`.env` 里的值只是初始默认值，启动后也能在「设置」页在线改，在线配置优先级更高。邮件（SMTP）配置没有环境变量，只在「设置 → 邮件通知」里填，口令用 `SECRET_KEY` 加密落库，接口只回 `password_set`。
