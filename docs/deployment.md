# 部署

面板跑起来只有一步：`sudo ./deploy.sh`。下面按你实际会遇到的顺序排：先在 PVE 上建 Token 并授权，再选裸机还是 Docker，最后是 HTTPS 和日常的服务管理。

> 返回 [README](../README.md) · 同系列：[配置项](configuration.md) · [公网加固](hardening.md) · [排查问题](troubleshooting.md)

## 准备 Proxmox API Token

面板靠 API Token 跟 Proxmox 通信，下面两节是**手动配置**的做法。嫌麻烦可以直接用
**批量接入**：面板里「节点 · 连接配置 → 批量接入」生成一条命令，复制到 PVE 节点上以
root 粘贴执行即可 —— 建账号、建令牌、注册到面板、结果自检都由脚本完成，失败会自动
回滚并在屏幕上说清原因。链接可以设**可用次数**（默认 1 次，即一次性；也可以填 5 次、
10 次或「不限次数」），适合一次接多台；同一集群重复执行不消耗次数，次数用尽或过了
有效期链接自动失效。

在 PVE 上执行：

```bash
pveum user add panel@pve --comment "pvecenter panel"
pveum user token add panel@pve panel --privsep 0
```

输出里的 `full-tokenid`（形如 `panel@pve!panel`）填 Token ID，`value` 填 Token Secret。

## 授权

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

## 裸机部署（推荐）

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

> 「立即更新」要联网拉代码（源码包安装下 tar.gz，git 安装下 `git fetch`），国内连 `github.com` 经常卡。0.3.2 起加了连接超时和总时长，失败直接报错而不是一直挂着；git 安装还会按「原样 → HTTP/1.1 → 退避重试 2 次 → 镜像」逐级重试，全失败才放弃，并把 `git --version`、脱敏后的 remote、`GIT_TRACE_CURL` 末尾写进更新日志。连不上就在 `systemctl edit pvecenter` 里写 `Environment=PC_UPDATE_MIRROR=https://ghfast.top/` 后重试，或者下源码包手工升级 / 改用 Docker。

> `SECRET_KEY` 生成之后别再改，换了它，库里已存的 PVE Token、SMTP 口令全都解不开。`.env` 已存在时，脚本只覆盖命令行上显式给出的键。

想前台跑着看日志（不装服务）：`npm run build && ./start-prod.sh`。`dist/` 存在时 FastAPI 会在 8080 同时提供前端页面和 `/api`，前后端同源，VNC 控制台的 WebSocket 直接可用。

## Docker 部署

不想在宿主机装 Python / Node / 数据库就用容器，宿主机只要有 Docker：

```bash
curl -fsSLO https://raw.githubusercontent.com/yjscloud/pvecenter/main/docker-compose.yml
export ADMIN_PASSWORD="$(openssl rand -base64 18)"
echo "登录口令：$ADMIN_PASSWORD"
docker compose up -d
```

打开 `http://你的服务器IP:8080`，账号 `admin`，口令就是上面生成并打印的那个。口令短于 12 位、或属于 `admin123` 这类弱口令时，首次建号会直接拒绝启动，而不是建出一个弱口令管理员。

三个口令都在 `docker-compose.yml` 里，搜 `★ 改这里`。每一行长这样：

```yaml
ADMIN_PASSWORD: ${ADMIN_PASSWORD:-你的口令}
```

`:-` 是 compose 的「取默认值」记号：`.env` 或环境变量给了就用那个，没给才用 `:-` 后面那段。改的时候只动 `:-` 后面、`}` 前面那一截，`$`、`{`、`}` 都只是语法。不设也能起，那时用的是文件里那段默认值，别让它留在公网可达的实例上。

要改的是 `ADMIN_PASSWORD`（面板登录）、`DB_PASSWORD`（连库）、`MYSQL_ROOT_PASSWORD`（数据库管理员）。也可以不碰这个文件，在同目录放个 `.env`，等号后直接写口令，优先级更高。

两个坑先说在前面：

- 口令只在第一次初始化时生效。`ADMIN_PASSWORD` 只在库里还没有管理员时用来建号，MySQL 也只在数据目录为空时读 `MYSQL_PASSWORD`。跑起来之后再改，要么 `docker compose down -v` 重建（数据会没），要么进库 `ALTER USER`（命令在 `docker-compose.yml` 末尾）。改登录口令请登录后在「个人中心 → 修改密码」改。
- `SECRET_KEY` 别动。它既是 JWT 签名密钥，也是库里密文（PVE Token、SMTP 口令）的加密根。写死在公开仓库等于把钥匙公示，所以容器首次启动随机生成一份存进 `panel_data` 卷；已有数据后改掉它，那些密文就都解不开了。

镜像默认构建 amd64（PVE 本身只有 x86_64），从 GHCR 拉。换 Docker Hub：`PVECENTER_IMAGE=docker.io/yjscloud/pvecenter:latest docker compose up -d`。升级：`docker compose pull && docker compose up -d`。

## 上 HTTPS

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

## 服务管理

`./deploy.sh` 装的是 systemd 服务，开机自启、进程挂了自动重启：

| 操作 | 命令 |
|---|---|
| 状态 / 启停 | `systemctl status\|start\|stop\|restart pvecenter` |
| 开机自启 | `systemctl enable pvecenter` |
| 跟随日志 | `journalctl -u pvecenter -f` |

改完代码，只动前端 `npm run build` 就够，动后端要 `systemctl restart pvecenter`。确认服务起来了：`curl http://127.0.0.1:8080/api/health`。

> Docker 部署这节可以跳过，容器本身带 `restart: unless-stopped`。

装完只是开始。准备把面板挂到公网，[公网暴露前后的加固](hardening.md) 里那几条建议先过一遍——那些开关不在界面里，改的是配置文件。
