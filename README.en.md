<div align="center">
<h1>pvecenter</h1>
<p><b>Management Panel for Proxmox VE 8.x / 9.x</b><br>
<sub>VMs · containers · templates · networking · backups · certificates · permissions, in one place</sub></p>
<p>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/yjscloud/pvecenter?color=blue" alt="License"></a>
  <a href="../../releases"><img src="https://img.shields.io/github/v/release/yjscloud/pvecenter?sort=semver&color=success&label=release" alt="Release"></a>
  <a href="https://github.com/yjscloud/pvecenter/actions/workflows/ci.yml"><img src="https://github.com/yjscloud/pvecenter/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="../../stargazers"><img src="https://img.shields.io/github/stars/yjscloud/pvecenter?style=flat" alt="Stars"></a>
  <a href="../../forks"><img src="https://img.shields.io/github/forks/yjscloud/pvecenter?style=flat" alt="Forks"></a>
  <a href="../../issues"><img src="https://img.shields.io/github/issues/yjscloud/pvecenter" alt="Issues"></a>
</p>
<p>
  <img src="https://img.shields.io/badge/Proxmox%20VE-8.x%20%2F%209.x-e57000" alt="Proxmox VE">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776ab" alt="Python">
  <img src="https://img.shields.io/badge/React-19-61dafb" alt="React">
  <img src="https://img.shields.io/badge/MySQL-8-4479A1" alt="MySQL">
  <img src="https://img.shields.io/badge/Docker-compose-2496ED" alt="Docker">
</p>
<p>
  <a href="#quick-start"><b>Quick start</b></a> ·
  <a href="#documentation">Docs</a> ·
  <a href="README.md">简体中文</a>
</p>
</div>

**pvecenter** is an open-source, self-hosted **Proxmox VE (PVE) web management panel**. One interface for QEMU virtual machines, LXC containers, cloud-init templates, storage and networking, snapshots and backups, internal tunneling, the VNC console, certificates and account permissions — and it can drive several PVE hosts at once. Install it as a systemd service or run it with Docker; it comes back by itself after a reboot.

The panel does no virtualization of its own; Proxmox still does the work. What it adds is one place for operations that otherwise live in the API, the CLI and each node separately, plus users, quotas and audit, which PVE does not ship.

> 自托管的 **Proxmox VE（PVE）Web 管理面板**：QEMU 虚拟机、LXC 容器、cloud-init 模板、
> 网络与防火墙、备份、监控、证书与 RBAC。
>
> Built for homelabs, small teams, and anyone who needs to open a PVE cluster to several people.

> Keywords: Proxmox VE panel · PVE panel · PVE web UI · LXC container management · QEMU / KVM ·
> cloud-init templates · self-hosted virtualization platform · Proxmox alternative UI · homelab ·
> self-hosted

**Live demo:** <https://pvecenter.yjscloud.com>

## Contents

- [Contact and feedback](#contact-and-feedback) · [Screenshots](#screenshots) · [Key features](#key-features)
- [Quick start](#quick-start) · [Requirements](#requirements) · [Bare metal](#bare-metal) · [Docker](#docker) · [Connect a PVE host](#connect-a-pve-host)
- [Documentation](#documentation) · [License](#license)

---

## Contact and feedback

Questions, feature requests or help with deployment: scan the WeChat code below, or leave a note in
[Issues](https://github.com/yjscloud/pvecenter/issues). Some features have not been tested hard yet — just
say what broke.

pvecenter is maintained by one person. If it saved you time, a voluntary tip helps pay for servers, test
environments and the upkeep; not tipping changes nothing about support.

<table>
  <tr>
    <td align="center" width="33%"><a href="docs/screenshots/wechat.png"><img src="docs/screenshots/wechat.png" alt="WeChat: feedback and deployment help" width="150"></a><br><sub><b>WeChat</b> · feedback / deployment help</sub></td>
    <td align="center" width="33%"><a href="docs/screenshots/weixin.jpg"><img src="docs/screenshots/weixin.jpg" alt="WeChat tip code: voluntary support" width="150"></a><br><sub><b>Tip</b> · voluntary</sub></td>
    <td align="center" width="33%"><a href="docs/screenshots/zhifubao.jpg"><img src="docs/screenshots/zhifubao.jpg" alt="Alipay tip code: voluntary support" width="150"></a><br><sub><b>Alipay tip</b> · voluntary</sub></td>
  </tr>
</table>

> Do not open a public issue for a security problem. Use the private channel in [SECURITY.md](.github/SECURITY.md).

---

## Screenshots

<table>
  <tr>
    <td width="50%"><a href="docs/screenshots/dashboard.png"><img src="docs/screenshots/dashboard.png" alt="pvecenter overview: cluster status, CPU / memory / storage use, nodes and resource distribution"></a><br><sub><b>Dashboard</b> · cluster status / resource use / distribution</sub></td>
    <td width="50%"><a href="docs/screenshots/vm-list.png"><img src="docs/screenshots/vm-list.png" alt="Proxmox VM list: batch power operations, migration and quotas"></a><br><sub><b>VM list</b> · batch actions / migration / quotas</sub></td>
  </tr>
  <tr>
    <td width="50%"><a href="docs/screenshots/lxc-list.png"><img src="docs/screenshots/lxc-list.png" alt="LXC management: lifecycle, snapshots, cloning and migration"></a><br><sub><b>LXC containers</b> · lifecycle / snapshots / migration</sub></td>
    <td width="50%"><a href="docs/screenshots/console.png"><img src="docs/screenshots/console.png" alt="In-browser console: graphical and terminal modes, no need to expose PVE port 8006"></a><br><sub><b>Console</b> · graphical / terminal, from the browser</sub></td>
  </tr>
</table>

---

## Key features

| Area | What it does |
|---|---|
| **VMs and containers** | four-step and batch creation, power operations, cloning, migration, live reconfiguration, disk resize, snapshots, OS reinstall; OVF / OVA import and VMDK / QCOW2 / RAW / OVA export |
| **Templates and storage** | a cloud-init template pipeline; browse and upload storage content (suffix corrected by file magic), or let PVE download it from a URL |
| **Networking** | bridge / bond / VLAN, the native PVE firewall and security groups, IP address pools, a built-in frpc tunnel |
| **Monitoring and backup** | live metrics, RRD charts, capacity forecasting, disk SMART / ZFS; backup now or on a schedule, delete protection, optional read-only PBS access |
| **Security operations** | SSH brute-force protection, login and action audit, security baseline, port and process inspection, emergency response |
| **Certificates and accounts** | multi-cloud / ACME certificates with auto-renewal and deployment; built-in and custom roles, quotas, two-factor auth, API tokens |
| **AI assistant** | attributes findings from inspection data, read-only command allowlist, write commands approved one by one |
| **Day to day** | background jobs, task queue, in-app notifications, a `Ctrl / ⌘ + K` command palette, one-click updates, branding and nav switches |

Short version: most of what the PVE web UI does, plus cross-cluster aggregation, batch operations, and the permission and audit layer PVE never had. Details and limits for each area are in the [feature guide](docs/features.en.md) (English outline; the full page is Chinese).

---

## Quick start

### Requirements

| Component | Version | Notes |
|---|---|---|
| Proxmox VE | 8.x / 9.x | tested on 8.4 and 9; both run the same code |
| Python | 3.11+ | 3.9 / 3.10 are rejected at startup |
| Database | MySQL 8.0+ or MariaDB | standard MySQL protocol; the bare-metal script installs MariaDB (10.11 on Debian 12) and the Docker image defaults to `mysql:8.0`; tables are created on first start |
| Node.js | 22.22+ | automatic: `deploy.sh` installs it; only needed up front if you build the frontend by hand |
| Docker + Compose | v2 | only for the container route |

The panel itself is light, but give it its own VM rather than sharing the PVE host: `deploy.sh` installs MariaDB and a systemd service on that machine, and the baseline checks and SSH login audit read that machine's own logs, so on a PVE node those readings describe PVE itself. Ubuntu, Debian, CentOS and TencentOS are supported, as are RHEL / Rocky / Alma / openEuler and Alpine; Debian 12+ and Ubuntu 24.04+ ship Python 3.11 already, older releases need 3.11 installed first.

| Resource | Starting point | Notes |
|---|---|---|
| CPU | 2 cores | 1 core works, but frontend builds and batch operations get noticeably slower |
| RAM | 2 GB | the frontend build (`npm run build`) is the peak; 1 GB machines often stall there. The Docker image does not build |
| Disk | 3 GB | source, dependencies, virtualenv and built assets come to about 1 GB; the rest is logs, metrics history and the database |

### Bare metal

```bash
git clone https://github.com/yjscloud/pvecenter.git
cd pvecenter
sudo ./deploy.sh
```

One run installs the Python dependencies, builds the frontend, creates the database and registers a systemd service. The last screen prints the panel address and the initial password, shown only once. Running it again does not break an existing install. To set the port or the database name:

```bash
sudo ./deploy.sh --port 9000 --db-name pvecenter_panel
```

`./deploy.sh --help` lists everything. Ports, HTTPS, the reverse proxy and service management are in the [deployment guide](docs/deployment.md) (Chinese).

Check that it came up:

```bash
curl http://127.0.0.1:8080/api/health
```

### Docker

No Python / Node / database on the host? Docker is enough:

```bash
curl -fsSLO https://raw.githubusercontent.com/yjscloud/pvecenter/main/docker-compose.yml
export ADMIN_PASSWORD="$(openssl rand -base64 18)"
echo "login password: $ADMIN_PASSWORD"
docker compose up -d
```

Open `http://your-server-ip:8080`, account `admin`, password the one just generated and printed (the backend refuses to start with fewer than 12 characters). The database and DB-admin passwords live in `docker-compose.yml` under `★ 改这里`; change those too before exposing the port.

### Connect a PVE host

First thing after logging in: connect a PVE host. If you would rather not create a user, issue a token and fill in the form by hand, use batch connect:

**Open the panel → Cluster nodes → Connection settings → Batch connect**, and the panel generates a command. Paste it into the PVE node as root. The script creates the account, issues the token, registers the connection and checks the result, rolling back and saying what went wrong if it fails.

The link can be limited to a number of uses: 1 by default, or 5, 10, unlimited. Re-running it on the same cluster does not use up a use; an exhausted or expired link stops working.

<p align="center">
  <a href="https://www.bilibili.com/video/BV1fqySzKEzv/">
    <img src="docs/screenshots/batch-connect-cover.png" alt="Batch connect dialog: generate a command, set region, note, lifetime and number of uses" width="640">
  </a>
  <br>
  <sub>▶ Click the cover to watch the demo (4 min, Chinese): batch connect PVE nodes, batch operations and migration for VMs and containers</sub>
</p>

---

## Documentation

The detailed docs are written in Chinese for now; this page is the English entry point, and the feature guide has an English heading-level outline.

| I want to… | Read |
|---|---|
| Install it, change the port, use Docker | [Deployment](docs/deployment.md) |
| Put the panel on the public internet | [Hardening](docs/hardening.md) |
| Look up an `.env` variable | [Configuration](docs/configuration.md) |
| Understand what a feature does | [Features (English outline)](docs/features.en.md) |
| Fix an empty list, a PVE connection error, a black console | [Troubleshooting](docs/troubleshooting.md) |
| Work on the code, run the tests | [Layout and tests](docs/development.md) |
| Send a pull request | [Contributing](CONTRIBUTING.md) |
| See what is planned | [Roadmap](docs/roadmap.md) |
| PBS, frp real IP, alert mail, data ownership | [All docs](docs/README.md) |

---

## License

[Apache-2.0](LICENSE).

If this project helps you, a star is the most direct way to support it.
