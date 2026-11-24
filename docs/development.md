# 目录结构与测试

接手代码之前先看这两块：仓库怎么分、测试怎么跑。后端和前端是分开的，改哪边跑哪边的检查。

> 返回 [README](../README.md) · 相关：[部署](deployment.md)

## 目录结构

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

## 测试

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
