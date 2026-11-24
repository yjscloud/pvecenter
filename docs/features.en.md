# Feature guide (English outline)

The full feature guide is written in Chinese: [features.md](features.md). This page translates its
section headings and adds a sentence or two per area, enough to tell whether a feature exists and
where its limits are. Parameters, API behaviour and the long edge cases stay in the Chinese page.

> Back to [README](../README.en.md) · Related: [Deployment](deployment.md) · [Configuration](configuration.md)

## Virtual machines (QEMU / KVM)

Four-step create wizard (VMID, OS, disks and NICs, cloud-init) for blank VMs, clones and cloud images.
Power on/off (graceful shutdown with a forced-stop fallback), reboot, suspend, delete; live changes to
CPU, memory, name, tags, autostart and protection; disk resize, disk move, cross-node migration.
Batch operations run per VM and report each result, and failed items can be retried on their own.
Guest password reset prefers the Guest Agent and falls back to `cipassword` plus a regenerated config
drive. Import from OVF / OVA, export to VMDK / QCOW2 / RAW / OVA (powered-off VMs only), and OS
reinstall via cloud-init that keeps VMID, MAC, CPU and disk topology.

## LXC containers

A separate page set, because containers are a separate PVE API. Three-step wizard, with templates taken
from storages that carry `vztmpl` content. Lifecycle, snapshots (disk only, no memory state), full
clones only (PVE has no linked clones for containers), migration, console, rootfs and mount point
resize, extra mount points, storage moves, NIC add/remove. Root password reset goes through `pct exec`
over the managed host's SSH, since containers have no Guest Agent.

## cloud-init template pipeline

Builds a template from a cloud image step by step, waiting for each PVE task: create the VM (importing
the disk via `import-from`, since PVE 9.2 removed `importdisk`), set boot order and cloud-init drive,
resize if asked, then convert to template. A failed step deletes the temporary VM. Deploying from a
template injects hostname, SSH keys and IP. The image suffix must match the real format: PVE only parses
`.qcow2` / `.raw` / `.vmdk` / `.vdi` / `.vpc` / `.qed` and copies anything else as a raw disk, which
produces templates that will not boot. Uploads correct the suffix from the file magic; images pulled by
PVE itself do not get that check.

## Nodes and multiple PVE connections

One panel can hold many Proxmox hosts: nodes, VMs, containers, storages, tasks and global search are
aggregated across connections, with each row labelled by its connection. Same-named nodes on two hosts
stay separate. Connections can carry a region tag, and the top bar narrows the page to one cluster.
Connections can be added by hand or through batch connect, which generates a one-shot command to run as
root on the PVE node. Permissions follow the connection: the built-in `region_admin` only sees and
manages resources inside assigned regions, and per-user node visibility rules are enforced in the
backend.

## Storage

Per-node capacity and usage, browsing `images` / `iso` / `import` / `vztmpl` / `backup` / `snippets`,
upload and delete, or handing a URL to PVE to download without proxying bytes through the panel.
Uploads are checked against the file magic first: a qcow2 in an `iso` directory is rejected with the
two ways to fix it, and `import` rewrites the suffix to `.qcow2`. Users only see volumes they own;
shared ISOs and container templates are unaffected.

## Networking and firewall (bridge / bond / VLAN)

Node-level bridge, bond and VLAN changes sit in a pending state until you apply them, matching PVE, so a
broken bridge does not cut the node off immediately. The network page is a cross-node read-only summary
plus IP address pools; edits happen on the node's own tab.

The firewall wraps the native PVE firewall, so rules are the same data the PVE UI and `pve-firewall`
see. Three scopes (cluster / node / guest), inbound and outbound rules, protocols, ports, source and
destination matching, PVE macros, interface pinning, logging and ordering. Security groups are
cluster-level rule sets, IP sets hold reusable ranges, and rule templates can be applied to many guests
at once with per-guest results. Permissions split into `firewall.view`, `firewall.manage` (guests you
own) and `firewall.cluster` (cluster level and security groups, admin by default). IP pools are a
subnet plus allocatable range plus bridge, with used addresses scanned from static guest config; they do
not need DHCP or SDN.

## Tunnels (frp)

A built-in frpc that installs, starts and stops from the panel, with a background job that restarts it
if it dies. Rules map a guest `IP:port` to a public port; the range defaults to 40000-50000 and is
configurable for provider-side restrictions, while 22 / 80 / 443 / 7000 / 7001 / 8006 are always
blocked. Users can only map their own guests, targets must pass a CIDR allowlist, and loopback,
reserved and panel addresses are always refused. Rules left behind after a guest changes owner are
marked stale.

## SSH login security and fail2ban

Reads the SSH log of the host running the panel (other cluster hosts are not readable, and the page says
where the data came from), falling back from `/var/log/secure` to `/var/log/auth.log` to `journalctl`.
Only real authentication failures are counted, deduplicated by sshd PID, so health checks and port scans
do not inflate the numbers. Block, trust or ignore a source IP in one click; fail2ban jails, ban counts
and manual bans are on the page, and custom policy only writes the panel's own
`jail.d/panel-<jail>.local`. Alerts go out through the existing Feishu and mail channels, including
recovery notices. Managed hosts extend the same features to remote machines over SSH, with host key
verification on first connect.

## Security baseline and port inspection

One architecture for both: the target can be the panel host (reading system files directly) or a managed
host (running one read-only command over SSH). The same judgement logic runs in both cases. The baseline
covers seven categories (SSH config, password policy, firewall, time sync, account security, key kernel
parameters) and scores A to D by severity, skipping checks it cannot perform rather than penalising
them. One-click hardening writes only panel-named files, validates with `sshd -t` and by re-reading
`/proc`, and rolls back on failure; items that can lock you out are left to manual confirmation. Port
inspection uses `ss -tulpn` (or `netstat`), flags non-loopback listeners as exposed, raises severity when
no firewall is active, and applies reverse-shell heuristics. It is a heuristic, not an antivirus: every
hit names the rule it matched and the panel never kills processes.

## Incident response

Isolate a suspicious VM in a deliberate order: evidence snapshot, then network cut, then shutdown, then
protection flag. Each step runs and reports separately; a failed snapshot does not block the network
cut but is marked red. The network cut sets `link_down=1` per NIC while preserving `bridge`, `tag` and
`macaddr`. Backup delete protection registers backup volumes, sets PVE's `protected` flag where
possible, and refuses deletions through the panel, so ransomware with an admin session still has to
clear protection first.

> Limits: true immutability (WORM) needs the storage side (PBS retention, S3 Object Lock, read-only
> mounts). Snapshots are crash-consistent disk copies without memory. `link_down` does not cover PCI
> passthrough or SR-IOV NICs. PVE exposes no storage checksums, so backup fingerprints hash
> `volid|size|ctime`: they detect deletion and replacement, not silent bit rot.

## AI assistant

Feeds existing inspection data (baseline, ports and processes, login audit, alerts, change history,
metrics) to a model to explain what to fix first. It is a conversation, not a one-shot summary: pick a
target, the model reads platform data and optionally runs read-only commands on the host, then writes a
report where every conclusion carries a `source_ref` back to the original check. Targets must be running,
safely managed, with a working Guest Agent and cloud-init; excluded targets are listed with reasons.

Read-only is the default. Tools are parameterised command templates, not a free-form shell: arguments
are regex and path allowlisted, no sudo, no `;`, `&&`, `||`, redirects or command substitution, plus a
denylist for sensitive files. Commands that change the system are shown and approved one by one, and
destructive patterns are blocked even if approved. An in-browser SSH terminal shares the same screen, so
your commands and the model's appear in order. Every run records model, steps, tokens and each tool call,
exportable as CSV.

## Monitoring and alerting

Totals and distribution for VMs, CPU / memory / storage rings, node overview, recent tasks, top 8 by
usage. Node and guest metrics stream over `/ws/metrics` (one connection per tab, auto-reconnect with
polling fallback); guest state and lock changes invalidate list and detail queries automatically. Disk
capacity is extrapolated linearly from root filesystem history to give daily growth and days-to-full,
and both nodes and guests get RRD charts. Alert rules take thresholds and durations, and notifications go
to mail, Feishu group bots, generic webhooks and in-app messages, with per-source muting.

## Task queue and in-app notifications

Tasks aggregate across connections, show the raw PVE log, can be stopped while running and cleared
afterwards; ownership is checked on every call, so knowing a UPID is not enough. In-app notifications
cover resource alerts, first login from a new IP, certificates about to expire, reinstalls and new panel
versions; clearing them is audited.

## Feishu bot

Operates VMs from a Feishu group: list, status, power on, create, snapshot, snapshot list, rollback,
backup. Shutdown, reboot and rollback send a card and wait for confirmation. Callbacks are checked by
signature and verification token, deduplicated by `event_id`, and moved to the background if they take
longer than three seconds.

## Command palette and global search

`Ctrl / ⌘ + K` opens a palette that is half navigation (filtered by language, permissions and entry
switches) and half resource search (VMs, nodes, storages, users in parallel, 200 ms debounce).

## Snapshots, backup and restore

Create snapshots with or without memory, roll back, delete, and see snapshots across VMs in one table.
Back up now in snapshot / suspend / stop mode, browse backup volumes, restore to a chosen VMID, delete,
and create scheduled PVE backup jobs. Registered backups are protected from panel-side deletion and
fingerprinted (`volid|size|ctime`) on a schedule, with alerts when they vanish or change.

## PBS backup server (optional)

Once PBS is registered as a storage in PVE, the storage page shows its badge, last backup, archive count
and verification state with no configuration. Deeper detail (repository usage, GC schedule, node load,
tasks, disk health) needs a read-only token registered per connection, and the panel only ever sends
GET. Prune, GC, verify and sync stay in the PBS console.

## VNC console

noVNC in the browser with auto-reconnect, fullscreen and Ctrl+Alt+Del. The backend proxies the WebSocket,
so PVE's own web interface never has to be exposed.

## Login and action audit

Host login audit parses `last` / `lastb` for account, terminal, source IP, time and result, plus boot
records and `sudo` / `su` escalation from auth.log. Events are merged into `audit_log` per-host with a
cursor every five minutes, idempotently, next to the panel's own actions. VM-internal login logs are not
readable, but VM IPs are matched against host logins. Panel writes, including failures, record user,
action, target, result, detail and source IP; a user with `audit.view` sees only their own records, and
reading the audit log is itself audited.

## Certificates (ACME / multi-cloud / PVE nodes)

Tencent Cloud, Alibaba Cloud and Huawei Cloud free certificates, your own ACME account (Let's Encrypt,
ZeroSSL, Google Trust Services, SSL.com, Actalis, Buypass, with wildcard, multi-domain, IP and EAB
support) and manual uploads. DNS-01 covers DNSPod, Alibaba, Huawei, Cloudflare, GoDaddy, Route 53,
Namecheap and Volcano Engine. Renewal is automatic and deploys to a local directory, a remote server over
SSH, a PVE VM (Guest Agent), an LXC container, the PVE node's own 8006 certificate, or a cloud load
balancer / CDN. Expiry warnings fire at 30 / 14 / 7 / 3 days; certificates can be revoked and exported as
PEM (zip) or PFX.

## Multi-user RBAC and quotas

| Built-in role | What it can do |
|---|---|
| `admin` | everything, including users, connection settings, deleting VMs and cluster-level firewall |
| `operator` | create, power, console, snapshot, backup, clone, reconfigure, firewall for owned guests; no user management, no VM deletion, no cluster firewall |
| `viewer` | read only |
| `region_admin` | everything, but only inside assigned regions |

Custom roles are built from a permission catalog; `admin` cannot be edited or deleted. Registration is off
by default and can be invite-based, code-based or self-service, with optional admin approval and preset
role and quotas. Quotas work in two layers (user, then role default) across VM and container counts,
cores, memory and disk, plus per-guest caps and snapshot limits, and are enforced on reconfiguration,
disk growth and snapshots as well as on creation. Users only see their own usage; platform-wide usage is
admin-only.

## Account security: 2FA, sessions and API tokens

The personal centre covers profile, password, two-factor auth, API tokens, login devices and effective
permissions. TOTP gives eight single-use recovery codes, and `TOTP_REQUIRED_ROLES` can force a role to
enrol. Trusted devices skip the second factor for seven days. Sessions can be revoked one by one or all
at once, password changes invalidate other devices, and a per-user session cap evicts the oldest.
Personal API tokens last at most 365 days, never expire-less, are shown once, and are created or revoked
from a browser session with confirmation.

## Panel maintenance (updates, jobs, branding, entry switches)

Check for updates in settings, skip a version, or change the release repository; updating swaps code
according to install type (tarball or git) and then runs `deploy.sh`, returning immediately with progress
in the log. Background jobs lists every scheduled task (inspections, alerts, certificate renewal,
ownership reconciliation, cleanup, frp supervision, version checks) with per-job toggles and manual runs.
Branding covers site name, subtitle, ICP number, logo and login background. Entry switches hide sidebar
entries together with menus, the palette and direct URLs, but the API still authorises independently;
system settings can never be hidden.
