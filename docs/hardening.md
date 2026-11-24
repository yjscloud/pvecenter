# 公网暴露前后的加固

面板一旦对外，一个账号失守就等于整个虚拟化平台失守。下面这些一条一条过，漏哪条都是缺口。

前半是部署时你自己要改的配置，后半是面板内部已经做了的事——知道它做了什么，才不会在反向代理上重复加一遍，也不会以为还有一层没设。

> 返回 [README](../README.md) · 相关：[部署](deployment.md) · [安全策略](../.github/SECURITY.md)

## 部署时要设的开关

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
3. **管理员强制 2FA**：`TOTP_REQUIRED_ROLES=admin`。管理员没绑 TOTP 之前，除 2FA 接口外一律 403（可在「个人中心」绑定）。登录失败锁定（账号 + IP 双维度）和人机验证公网环境别关；人机验证对**登录、自助注册、忘记密码、重置密码四个匿名入口同时生效**，否则注册接口会被脚本拿去刷审批队列，而一条重置链接就足以改掉口令。
4. **新 IP 登录告警**：默认开（`LOGIN_IP_ALERT=true`）。某账号第一次从新 IP 登录成功时，所有管理员收到站内消息；配了 SMTP 再发一封邮件。审计里对应 `auth.new_ip`。
5. **别把 8080 裸奔到公网**：Docker 部署建议 `PANEL_BIND=127.0.0.1`，只让同机反代连。`FORWARDED_ALLOW_IPS` 绝不要填 `*` —— 客户端能伪造来源 IP，白名单、登录锁定、审计 IP 会一起失真。
6. **定期审计**：`/api/audit`、`/api/host-audit` 默认只有管理员可读。重点看登录失败、`auth.new_ip`、用户 / 权限变更，以及删机、改凭据这类敏感操作。
7. **接口文档默认关闭**：`/api/docs`、`/api/redoc`、`/api/openapi.json` 一律 404。完整 OpenAPI 等于一份全量攻击地图。排障确需查看时用 `API_DOCS_ENABLED=true` 临时打开，打开后这三个地址仍要求「已登录 + `settings.manage`」。
8. **`/api/health` 对匿名只答「活着」**：未登录只返回 `{"status":"ok","authenticated":false}`，不再给 PVE 版本、节点数和连接状态（版本号能直接对公开漏洞库）。登录后仍是完整读数（`authenticated:true`），深度诊断走 `/api/config/diagnostics`（管理员）。`authenticated` 用来把「探测结果未知」和「配了但连不上」分开，界面不会把前者误报成 PVE 故障。
9. **安全响应头由面板统一下发**：CSP（普通页面 `script-src 'self'`）、`X-Content-Type-Options: nosniff`、`Referrer-Policy: strict-origin-when-cross-origin`、`Permissions-Policy`、`frame-ancestors 'none'`、`X-Frame-Options: DENY` 和 HSTS 都在应用层加（`backend/app/main.py` 的 `SecurityHeadersMiddleware`）。反代只做 TLS 终结，不要重复添加同名头。
10. **面板→PVE 默认不校验证书**：新建连接的 `verify_ssl` 默认 `false`（`PVE_VERIFY_SSL` 也是）。PVE 出厂是自签证书（cluster CA 签发、`CN=pve.local`、无 IP SAN），默认开着校验会在握手阶段失败，很容易误判成地址或令牌填错。要校验就别只勾开关：把 PVE 证书 PEM 填进连接的「证书 PEM」（`ca_cert`），按该证书钉扎 —— 只信这一张，比系统信任库更严，也不受那个开关影响。取证书：`openssl s_client -connect <PVE-IP>:8006 </dev/null 2>/dev/null | openssl x509`，或从任意节点 `cat /etc/pve/pve-root-ca.pem`。正规 CA 签发（或已钉扎）时再把开关打开。连接未校验时启动日志会告警。
11. **挂了反代就把后端绑回回环**：`HOST=127.0.0.1` 时 8080 不对内网开放，全部入口经 nginx（默认安装是 `0.0.0.0`，可直接用 `IP:8080`）。反代侧再配 `client_header_timeout` / `client_body_timeout` / `send_timeout` 与 `limit_conn`，抵御慢速连接耗尽 worker。
12. **API Token 有效期上限 365 天**，不支持「永不过期」：令牌是无人值守凭据，泄漏后靠到期兜底。长期自动化就在到期前重新签发（列表里能看到剩余天数）。
13. **不回显组件版本**：应用不再发 `Server` / `X-Powered-By`（`run.py` 的 `server_header=False` + 安全头中间件兜底）；nginx 侧用 `server_tokens off;` 只留 `Server: nginx`。
14. **生产不发布 sourcemap**：`vite.config.ts` 里 `build.sourcemap=false` —— `.map` 的 `sourcesContent` 就是完整 TS 源码。nginx 再对 `*.map` 返回 404 兜底，防止误传旧产物。要排障用 `npm run dev`。
15. **版本与 RBAC 清单需登录**：`/api/auth/permissions` 需登录，`/api/version` 进一步收紧为仅管理员（`settings.manage`）；只有 `/api/config/site`（站点名、Logo、备案号）保持匿名，供登录页与官网渲染。

需要客户端证书（mTLS）时，在 Nginx 的 server 块里加：

```nginx
ssl_client_certificate /etc/nginx/client-ca.crt;
ssl_verify_client on;   # 或 optional + 下面的 if 判断
# if ($ssl_client_verify != SUCCESS) { return 403; }
```

## 面板自己做了什么

这一段不用你配，但值得知道边界在哪，免得在反代上重复做一遍。

密钥和口令。`SECRET_KEY` 留占位值或短于 32 位会直接拒绝启动；`ADMIN_PASSWORD` 空着、短于 12 位或是 `admin123` 这类弱口令同样拒绝启动。这两个后端会拦，别偷懒。

必须套 HTTPS。登录凭据和 API Token 在 HTTP 上是明文。打开 `FORCE_HTTPS=true`，由 Nginx / Caddy 在 443 上终结 TLS。反代要带 `X-Forwarded-Proto`，`FORWARDED_ALLOW_IPS` 只保留反代地址，否则公网请求可以自称 https 绕过跳转。面板到 PVE 的校验也补上 —— 用连接里的「证书 PEM」（`ca_cert`）钉扎，或 `PVE_VERIFY_SSL=true` —— 免得中间人截走 Token。`--privsep 1` 配合精确的 ACL 能让面板只碰该碰的资源，PVE 的 8006 也就不用对外暴露，流量全走面板后端。

密钥类配置不回传前端。`GET /api/config/connection` 只返回 `token_secret_set`，更新时留空即保持原值；SMTP 口令同样只回 `password_set`。管理员也看不到别人的 Webhook 与云密钥。

登录防护有三层。登录页的人机验证排在密码校验之前（脚本摸不到「密码对不对」这个信号），验证码没过不计入失败次数 —— 否则拿一张错图反复提交就能锁死任意账号。密码层面按账号和来源 IP 双计数，连续失败锁定若干分钟，计数落 MySQL、重启不清零。再往外是全局限流，按来源 IP 统计 `/api/*`，敏感入口单独一条更紧的线，超限返回 429。限流只信可信反代传来的 `X-Forwarded-For`，客户端自己伪造绕不过去。

会话可撤销，写操作过 CSRF。登录签发 access + refresh 两个令牌，refresh 放在 `HttpOnly` Cookie 里，JavaScript 读不到、XSS 也偷不走；登出会撤销服务端会话，当次令牌立刻失效。「个人中心 → 登录设备」能逐个踢下线，「退出所有设备」或管理员踢人会让该账号所有已发出的令牌作废，改密码同样踢掉其它设备。写请求要带 `X-CSRF-Token`（值取自同样下发但故意可读的 `panel_csrf` Cookie），跨站页面读不到它、伪造不出来；WebSocket 握手同样用 Cookie 鉴权并校验 `Origin`，令牌不再出现在 URL 里。

两步验证与二次确认。`TOTP_REQUIRED_ROLES=admin` 表示管理员必须先绑认证器才能用面板，绑定一次性给出 8 张恢复码、只显示一次，密钥用 `SECRET_KEY` 加密落库。删虚拟机、改连接凭据、增删账号、改集群级防火墙、批量下发规则、写 fail2ban 策略这些操作，即便 token 还在有效期也不够，后端返回 `403 + X-Step-Up: required`，要求重新输密码（开了两步验证还要动态码）。敏感读取也进审计：查看连接配置、邮件配置、翻阅审计日志本身都会记一条。「谁把集群 Token 抄走了」这类问题，只能靠读操作的痕迹回答。
