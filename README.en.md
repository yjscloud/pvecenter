# pvecenter — Management Panel for Proxmox VE 8.x / 9.x (PVE Web UI)

**English | [简体中文](README.md)**

[![License](https://img.shields.io/github/license/yjscloud/pvecenter?color=blue)](LICENSE) [![Release](https://img.shields.io/github/v/release/yjscloud/pvecenter?sort=semver&color=success&label=release)](../../releases) [![CI](https://github.com/yjscloud/pvecenter/actions/workflows/ci.yml/badge.svg)](https://github.com/yjscloud/pvecenter/actions/workflows/ci.yml) [![Stars](https://img.shields.io/github/stars/yjscloud/pvecenter?style=flat)](../../stargazers) [![Forks](https://img.shields.io/github/forks/yjscloud/pvecenter?style=flat)](../../forks) [![Issues](https://img.shields.io/github/issues/yjscloud/pvecenter)](../../issues) ![Proxmox VE](https://img.shields.io/badge/Proxmox%20VE-8.x%20%2F%209.x-e57000) ![Python](https://img.shields.io/badge/python-3.11%2B-3776ab) ![React](https://img.shields.io/badge/react-19-61dafb)

**pvecenter** is an open-source, self-hosted **Proxmox VE (PVE) web management panel**. One interface for QEMU virtual machines, LXC containers, cloud-init templates, storage and networking, snapshots and backups, internal tunneling, the VNC console, certificates and account permissions — and it can drive several PVE hosts at once. Install it as a systemd service or run it with Docker; it comes back by itself after a reboot.

> 自托管的 **Proxmox VE（PVE）Web 管理面板**：QEMU 虚拟机、LXC 容器、cloud-init 模板、
> 网络与防火墙、备份、监控、证书与 RBAC。

[Live demo](https://pvecenter.yjscloud.com) · [Screenshots](#screenshots) · [Key features](#key-features) ·
[Quick start](#1-deployment) · [Troubleshooting](#6-troubleshooting) · [简体中文](README.md)

Built for homelabs, small teams, and anyone who needs to open a PVE cluster to several people.

> Keywords: Proxmox VE panel · PVE panel · PVE web UI · LXC container management · QEMU / KVM ·
> cloud-init templates · self-hosted virtualization platform · Proxmox alternative UI · homelab ·
> self-hosted

## Contact and feedback

Questions, feature requests or help with deployment: scan the WeChat code below, or leave a note in
[Issues](https://github.com/yjscloud/pvecenter/issues). Some features have not been tested hard yet — just
say what broke.

pvecenter is maintained by one person. If it saved you time, a voluntary tip helps pay for servers, test
environments and the upkeep; not tipping changes nothing about support.

<table>
  <tr>
    <td align="center" width="33%"><img src="docs/screenshots/wechat.png" alt="WeChat: feedback and deployment help" width="180" height="257"><br><sub><b>WeChat</b> · feedback / deployment help</sub></td>
    <td align="center" width="33%"><img src="docs/screenshots/weixin.jpg" alt="WeChat tip code: voluntary support" width="180" height="257"><br><sub><b>Tip</b> · voluntary</sub></td>
    <td align="center" width="33%"><img src="docs/screenshots/zhifubao.jpg" alt="Alipay tip code: voluntary support" width="180" height="257"><br><sub><b>Alipay tip</b> · voluntary</sub></td>
  </tr>
</table>

## Screenshots

> Screenshots live in `docs/screenshots/`, ideally **1600×900** (16:9). Swap the filenames freely; while a
> file is missing GitHub shows the alt text, and it appears as soon as you add the image.

<!--
Placeholder list (each shows up once the file is in docs/screenshots/):
vm-list.png / vm-create.png / lxc-list.png / template-pipeline.png / firewall.png /
ssh-security.png / security-baseline.png / ai-assistant.png / monitoring.png /
backup.png / console.png / audit-log.png / certificates.png / users-rbac.png
-->

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/dashboard.png" alt="pvecenter dashboard: node status, CPU / memory / storage and recent tasks"><br><sub><b>Dashboard</b> · node status / resource use / recent tasks</sub></td>
    <td width="50%"><img src="docs/screenshots/vm-list.png" alt="Proxmox VM list: batch power operations, migration and quotas"><br><sub><b>VM list</b> · batch actions / migration / quotas</sub></td>
  </tr>
  <tr>
    <td width="50%"><img src="docs/screenshots/lxc-list.png" alt="LXC management: lifecycle, snapshots, cloning and migration"><br><sub><b>LXC containers</b> · lifecycle / snapshots / migration</sub></td>
    <td width="50%"><img src="docs/screenshots/console.png" alt="In-browser noVNC console: no need to expose PVE port 8006"><br><sub><b>VNC console</b> · drive it from the browser</sub></td>
  </tr>
</table>

## Key features

- **Virtual machines**: four-step and batch creation, power operations, cloning, migration, live reconfiguration, disk resize, snapshots, OS reinstall; OVF / OVA import and VMDK / QCOW2 / RAW / OVA export
- **LXC containers**: create, start/stop, snapshots, cloning, migration, rootfs resize, console
- **cloud-init templates**: build one from a cloud image or a download URL, then inject hostname, SSH key and IP on clone
- **Many PVE hosts**: aggregate nodes, guests, storage and tasks across connections; focus on one cluster or hand out region-scoped administration
- **Storage**: browse content, upload (suffix corrected by file magic), let PVE download from a URL, delete volumes, capacity and usage
- **Networking and firewall**: bridge / bond / VLAN, IP address pools, PVE native firewall, security groups, IP sets
- **Internal tunneling**: a built-in frpc that maps guest ports to the internet, with port ranges, target allowlists and per-user grants
- **Monitoring and alerting**: live metrics, RRD charts, capacity forecasting, disk SMART / ZFS; notifications over mail, Feishu, webhooks and the in-app inbox
- **Backup and restore**: back up now or on a schedule, browse volumes, restore to a given VMID, delete protection, integrity checks and PBS verification state; optional read-only PBS access for repository usage, GC / verify tasks and disk health
- **Certificates**: Tencent Cloud / Alibaba Cloud / Huawei Cloud / ACME / hosted certificates, auto-renewed and deployed to the local host, SSH hosts, PVE guests or nodes
- **Security operations**: SSH brute-force protection, login audit, security baseline, port and process inspection, emergency response
- **AI assistant**: attributes findings from inspection data, read-only command allowlist, write commands approved one by one
- **Accounts and permissions**: built-in and custom roles, per-user / per-role quotas, node visibility, two-factor auth, session management, personal API tokens
- **Day to day**: background jobs, task queue, in-app notifications, a `Ctrl / ⌘ + K` command palette, one-click updates, branding and nav switches

## Quick start

Install it as a systemd service so it comes back after a reboot:

```bash
git clone https://github.com/yjscloud/pvecenter.git
cd pvecenter
sudo ./deploy.sh
```

The last screen prints the panel address and the initial password, shown only once. Prefer containers?
See [Docker deployment](#docker-deployment); both routes are covered under [1. Deployment](#1-deployment).

Stack: Python 3.11+ / FastAPI / httpx, React 19 + TypeScript + Vite, MySQL 8.

## Contents

- [Screenshots](#screenshots) · [Key features](#key-features)
- [1. Deployment](#1-deployment): [Create a token](#create-a-proxmox-api-token) · [Grant permissions](#grant-permissions) ·
  [Bare metal](#bare-metal-deployment-recommended) · [Docker](#docker-deployment) · [HTTPS](#put-it-behind-https) ·
  [Service management](#service-management)
- [2. Features](#2-features) · [3. Configuration](#3-configuration) · [4. Project layout](#4-project-layout) ·
  [5. Tests](#5-tests) · [6. Troubleshooting](#6-troubleshooting) · [7. Security notes](#7-security-notes) ·
  [8. Roadmap](#8-roadmap)
- [Contributing](#contributing) · [License](#license)

---

## 1. Deployment

### Create a Proxmox API token

The panel talks to Proxmox with an API token. On a PVE node:

```bash
pveum user add panel@pve --comment "pvecenter panel"
pveum user token add panel@pve panel --privsep 0
```

Put the `full-tokenid` from the output (for example `panel@pve!panel`) into the panel as Token ID, and
the `value` as Token Secret.

### Grant permissions

```bash
pveum acl modify / --user panel@pve --roles PVEVMAdmin,PVEDatastoreUser,PVESDNUser,PVEAuditor,PVEVMUser
```

For least privilege, split them by use:

| Capability | Role |
|---|---|
| Read-only monitoring | `PVEAuditor` |
| Power on / off, console | `PVEVMUser` |
| Create / delete / reconfigure / snapshot | `PVEVMAdmin` |
| Backups, ISO and template storage | `PVEDatastoreUser` (`PVEDatastoreAdmin` to write) |
| Bridges / VLANs | `PVESDNUser` |

> The console also needs a PVE user name and password. Proxmox does not let an API token touch
> `vncproxy`; it only accepts a ticket from a user password. Without it, only the console is missing —
> everything else works.

### Bare-metal deployment (recommended)

Any systemd distribution works (Ubuntu / Debian / CentOS / Rocky / Alma / Fedora). It installs a unit, so the
panel comes back after a reboot, restarts if the process dies, and a code change only needs
`systemctl restart`. Panel and database run straight on the host — the least fuss for a long-lived install.

```bash
git clone https://github.com/yjscloud/pvecenter.git
cd pvecenter
sudo ./deploy.sh
```

One run installs the Python dependencies, builds the frontend, creates the database and installs the
unit, asking nothing: port `8080`, database `pvecenter_panel`, user `pvecenter`, `SECRET_KEY` and the
admin password generated at random. The last screen prints the address and password. Re-running it will
not damage an existing install.

Pass your own values and it still won't ask:

```bash
sudo ./deploy.sh --port 9000 --mysql-root-password '<root password>' \
                 --db-name pvecenter_panel --db-user pvecenter --db-password '<db password>'
```

Common flags: `--port`, `--service` (unit name), `--user` (run-as user, default `root`), `--skip-frontend`
(reuse an existing `dist/`, for machines without Node), `--no-systemd` (prepare the environment only, no
root needed), `--reconfigure` (confirm each value), `--help` for the rest.

> Building the frontend locally needs Node.js **22.22+** (the floor for `react-router 8`; vite 8 wants
> 22.12+ itself). If the local version is too old, `deploy.sh` fills the gap: first from the distro
> repository (RHEL 9 switches to the `nodejs:22` module stream), and if that fails it downloads the
> official binary from npmmirror / nodejs.org into `.node/` under the repo and builds there — no system
> sources touched, no system Node replaced, delete the directory to uninstall, re-runs reuse it. Pin a
> version with `NODE_BOOTSTRAP_VERSION=22.22.1 sudo ./deploy.sh`. Only when all three routes fail
> (musl-based Alpine, fully offline, unsupported architecture) does it stop and print the manual options:
> NodeSource / nvm, build elsewhere and copy `dist/` over with `--skip-frontend`, or switch to Docker. Not
> a single Node on the host is fine as long as `dist/` is current.

> “Update now” downloads a source archive, and `github.com` is often slow from mainland China. Since 0.3.2
> the download has a connect timeout and an overall deadline, so a failure reports an error instead of
> hanging. If it really can't connect, add a mirror prefix in `systemctl edit pvecenter`
> (`Environment=PC_UPDATE_MIRROR=https://ghfast.top/`) and retry — or grab the archive and upgrade by hand,
> or switch to Docker.

> Never change `SECRET_KEY` once generated: every stored PVE token and SMTP password was encrypted with it
> and becomes undecryptable. When `.env` already exists, the script only overwrites keys you pass
> explicitly on the command line.

To watch the logs in the foreground without installing a service: `npm run build && ./start-prod.sh`.
When `dist/` exists, FastAPI serves both the frontend and `/api` on 8080, so everything is same-origin and
the console's WebSocket needs no extra proxy.

### Docker deployment

Don't want Python / Node / MySQL on the host? Run it in a container; the host only needs Docker:

```bash
curl -fsSLO https://raw.githubusercontent.com/yjscloud/pvecenter/main/docker-compose.yml
docker compose up -d
```

Open `http://<server-ip>:8080` and sign in as `admin` / `pvecenter@2026`. Change that password.

All three passwords live in `docker-compose.yml`; search for `★ 改这里`. The lines look like this:

```yaml
ADMIN_PASSWORD: ${ADMIN_PASSWORD:-pvecenter@2026}
```

`:-` is compose's “use this default” marker: if `.env` or the environment provides a value, use it,
otherwise use what follows. The `-`, `$`, `{` and `}` are syntax only — the actual password is the
`pvecenter@2026` part, so change just that segment:

```yaml
ADMIN_PASSWORD: ${ADMIN_PASSWORD:-MyPassw0rd2026}
```

The three are `ADMIN_PASSWORD` (panel login), `DB_PASSWORD` (database) and `MYSQL_ROOT_PASSWORD` (MySQL
admin). You can also leave the file alone and drop a `.env` next to it with the password after the equals
sign; `.env` wins.

Two things to know up front:

- Passwords only take effect on first initialisation. `ADMIN_PASSWORD` is used only while the database has
  no administrator, and MySQL reads `MYSQL_PASSWORD` only when its data directory is empty. Changing them
  later means `docker compose down -v` and a rebuild (data gone) or an `ALTER USER` by hand (commands at
  the bottom of `docker-compose.yml`). To change your login password, use Profile → Change password.
- Leave `SECRET_KEY` alone. It signs login JWTs and is the encryption root for stored ciphertext (PVE
  tokens, SMTP password). Committing one to a public repo publishes your key, so the container generates
  one on first start and keeps it in the `panel_data` volume; change it later and every stored secret
  becomes undecryptable.

Images build for amd64 (PVE is x86_64-only) and come from GHCR. For Docker Hub:
`PVECENTER_IMAGE=docker.io/yjscloud/pvecenter:latest docker compose up -d`. Upgrade with
`docker compose pull && docker compose up -d`.

### Put it behind HTTPS

The panel doesn't do TLS itself. To expose it, terminate TLS on 443 with Nginx or Caddy and proxy to 8080.
The WebSocket must be proxied too, or the console won't connect:

```nginx
server {
    listen 443 ssl http2;
    server_name panel.example.com;
    ssl_certificate     /etc/letsencrypt/live/panel.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.com/privkey.pem;
    # Security headers (HSTS / CSP / nosniff / Referrer-Policy / Permissions-Policy)
    # are emitted by the panel itself — do **not** add them again here: duplicate
    # headers of the same name make an invalid response.
    # If the proxy layer serves other sites too, add what you need, but never
    # duplicate what the panel already sends.
    server_tokens off;          # answer "Server: nginx" without the version

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;        # both lines are needed by the console
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;    # tells the panel the outside is https
        proxy_read_timeout 3600s;                      # the console is long-lived
        proxy_buffering off;
    }
}
```

Turn on forced HTTPS on the panel side as well, otherwise hitting 8080 directly is still plain HTTP:

```
FORCE_HTTPS=true
FORWARDED_ALLOW_IPS=127.0.0.1
```

Set `FORWARDED_ALLOW_IPS` to the proxy address only. Widen it and anyone can claim
`X-Forwarded-Proto: https` to skip the redirect. With Docker, put both into the `.env` next to
`docker-compose.yml` (or straight into `environment:`).

### Public exposure hardening (strongly recommended)

The panel is a full-control entry point to PVE: one compromised account means the whole virtualization
platform is gone — power operations, deleting guests, changing passwords, the VNC console, host networking
and firewall all live in here. “Only the panel needs an inbound entry point” in the FAQ describes the
design, not a security boundary. Exposing it to the internet takes extra defences.

**None of the switches below are in the UI.** They live in a config file; which one depends on the
deployment:

| Deployment | File to edit | Notes |
|---|---|---|
| Source / bare metal (`deploy.sh`, systemd) | `<install dir>/backend/.env` | Created by `deploy.sh` (template `backend/.env.example`). On this host that is `/data/pvecenter/backend/.env`; `--reconfigure` asks for each value and writes back |
| Docker Compose | the `.env` next to `docker-compose.yml` | Compose reads it via `${VAR:-default}`; editing the compose `environment:` block works too |
| Backend only (`start-prod.sh` / your own unit) | whatever `backend/.env` the process loads | `systemctl cat pvecenter` shows `EnvironmentFile=` / `WorkingDirectory=` |

Restart to apply: `systemctl restart pvecenter`, or `docker compose up -d` for Docker. The same paths appear
in the matching entries under Settings → Security posture.

1. **Prefer a private entrance**: put the panel behind a VPN / zero-trust gateway, or allow only fixed
   office / jump-host networks. For frp, restrict sources on the frp server or enable mTLS at the proxy.
2. **Source allowlist (the panel enforces it too)**: set `PANEL_ALLOW_CIDRS` and non-listed sources cannot
   even see the login page or `/api/openapi.json` (HTTP and VNC WebSockets are both blocked).
   ```
   PANEL_ALLOW_CIDRS=203.0.113.0/24,10.8.0.0/24,192.168.1.10
   ```
   Behind a proxy, `FORWARDED_ALLOW_IPS` must hold the proxy's **real address**, otherwise the allowlist
   sees the proxy IP. Nginx can do the same job — either or both:
   ```nginx
   allow 203.0.113.0/24;
   allow 10.8.0.0/24;
   deny  all;
   ```
   With a CDN / cloud WAF in front, restore the real source with `set_real_ip_from` + `real_ip_header`, or
   `$remote_addr` is always the CDN address.
3. **Require 2FA for admins**: `TOTP_REQUIRED_ROLES=admin`. An admin who hasn't bound TOTP gets 403 on
   everything except the 2FA endpoints (bind it under Profile). Keep login lockout (per account and per
   IP) and the human check on in a public deployment; the check guards **sign-in and self-registration
   alike** (slider / image code), otherwise the registration endpoint gets scripted into flooding the
   approval queue.
4. **New-IP sign-in alerts**: on by default (`LOGIN_IP_ALERT=true`). The first successful sign-in of an
   account from a new IP notifies every admin in-app, plus email once SMTP is set up. It shows in the audit
   log as `auth.new_ip`, searchable per account.
5. **Don't expose 8080 directly**: with Docker, set `PANEL_BIND=127.0.0.1` so only a same-host proxy can
   connect. Never set `FORWARDED_ALLOW_IPS=*`: clients can forge their source IP, and the allowlist, login
   lockout and audit IPs all go wrong together.
6. **Audit regularly**: `/api/audit` and `/api/host-audit` are admin-only by default. Watch failed
   sign-ins, `auth.new_ip`, user / permission changes and sensitive operations (deleting guests, changing
   credentials).
7. **API docs are off by default**: `/api/docs`, `/api/redoc` and `/api/openapi.json` return 404. The full
   OpenAPI definition is a complete attack map — every endpoint, field constraint and hidden route. For
   troubleshooting, open it temporarily with `API_DOCS_ENABLED=true`; even then those three addresses need
   a signed-in user with `settings.manage`, and anonymous callers only get 401.
8. **`/api/health` tells anonymous callers only “alive”**: `{"status":"ok"}`, no PVE version, node count or
   connection state (a version string matches straight against public vulnerability lists). Signed-in
   callers still get the full reading; deep diagnostics live at `/api/config/diagnostics` (admin).
9. **Security headers come from the panel**: CSP (`script-src 'self'` on normal pages),
   `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
   `Permissions-Policy`, `frame-ancestors 'none'`, `X-Frame-Options: DENY` and HSTS are all added at the
   application layer (see `SecurityHeadersMiddleware` in `backend/app/main.py`). The proxy terminates TLS
   only — **don't add the same headers again**; two HSTS headers make an invalid response.
10. **TLS verification towards PVE is off by default**: new connections start with `verify_ssl: false`
    (`PVE_VERIFY_SSL` too). PVE ships a self-signed certificate (cluster CA, `CN=pve.local`, no IP SAN), so
    verification fails at the handshake and reads like a wrong address or token while wiring up a fresh
    connection. If you want it, don't just tick the switch: paste the PVE certificate PEM into the
    connection's **Certificate PEM** field and pin to it (`ca_cert` trusts only that one certificate and
    must be refreshed after rotation) — stricter than the system trust store and independent of the switch.
    Fetch it with `openssl s_client -connect <PVE-IP>:8006 </dev/null 2>/dev/null | openssl x509`, or read
    `/etc/pve/pve-root-ca.pem` on any node. Turn the switch on when the certificate comes from a public CA
    (or is already pinned). Unverified connections are flagged in the startup log.
11. **Put the backend back on loopback once a proxy is in front**: with `HOST=127.0.0.1`, 8080 isn't open
    to the LAN and every entry goes through nginx (a default install binds `0.0.0.0` so `IP:8080` works out
    of the box; once a proxy is in place, switch back). On the proxy, also set `client_header_timeout` /
    `client_body_timeout` / `send_timeout` and `limit_conn` (the nginx example above carries the first
    three) so slow connections can't exhaust workers.
12. **API tokens are capped at 365 days**, with no “never expires”: a token is an unattended credential, and
    expiry is the fallback after a leak. Re-issue before it lapses (the list shows the remaining days).
13. **Don't advertise component versions**: the app no longer sends `Server` / `X-Powered-By`
    (`server_header=False` in `run.py` plus the security-header middleware), and nginx should use
    `server_tokens off;` so it answers `Server: nginx` only.
14. **No sourcemaps in production**: `build.sourcemap=false` in `vite.config.ts` — `sourcesContent` in a
    `.map` is the complete TypeScript source. nginx answers 404 for `*.map` as a second line of defence
    against stale artefacts. Use `npm run dev` when you need symbols.
15. **Version and RBAC listings need a session**: `/api/version` and `/api/auth/permissions` now require a
    signed-in user; only `/api/config/site` (site name, logo, ICP number) stays anonymous so the login and
    landing pages can render.

For client certificates (mTLS), add this to the Nginx server block:

```nginx
ssl_client_certificate /etc/nginx/client-ca.crt;
ssl_verify_client on;   # or optional plus the if below
# if ($ssl_client_verify != SUCCESS) { return 403; }
```

### Service management

`./deploy.sh` installs a systemd unit, so the panel comes back after a reboot and restarts if the process
dies:

| Action | Command |
|---|---|
| Status / start / stop / restart | `systemctl status\|start\|stop\|restart pvecenter` |
| Enable at boot | `systemctl enable pvecenter` |
| Follow the logs | `journalctl -u pvecenter -f` |

A frontend-only change needs `npm run build`; a backend change needs `systemctl restart pvecenter`.
Confirm it came up with `curl http://127.0.0.1:8080/api/health`.

> On Docker, skip this section — the container already carries `restart: unless-stopped`.

---

## 2. Features

### Virtual machines (QEMU / KVM)

A four-step wizard (VMID, OS, disks and NICs, cloud-init) covering blank creation, cloning from a template
and importing a cloud image. Power on/off (graceful shutdown with a timeout and force-stop fallback),
reboot, suspend, resume, delete; online changes to CPU / memory / name / tags / start-at-boot / protection,
disk resize, storage migration, cross-node migration. The list lets you act on several guests at once and
reports per target — the third failure doesn't stop the rest, and failed ones can be retried alone.
Resetting a guest user password prefers the Guest Agent (immediate) and falls back to writing `cipassword`
through cloud-init plus a regenerated config drive when the agent is missing or the guest is off.

Batch creation uses the same endpoint (`POST /api/vms/batch`): submit several guests, each advances to
success or failure, failures are not retried automatically. Besides template / image creation you can
**import OVF / OVA** (upload → parse the OVF → confirm target storage, bridge and bus; firmware, controller,
EFI and serial mismatches are reported item by item), **export** as VMDK / QCOW2 / RAW / OVA through a
background job (powered-off guests only, artefacts stream straight to the browser), and **reinstall** from a
cloud-init template while keeping VMID, MAC, CPU and disk topology — the network keeps the original address
by default.

<!-- screenshot: docs/screenshots/vm-create.png -->
![Four-step VM creation wizard: OS, disks and NICs, cloud-init](docs/screenshots/vm-create.png)

### LXC containers

Containers are a different set of PVE endpoints and a parallel set of pages here. A three-step wizard takes
templates from storages whose content type includes `vztmpl` (when there is none, it prints the
`pveam download` command). Lifecycle, snapshots (without memory state), cloning (full only — PVE has no
linked clones for containers), migration, console; rootfs and mount point resize, adding mount points,
storage migration; adding and removing NICs, with the IP written into the NIC config and effective after a
restart.

<!-- screenshot: docs/screenshots/lxc-list.png -->
![LXC management: lifecycle, snapshots and migration](docs/screenshots/lxc-list.png)

Resetting the root password inside a container goes through SSH → Managed hosts. Containers have no Guest
Agent and PVE has no “run a command inside the container” endpoint, so the panel runs `pct exec` with that
host's SSH credentials. With no credentials configured, the dialog says so and suggests using the container
console and `passwd`.

### cloud-init template pipeline

Build a template from a cloud image in one action; the backend runs the steps in order and waits for each
task: create the VM (the system disk uses `import-from=<image>` so PVE imports it directly, instead of the
`importdisk` endpoint removed in PVE 9.2) → configure boot order and the cloud-init drive → resize if needed
→ convert to a template. Any failure deletes the temporary VM, so no half-built guest is left. Once built,
“deploy from template” starts guests in batch and injects hostname, SSH key and IP on clone.

<!-- screenshot: docs/screenshots/template-pipeline.png -->
![cloud-init template pipeline: from a cloud image to a template ready for batch cloning](docs/screenshots/template-pipeline.png)

> **An image's extension must match its real format**: `.qcow2` / `.raw` / `.vmdk` / `.vdi` / `.vpc` /
> `.qed`. PVE only parses those suffixes and copies anything else verbatim as a raw disk. The classic case
> is Ubuntu's official `*-cloudimg-amd64.img`: it is really a qcow2 (3.5 GiB virtual, 248 MiB on disk), and
> copied as raw its first sector is `QFI\xfb` instead of a boot signature — the template builds fine and
> then stops at `Boot failed: not a bootable disk`. Uploads through the panel are detected by file magic and
> renamed (with a notice); files pushed in through the PVE UI or CLI can't be intercepted, so rename them
> yourself or run `qemu-img convert -O raw <source> <target>.raw`.
>
> The image can live on dir / NFS / CIFS or block storage: the source only has to be readable from that
> node, and the target storage must support `images`.
>
> You don't have to download by hand either: fill in “image download URL” and the panel hands it to **PVE
> itself**, which downloads into the import directory of the chosen storage (the panel never relays the
> bytes, so a tens-of-gigabytes image neither fills memory nor blows up the reverse proxy), then selects it
> as the image source. The file name is corrected to a suffix PVE parses (`.img` → `.qcow2`).

### Nodes and multiple PVE connections

One panel can talk to several Proxmox hosts: nodes, guests, storage, tasks and global search are
**aggregated across connections**, and every row says where it came from. Same-named nodes and storages on
two connections are never merged — they are separate resources on separate machines.

A connection can carry a “region” label (site / environment), and the node list opens as
region → connection → nodes. The cluster scope in the top bar narrows the current page to one cluster
(stored per user, exposed as `?scope=`, hidden when there is only one connection). The connection settings
page is administrator-only, and token secrets never go back to the browser.

Permissions follow connections too: the built-in `region_admin` role sees and manages only the regions
assigned to it, and a connection without a region belongs to no region administrator — otherwise “forgot to
set the region” would be a gap anyone could manage. Ordinary accounts can also have per-node visibility:
hide special nodes from ordinary users, then allow specific users or roles. The order is
user > role > default > visible when no record exists, and writes are enforced by the backend.

The node detail page has a disk health card: SMART attributes and wear, failed attributes highlighted, ZFS
pool state and fragmentation.

When a guest is reassigned (or taken back), what data follows and what doesn't is defined in
[Ownership and data boundaries](docs/归属与安全数据边界.md).

### Storage management

The storage page lists capacity and usage per node, browses `images` / `iso` / `import` / `vztmpl` /
`backup` / `snippets` content, uploads, deletes, or hands a URL to PVE instead of transferring bytes through
the panel.

Uploads stream straight to PVE, no memory buffering. Before sending, the panel reads the file magic: a
qcow2 / vmdk dropped into an `iso` storage is refused on the spot with two workable alternatives, and under
`import` the suffix is corrected (`.img` → `.qcow2`) with a notice — otherwise PVE copies the qcow2
container verbatim as a raw disk and the template stops at `not a bootable disk`. Download URLs behave the
same (archives and mismatched suffixes are refused before the download starts), and the task only reports a
result at the end, so the page shows elapsed time instead of inventing a percentage.

Volume lists are filtered by ownership for ordinary users; shared content such as ISOs and container
templates is not.

### Networking and firewall (Bridge / Bond / VLAN)

Create, edit and delete node-level Linux bridges / bonds / VLANs. Changes go into “pending” first and only
apply on “Apply configuration” — the same model as Proxmox, so a mistyped bridge can't cut the node off the
network.

<!-- screenshot: docs/screenshots/firewall.png -->
![PVE networking and firewall: bridge / bond / VLAN, security groups and IP sets](docs/screenshots/firewall.png)

The firewall wraps the native Proxmox firewall rather than building another rule engine, so rules
configured here are the same data the PVE UI and `pve-firewall` see. Three scopes (cluster / node / guest,
qemu and lxc detected automatically); rules support in/out, ACCEPT / DROP / REJECT, protocol, ports, source
and destination (IP, CIDR, `+ipset`), PVE macros, interface binding, log level and ordering. Security groups
are cluster-level rule sets: reference one rule and the whole group applies; edit the group and every
reference changes. IP sets hold reusable ranges; rule templates can be pushed to many guests at once
(replace or append) with per-target reporting, and warn when a guest NIC lacks `firewall=1` because the
rules won't take effect. Permissions split into `firewall.view` / `firewall.manage` (own guests) /
`firewall.cluster` (cluster-level and security groups, admin only by default).

The networking page also holds **IP address pools**: a pool = subnet + assignable range + bridge, with used
addresses derived by scanning every guest's static configuration. When creating, cloning or editing a NIC
you can pick a free address from the pools assigned to you, and address plus gateway are filled in. It
doesn't depend on DHCP or SDN; creating and editing pools still needs `network.manage`.

### Internal tunneling (frp)

The panel ships an frpc: enter the frps address and token, then install, start and stop it — a background
job watches it and restarts it when it dies. A rule maps `<guest IP>:<port>` to a public port. Ports default
to 40000–50000 and administrators can move that range to whatever frps actually serves (common when NAT or a
cloud security group opens only a few ports); 22 / 80 / 443 / 7000 / 7001 / 8006 and friends are always
refused regardless of the range.

Escalation is denied by default: ordinary users may only map their own guests, targets must pass an
allowlist (`FRP_ALLOW_LOCAL_CIDRS`, plus per-user grants), and loopback, reserved and the panel's own
address are always refused — pointing at the panel's subnet is blocked by default. Creating, editing and
deleting a rule asks for confirmation again; when a guest changes owner the old rule is marked `stale`
instead of quietly serving someone else. The merged view returns only your own rules with the token
stripped, and the log endpoint is administrator-only.

### SSH login security and fail2ban

Reads the SSH log of the host running the panel; other hosts in the cluster are out of reach, and the page
states which host the data came from. The source falls back in three steps — `/var/log/secure` →
`/var/log/auth.log` → `journalctl -u ssh -u sshd` — counts only real authentication failures, de-duplicates
by sshd PID, and ignores health checks and port-scan noise. Failed sources are grouped by IP and can be
banned in one click, marked trusted or added to an ignore list; fail2ban jails, ban counts, unbanning and
manual bans are all on the page, and custom policies are written only to the panel's own
`jail.d/panel-<jail>.local`, never `jail.local`. Alerts use the existing Feishu and mail channels: an IP
crossing the failure threshold or an unfamiliar IP signing in successfully raises a notice, and a recovery
notice follows when the attack stops.

After filling in address, user and private key or password under “Managed hosts” (encrypted with
`SECRET_KEY`), the same works on remote machines; the first connection asks you to confirm the SSH
fingerprint and later changes are refused. Permissions are `ssh.view` (visible to everyone by default) and
`ssh.manage` (ban, change policy — admin only by default).

<!-- screenshot: docs/screenshots/ssh-security.png -->
![SSH login security: failed sources grouped, fail2ban bans and alerts](docs/screenshots/ssh-security.png)

### Security baseline and port inspection

Both share one architecture. The target can be the local host (reading system files directly) or a managed
host (reusing the SSH channel to run one read-only command and parse the output locally); local and remote
share the same evaluation logic, so conclusions stay on the same footing. Permissions are `baseline.view` /
`baseline.manage` and `ports.view` / `ports.manage`, the latter two admin-only by default.

<!-- screenshot: docs/screenshots/security-baseline.png -->
![Security baseline and port inspection: score, hardening items and suspicious processes](docs/screenshots/security-baseline.png)

The baseline covers seven categories: SSH configuration, password policy, firewall, time sync, account
safety (empty passwords, UID 0) and key kernel parameters. The score is weighted by severity (high 3, medium
2, low 1) into an A–D grade; items that can't be checked stay out of the denominator, so missing permissions
never cause a false deduction. One-click hardening writes only files the panel names itself, validates SSH
with `sshd -t` immediately and re-reads `/proc` after sysctl changes, rolling back automatically on failure.
Items that could lock you out — disabling SSH password authentication, for example — are excluded from
one-click hardening and must be confirmed one by one.

Port inspection uses `ss -tulpn` (`netstat` as a fallback) to list protocol, listening address, owning
process and exposure; listening on a non-loopback address counts as “public”, and the firewall state
collected in the same pass raises the risk level when no firewall is active. Suspicious processes use
reverse-shell heuristics: `/dev/tcp` redirects, `nc -e`, `socat exec:`, sockets and `dup2` in scripts,
`curl|sh` and so on, combined with context (a shell holding an outbound connection, an executable in `/tmp`,
a deleted executable, known miner names). Policies accept “expected ports” and a process allowlist regex to
cut false positives.

> To be clear: port inspection is a heuristic, not an antivirus engine. Every hit lists the rule it matched,
> and the panel never kills a process on that basis. “Public” only means a non-loopback listener — whether
> it is really reachable depends on the firewall and the upstream network, so the report includes the
> firewall state and leaves the call to you. To inspect a Proxmox node, add it as a managed host; the PVE API
> has no “run a shell on the node” endpoint.

### Emergency response

- Isolate a suspicious VM in one click: snapshot for evidence → cut the network → power off → turn on VM
  protection. The order is deliberate; powering off first destroys the scene. Each step runs and reports
  independently, so a failed snapshot doesn't stop the network cut but is flagged in red. The cut sets
  `link_down=1` per NIC by editing that segment of the raw config string, leaving `bridge`, `tag` and
  `macaddr` untouched.
- Backup delete protection: register a backup volume as protected and set the `protected` flag on the PVE
  volume where possible; the panel's delete endpoint refuses protected backups. Even with an admin session,
  ransomware can't delete one without lifting protection in the UI first. Registered backups are also
  compared with actual PVE content periodically — a missing volume or changed metadata raises an alert.

> Boundaries: real immutability (WORM) can only come from the storage side (PBS retention, S3 Object Lock,
> read-only mounts); the PVE API can't do it. A snapshot is a crash-consistent disk copy, not a memory
> image; `link_down` only cuts the PVE virtual NIC, so PCI passthrough / SR-IOV NICs are unaffected. PVE
> doesn't return checksums for storage content, so a backup fingerprint is a hash of `volid|size|ctime` —
> enough to catch deletions and replacements, not silent bit flips.

### AI operations assistant

It hands the findings the platform already has (security baseline / ports and processes / login audit /
alerts / change log / metric history) to a large model for attribution and answers “which one do I fix
first”. Not a one-shot summary but a conversation: pick a managed host → read platform data first, run
read-only commands when needed → answer as it goes → conclusions become a report with `source_ref` on every
item pointing back to the original check, so you can verify them yourself.

<!-- screenshot: docs/screenshots/ai-assistant.png -->
![AI operations assistant: attribution from inspection data, every command approved individually](docs/screenshots/ai-assistant.png)

Read-only is the default tier. The tools are a finite set of parameterised command templates, with no “run
any command” catch-all; parameters are guarded by regex and path allowlists, and sudo is not used. Only
after ticking “authorise the AI to run commands on the target host” may the model compose read-only
pipelines itself (such as `ps aux | grep nginx`), and then the first executable of every segment must be on
the read-only allowlist, with `;`, `&&`, `||`, redirects and command substitution rejected, plus a blacklist
for things like `/etc/shadow`. Commands that change the system are always shown first and run only after
you approve them one by one; `rm -rf /`, `mkfs`, `dd` and power-off/reboot are blocked even when approved.

The remote terminal shares the same view: an interactive SSH terminal in the browser (xterm.js loaded on
demand), where your own commands and the AI's commands and output appear in the order they happened, so
there's no second window to check “what did the AI actually do on my machine”. Visibility follows
permissions (ordinary users see alerts / backups / inspection results for their own guests; SSH login
analysis needs `ssh.view`), and every run records model, step count, tokens and each tool call under
“Investigation records”, replayable and exportable as CSV (one row = one tool call). There is also a token
consumption view with 1 / 7 / 30 day totals, the input/output split and a per-model breakdown.

It needs `ai.exec` (authorise command execution) and `ai.terminal` (open a remote terminal), both admin-only
by default. A terminal hands the host's login credentials to whoever uses it, and the assistant's execution
powers go far beyond any other page.

> The model offers attribution suggestions, not conclusions. Every item carries a `source_ref` pointing at
> the original check — verify it yourself. The command allowlist and blacklist are generated live by the
> backend, so what “capability notes” shows is the same rule set enforced at execution time. Authorisation
> lasts for the current session only and resets when you switch hosts or start a new one; write-command
> approvals time out after 120 seconds and a timeout counts as a refusal.

### Monitoring and alerting

Total VM count and run distribution, CPU / memory / storage ring charts, a node overview, recent tasks and
the top 8 resource consumers; live node metrics come over `/ws/metrics` (pushed every 5 seconds,
reconnecting automatically and falling back to 10-second polling); disk capacity is extrapolated linearly
from the node root partition history for a daily growth rate and estimated days until full; nodes and guests
each have RRD charts. Alert rules take a threshold and a duration; notifications can go out over mail, a
Feishu group bot, generic webhooks and the in-app inbox (WeCom / DingTalk / Slack are covered by webhook
body presets — fill in the template), firing and recovery each send one message, and every channel can be
muted per alert source. When alert mail never arrives, work through the
[alert mail troubleshooting guide](docs/告警邮件排查手册.md) layer by layer instead of guessing at SMTP.

<!-- screenshot: docs/screenshots/monitoring.png -->
![Monitoring and alerting: node metrics, capacity forecasting and alert rules](docs/screenshots/monitoring.png)

### Task queue and in-app notifications

The task queue aggregates across connections (filtered by cluster scope) and exposes the raw PVE task log,
termination of a running task and cleanup of history; every step carries ownership checks, so knowing a
UPID isn't enough to touch someone else's task.

In-app notifications live behind the bell in the top bar: unread counts are polled separately and the panel
opens in its own window rather than a dropdown. Resource alerts, first sign-in from a new IP, certificates
about to expire, completed reinstalls and new panel versions all land there; clearing the list is audited.
Mail and Feishu stay independent, and the “mute by source” switches apply to all of them.

### Feishu bot

Bring the panel into a Feishu group: send “列表” (list), “状态 <name/ID>” (status), “开机 <name/ID>” (start),
“创建” (create), “快照”, “快照列表”, “回滚” (rollback) or “备份” (backup) to operate virtual machines.
Power-off, reboot and rollback first post a card and only run after you confirm.

The callback endpoint validates the signature and verification token, de-duplicates by `event_id` against
replays, and moves work off the 3-second request when it takes longer. App ID / Secret / Encrypt Key are
visible and editable to administrators only, the API returns `*_set` instead of secrets, and reading the
configuration is audited too.

### Command palette and global search

`Ctrl / ⌘ + K` opens the palette: one half is page navigation (matched in the current language, filtered by
permissions and nav switches), the other half is resource search (guests, nodes, storage and users queried
concurrently through `/api/search`, debounced by 200 ms). ↑↓ to move, Enter to open, Esc to close.

### Snapshots, backups and restore

Snapshot creation (optionally with memory state), rollback and deletion, plus a cross-guest view that pools
every machine's snapshots into one table; back up now (snapshot / suspend / stop modes), browse volumes,
restore to a given VMID, delete; create PVE scheduled backup jobs visually. Volumes can be registered as
protected and the panel's delete endpoint refuses them; a background job periodically compares registered
backups with actual PVE content (a `volid|size|ctime` fingerprint) and alerts when one disappears or its
metadata changes.

### Proxmox Backup Server (optional)

Once PBS is registered as a storage in PVE, the storage page shows a PBS badge, the last backup time, the
archive count and verification state, plus a console link — no extra configuration needed.

To also see PBS's own repository usage, GC schedule, node load, recent tasks and disk health, register a
read-only token under Nodes → Connections → PBS read-only access (`Datastore.Audit` / `Sys.Audit` is
enough). The panel only issues GETs against PBS: prune / GC / verify / sync stay in the PBS console, and the
credential itself carries no write permission. Without a token the whole block stays hidden — no empty
card on the backup page.

Two complementary inspections feed the alerting pipeline: PBS archive verification failures
(`backup verification`), and GC failures / disk health problems (`PBS health`). See
[docs/PBS监控.md](docs/PBS监控.md) (Chinese).

<!-- screenshot: docs/screenshots/backup.png -->
![Backup and restore: back up now, scheduled jobs, backup volumes and restore](docs/screenshots/backup.png)

### VNC console

An in-browser noVNC graphical console with automatic reconnect, full screen and a Ctrl+Alt+Del button. The
backend relays the WebSocket in both directions, so the PVE web interface never has to be exposed.

### Login audit and operation audit

Host login audit: `last` / `lastb` are parsed into account, terminal, source IP, time and status, plus boot
records and sudo / su escalation from auth.log. Events are pulled incrementally into `audit_log` with one
cursor per host, in the same table as the panel's own operations; the job runs every 5 minutes and
re-imports don't duplicate rows. Login logs inside a VM are unreachable, but the VM's IP can be matched
against each host's login records to answer “was this VM used to sign in to the host?”. If a host exposed
through frp should record the real source IP, see the [frp real-IP guide](docs/frp-real-ip.md).

<!-- screenshot: docs/screenshots/audit-log.png -->
![Login audit and operation audit: source IP, action and result](docs/screenshots/audit-log.png)

### Certificate management (ACME / multi-cloud / PVE nodes)

Talks to Tencent Cloud, Alibaba Cloud and Huawei Cloud free certificates, your own ACME account (Let's
Encrypt / ZeroSSL, with wildcard and multi-domain support) and manually uploaded managed certificates.
Renewal happens before expiry and deploys per site to a local directory, a remote SSH server, a PVE virtual
machine (Guest Agent), an LXC container, or the PVE node's own 8006 management certificate; a reload command
can follow, and on PVE nodes PVE writes the file and restarts pveproxy itself. Multiple site bindings, a
certificate inventory, expiry monitoring and failure back-off are included.

<!-- screenshot: docs/screenshots/certificates.png -->
![Certificate management: issuing channels, site list and automatic renewal](docs/screenshots/certificates.png)

### Multi-user, RBAC and quota

| Built-in role | What it can do |
|---|---|
| `admin` | Everything, including user management, connection settings, deleting VMs and cluster-level firewall |
| `operator` | Create / power / console / snapshot / backup / clone / reconfigure, manage the firewall of its own guests, view SSH statistics; cannot manage users, delete VMs or change cluster-level firewall |
| `viewer` | Read-only |
| `region_admin` | Region administrator: only sees and manages resources inside the regions (connection `region` labels) assigned to it |

Beyond those you can define **custom roles**: the backend groups every permission into a catalog
(`GET /api/permissions/catalog`) that you tick through. `admin` cannot be edited or deleted, and a role still
in use cannot be deleted either.

<!-- screenshot: docs/screenshots/users-rbac.png -->
![Multi-user and RBAC: roles, permissions, quota and approvals](docs/screenshots/users-rbac.png)

- Self-registration with admin approval: a new account stays `pending` and can't sign in; approval assigns
  role and permissions in one step. Email is required and the result is mailed (mail is a side channel — a
  failed send is only audited). Password reset uses a one-time link and answers the same sentence whether or
  not the account exists, so it can't be used to probe accounts.
- Every write operation (including failures) goes to the audit log with user, action, target, result, details
  and source IP, filterable; the result is one of `success` / `failed` / `partial` / `denied` / `accepted`.
  Ordinary accounts with `audit.view` only see their own records, and reading the audit log is recorded too.
- Guard rails: the last administrator can't be deleted, disabled or demoted, and the signed-in account can't
  be deleted.
- Resource quotas have two layers: a user-level setting wins, otherwise the role default applies, and only
  when neither exists is the account unlimited. Dimensions are guest count, cores, memory and disk for
  virtual machines and containers separately, plus per-guest caps on cores / memory / disk and a snapshot
  quota (total and per guest). “Inherit” and “custom but empty” differ — the latter explicitly means
  unlimited and overrides the role default. Quotas are enforced on reconfiguration, disk growth and
  snapshots, not just creation; guarding the front door alone guards nothing. Ordinary users see usage for
  **their own** guests, while platform-wide used / limit is shown only to administrators and on the settings
  page — so “how many are left in the cluster” is never mistaken for “how many may I still create”, and
  cluster size isn't disclosed.
- Connection settings can be changed online, are stored in MySQL and take effect on save without a restart;
  the token secret is never sent to the frontend. “Environment self-check” asks Proxmox for the credential's
  effective permissions and lists API reachability, node visibility, storage, guests and console credentials
  item by item, turning each anomaly into a runnable fix command.

### Account security: 2FA, sessions and API tokens

The profile page is split into basic information / change password / two-factor / API tokens / signed-in
devices / my permissions. Binding two-factor hands out 8 recovery codes shown once; `TOTP_REQUIRED_ROLES`
can force a role to bind TOTP before using the panel. “Trust this device” skips the code for 7 days on that
browser unless `TOTP_TRUST_ENABLED=false`.

Signed-in devices lists active sessions: revoke one, or sign out everywhere. Administrators can revoke every
session of an account, and a password change invalidates tokens on other devices. A single account keeps at
most `SESSION_MAX_PER_USER` devices; a new sign-in pushes out the oldest.

Personal API tokens are for automation: at most 365 days, “never expires” is not accepted, the clear text
appears once at creation (only a SHA-256 hash is stored), and issuing or revoking needs a browser session
plus step-up confirmation. Expired tokens are cleaned up by a background job.

### Panel maintenance (updates / jobs / branding / nav switches)

- **Updates**: the settings page checks for a new version, can skip one, and can point at another release
  repository. “Update now” downloads the source archive, `rsync`s it into place (excluding `.git`,
  `backend/.env`, `data`, `dist` and friends) and restarts; the request returns immediately and the log file
  has the progress. When GitHub is unreachable, set a `PC_UPDATE_MIRROR` prefix.
- **Background jobs**: every scheduled job is listed here (inspections, alerts, certificate renewal,
  ownership reconciliation, data cleanup, frp watchdog, version checks…), with intervals from 10 seconds to
  30 days, per-job enable / disable, “run now” with that run's result, and a reset to defaults.
- **Site branding**: site name, subtitle, ICP filing number, a custom logo and a login-page background image
  (both take effect immediately and can be removed to fall back to the built-in icon). `/api/config/site`
  stays anonymous because the login page reads it.
- **Nav switches**: individual sidebar entries can be switched off. Disabling one hides it from the sidebar,
  the top-bar menu, `Ctrl + K` and direct URLs alike, but **the API does not refuse it** (an entry is not an
  authorization); only “Settings” always stays reachable, otherwise there'd be no way back.

---

## 3. Configuration

`backend/.env` (Docker passes the same names as environment variables; the full sample is
`backend/.env.example`):

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | placeholder | Required. Signs login JWTs and is the encryption root for stored ciphertext; at least 32 random characters. A placeholder or a short value refuses to start |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / empty | Administrator created on first start. At least 12 characters; empty or weak passwords refuse to start |
| `HOST` / `PORT` | `0.0.0.0` / `8080` | Bind address. All interfaces by default, so `IP:8080` works right after install; switch back to `127.0.0.1` behind a reverse proxy |
| `FORCE_HTTPS` | `false` | Plain HTTP gets a 308 redirect to https, HTTPS responses carry HSTS; only loopback is exempt |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Which sources may set `X-Forwarded-Proto` / `X-Forwarded-For`. Keep it at the proxy address; **never `*`** |
| `PANEL_ALLOW_CIDRS` | empty | Panel access allowlist (IP / CIDR, comma separated). Non-listed sources can't reach the login page or any WebSocket |
| `LOGIN_IP_ALERT` | `true` | Notify administrators in-app and by mail on the first sign-in from a new IP |
| `API_DOCS_ENABLED` | `false` | Mount `/api/docs`, `/api/redoc`, `/api/openapi.json` (admin sign-in required); keep off in production |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | `5` / `15` | How many failures trigger a lockout, counted per account and per source IP |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | `720` / `14` | Token lifetimes; the latter is how long one sign-in can last |
| `SESSION_MAX_PER_USER` | `10` | How many signed-in devices one account keeps; a new sign-in pushes out the oldest. `0` = unlimited |
| `COOKIE_SECURE` | follows `FORCE_HTTPS` | Whether the refresh cookie is HTTPS-only |
| `TOTP_REQUIRED_ROLES` | empty (`.env.example` shows `admin`) | Roles forced to set up two-factor authentication |
| `TOTP_ISSUER` | `pvecenter` | Issuer name in the authenticator app; use the panel's public domain |
| `TOTP_TRUST_ENABLED` | `true` | Whether “trust this device” is offered (7 days on the same browser). `false` requires the second factor every time |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_PER_MINUTE` / `RATE_LIMIT_AUTH_PER_MINUTE` | `true` / `300` / `30` | Rate-limit switch, per-IP per-minute limits for ordinary requests and for sensitive endpoints (sign-in / registration / password reset / refresh / 2FA) |
| `CAPTCHA_RATE_LIMIT_PER_MINUTE` | `6` | Per-IP limit on captcha issuing (per minute), so anonymous traffic can't turn it into a CPU amplifier |
| `STEP_UP_REQUIRED` / `STEP_UP_WINDOW_MINUTES` | `true` / `5` | Whether dangerous operations ask for confirmation again, and how long that confirmation stays valid |
| `FRP_ALLOW_LOCAL_CIDRS` / `FRP_BLOCK_PANEL_SUBNET` | empty / `true` | Tunnel target allowlist: ordinary users may only point at their own guest IPs, and networks can be opened here (or single IPs per user on the frp page); pointing at the panel's own subnet is blocked by default |
| `FRP_REMOTE_PORT_MIN` / `FRP_REMOTE_PORT_MAX` | `40000` / `50000` | Initial / fallback public port range. Administrators can set it on the frp page to whatever frps serves (applied immediately and stored; the page wins). Critical ports such as 22 / 80 / 443 / 7000 / 7001 / 8006 are always refused |
| `PVE_HOST` / `PVE_PORT` | empty / `8006` | Proxmox address (no protocol) and API port |
| `PVE_TOKEN_ID` / `PVE_TOKEN_SECRET` | empty | The form is `panel@pve!panel` and the token UUID |
| `PVE_DEFAULT_NODE` | empty | Default node for node-scoped endpoints; a fallback when several nodes exist |
| `PVE_VERIFY_SSL` | `false` | Whether outbound connections verify the PVE certificate. Off by default: PVE is self-signed and verification fails at the handshake. For real security pin the certificate with “Certificate PEM” (`ca_cert`), or set this to `true` when it comes from a public CA |
| `PVE_TRUST_ENV` | `false` | Whether requests honour `HTTP_PROXY` / `HTTPS_PROXY` from the environment |
| `PVE_CONSOLE_USER` / `PVE_CONSOLE_PASSWORD` | empty | PVE account for the console; the console is unavailable when empty |
| `CORS_ORIGINS` | `http://localhost:5173` | Allowed frontend origins, comma separated |
| `HTTP_TIMEOUT` / `TASK_POLL_INTERVAL` | `30` / `2` | PVE request timeout (seconds) and task polling interval (seconds) |
| `DEBUG` | `false` | Adds `X-PVE-Calls` / `X-DB-Queries` counters to every response for performance work; keep off in production |
| `METRICS_HISTORY_ENABLED` / `METRICS_SAMPLE_INTERVAL` / `METRICS_RETENTION_DAYS` | `true` / `60` / `30` | Sampling into `metrics_history`: switch, interval (seconds) and retention (days, `0` = keep forever). PVE's own RRD rolls over and is lost when the panel restarts or a host is reinstalled |
| `PC_UPDATE_MIRROR` | empty | Mirror prefix for the one-click update (only after a direct GitHub attempt fails). Not in `.env.example`; add it with `Environment=` under `systemctl edit pvecenter` |
| `SSH_FORWARD_LOG_HOST` / `SSH_FORWARD_PROXIES` / `SSH_FORWARD_ROUTES` / `SSH_FORWARD_HAPROXY_CFG` | empty | Real source-IP recovery behind an entry host (haproxy + PROXY protocol): which host to read, optional overrides and explicit routes, haproxy config path. See the [frp real-IP guide](docs/frp-real-ip.md) |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `127.0.0.1` / `3306` / empty | MySQL connection; the database name is required |
| `DB_POOL_SIZE` | `8` | Connection pool ceiling |

Only MySQL is supported; create the database first, tables are created on startup:

```sql
CREATE DATABASE pvecenter_panel CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

Values in `.env` are initial defaults only; they can also be changed online under Settings, and the online
configuration wins. Mail (SMTP) has no environment variables — configure it under Settings → Mail
notifications. Its password is encrypted with `SECRET_KEY` before storage and the API only returns
`password_set`, never the clear text.

---

## 4. Project layout

```
pvecenter/
├── Dockerfile / docker-compose.yml / docker-entrypoint.sh   container packaging and deployment
├── deploy.sh                  bare-metal installer, idempotent, safe to re-run
├── start.sh / start-prod.sh   dev mode (with Vite) / production (backend serves dist/)
├── backend/
│   ├── run.py                 entry point
│   ├── .env.example           configuration sample
│   └── app/
│       ├── main.py            FastAPI app, route mounting, background job registration
│       ├── pve.py             Proxmox API client (token + ticket auth, multiple connections)
│       ├── vmconfig.py        builds and parses VM / container configuration
│       ├── bulk.py            batch operation orchestration
│       ├── security.py        JWT sessions, RBAC, regions / node visibility, audit helpers
│       ├── quota.py           two-layer (user / role) resource and snapshot quotas
│       ├── ownership.py       ownership table for guests and imported images (tenant isolation)
│       ├── scheduler.py       the panel's own scheduled job engine
│       ├── alerting.py        alert rules, channels (mail / Feishu / webhook / inbox), root-cause grouping
│       ├── store.py           MySQL: users, audit log, connection settings
│       └── routers/           one module per page: vms / lxc / templates / console /
│                              tasks / network / storages / firewall / backups /
│                              frp / feishu / alerts / certs / ai / baseline /
│                              portguard / isolation / ssh / hostaudit / users /
│                              roles / tokens / scheduler / update / audit / config
└── src/
    ├── api/                   axios client, types, endpoint wrappers
    ├── hooks/                 auth, toast, task waiting, WebSocket
    ├── components/ · pages/   layout, shared UI components and pages
    ├── i18n/                  Chinese and English strings
    └── styles/                light theme design system (single theme)
```

---

## 5. Tests

```bash
cd backend
../.venv/bin/python -m pip install -r requirements-dev.txt
../.venv/bin/python -m pytest        # roughly 1000 cases
```

Three layers, none needing a real Proxmox: pure logic (configuration assembly, permission matrices, field
aliases, partial failures and ownership checks in batch operations); a simulated Proxmox (a fake HTTP server
to verify auth headers, response unwrapping, task polling and error-to-status mapping); and full API paths
(TestClient from request to response, covering RBAC, audit, the three firewall tiers, SSH log parsing and
alerting, remote hosts, baseline decisions, port heuristics and every password channel).

> API tests connect to a separate `<DB_NAME>_test` database and skip automatically when MySQL is unreachable
> or the account can't create it, so the business database is never touched.

Frontend:

```bash
npm run typecheck     # tsc --noEmit
npm run build         # tsc -b && vite build
npm run verify        # all of the above, plus the i18n consistency check
```

To check the build output without Nginx, start the built-in static server (with `/api` and WebSocket
proxying): `npm run build && npm run serve:dist` (<http://localhost:8090>).

It talks to the PVE REST API; 8.x and 9.x share one code path with no per-version branches. VM and container
creation, start/stop and the console have been verified on PVE 8.4 and 9. The version string is for display
only, so upgrading Proxmox doesn't require code changes.

---

## 6. Troubleshooting

**You can sign in, but the VM and storage lists are empty.** Open Settings → Environment self-check first.
This is the most common API-token trap: a token created in the Proxmox UI has “privilege separation” ticked
by default, so it starts with no permissions. Privileged reads return 403, while endpoints like `/storage`
return 200 with an empty array and it looks like the panel is broken. The self-check asks PVE for the
credential's effective permissions and lists the exact commands:

```bash
pveum acl modify / --tokens 'root@pam!panel' --roles PVEVMAdmin
pveum acl modify / --tokens 'root@pam!panel' --roles PVEDatastoreUser
pveum acl modify / --tokens 'root@pam!panel' --roles PVEAuditor   # node metrics need Sys.Audit
```

The tell-tale sign: `privsep` is 1 in `pveum token list` and `/access/permissions` returns empty.

**The connection fails with 502 although Proxmox answers pings.** Check whether the panel host has
`HTTP_PROXY` / `HTTPS_PROXY` set — the panel doesn't inherit them by default. If it really must go through a
proxy, set `PVE_TRUST_ENV=true`; otherwise add the Proxmox address to `NO_PROXY`.

**The configuration looks right but the cluster is unreachable.** Connection settings resolve as
“database > `.env`”, so an address saved earlier wins over the new `.env` value. Change it under Settings, or
delete the `pve_connection` row from the `settings` table and restart. An unreachable Proxmox doesn't stop
the panel from starting; it only logs a warning — which is what gives you a chance to get in and fix it.

**With two or more PVE hosts, an action fails with `hostname lookup 'node02' failed`.** Node names are local
to each PVE host, so this usually doesn't mean DNS is broken — it means the request went to a **different**
PVE that doesn't have that node, and PVE tried to resolve the name as a host. Node-scoped endpoints
(storage, backups, console, template wizard, download tasks…) correct the connection from the node's owner,
and the frontend sends `X-PVE-Connection`. When you hit it, check which connection the node belongs to,
confirm the connection status on the Nodes page, or switch the cluster scope in the top bar and retry.

**The console doesn't open**: a Proxmox design limit — an API token can't access `vncproxy`, so add a PVE
account and password under Settings (ideally `PVEVMUser` only). **It connects but stays black**: templates
built by the panel attach only `serial0` and deliberately do **not** point `vga` at it — pointing it there
leaves nothing but serial output in VNC, and an image without `console=ttyS0` goes black right after GRUB.
For serial logs, switch the console to xterm; don't change `vga`.

**The template was built, but clones stop at `Boot failed: not a bootable disk`**: the image extension
doesn't match its real format. Ubuntu's official `*-cloudimg-amd64.img` is really a qcow2; PVE doesn't
recognise the suffix and copies the container verbatim as the system disk, so sector 0 isn't a boot sector.
Check with `qemu-img info <image>`, then convert to raw (`qemu-img convert -O raw`) or rename it to `.qcow2`
and rebuild the template. To inspect the disk itself:

```bash
qemu-img info <volume path>   # the virtual size should be the image's virtual size (3.5G for Ubuntu cloud images), not the file bytes
qemu-img dd -f qcow2 if=<volume path> bs=512 count=1 of=/tmp/mbr.bin && hexdump -C /tmp/mbr.bin | tail -2
```

The last two bytes should be `55 aa` (`eb 63 90` at the start is GRUB's MBR); `51 46 49 fb` means the whole
qcow2 container was written to the disk verbatim.

**Docker: the container won't start or keeps restarting.** Almost always a database password mismatch. Run
`docker compose logs panel | tail -30` and look for `Access denied` / `Can't connect`; the fix is in the two
notes under [Docker deployment](#docker-deployment).

**The panel runs on Python 3.9 / 3.10 and some features misbehave (typically: alert mail never arrives,
updates fail).** Dependencies are pinned to Python 3.11+, the hardest floor being Pillow 12 (≥3.10) — on 3.9
`pip install -r backend/requirements.txt` fails to resolve, dependencies stay old and the built-in updater
stops at the install step. Current `start-prod.sh` / `run.py` refuse an old interpreter outright and print
the install commands; `/api/version` shows the interpreter actually in use. Install 3.11+, then `rm -rf
.venv` and start again (database and configuration are untouched).

**The host security pages read nothing**: inside a container, `/var/log` and `/proc` belong to the container,
not the host. The page says so; mount them read-only as the comments in `docker-compose.yml` describe.

---

## 7. Security notes

Keys and passwords. A placeholder `SECRET_KEY` or one shorter than 32 characters refuses to start; so does
an empty `ADMIN_PASSWORD`, one under 12 characters or a weak one like `admin123`. The backend enforces both
— don't cut corners.

HTTPS is mandatory in practice. Credentials and API tokens travel in clear text over HTTP. Set
`FORCE_HTTPS=true` and terminate TLS on 443 with Nginx / Caddy, sending `X-Forwarded-Proto`; keep
`FORWARDED_ALLOW_IPS` at the proxy address only, or a public request can claim to be https and skip the
redirect. Close the panel-to-PVE gap too — pin the certificate with the connection's “Certificate PEM”
(`ca_cert`) or set `PVE_VERIFY_SSL=true` — so the token can't be intercepted in between. `--privsep 1` with
precise ACLs keeps the panel to the resources it needs, and PVE's 8006 never has to be exposed: everything
goes through the panel backend.

Secrets never come back to the frontend. `GET /api/config/connection` returns only `token_secret_set`, and
leaving that field empty on update keeps the existing value; the SMTP password only reports `password_set`.
Administrators can't read other users' webhooks or cloud keys either.

Sign-in protection has three layers. The human check runs before the password is verified (a script can't
read “was the password right?”), and a failed captcha doesn't count towards the failure limit — otherwise
wrong images could lock any account. Passwords are counted per account and per source IP, locked for several
minutes, and the counters live in MySQL so a restart doesn't clear them. Outside that sits a per-IP rate
limit over `/api/*` with a tighter line for sensitive endpoints, answering 429; it only trusts
`X-Forwarded-For` from a configured proxy, so forged headers get nowhere.

Sessions are revocable and writes go through CSRF. Sign-in issues an access and a refresh token; the refresh
token lives in an `HttpOnly` cookie that JavaScript can't read and XSS can't steal. Logging out revokes the
server-side session and kills the current token at once; “Profile → Login devices” kicks devices one by one,
and “sign out everywhere” or an admin kick drops every token of that account — as does a password change.
Writes must carry `X-CSRF-Token` (from the deliberately readable `panel_csrf` cookie), which a cross-site
page can't read or forge; the WebSocket handshake authenticates by cookie and checks `Origin`, so tokens no
longer appear in URLs.

Two-factor and step-up. `TOTP_REQUIRED_ROLES=admin` makes administrators bind an authenticator first;
binding hands out eight recovery codes once, and the secret is encrypted with `SECRET_KEY`. Deleting a VM,
changing connection credentials, adding or removing accounts, editing cluster-level firewall policy, pushing
rules in batch and writing fail2ban policies need more than a valid token: the backend answers
`403 + X-Step-Up: required` and asks for the password again (plus a TOTP code with 2FA on). Sensitive reads
are audited too — viewing connection settings, mail settings or the audit log each leaves a record. “Who
copied the cluster token” can only be answered from traces of reads.

---

## 8. Roadmap

What comes next, by importance rather than schedule:

- SDN networking. The “Networking” page handles node-level bridge / bond / VLAN and IP pools; PVE 8.x / 9.x
  SDN (zone / vnet / subnet / controller / IPAM) has no entry point yet. It is a cluster-level object where
  one configuration applies to the whole cluster, so the baseline is: reads always available, writes always
  behind a diff preview and a confirmation, and no “rebuild the whole SDN” style non-rollbackable action.
- Billing and usage. The groundwork is in place — metrics are sampled into `metrics_history` and quotas
  already count CPU / memory / disk — what's missing is the pricing and invoice layer: “by whom, how much,
  worth what”. The easiest thing to get wrong is the basis: PVE's network counters are cumulative and reset
  on reboot, migration or driver reset, so samples must be differenced and rollbacks dropped, otherwise the
  report shows negative or exploding traffic.
- The next step for verifiable backups is PBS. Registering, fingerprint checks, refusing deletes in the panel
  and alerting already catch deletions and replacements; silent bit rot only becomes visible with PBS
  verification checksums and immutability.
- Resource pools (PVE pools). Tenant isolation is partly there (ownership table, region administrators,
  quotas, node visibility); what's missing is a pool management UI and “isolation by pool” — today there is
  only a passthrough read-only `GET /api/cluster/pools`.
- Other candidates: report subscriptions (scheduled weekly / monthly reports — the metric history is already
  sufficient), a native Telegram alert channel (WeCom / DingTalk / Slack work today through webhook presets),
  Prometheus metrics export, audit forwarding to a SIEM, LDAP / OIDC single sign-on, a dark theme and theme
  switching, a PWA.

Known UI gaps (the API exists, the page doesn't): Ceph cluster status (`GET /api/cluster/ceph/status` is
already passed through); IP pools only offer “pick an address from the pools assigned to me”, with no
request → approval → assignment flow.

The rationale and acceptance criteria for each item live in issues.

---

## Contributing

[Issues](https://github.com/yjscloud/pvecenter/issues), forks and pull requests are welcome. Before
submitting, run `npm run verify` and the backend `pytest`; feature requests are easier to prioritise with the
use case attached.

If the project helps you, a star is the most direct support.

---

## License

Released under the [Apache-2.0](LICENSE) license.
