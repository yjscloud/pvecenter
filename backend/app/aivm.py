"""AI 助手的「虚拟机内取证」：租约、策略、执行与回滚。

与 ``aitools``（SSH 到受管主机）的分工
------------------------------------
``aitools`` 的世界是**宿主机**：``df`` / ``free`` / ``journalctl``，凭据是 SSH。
但面板管理的是虚拟机，用户真正想查的是「这台 VM 里 nginx 为什么起不来」。
一条 SSH 通道回答不了这个问题 —— 它连不进去，也不该为了这个去客户机里配密钥。

这里走 **QEMU Guest Agent**：PVE 侧已经有 ``agent/exec`` / ``exec-status`` /
``file-open|write|close`` 的封装（见 ``pve.ProxmoxClient``），不需要在客户机里
放任何凭据，也不用开 22 端口。代价是必须在 VM 里装上并启动 ``qemu-guest-agent``。

安全模型（四层，缺一不可）
--------------------------
1. **租约**：授权不是「打开开关」，而是「给某个用户、某台 VM、一组目录与动词、
   一段时间」。租约过期即失效，可随时撤销；每一步都受 ``actions_max`` 限制。
2. **策略引擎**：目录白名单（读 / 写分开）、systemd 单元白名单、只读命令白名单
   （复用 ``aitools.validate_command``）、危险操作一律拒绝。
3. **分级审批**：L0 只读自动执行；L1（写临时目录）会话级确认一次；L2（改配置、
   重启服务）逐条确认并展示 diff；L3（删除、重启整机、改网络）**本模块不提供**。
4. **审计与回滚**：每个动作落 ``ai_vm_actions``（含目标、风险、判定、输出摘要、
   diff、备份位置），写操作前先备份原文件，必要时先给 VM 打快照。

为什么不用「让模型拼一条 shell 命令再审批」
------------------------------------------
那正是 ``host_propose_command`` 的做法，对宿主机可以（命令是用户自己审的），
对客户机不行：一条 ``sed -i`` 的 diff 藏在正则里，审批的人看不出改了什么。
这里所有写操作都是**结构化动作**（路径 + 完整新内容 / 单元名），由服务端生成
diff 给用户看 —— 看到什么就执行什么。
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import ntpath
import posixpath
import re
import shlex
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from . import ai_approval, aiterm, aitools, database, formatters, i18n, security, store
from .pve import ProxmoxError, get_client, target_connection_id
from .vm_scope import bind_node_connection

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- 常量

#: 默认租约时长与上限。15 分钟够排查一个问题；2 小时是「人还在电脑前盯着」的上限。
DEFAULT_MINUTES = 15
MAX_MINUTES = 120
#: 单次租约内允许的动作数。够用又不至于让一次授权变成整晚的自由发挥。
DEFAULT_ACTIONS = 40
MAX_ACTIONS = 200
#: 回给模型的单条输出上限（与 aitools.MAX_OUTPUT 同量级）。
MAX_OUTPUT = 6000
#: 客户机内单条命令的超时。agent 本身没有超时参数，靠这里轮询兜住。
EXEC_TIMEOUT = 20.0
#: 运维授权下的超时：装一个包、跑一次配置检查动辄几十秒，20 秒根本不够。
#: 上限仍要卡住 —— 否则一条 `tail -f` 会把整轮对话挂死在等待里。
EXEC_TIMEOUT_OPS = 300.0
#: 单次读取 / 写入的字节上限。诊断用不着整块磁盘镜像。
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 120_000
#: 备份与快照
BACKUP_DIR = "/var/lib/pvecenter-ai/backup"
#: Windows 的回滚备份目录（不能沿用 POSIX 路径）
WINDOWS_BACKUP_DIR = "C:\\ProgramData\\pvecenter-ai\\backup"

#: 默认只读目录。刻意包含 /proc 与 /sys —— 排查内核态问题时它们几乎是唯一证据。
DEFAULT_FS_RO = ("/var/log", "/etc", "/proc", "/sys", "/tmp", "/var/lib")
#: 可申请的写目录。**不提供 /etc 之外的系统目录**，且 /etc 单独需要 ai.vm.write。
WRITABLE_DIRS = ("/tmp", "/var/tmp", "/var/log", "/etc", "/opt", "/srv")

#: 风险分级：0 只读、1 写临时文件、2 改配置 / 重启服务、3 破坏性（本模块不提供）
RISK_READ, RISK_TMP, RISK_CONFIG, RISK_DESTROY = 0, 1, 2, 3
RISK_LABELS = {
    RISK_READ: "只读",
    # 运维授权下，白名单之外又看不出会改状态的命令落到这一级（例如 `nginx -t`）：
    # 不拦，但要求一次审批。叫「写临时文件」会让审批卡显示得莫名其妙。
    RISK_TMP: "执行命令（需批准）",
    RISK_CONFIG: "改配置 / 重启服务",
    RISK_DESTROY: "破坏性操作",
}

#: 授权级别。read = 只看；write = 目录/服务清单内的受限变更（P1 的能力）；
#: ops = 整机运维：可装软件、改任意配置、重启任意服务，命令由服务端黑名单兜底。
#: 这三档不是「宽一点」的关系，而是「谁承担后果」的关系 —— 必须由用户显式选择，
#: 不能悄悄把一张 write 票升级成 ops。
ACCESS_READ, ACCESS_WRITE, ACCESS_OPS = "read", "write", "ops"
ACCESS_LABELS = {
    ACCESS_READ: "只读",
    ACCESS_WRITE: "受限变更",
    ACCESS_OPS: "运维授权",
}
ACCESS_LABELS_EN = {
    ACCESS_READ: "read-only",
    ACCESS_WRITE: "scoped write",
    ACCESS_OPS: "ops",
}

#: ops 模式下**连用户批准也不放行**的命令。这些动作的结果不可回滚：
#: 整机重启会让会话断开、格式化/删根会直接毁掉客户机、改认证材料会留下后门。
#: 其余动作都交给「逐条审批 + 审计 + 快照」三层，而不是靠命令白名单把能力关死。
#: 说明：命令名的匹配都锚在「一条命令的开头」（行首或 `;` / `&&` / `||` / `|`
#: 之后）。不锚的话 `grep reboot /var/log/syslog` 也会被拦 —— 那是排查重启原因的
#: 常用命令，属于把安全做成了不可用。
OPS_FORBIDDEN = (
    # 删根（`rm -rf /`、`rm -rf /*`），普通目录删除不在此列
    r"(^|[;&|]\s*)(sudo\s+)?rm\s+(-[^\s]+\s+)*/(\s|\*|$)",
    r"--no-preserve-root",
    # 分区表 / 文件系统 / 裸盘
    r"(^|[;&|]\s*)(sudo\s+)?(mkfs|mke2fs|wipefs|fdisk|parted|sgdisk)\b",
    r"(^|[;&|]\s*)(sudo\s+)?dd\b[^\n]*\bof=/dev/",
    r">\s*/dev/(sd|nvme|vd|xvd|hd)",
    # 整机开关机：会把这一轮排查直接掐断，也可能是业务中断的源头
    r"(^|[;&|]\s*)(sudo\s+)?(shutdown|poweroff|halt|reboot)\b",
    r"(^|[;&|]\s*)(sudo\s+)?init\s+0\b",
    r"(^|[;&|]\s*)(sudo\s+)?systemctl\s+(reboot|poweroff|halt|kexec)\b",
    # 认证材料：拿到这些就等于拿到机器，装软件用不到
    r"/etc/(shadow|gshadow)\b",
    r"authorized_keys",
    r"(^|[;&|]\s*)(sudo\s+)?(chpasswd|usermod\s+-p)\b",
    # 抗取证与暴力破坏
    r"(^|[;&|]\s*)(sudo\s+)?history\s+-c\b",
    r":\s*\(\s*\)\s*\{",
    r"(^|[;&|]\s*)(sudo\s+)?docker\s+run\b[^\n]*--privileged",
)
#: ops 命令的长度上限：一条正常运维命令远到不了这个量级，超长多半是在塞脚本
OPS_MAX_COMMAND = 4000

#: Windows 的不可回滚动作（cmd / powershell 语义）
WINDOWS_OPS_FORBIDDEN = (
    r"(^|[&|]\s*)(format|diskpart|bcdedit|shutdown|reboot)\b",
    r"(^|[&|]\s*)reg\s+delete\s+hklm",
    r"(^|[&|]\s*)del\s+/[sq]\s+[a-z]:\\?\s*$",
    r"(^|[&|]\s*)rd\s+/s\s+/q\s+[a-z]:\\?\s*$",
)

#: 「这条命令会改变客户机状态」的粗判。命中就要逐条审批，没命中且能过只读白名单的
#: 命令可以直接跑 —— 让 `journalctl -u nginx` 也弹审批，用户很快就会无脑点「批准」，
#: 那比不弹更危险。判错的代价只是多一次审批，方向是安全的。
OPS_MUTATING = re.compile(
    r"(^|[;&|]\s*|\s)("
    r"apt|apt-get|aptitude|dnf|yum|zypper|pacman|apk|"
    r"pip|pip3|npm|yarn|pnpm|gem|snap|composer|"
    r"systemctl|service|rc-service|"
    r"rm|mv|cp|ln|mkdir|rmdir|touch|truncate|tee|"
    r"sed|awk|perl|python|python3|sh|bash|zsh|"
    r"chmod|chown|chgrp|setfacl|"
    r"useradd|userdel|usermod|groupadd|groupdel|"
    r"iptables|ip6tables|nft|ufw|firewall-cmd|"
    r"crontab|"
    r"mount|umount|swapon|swapoff|"
    r"kill|pkill|killall|"
    r"curl|wget|"
    r"docker|podman|kubectl|"
    r"git|"
    r"sysctl|modprobe|insmod|rmmod"
    r")\b",
    re.IGNORECASE,
)

#: 白名单之外、但确实是只读的子命令（容器 / 包管理 / 网络状态）。
#: 不认这些的话 `docker ps`、`apt list --installed` 都要弹审批，而它们只是看一眼。
OPS_READ_ONLY = re.compile(
    r"^\s*(docker|podman)\s+(ps|images|logs|inspect|version|info|stats|top|port)\b"
    r"|^\s*kubectl\s+(get|describe|logs|top|version|api-resources|explain)\b"
    r"|^\s*(dpkg\s+-l|rpm\s+-qa|pip3?\s+(list|show)|npm\s+(ls|list))"
    r"|^\s*(apt|apt-get)\s+(list|show|search)"
    r"|^\s*(dnf|yum)\s+(list|info|search)"
    r"|^\s*(ss|netstat)\b"
    r"|^\s*ip\s+(a|addr|route|link)\b"
    r"|^\s*nft\s+list\b"
    r"|^\s*iptables\s+-L\b",
    re.IGNORECASE,
)

#: 无论授权怎么写，这些路径一律不给碰（与 aitools 同源，再补几条客户机特有的）
FORBIDDEN_HINTS = tuple(aitools.FORBIDDEN_HINTS) + (
    "/root",
    ".ssh",
    "/etc/shadow",
    "/etc/gshadow",
    "/boot",
    "/dev",
    "authorized_keys",
    # 主机私钥形如 /etc/ssh/ssh_host_rsa_key，既没有扩展名也不含 ".ssh"，
    # 只靠上面那几条会漏 —— 而它在 /etc 这个默认可读目录里，等于默认暴露。
    "ssh_host_",
    "_key",
    "/etc/pve/priv",
)

# ------------------------------------------------------------ Windows 客户机
# Windows 的路径语义与 POSIX 完全不同（盘符、反斜杠、大小写不敏感），拿 POSIX
# 那套 posixpath 去判会把每一个合法路径都判成越权 —— 等于这个功能在 Windows 上
# 直接不可用。所以按 ostype 分两套规则，而不是硬套一套。
WINDOWS_FS_RO = ("C:\\ProgramData", "C:\\Windows\\Logs", "C:\\inetpub\\logs", "C:\\Temp")
WINDOWS_WRITABLE = ("C:\\ProgramData", "C:\\inetpub\\wwwroot", "C:\\Temp")
WINDOWS_FORBIDDEN = (
    "windows\\system32\\config",
    "\\sam",
    "\\security",
    "\\system32\\drivers\\etc\\hosts",
    ".ssh",
    "id_rsa",
    ".pfx",
    ".pem",
    ".key",
)

#: Windows 只读诊断命令：cmd.exe /c 跑，语义与 Linux 那套白名单一一对应
WINDOWS_COMMANDS = (
    "ipconfig",
    "tasklist",
    "netstat",
    "systeminfo",
    "dir",
    "type",
    "sc ",
    "reg query",
    "whoami",
    "hostname",
    "get-",
    "wmic",
    "net ",
    "ping",
    "route",
    "nslookup",
    "wevtutil",
)

#: 策略配置键（管理员可改，见 routers/ai.py 的 /vm/policy）
POLICY_KEY = "ai_vm_policy"
#: 默认策略：生产标签禁写、单租约最长 2 小时。高危动作双人审批默认**关闭**
DEFAULT_POLICY: Dict[str, Any] = {
    "max_minutes": MAX_MINUTES,
    # 双人审批是可选项，默认关闭：默认开启的风险是「谁都卡住」——单管理员部署里
    # 没人能投第二票，高危动作只能等到超时被拒。需要两人复核的环境由管理员在
    # 「虚拟机授权策略」里显式打开；打开后若系统里仍没有第二位审批人，运行时
    # 会自动降级为单人（见 _has_second_approver）。
    "double_approve": False,
    "deny_write_tags": ["prod", "production"],
    "auto_snapshot": True,
    "keep_snapshot_hours": 24,
    # 是否允许签发「运维授权」（整机 shell）。默认允许 —— 这正是用户要的自由；
    # 但管理员可以一键关掉，关掉后已有票据仍按签发时的级别执行。
    "allow_ops": True,
}

#: 客户机内的受限执行器（diagd）。装了就用它跑（非 root + cgroup + kill 开关），
#: 没装则退回「兼容模式」：由服务端白名单兜底，客户机内没有任何隔离。
DIAGD_PATH = "/usr/local/sbin/pvecenter-diagd"

#: 动作级审计里「策略拒绝」与「执行故障」必须分开记：前者是 AI 想越界，
#: 后者是客户机不配合，两者的处置完全不同。
UNIT_RE = re.compile(r"^[A-Za-z0-9@_.\\-]{1,64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_vm_leases (
    id            VARCHAR(40)  NOT NULL,
    username      VARCHAR(64)  NOT NULL DEFAULT '',
    connection_id VARCHAR(64)  NOT NULL DEFAULT '',
    node          VARCHAR(64)  NOT NULL DEFAULT '',
    vmid          INT          NOT NULL DEFAULT 0,
    vm_name       VARCHAR(191) NOT NULL DEFAULT '',
    guest_type    VARCHAR(8)   NOT NULL DEFAULT 'qemu',
    os_type       VARCHAR(8)   NOT NULL DEFAULT 'linux',
    tags          VARCHAR(191) NOT NULL DEFAULT '',
    mode          VARCHAR(8)   NOT NULL DEFAULT 'compat',
    access        VARCHAR(8)   NOT NULL DEFAULT 'write',
    double_approve TINYINT     NOT NULL DEFAULT 0,
    policy_note   VARCHAR(191) NOT NULL DEFAULT '',
    fs_ro         TEXT,
    fs_rw         TEXT,
    units         TEXT,
    risk_max      INT          NOT NULL DEFAULT 1,
    minutes       INT          NOT NULL DEFAULT 15,
    expires_at    BIGINT       NOT NULL DEFAULT 0,
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',
    reason        VARCHAR(512) NOT NULL DEFAULT '',
    snapshot      VARCHAR(64)  NOT NULL DEFAULT '',
    actions_used  INT          NOT NULL DEFAULT 0,
    actions_max   INT          NOT NULL DEFAULT 40,
    created       BIGINT       NOT NULL DEFAULT 0,
    revoked_at    BIGINT       NOT NULL DEFAULT 0,
    revoke_reason VARCHAR(256) NOT NULL DEFAULT '',
    PRIMARY KEY (id),
    KEY idx_ai_vm_lease_user (username),
    KEY idx_ai_vm_lease_exp (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS ai_vm_actions (
    id         VARCHAR(40)  NOT NULL,
    lease_id   VARCHAR(40)  NOT NULL DEFAULT '',
    username   VARCHAR(64)  NOT NULL DEFAULT '',
    seq        INT          NOT NULL DEFAULT 0,
    tool       VARCHAR(32)  NOT NULL DEFAULT '',
    target     VARCHAR(512) NOT NULL DEFAULT '',
    risk       INT          NOT NULL DEFAULT 0,
    verdict    VARCHAR(16)  NOT NULL DEFAULT '',
    ok         TINYINT      NOT NULL DEFAULT 0,
    exitcode   INT          NOT NULL DEFAULT 0,
    output     TEXT,
    diff       TEXT,
    backup     VARCHAR(512) NOT NULL DEFAULT '',
    elapsed_ms INT          NOT NULL DEFAULT 0,
    created    BIGINT       NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    KEY idx_ai_vm_action_lease (lease_id),
    KEY idx_ai_vm_action_created (created)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


class ToolDenied(Exception):
    """策略拒绝。文本直接回给模型 —— 它需要知道「为什么不行」才能换路子。"""


#: 双人审批默认值从「开」改为「关」的一次性迁移标记。
#: 它必须落库：管理员之后手动打开双人审批，不该被下次重启又按「旧默认值」改回去。
DOUBLE_APPROVE_MIGRATED_KEY = "ai_vm_policy_double_approve_default_off"


async def _migrate_double_approve_default() -> None:
    """把「按旧默认值打开」的双人审批关掉，只做一次。

    旧版本默认开启，而策略页保存时会把当时的整份配置写进库 —— 只改代码里的
    默认值，这些部署读到的仍然是库里的 ``true``，等于默认值没变。迁移只处理
    「库里写着 true」这一种情况，且只执行一次，之后以管理员的选择为准。
    """
    if await store.get_setting(DOUBLE_APPROVE_MIGRATED_KEY, ""):
        return
    raw = await store.get_setting(POLICY_KEY, "")
    if raw:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("double_approve"):
            data["double_approve"] = False
            await store.set_setting(
                POLICY_KEY, json.dumps(data, ensure_ascii=False)
            )
            logger.info(
                "双人审批默认值改为关闭，已迁移旧策略（可在「虚拟机授权策略」里重新打开）"
            )
    await store.set_setting(DOUBLE_APPROVE_MIGRATED_KEY, "1")


async def init_table() -> None:
    async with database.connect() as db:
        await db.executescript(SCHEMA)
        # 旧库补列（P2 之后新增的能力）。逐列判断而不是 try/except ADD COLUMN：
        # MySQL 的「重复列」报错和「表不存在」报错长得像，吞异常会把真正的问题藏起来。
        columns = await database.table_columns(db, "ai_vm_leases")
        # 客户机是 Linux 还是 Windows：决定路径语义、命令白名单与执行方式
        if "os_type" not in columns:
            await db.execute(
                "ALTER TABLE ai_vm_leases ADD COLUMN os_type VARCHAR(8)"
                " NOT NULL DEFAULT 'linux'"
            )
        # PVE 的 tags：策略按标签生效（prod 禁写、test 可预授权）
        if "tags" not in columns:
            await db.execute(
                "ALTER TABLE ai_vm_leases ADD COLUMN tags VARCHAR(191)"
                " NOT NULL DEFAULT ''"
            )
        # standard = 客户机内装了 diagd（非 root + cgroup + 可 kill）；
        # compat = 没有，只能靠服务端白名单
        if "mode" not in columns:
            await db.execute(
                "ALTER TABLE ai_vm_leases ADD COLUMN mode VARCHAR(8)"
                " NOT NULL DEFAULT 'compat'"
            )
        # 授权级别（read / write / ops）。旧票没有这一列，按 write 补齐 ——
        # 它们的 fs_rw / units 就是当时的实际边界，升成 ops 等于悄悄放宽权限。
        if "access" not in columns:
            await db.execute(
                "ALTER TABLE ai_vm_leases ADD COLUMN access VARCHAR(8)"
                " NOT NULL DEFAULT 'write'"
            )
        # 高危动作是否要求两人批准（由策略决定，签发时固化到票面）
        if "double_approve" not in columns:
            await db.execute(
                "ALTER TABLE ai_vm_leases ADD COLUMN double_approve TINYINT"
                " NOT NULL DEFAULT 0"
            )
        if "policy_note" not in columns:
            await db.execute(
                "ALTER TABLE ai_vm_leases ADD COLUMN policy_note VARCHAR(191)"
                " NOT NULL DEFAULT ''"
            )
        await db.commit()
    await _migrate_double_approve_default()


# --------------------------------------------------------------- 运行时上下文
# 租约与推送器放在 contextvar 里，而不是一路透传参数：
# 工具循环在 ai.py / aichat.py 两处，签名已经很长；多一个 vm_lease 参数要改
# 十几个调用点，而且很容易在某个分支漏传 —— 漏传的后果是「授权了但工具不可用」，
# 排查起来很费劲。contextvar 由路由入口设置、随 asyncio task 自动继承，
# 天然与「这次请求」同生命周期。

_LEASE: ContextVar[Optional[Dict[str, Any]]] = ContextVar("aivm_lease", default=None)
_NOTIFY: ContextVar[Optional[Callable[[Dict[str, Any]], Awaitable[None]]]] = ContextVar(
    "aivm_notify", default=None
)


def current_lease() -> Optional[Dict[str, Any]]:
    return _LEASE.get()


def has_lease() -> bool:
    return _LEASE.get() is not None


def set_notify(notify: Optional[Callable[[Dict[str, Any]], Awaitable[None]]]) -> None:
    """把本次请求的事件推送器交给本模块（审批事件要用它发出去）。"""
    _NOTIFY.set(notify)


@contextmanager
def scope(lease: Optional[Dict[str, Any]]):
    """在一个请求内激活租约。退出时恢复，不污染下一个请求。"""
    token = _LEASE.set(lease)
    try:
        yield lease
    finally:
        _LEASE.reset(token)


# ------------------------------------------------------------------- 租约读写

def new_id() -> str:
    return uuid.uuid4().hex


def _json_list(value: Optional[str]) -> List[str]:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def _row_to_lease(row: Any) -> Dict[str, Any]:
    item = dict(row)
    item["fs_ro"] = _json_list(item.get("fs_ro"))
    item["fs_rw"] = _json_list(item.get("fs_rw"))
    item["units"] = _json_list(item.get("units"))
    item["expires_at"] = int(item.get("expires_at") or 0)
    item["actions_used"] = int(item.get("actions_used") or 0)
    item["actions_max"] = int(item.get("actions_max") or DEFAULT_ACTIONS)
    item["risk_max"] = int(item.get("risk_max") or RISK_TMP)
    item["vmid"] = int(item.get("vmid") or 0)
    item["double_approve"] = int(item.get("double_approve") or 0)
    item["os_type"] = str(item.get("os_type") or "linux")
    now = int(time.time())
    # 状态是「算」出来的，不是「轮询改」出来的：没有人会为了租约到期去起一个定时器，
    # 读的时候发现过期就当场按 expired 处理，还能顺手修正库里的状态。
    if item.get("status") == "active" and item["expires_at"] <= now:
        item["status"] = "expired"
    item["remaining"] = max(0, item["expires_at"] - now)
    return item


def public_lease(lease: Dict[str, Any]) -> Dict[str, Any]:
    """给前端的形状：去掉内部字段，保留可读的剩余时间与风险标签。"""
    return {
        "id": lease.get("id"),
        "username": lease.get("username"),
        "connection_id": lease.get("connection_id"),
        "node": lease.get("node"),
        "vmid": lease.get("vmid"),
        "vm_name": lease.get("vm_name"),
        "guest_type": lease.get("guest_type"),
        "fs_ro": lease.get("fs_ro") or [],
        "fs_rw": lease.get("fs_rw") or [],
        "units": lease.get("units") or [],
        "risk_max": lease.get("risk_max"),
        "risk_label": RISK_LABELS.get(int(lease.get("risk_max") or 0), ""),
        "minutes": lease.get("minutes"),
        "expires_at": lease.get("expires_at"),
        "remaining": lease.get("remaining", 0),
        "status": lease.get("status"),
        "reason": lease.get("reason"),
        "snapshot": lease.get("snapshot") or "",
        "actions_used": lease.get("actions_used"),
        "actions_max": lease.get("actions_max"),
        "created": lease.get("created"),
        "revoke_reason": lease.get("revoke_reason") or "",
        "writable": bool(
            lease.get("fs_rw")
            or lease.get("units")
            or str(lease.get("access") or "") == ACCESS_OPS
        ),
        "os_type": lease.get("os_type") or "linux",
        "tags": [tag for tag in str(lease.get("tags") or "").split(";") if tag],
        # standard 才意味着「客户机里有沙箱」，compat 只是「服务端白名单兜底」——
        # 这个区别必须让用户看见，否则他会以为授权之后客户机是隔离的。
        "mode": lease.get("mode") or "compat",
        # 授权级别必须单独暴露：前端要能一句话说清「这张票能装软件」，
        # 而不是让用户从 fs_rw 是不是 "/" 去猜。
        "access": str(lease.get("access") or ACCESS_WRITE),
        "access_label": ACCESS_LABELS.get(
            str(lease.get("access") or ACCESS_WRITE), ""
        ),
        "double_approve": bool(lease.get("double_approve")),
        "policy_note": lease.get("policy_note") or "",
    }


async def _has_second_approver(owner: str) -> bool:
    """除发起人之外，系统里是否还有人能投「第二票」。

    双人审批的前提是**有第二个人**。单管理员部署里没有第二个人，条件就永远凑
    不齐，到点只能按拒绝收口 —— 用户看到的是「批准了，AI 却什么都不执行」，
    而界面上还写着「等待另一位批准」。与其造一个不可能满足的门槛，不如承认
    这次只有一票，并在票面 note 里写明原因。

    查不到用户表时按「有人」处理：安全侧宁可严格，不要因为一次数据库异常
    就把高危动作的双人审批悄悄关掉。
    """
    try:
        users = await store.list_users()
    except Exception:  # noqa: BLE001 - 任何异常都不该让授权流程中断
        logger.warning("统计审批人失败，双人审批按「有人」处理", exc_info=True)
        return True
    for item in users:
        name = str(item.get("username") or "")
        if not name or name == owner:
            continue
        if str(item.get("status") or "") != "active":
            continue
        if security.has_user_permission(item, "ai.vm.write"):
            return True
    return False


async def create_lease(
    *,
    username: str,
    node: str,
    vmid: int,
    vm_name: str = "",
    connection_id: str = "",
    guest_type: str = "qemu",
    fs_ro: Optional[List[str]] = None,
    fs_rw: Optional[List[str]] = None,
    units: Optional[List[str]] = None,
    risk_max: int = RISK_TMP,
    minutes: int = DEFAULT_MINUTES,
    actions_max: int = DEFAULT_ACTIONS,
    reason: str = "",
    os_type: str = "linux",
    tags: str = "",
    mode: str = "compat",
    access: str = ACCESS_WRITE,
) -> Dict[str, Any]:
    """签发一张租约。调用方负责权限与归属校验，这里只管参数合法化与落库。"""
    windows = str(os_type).lower() == "windows"
    access = str(access or "").lower()
    if access not in (ACCESS_READ, ACCESS_WRITE, ACCESS_OPS):
        access = ACCESS_WRITE
    default_ro = list(WINDOWS_FS_RO) if windows else list(DEFAULT_FS_RO)
    ro = _normalise_dirs(
        fs_ro if fs_ro is not None else default_ro, writable=False, windows=windows
    )
    rw = _normalise_dirs(fs_rw or [], writable=True, windows=windows)
    unit_list = [u for u in (units or []) if UNIT_RE.match(str(u or ""))]

    if access == ACCESS_OPS:
        # 运维授权不预先圈目录、也不预先列服务：那两份清单本来就是为了「猜 AI 要用
        # 到哪些路径」，而装软件要碰 /usr、/var/lib/dpkg、/etc/apt，猜不准就只能
        # 一直报「不在授权范围内」。真正的边界换成「逐条审批 + 不可回滚黑名单」。
        # "/" 只是票面上的说明，_check_path 在 ops 下并不用它做放行判断。
        ro = ["C:\\"] if windows else ["/"]
        rw = ["C:\\"] if windows else ["/"]
        unit_list = []
        risk_max = RISK_CONFIG
    elif access == ACCESS_READ:
        rw, unit_list = [], []
        risk_max = RISK_READ
    elif rw or unit_list:
        risk_max = max(risk_max, RISK_CONFIG)
    else:
        risk_max = RISK_READ if risk_max <= RISK_READ else risk_max

    # 策略：按标签禁写、按策略收紧时长。命中禁写标签时**直接拒绝**而不是静默降级 ——
    # 降级会让用户以为拿到了写权限，然后每一次写都失败，比一开始就说不行更糟。
    pol = await policy()
    if access == ACCESS_OPS and not pol.get("allow_ops", True):
        raise ToolDenied(
            i18n.pick(
                "管理员已关闭「运维授权」，只能签发只读或受限变更",
                "The administrator disabled ops grants; only read-only or scoped "
                "write grants are available",
            )
        )
    tag_list = [tag for tag in str(tags or "").split(";") if tag]
    deny_tags = {str(tag).lower() for tag in pol.get("deny_write_tags") or []}
    blocked = [tag for tag in tag_list if tag.lower() in deny_tags]
    policy_note = ""
    if blocked:
        policy_note = "标签命中禁止写入：" + "、".join(blocked)
        if rw or unit_list or access == ACCESS_OPS:
            raise ToolDenied(
                i18n.pick(
                    f"这台机器带有 {policy_note}，策略不允许授予写权限",
                    f"This VM carries {policy_note}; policy forbids write grants",
                )
            )

    minutes = max(1, min(int(pol.get("max_minutes") or MAX_MINUTES), int(minutes or DEFAULT_MINUTES)))
    actions_max = max(1, min(MAX_ACTIONS, int(actions_max or DEFAULT_ACTIONS)))

    # 票面如实记录「这次到底需要几个人批」：没有第二个人时不要写成 2，
    # 否则界面会一直显示「等待另一位批准」，而那位根本不存在。
    need_two = bool(pol.get("double_approve")) and risk_max >= RISK_CONFIG
    if need_two and not await _has_second_approver(str(username or "")):
        need_two = False
        policy_note = (policy_note + "；" if policy_note else "") + "无第二位审批人，按单人审批"

    now = int(time.time())
    lease_id = new_id()
    lease = {
        "id": lease_id,
        "username": str(username or "")[:64],
        "connection_id": str(connection_id or "")[:64],
        "node": str(node or "")[:64],
        "vmid": int(vmid),
        "vm_name": str(vm_name or "")[:191],
        "guest_type": "lxc" if str(guest_type) == "lxc" else "qemu",
        "os_type": "windows" if windows else "linux",
        "tags": ";".join(tag_list)[:191],
        # 沙箱模式在建租约时探测一次并固化：中途装上/卸掉 diagd 不该让同一次
        # 排查的隔离等级悄悄变化。
        "mode": "standard" if str(mode) == "standard" else "compat",
        "access": access,
        # 双人审批只对「改配置 / 重启」及以上生效：只读命令也要两个人点头，
        # 那就没人用得下去了。
        "double_approve": int(need_two),
        "policy_note": policy_note[:191],
        "fs_ro": ro,
        "fs_rw": rw,
        "units": unit_list,
        "risk_max": min(int(risk_max), RISK_CONFIG),
        "minutes": minutes,
        "expires_at": now + minutes * 60,
        "status": "active",
        "reason": str(reason or "")[:500],
        "snapshot": "",
        "actions_used": 0,
        "actions_max": actions_max,
        "created": now,
        "revoked_at": 0,
        "revoke_reason": "",
        "remaining": minutes * 60,
    }
    async with database.connect() as db:
        await db.execute(
            "INSERT INTO ai_vm_leases (id, username, connection_id, node, vmid, vm_name,"
            " guest_type, os_type, tags, mode, access, double_approve, policy_note,"
            " fs_ro, fs_rw, units, risk_max, minutes, expires_at, status,"
            " reason, snapshot, actions_used, actions_max, created)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lease["id"],
                lease["username"],
                lease["connection_id"],
                lease["node"],
                lease["vmid"],
                lease["vm_name"],
                lease["guest_type"],
                lease["os_type"],
                lease["tags"],
                lease["mode"],
                lease["access"],
                lease["double_approve"],
                lease["policy_note"],
                json.dumps(ro, ensure_ascii=False),
                json.dumps(rw, ensure_ascii=False),
                json.dumps(unit_list, ensure_ascii=False),
                lease["risk_max"],
                lease["minutes"],
                lease["expires_at"],
                "active",
                lease["reason"],
                "",
                0,
                lease["actions_max"],
                now,
            ),
        )
        await db.commit()
    return lease


def _normalise_dirs(
    items: List[str], *, writable: bool, windows: bool = False
) -> List[str]:
    """只留绝对路径，去掉 ``..``，且必须在允许的根之下。

    按客户机系统分两套：Windows 用 ``ntpath``（盘符 + 反斜杠 + 大小写不敏感），
    拿 ``posixpath`` 去判会把 ``C:\\ProgramData`` 当成相对路径全部丢掉。
    """
    out: List[str] = []
    roots = WINDOWS_WRITABLE if writable else WINDOWS_FS_RO
    for raw in items or []:
        text = str(raw or "").strip()
        if windows:
            text = ntpath.normpath(text)
            if not ntpath.isabs(text) or ".." in text.split("\\"):
                continue
            lowered = text.lower()
            if any(hint in lowered for hint in WINDOWS_FORBIDDEN):
                continue
            # 盘符根（C:\）不允许整盘授权
            if len(text) <= 3:
                continue
            if writable and not any(
                lowered == root.lower()
                or lowered.startswith(root.lower().rstrip("\\") + "\\")
                for root in roots
            ):
                continue
        else:
            text = posixpath.normpath(text)
            if not text.startswith("/") or text == "/" or ".." in text.split("/"):
                continue
            lowered = text.lower()
            if any(hint in lowered for hint in FORBIDDEN_HINTS):
                continue
            if writable and not any(
                text == d or text.startswith(d + "/") for d in WRITABLE_DIRS
            ):
                continue
        if text not in out:
            out.append(text)
    return out


async def policy() -> Dict[str, Any]:
    """当前生效的策略（管理员配置与默认值合并）。

    配置坏掉时退回默认值并留日志：这里静默失败会让「prod 禁写」悄悄失效，
    而调用方无从发现 —— 策略函数的返回值必须永远可用。
    """
    raw = await store.get_setting(POLICY_KEY, "")
    merged = dict(DEFAULT_POLICY)
    if not raw:
        return merged
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ai_vm_policy 不是合法 JSON，按默认策略执行")
        return merged
    if not isinstance(data, dict):
        return merged
    for key in ("max_minutes", "double_approve", "auto_snapshot", "keep_snapshot_hours"):
        if key in data:
            merged[key] = data[key]
    if "allow_ops" in data:
        # 存的是 JSON，历史配置里可能是字符串 "false" —— 直接 bool() 会得到 True，
        # 等于把「关闭运维授权」这个安全开关读反。这里只认明确的假值。
        merged["allow_ops"] = str(data["allow_ops"]).lower() not in ("false", "0", "no", "")
    if isinstance(data.get("deny_write_tags"), list):
        merged["deny_write_tags"] = [str(tag) for tag in data["deny_write_tags"]]
    return merged


async def describe_guest(
    node: str, vmid: int, guest_type: str = "qemu"
) -> Dict[str, Any]:
    """读客户机的 ostype / 标签 / 名称。

    失败一律降级成「Linux、无标签」而不是报错：这几个字段只影响策略精度，
    连不上 PVE 时不该连授权都做不了（授权本身还有归属校验兜底）。
    """
    client = get_client()
    cfg: Dict[str, Any] = {}
    try:
        if str(guest_type) == "lxc":
            cfg = await client.lxc_config(node, vmid) or {}
        else:
            cfg = await client.qemu_config(node, vmid) or {}
    except Exception:  # noqa: BLE001 - 取不到就按默认口径
        cfg = {}
    ostype = str(cfg.get("ostype") or "").lower()
    return {
        "os_type": "windows" if ostype.startswith("win") else "linux",
        "tags": str(cfg.get("tags") or ""),
        "name": str(cfg.get("name") or ""),
    }


#: agent ping 的等待上限。客户机没装/没起 agent 时 PVE 会一直等，不能让
#: 「签发授权」这个请求挂在这里。
AGENT_PING_TIMEOUT = 5.0


async def probe_agent(node: str, vmid: int, timeout: float = AGENT_PING_TIMEOUT) -> bool:
    """客户机是否应答 QEMU Guest Agent —— 也就是能不能被平台安全管控。

    只 ping、不执行任何东西：这是签发前唯一一个既便宜又能证明「进得去」的检查。
    没有 agent 的机器即使签了票也跑不了任何工具，应该在签发那一刻就说清楚，
    而不是让用户以为授权成功、第一条命令却全线失败。
    """
    client = get_client()
    try:
        await asyncio.wait_for(client.qemu_agent_ping(node, vmid), timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 - 超时/未安装/未运行都按不可用处理
        return False


async def detect_mode(lease: Dict[str, Any]) -> str:
    """探测客户机里有没有装 diagd（有 = 标准模式，能施加 cgroup 并且能 kill）。"""
    try:
        result = await _agent_exec(
            {**lease, "mode": "compat"}, [DIAGD_PATH, "probe"], timeout=8.0
        )
    except Exception:  # noqa: BLE001 - 探测失败就是没有
        return "compat"
    # _agent_exec 的键是 out / err（不是在 PVE 响应里的 out-data）。曾经写成
    # stdout/stderr，结果 probe 永远读不到输出 —— 客户机装没装 diagd 都会被判成
    # compat，沙箱模式实际上从未生效。
    text = f"{result.get('out') or ''}{result.get('err') or ''}"
    return "standard" if "ok" in text.lower() else "compat"


def is_ops(lease: Dict[str, Any]) -> bool:
    """这张票是不是「运维授权」（整机 shell）。"""
    return str(lease.get("access") or "") == ACCESS_OPS


async def get_lease(lease_id: str) -> Optional[Dict[str, Any]]:
    if not lease_id:
        return None
    async with database.connect() as db:
        cursor = await db.execute("SELECT * FROM ai_vm_leases WHERE id=?", (lease_id,))
        row = await cursor.fetchone()
    return _row_to_lease(row) if row else None


async def list_leases(username: str = "", limit: int = 30) -> List[Dict[str, Any]]:
    """最近若干张租约。``username`` 为空表示管理员视角（看全部）。"""
    clause = " WHERE username=?" if username else ""
    params: List[Any] = [username] if username else []
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM ai_vm_leases"
            + clause
            + " ORDER BY created DESC LIMIT ?",
            tuple(params + [max(1, min(100, limit))]),
        )
        rows = await cursor.fetchall()
    return [_row_to_lease(row) for row in rows]


async def revoke_lease(lease_id: str, *, reason: str = "") -> Dict[str, Any]:
    now = int(time.time())
    async with database.connect() as db:
        await db.execute(
            "UPDATE ai_vm_leases SET status='revoked', revoked_at=?, revoke_reason=?"
            " WHERE id=? AND status='active'",
            (now, str(reason or "")[:250], lease_id),
        )
        await db.commit()
    # 撤销不只是「不再签发新动作」：已经在客户机里跑着的那条也得停。否则用户点了
    # 撤销，屏幕上却还有一条 `journalctl -f` 在转 —— 那是最伤信任的画面。
    lease = await get_lease(lease_id)
    kill: Dict[str, Any] = {
        "ok": False,
        "output": i18n.pick("没有可终止的进程", "Nothing to terminate"),
    }
    if lease:
        kill = await kill_lease(lease)
    return {"ok": True, "kill": kill}


async def kill_lease(lease: Dict[str, Any]) -> Dict[str, Any]:
    """终止该租约在客户机内仍在运行的进程（仅标准模式）。

    兼容模式下没有可用的开关：Guest Agent 只给 pid、不给 kill，而我们刻意不为了
    kill 去开一条 root shell。这时返回「做不到」比假装成功更诚实 —— 前端会提示
    「已撤销授权，但正在执行的那条命令会自己跑完」。
    """
    if str(lease.get("mode")) != "standard" or str(lease.get("os_type")) == "windows":
        return {
            "ok": False,
            "output": i18n.pick(
                "兼容模式没有客户机内的终止开关（正在执行的命令会自行结束）",
                "Compat mode has no in-guest kill switch; a running command will finish on its own",
            ),
        }
    try:
        result = await _agent_exec(
            lease,
            [DIAGD_PATH, "kill", "--lease", str(lease.get("id") or "")],
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001 - 杀不掉不该让撤销接口失败
        return {"ok": False, "output": str(exc)[:200]}
    return {"ok": result["exitcode"] == 0, "output": _truncate(result["out"] or result["err"])}


async def resolve_lease(
    lease_id: str, *, username: str, is_admin: bool = False
) -> Optional[Dict[str, Any]]:
    """取一张可用租约：必须是自己的（或管理员）、active、未过期。"""
    lease = await get_lease(lease_id)
    if not lease:
        return None
    if lease.get("status") != "active":
        return None
    if not is_admin and lease.get("username") != username:
        return None
    return lease


async def active_lease_for(
    username: str, connection_id: str, node: str, vmid: int
) -> Optional[Dict[str, Any]]:
    """同一台机器上是否已有生效中的租约（前端据此显示「已授权」而不是再申请）。"""
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM ai_vm_leases WHERE username=? AND connection_id=?"
            " AND node=? AND vmid=? AND status='active' AND expires_at>?"
            " ORDER BY created DESC LIMIT 1",
            (username, connection_id, node, vmid, int(time.time())),
        )
        row = await cursor.fetchone()
    return _row_to_lease(row) if row else None


async def _bump_actions(lease_id: str) -> int:
    """用一条 UPDATE 原子自增，返回本次的序号（并发下也不会两条动作共用序号）。"""
    async with database.connect() as db:
        await db.execute(
            "UPDATE ai_vm_leases SET actions_used = actions_used + 1 WHERE id=?",
            (lease_id,),
        )
        cursor = await db.execute(
            "SELECT actions_used FROM ai_vm_leases WHERE id=?", (lease_id,)
        )
        row = await cursor.fetchone()
        await db.commit()
    return int((dict(row).get("actions_used") if row else 0) or 0)


# --------------------------------------------------------------------- 执行层

async def _client_for(lease: Dict[str, Any]):
    """绑定到租约所在的那条 PVE 连接，再取客户端。

    多连接部署里「节点名」在每台 PVE 上都成立：不显式绑定就会打到当前连接，
    PVE 把节点名当主机名去解析，回一句 ``hostname lookup failed``。
    """
    node = str(lease.get("node") or "")
    if node:
        try:
            await bind_node_connection(node)
        except Exception:  # noqa: BLE001 - 定位失败就沿用当前连接，让 PVE 报真实错误
            logger.debug("AI VM：绑定节点连接失败 %s", node, exc_info=True)
    return get_client()


async def _mirror_host(lease: Dict[str, Any]) -> str:
    """这台客户机若同时是**受管主机**，返回它的 id，用于把执行过程镜像到终端。

    为什么要这一步：VM 内的命令走 QEMU Guest Agent，而用户看到的终端是一条到
    受管主机的 SSH —— 两条通道本来是分开的，不主动镜像，用户在终端里就完全
    看不到 AI 在客户机里做了什么，只能看到一句「已执行」。这里只负责**显示**，
    执行位置仍然是客户机，不会把命令搬到宿主机上去跑。
    """
    cached = lease.get("_mirror_host_id")
    if cached is not None:
        return str(cached)
    host_id = ""
    node = str(lease.get("node") or "")
    vmid = int(lease.get("vmid") or 0)
    if node and vmid:
        try:
            from . import sshremote

            for item in await sshremote.list_hosts(include_disabled=False):
                if (
                    str(item.get("node") or "") == node
                    and int(item.get("vmid") or 0) == vmid
                ):
                    host_id = str(item.get("id") or "")
                    break
        except Exception:  # noqa: BLE001 - 查不到就只是不镜像，不能影响执行
            logger.debug("AI VM：查找对应的受管主机失败", exc_info=True)
    if not host_id:
        # 这台客户机没有登记成受管主机（手填 IP 加进来的主机就没有 node/vmid）。
        # 此时若用户只开着一台终端，就镜像到那儿：他想看的是「AI 在做什么」，
        # 而不是「我开了几台机器」。开多台时不猜，宁可不显示也不串台。
        try:
            opened = aiterm.sessions_of_user(str(lease.get("username") or ""))
            if len(opened) == 1:
                host_id = opened[0].host_id
        except Exception:  # noqa: BLE001
            logger.debug("AI VM：取用户终端失败", exc_info=True)
    lease["_mirror_host_id"] = host_id
    return host_id


async def _agent_exec(
    lease: Dict[str, Any], argv: List[str], *, timeout: float = EXEC_TIMEOUT
) -> Dict[str, Any]:
    """执行一条客户机内命令，并把「发送 → 执行 → 结局」镜像到终端。

    镜像只是显示：真正执行仍在客户机里（见 :func:`_agent_exec_inner`）。之所以
    连输出一起贴出来，是因为用户要能确认命令**确实跑了**、跑出了什么，而不是
    只看一句「已完成」。
    """
    username = str(lease.get("username") or "")
    host_id = await _mirror_host(lease)
    # 展示用的命令：``sh -c '…'`` 这层壳是执行细节，终端里只显示它包着的命令，
    # 否则用户看到的是「sh -c systemctl…」，还得自己从壳里往外读。
    if (
        len(argv) >= 3
        and argv[0] in ("sh", "/bin/sh", "/usr/bin/sh")
        and argv[1] == "-c"
    ):
        display = str(argv[2])
    elif len(argv) >= 3 and argv[0].lower().endswith("cmd.exe") and argv[1] == "/c":
        display = str(argv[2])
    else:
        display = " ".join(shlex.quote(part) for part in argv) if argv else ""
    if host_id and display:
        aiterm.mirror_command_start(username, host_id, display)
        aiterm.mirror_notice(
            username,
            host_id,
            i18n.pick(
                "（以下命令在客户机内通过 QEMU Guest Agent 执行）",
                "(running inside the guest via the QEMU Guest Agent)",
            ),
        )
    try:
        result = await _agent_exec_inner(lease, argv, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 失败也要在终端里留痕
        if host_id:
            aiterm.mirror_output(username, host_id, str(exc)[:2000] + "\n")
            aiterm.mirror_command_end(username, host_id, False)
        raise
    if host_id:
        body = str(result.get("out") or "") + str(result.get("err") or "")
        if body:
            aiterm.mirror_output(username, host_id, body)
        aiterm.mirror_command_end(
            username, host_id, int(result.get("exitcode") or 0) == 0
        )
    return result


async def _agent_exec_inner(
    lease: Dict[str, Any], argv: List[str], *, timeout: float = EXEC_TIMEOUT
) -> Dict[str, Any]:
    """在客户机里跑一条 **argv**（不过 shell），拿回退出码与输出。

    Guest Agent 是「先返回 pid、再轮询 exec-status」的异步协议，PVE 不提供阻塞式
    调用。轮询间隔 0.3s：太密会给 PVE 制造无谓请求，太疏一条 10 秒的命令要等半天。
    """
    if str(lease.get("guest_type") or "qemu") == "lxc":
        raise ToolDenied(
            i18n.pick(
                "容器暂不支持 AI 内联执行（需要节点侧 pct 通道）",
                "Inline execution inside containers is not available yet "
                "(it needs the node-side pct channel)",
            )
        )
    # 标准模式：命令交给客户机内的受限执行器 —— 它把实际进程降到非 root 用户、
    # 套上 cgroup（CPU/内存上限）、并登记 pid 以便撤销时能 kill。
    # 兼容模式：直接跑，客户机里没有任何隔离，安全性全靠服务端白名单。
    if (
        str(lease.get("mode")) == "standard"
        and str(lease.get("os_type")) != "windows"
        and argv
        and argv[0] != DIAGD_PATH
        # 运维授权里的变更命令直接跑：diagd 的 512MB 地址空间上限会把 apt / pip /
        # npm 这类安装直接掐死（子进程继承硬上限，外面提不上去）。这一档已经过
        # 用户逐条审批，代价是失去 cgroup 上限与「按租约 kill」。
        and not lease.get("_direct")
    ):
        argv = [
            DIAGD_PATH,
            "run",
            "--id",
            str(lease.get("_action_id") or "-"),
            "--timeout",
            str(int(max(1.0, timeout))),
            "--",
        ] + list(argv)
        # 给 diagd 自己的收口（杀 cgroup、落日志）留出余量，否则外面先超时、
        # 里面还在跑，撤销时就少了一个能 kill 的句柄。
        timeout = float(timeout) + 5.0

    node = str(lease.get("node") or "")
    vmid = int(lease.get("vmid") or 0)
    client = await _client_for(lease)
    try:
        started = await client.qemu_agent_exec(node, vmid, argv)
    except ProxmoxError as exc:
        raise ToolDenied(
            i18n.pick(
                "客户机没有响应 QEMU Guest Agent（请确认虚拟机内已安装并运行 "
                "qemu-guest-agent，且虚拟机配置里 agent=1）：",
                "The guest is not answering the QEMU Guest Agent (make sure "
                "qemu-guest-agent is installed and running inside the VM, and "
                "agent=1 is set in its config): ",
            )
            + str(exc.message)[:200]
        ) from exc
    pid = (started or {}).get("pid")
    if not pid:
        raise ToolDenied(
            i18n.pick("客户机没有返回执行句柄", "The guest returned no execution handle")
        )

    deadline = time.monotonic() + max(1.0, float(timeout))
    status: Dict[str, Any] = {}
    while True:
        try:
            status = await client.qemu_agent_exec_status(node, vmid, pid)
        except ProxmoxError as exc:
            raise ToolDenied(str(exc.message)[:300]) from exc
        if status.get("exited"):
            break
        if time.monotonic() >= deadline:
            # 超时不杀进程（agent 没有 kill 接口），但必须如实告诉模型：
            # 它以为命令失败、其实可能还在跑，这个区别会影响后续判断。
            raise ToolDenied(
                i18n.pick(
                    f"命令超过 {int(timeout)} 秒仍未返回，已停止等待（进程可能仍在客户机内运行）",
                    f"The command did not return within {int(timeout)}s; stopped waiting "
                    "(it may still be running inside the guest)",
                )
            )
        await asyncio.sleep(0.3)

    return {
        "exitcode": int(status.get("exitcode") or 0),
        "out": formatters.decode_agent_output(status.get("out-data")),
        "err": formatters.decode_agent_output(status.get("err-data")),
    }


async def _run_shell(
    lease: Dict[str, Any], command: str, *, timeout: float = EXEC_TIMEOUT
) -> Dict[str, Any]:
    """执行一条**已经过白名单校验**的只读命令。

    用 ``sh -c`` 而不是拆 argv：管道（``ps aux | grep nginx``）在诊断里太常用，
    而 agent 的 exec 只接受 argv、不认管道。安全性由 ``aitools.validate_command``
    保证（禁 ``;`` / ``&&`` / 重定向 / 命令替换，每段首命令在白名单内）。

    Windows 走 ``cmd.exe /c``：白名单在 :func:`_check_read_command` 里换成另一套
    （``ipconfig`` / ``tasklist`` / ``type`` 这些在文件系统里并不存在，用 POSIX
    规则比对会全部拒绝）。
    """
    if str(lease.get("os_type")) == "windows":
        return await _agent_exec(lease, ["cmd.exe", "/c", command], timeout=timeout)
    return await _agent_exec(lease, ["sh", "-c", command], timeout=timeout)


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + i18n.pick(
        f"\n…（输出已截断，共 {len(text)} 字符）",
        f"\n… (output truncated, {len(text)} chars in total)",
    )


# --------------------------------------------------------------------- 策略层

def _check_ops_path(lease: Dict[str, Any], path: str, *, write: bool) -> str:
    """运维授权下的路径检查：**不再要求在预设目录内**，但仍拒绝认证材料与设备节点。

    装一个包会写到 /usr、/var/lib/dpkg、/etc/apt，逐个预先授权既不现实也没意义；
    这里把边界从「哪些目录」换成「哪些文件永远不能碰」。``write`` 目前不改变判断，
    保留参数是为了与 :func:`_check_path` 同签名。
    """
    del write
    if str(lease.get("os_type")) == "windows":
        text = ntpath.normpath(str(path or "").strip())
        if not ntpath.isabs(text) or len(text) > 240:
            raise ToolDenied(
                i18n.pick(
                    "路径必须是绝对路径（例如 C:\\ProgramData\\app\\app.log）",
                    "Path must be absolute (e.g. C:\\ProgramData\\app\\app.log)",
                )
            )
        if ".." in text.split("\\"):
            raise ToolDenied(i18n.pick("路径不合法", "Invalid path"))
        if any(hint in text.lower() for hint in WINDOWS_FORBIDDEN):
            raise ToolDenied(
                i18n.pick("该路径不在允许范围内", "This path is not allowed")
            )
        return text

    text = str(path or "").strip()
    if not text.startswith("/") or len(text) > 200:
        raise ToolDenied(i18n.pick("路径必须是绝对路径", "Path must be absolute"))
    text = posixpath.normpath(text)
    if text == "/" or ".." in text.split("/"):
        raise ToolDenied(i18n.pick("路径不合法", "Invalid path"))
    if any(hint in text.lower() for hint in FORBIDDEN_HINTS):
        raise ToolDenied(
            i18n.pick("该路径不在允许范围内", "This path is not allowed")
        )
    return text


def _check_path(lease: Dict[str, Any], path: str, *, write: bool) -> str:
    """路径审计：两套语义（POSIX / Windows），但**同一条底线**——
    必须在授权目录之下，且不碰密钥、密码库、系统核心配置。"""
    if is_ops(lease):
        return _check_ops_path(lease, path, write=write)
    roots = lease.get("fs_rw") if write else lease.get("fs_ro")
    # 防御：运维票的 "/" 只用于票面展示。万一 access 字段被改回 write 而目录列表
    # 还留着 "/"，这里把它剔掉 —— 否则「改一个字段」就等于放开了整台机器。
    if str(lease.get("os_type")) == "windows":
        roots = [item for item in roots or [] if len(str(item).strip()) > 3]
    else:
        roots = [item for item in roots or [] if str(item).strip().rstrip("/")]
    scope_hint = i18n.pick(
        "该路径不在本次授权范围内（可写目录："
        + ("、".join(lease.get("fs_rw") or []) or "无")
        + "）",
        "This path is outside the granted scope (writable: "
        + (" / ".join(lease.get("fs_rw") or []) or "none")
        + ")",
    )

    if str(lease.get("os_type")) == "windows":
        text = ntpath.normpath(str(path or "").strip())
        if not ntpath.isabs(text) or len(text) > 240:
            raise ToolDenied(
                i18n.pick(
                    "路径必须是绝对路径（例如 C:\\ProgramData\\app\\app.log）",
                    "Path must be absolute (e.g. C:\\ProgramData\\app\\app.log)",
                )
            )
        if ".." in text.split("\\"):
            raise ToolDenied(i18n.pick("路径不合法", "Invalid path"))
        lowered = text.lower()
        if any(hint in lowered for hint in WINDOWS_FORBIDDEN):
            raise ToolDenied(
                i18n.pick("该路径不在允许范围内", "This path is not allowed")
            )
        if not any(
            lowered == str(root).lower()
            or lowered.startswith(str(root).rstrip("\\").lower() + "\\")
            for root in roots or []
        ):
            raise ToolDenied(scope_hint)
        return text

    text = str(path or "").strip()
    if not text.startswith("/") or len(text) > 200:
        raise ToolDenied(i18n.pick("路径必须是绝对路径", "Path must be absolute"))
    text = posixpath.normpath(text)
    if text == "/" or ".." in text.split("/"):
        raise ToolDenied(i18n.pick("路径不合法", "Invalid path"))
    lowered = text.lower()
    if any(hint in lowered for hint in FORBIDDEN_HINTS):
        raise ToolDenied(
            i18n.pick("该路径不在允许范围内", "This path is not allowed")
        )
    if not any(text == root or text.startswith(root.rstrip("/") + "/") for root in roots or []):
        raise ToolDenied(scope_hint)
    return text


def _check_unit(lease: Dict[str, Any], unit: str) -> str:
    text = str(unit or "").strip()
    if not UNIT_RE.match(text):
        raise ToolDenied(i18n.pick("单元名不合法", "Invalid unit name"))
    allowed = lease.get("units") or []
    if text not in allowed:
        raise ToolDenied(
            i18n.pick(
                "这个服务不在本次授权可重启的清单里（" + ("、".join(allowed) or "无") + "）",
                "This unit is not on the granted restart list ("
                + (" / ".join(allowed) or "none") + ")",
            )
        )
    return text


def _check_read_command(command: str, os_type: str = "linux") -> str:
    text = str(command or "").strip()
    if not text:
        raise ToolDenied(i18n.pick("命令不能为空", "Command must not be empty"))

    # Windows：cmd.exe 内建命令在文件系统里根本不存在（type / dir / sc），拿 Linux
    # 那份白名单去比对会全数拒绝 —— 这就是「Windows 客户机一律不可用」的成因。
    if str(os_type) == "windows":
        lowered = text.lower()
        # & 是命令分隔符（等价于 ;），> < 是重定向，%VAR% 会展开环境变量，
        # 这三类都能把「一条只读命令」变成别的东西。管道 | 留着：
        # `ipconfig | findstr IPv4` 是最常见的用法，且它不构成命令串联。
        if any(token in text for token in ("&", ">", "<", "%", "`", "$(")):
            raise ToolDenied(
                i18n.pick(
                    "Windows 只读命令不允许 & / > / < / % / 反引号",
                    "Windows read-only commands may not use & / > / < / % / backticks",
                )
            )
        if not any(lowered.startswith(cmd) for cmd in WINDOWS_COMMANDS):
            raise ToolDenied(
                i18n.pick(
                    "这条命令不在 Windows 只读白名单内（ipconfig / tasklist / netstat / "
                    "sc query / type / dir / wevtutil 等）",
                    "This command is not on the Windows read-only allowlist "
                    "(ipconfig / tasklist / netstat / sc query / type / dir / wevtutil, …)",
                )
            )
        return text

    # 复用宿主机那套白名单：同一份规则维护两遍必然分叉。
    # 它抛的是 aitools.ToolError，这里转成 ToolDenied —— 两者的去向不同：
    # ToolDenied 会被记成「策略拒绝」，而普通异常会被记成执行故障。把「被拦下」
    # 记成「出错」，事后看审计就分不清「AI 试图越界」和「客户机没响应」。
    try:
        aitools.validate_command(text)
    except aitools.ToolError as exc:
        raise ToolDenied(str(exc)) from exc
    return text


def _check_ops_command(lease: Dict[str, Any], command: str) -> str:
    """运维授权的命令检查：不查白名单，只挡「不可回滚」的那几类。

    为什么不再用白名单：装软件的命令组合是开放的（apt / dnf / 发行版特有的包管理器
    与本地脚本），穷举必然写不全，写不全的后果就是「用户授权了运维，AI 还是什么都
    干不了」。安全边界因此从「能跑什么」换成「什么绝对不能跑 + 每一步都要人点头」。
    """
    text = str(command or "").strip()
    if not text:
        raise ToolDenied(i18n.pick("命令不能为空", "Command must not be empty"))
    if "\x00" in text or len(text) > OPS_MAX_COMMAND:
        raise ToolDenied(
            i18n.pick(
                f"命令过长（上限 {OPS_MAX_COMMAND} 字符），请拆成几条",
                f"Command too long (limit {OPS_MAX_COMMAND} characters); split it up",
            )
        )
    windows = str(lease.get("os_type")) == "windows"
    patterns = WINDOWS_OPS_FORBIDDEN if windows else OPS_FORBIDDEN
    lowered = text.lower()
    for pattern in patterns:
        if re.search(pattern, lowered, re.MULTILINE):
            raise ToolDenied(
                i18n.pick(
                    "这条命令包含不可回滚的操作（删根 / 重分区 / 整机关机重启 / "
                    "修改认证信息等），即使运维授权也不会执行。请让用户手动处理。",
                    "This command contains an irreversible operation (removing the root "
                    "filesystem, repartitioning, rebooting the VM, altering credentials, …). "
                    "It will not run even under an ops grant; ask the user to do it manually.",
                )
            )
    return text


def _ops_command_risk(lease: Dict[str, Any], command: str) -> int:
    """运维命令的风险分级：决定「直接跑 / 单次审批 / 双人审批」。

    能过只读白名单的命令按只读处理 —— ``journalctl -u nginx`` 每次都弹审批，
    用户很快就会无脑点「批准」，那时审批就失效了。只有真正会改状态的命令
    （装包、写配置、重启服务、带 ``;`` / ``&&`` 的复合命令）才要求批准。
    """
    if str(lease.get("os_type")) == "windows":
        # Windows 没有可靠的白名单：凡是不在只读清单里的都按变更处理
        lowered = command.strip().lower()
        if any(
            lowered.startswith(cmd.strip())
            for cmd in WINDOWS_COMMANDS
        ) and not any(token in command for token in ("&", ">", "<", "%", "`", "$(")):
            return RISK_READ
        return RISK_CONFIG
    if any(token in command for token in (";", "&&", "||", "`", "$(")):
        # 复合命令里任何一段都可能在改状态，而且拼装方式千变万化 —— 按变更处理
        return RISK_CONFIG
    try:
        aitools.validate_command(command)
        return RISK_READ
    except aitools.ToolError:
        pass
    # 管道不在这里放行：`docker ps | xargs rm` 的开头看着人畜无害
    if "|" not in command and OPS_READ_ONLY.match(command):
        return RISK_READ
    if OPS_MUTATING.search(command):
        return RISK_CONFIG
    return RISK_TMP


def _risk_allowed(lease: Dict[str, Any], risk: int) -> None:
    if risk > int(lease.get("risk_max") or RISK_READ):
        raise ToolDenied(
            i18n.pick(
                f"本次授权最高允许「{RISK_LABELS.get(int(lease.get('risk_max') or 0), '')}」，"
                f"该操作属于「{RISK_LABELS.get(risk, '')}」",
                f"This grant allows at most “{RISK_LABELS.get(int(lease.get('risk_max') or 0), '')}”; "
                f"this action is “{RISK_LABELS.get(risk, '')}”",
            )
        )


async def _ask(
    lease: Dict[str, Any],
    *,
    tool: str,
    target: str,
    purpose: str,
    risk: int,
    detail: str = "",
) -> bool:
    """推一个审批卡给前端并挂起。没有推送器（一次性排查）时按拒绝处理。"""
    notify = _NOTIFY.get()
    if notify is None:
        return False
    # 双人审批只对「改配置 / 重启」及以上生效，且由租约的策略字段决定 ——
    # 签发时固化，中途改配置不会影响这一轮（否则同一张票前后两套规则）。
    required = 2 if (lease.get("double_approve") and risk >= RISK_CONFIG) else 1
    # 只有一个人能审批时，第二票永远等不到：到点按拒绝收口，用户看到的就是
    # 「批准了、AI 却停着不动」。运行时再核一次「还有没有别人」，让已经签出去的
    # 旧票据也能立刻可用（不必等它过期或重签）。
    if required > 1 and not await _has_second_approver(str(lease.get("username") or "")):
        required = 1
    # 终端里先亮一行「等确认执行什么」：用户左边看终端、右边看审批卡时，
    # 不用来回对照也知道这条命令是发给哪台机器、要干什么。
    mirror_host = await _mirror_host(lease)
    if mirror_host:
        aiterm.mirror_approval_wait(
            str(lease.get("username") or ""), mirror_host, target, purpose
        )
    approval_id = uuid.uuid4().hex[:12]
    # 先登记等待点再推送：反过来的话，手快的用户点了「批准」会撞上一个还不存在
    # 的 Future 而被丢弃，表现成「点了没反应」（宿主机那条审批也有同样的隐患）。
    ai_approval.prepare(
        approval_id, owner=str(lease.get("username") or ""), required=required
    )
    await notify(
        {
            "type": "approval",
            "approval_id": approval_id,
            "kind": "vm_action",
            "tool": tool,
            "command": target,
            "purpose": purpose,
            "risk": risk,
            "risk_label": RISK_LABELS.get(risk, ""),
            "detail": detail,
            "timeout": int(ai_approval.APPROVAL_TIMEOUT),
            "required": required,
        }
    )
    # owner 让审批接口能校验「批准的人就是发起人」，堵住「任取一个 approval_id
    # 就能替别人拍板」的老问题（见 routers/ai.py 的 /approval）。
    approved = await ai_approval.wait_for_approval(
        approval_id, owner=str(lease.get("username") or ""), required=required
    )
    # 一行收口日志：以后遇到「批了却不执行」，先看这里是 approved 还是根本没等到
    logger.info(
        "VM 动作审批收口：id=%s approved=%s required=%s risk=%s tool=%s",
        approval_id,
        approved,
        required,
        risk,
        tool,
    )
    return approved


# ------------------------------------------------------------------- 审计落库

async def _record(
    lease: Dict[str, Any],
    *,
    seq: int,
    tool: str,
    target: str,
    risk: int,
    verdict: str,
    ok: bool,
    exitcode: int = 0,
    output: str = "",
    diff: str = "",
    backup: str = "",
    elapsed_ms: int = 0,
) -> str:
    action_id = new_id()
    try:
        async with database.connect() as db:
            await db.execute(
                "INSERT INTO ai_vm_actions (id, lease_id, username, seq, tool, target,"
                " risk, verdict, ok, exitcode, output, diff, backup, elapsed_ms, created)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action_id,
                    str(lease.get("id") or ""),
                    str(lease.get("username") or "")[:64],
                    int(seq),
                    tool[:32],
                    target[:500],
                    int(risk),
                    verdict[:16],
                    1 if ok else 0,
                    int(exitcode),
                    _truncate(output, 2000),
                    (diff or "")[:8000],
                    (backup or "")[:500],
                    int(elapsed_ms),
                    int(time.time()),
                ),
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不该让排查本身挂掉
        logger.warning("AI VM 动作审计写入失败", exc_info=True)
    return action_id


async def list_actions(lease_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM ai_vm_actions WHERE lease_id=? ORDER BY seq ASC, created ASC"
            " LIMIT ?",
            (lease_id, max(1, min(300, limit))),
        )
        rows = await cursor.fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["ok"] = bool(item.get("ok"))
        item["risk_label"] = RISK_LABELS.get(int(item.get("risk") or 0), "")
        items.append(item)
    return items


# ------------------------------------------------------------------- 工具实现

async def _tool_list_dir(lease: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    windows = str(lease.get("os_type")) == "windows"
    default = "C:\\\\ProgramData" if windows else "/var/log"
    path = _check_path(lease, str(args.get("path") or default), write=False)
    if not _is_dir_allowed(lease, path):
        raise ToolDenied(
            i18n.pick("只能列授权目录下的路径", "Only granted directories may be listed")
        )
    if windows:
        result = await _agent_exec(lease, ["cmd.exe", "/c", f'dir /a "{path}"'])
        command = f"dir {path}"
    else:
        result = await _agent_exec(lease, ["ls", "-la", "--", path])
        command = f"ls -la {path}"
    return {
        "ok": result["exitcode"] == 0,
        "output": _truncate(result["out"] or result["err"]),
        "command": command,
        "exitcode": result["exitcode"],
    }


def _is_dir_allowed(lease: Dict[str, Any], path: str) -> bool:
    """路径是否落在授权根之下。Windows 要按大小写不敏感 + 反斜杠比较。"""
    windows = str(lease.get("os_type")) == "windows"
    roots = list(lease.get("fs_ro") or []) + list(lease.get("fs_rw") or [])
    target = path.lower() if windows else path
    sep = "\\" if windows else "/"
    for root in roots:
        base = str(root).lower() if windows else str(root)
        if target == base or target.startswith(base.rstrip(sep) + sep):
            return True
    return False


async def _tool_read_file(lease: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    path = _check_path(lease, str(args.get("path") or ""), write=False)
    try:
        lines = max(1, min(2000, int(str(args.get("lines") or "200"))))
    except ValueError:
        lines = 200
    windows = str(lease.get("os_type")) == "windows"
    if windows:
        # Windows 没有 head，只能 type 全文后在这里截断行数
        result = await _agent_exec(lease, ["cmd.exe", "/c", f'type "{path}"'])
        text = "\n".join((result["out"] or "").splitlines()[:lines])
        command = f"type {path}"
    else:
        # head 而不是 cat：几千行的日志整段回给模型既烧 token 又把重点淹掉。
        # 需要更多就用 lines 调大或配合 grep（vm_run_command）。
        result = await _agent_exec(lease, ["head", "-n", str(lines), "--", path])
        text = result["out"]
        command = f"head -n {lines} {path}"
    if len(text.encode("utf-8", "replace")) > MAX_READ_BYTES:
        text = text.encode("utf-8", "replace")[:MAX_READ_BYTES].decode("utf-8", "ignore")
    return {
        "ok": result["exitcode"] == 0,
        "output": _truncate(text or result["err"]),
        "command": command,
        "exitcode": result["exitcode"],
    }


async def _tool_run_command(lease: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    raw = str(args.get("command") or "")
    if is_ops(lease):
        # 运维授权：命令本身是开放的，但会改状态的每一步都要当场批准。
        command = _check_ops_command(lease, raw)
        risk = _ops_command_risk(lease, command)
        _risk_allowed(lease, risk)
        if risk > RISK_READ and not await _ask(
            lease,
            tool="vm_run_command",
            target=command,
            purpose=i18n.pick(
                f"在客户机内执行（{RISK_LABELS.get(risk, '')}）：{command[:160]}",
                f"Run inside the guest ({RISK_LABELS.get(risk, '')}): {command[:160]}",
            ),
            risk=risk,
            detail=i18n.pick(
                "以 root 执行，输出与退出码会记入审计。装包或改配置可能让服务短暂中断。",
                "Runs as root; output and exit code are audited. Installing packages or "
                "editing configs may briefly interrupt services.",
            ),
        ):
            return {
                "ok": False,
                "output": i18n.pick(
                    "用户没有批准这条命令。不要原样重试；先说明它为什么必要、"
                    "会改动哪些文件或服务。",
                    "The user did not approve this command. Do not retry it verbatim; "
                    "explain why it is necessary and what it will change first.",
                ),
                "command": command,
                "exitcode": 1,
                "risk": risk,
            }
        if risk >= RISK_CONFIG:
            # 见 _agent_exec：变更命令绕开 diagd 的内存上限，否则装不上包。
            lease["_direct"] = True
        try:
            result = await _run_shell(lease, command, timeout=EXEC_TIMEOUT_OPS)
        finally:
            lease.pop("_direct", None)
    else:
        command = _check_read_command(raw, str(lease.get("os_type")))
        risk = RISK_READ
        result = await _run_shell(lease, command)
    ok = result["exitcode"] == 0
    text = result["out"] if ok else (result["err"] or result["out"])
    return {
        "ok": ok,
        "output": _truncate(text),
        "command": command,
        "exitcode": result["exitcode"],
        "risk": risk,
    }


async def _read_remote_text(lease: Dict[str, Any], path: str) -> str:
    """读一个文件（用于备份与 diff）。读不到就返回空串，由调用方按「新建」处理。"""
    if str(lease.get("os_type")) == "windows":
        result = await _agent_exec(lease, ["cmd.exe", "/c", f'type "{path}"'])
        return result["out"] or ""
    quoted = shlex.quote(path)
    try:
        result = await _agent_exec(
            lease, ["sh", "-c", f"test -f {quoted} && cat {quoted} || true"]
        )
    except ToolDenied:
        raise
    return result["out"] or ""


async def _tool_write_file(lease: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    path = _check_path(lease, str(args.get("path") or ""), write=True)
    content = str(args.get("content") or "")
    if len(content.encode("utf-8", "replace")) > MAX_WRITE_BYTES:
        raise ToolDenied(
            i18n.pick(
                f"单次写入上限 {MAX_WRITE_BYTES} 字节，请拆成多次或改用命令",
                f"Single write limit is {MAX_WRITE_BYTES} bytes; split it up",
            )
        )
    _risk_allowed(lease, RISK_CONFIG)

    before = await _read_remote_text(lease, path)
    diff = _make_diff(path, before, content)
    purpose = i18n.pick(
        f"写入 {path}（{len(content)} 字符，含 {diff.count(chr(10))} 行 diff）",
        f"Write {path} ({len(content)} chars, {diff.count(chr(10))} diff lines)",
    )
    if not await _ask(
        lease,
        tool="vm_write_file",
        target=path,
        purpose=purpose,
        risk=RISK_CONFIG,
        detail=diff,
    ):
        return {
            "ok": False,
            "output": i18n.pick(
                "用户没有批准这次写入。不要重复提交同样的内容，先说明为什么必须改。",
                "The user did not approve this write. Do not resubmit it; explain why "
                "the change is necessary first.",
            ),
            "command": f"write {path}",
            "exitcode": 1,
        }

    # 备份：写之前先把原文件存一份。放在客户机的固定目录而不是面板里 ——
    # 回滚要在客户机上完成，备份在面板上就得再传一次（大文件、跨网络、还得处理权限）。
    backup = ""
    stamp = int(time.time())
    if before:
        if str(lease.get("os_type")) == "windows":
            root = f"{WINDOWS_BACKUP_DIR}\\{lease.get('id')}"
            backup = f"{root}\\{stamp}-{ntpath.basename(path)}.bak"
            # 2>nul / >nul：Windows 的 mkdir 在目录已存在时报错、copy 会刷屏，
            # 这两处噪音对判断成败没有帮助。真正的判据是下面那个退出码。
            mk = await _agent_exec(
                lease,
                [
                    "cmd.exe",
                    "/c",
                    f'mkdir "{root}" 2>nul & copy /y "{path}" "{backup}" >nul',
                ],
            )
            err = mk["err"] or mk["out"]
        else:
            backup = f"{BACKUP_DIR}/{lease.get('id')}/{stamp}-{posixpath.basename(path)}.bak"
            quoted_src = shlex.quote(path)
            quoted_bak = shlex.quote(backup)
            mk = await _agent_exec(
                lease,
                ["sh", "-c", f"mkdir -p {shlex.quote(BACKUP_DIR + '/' + str(lease.get('id'))) }"
                 f" && cp -p {quoted_src} {quoted_bak}"],
            )
            err = mk["err"]
        if mk["exitcode"] != 0:
            raise ToolDenied(
                i18n.pick(
                    "无法创建回滚备份，已中止写入：" + err[:200],
                    "Could not create a rollback backup; write aborted: " + err[:200],
                )
            )

    await _ensure_snapshot(lease)
    await _write_remote(lease, path, content)
    after = await _read_remote_text(lease, path)
    ok = after == content
    return {
        "ok": ok,
        "output": i18n.pick(
            f"已写入 {path}（{len(content)} 字符）" + ("，内容校验一致" if ok else "，但回读内容不一致，请检查"),
            f"Wrote {path} ({len(content)} chars)"
            + (", verified" if ok else ", but the read-back differs — please check"),
        ),
        "command": f"write {path}",
        "exitcode": 0 if ok else 1,
        "diff": diff,
        "backup": backup,
    }


async def _write_remote(lease: Dict[str, Any], path: str, content: str) -> None:
    """用 Guest Agent 的文件接口写（base64），不经 shell —— 内容里有引号、反引号、
    ``$`` 都不会被解释，这是「写配置」最要紧的一条。"""
    client = await _client_for(lease)
    node = str(lease.get("node") or "")
    vmid = int(lease.get("vmid") or 0)
    handle = await client.qemu_agent_file_open(node, vmid, path, "wb")
    if not handle:
        raise ToolDenied(
            i18n.pick("客户机拒绝打开该文件（权限不足？）", "The guest refused to open the file")
        )
    try:
        payload = base64.b64encode(content.encode("utf-8")).decode("ascii")
        # 单次 buf 太大时 PVE 会回 400，按 96KB 分块
        step = 96 * 1024
        for start in range(0, len(payload), step):
            await client.qemu_agent_file_write(node, vmid, handle, payload[start : start + step])
    finally:
        try:
            await client.qemu_agent_file_close(node, vmid, handle)
        except ProxmoxError:  # noqa: BLE001
            logger.debug("AI VM：关闭文件句柄失败", exc_info=True)


async def _tool_restart_unit(lease: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    if str(lease.get("os_type")) == "windows":
        # Windows 的服务名与 Linux 的 unit 名不是一回事（显示名 / 服务名 / 进程名
        # 三套并存），先不支持比支持一半更好：免得 AI 拿着 "nginx" 去 sc restart。
        raise ToolDenied(
            i18n.pick(
                "Windows 客户机暂不支持重启服务，请让用户手动操作",
                "Restarting services on Windows guests is not supported yet; "
                "ask the user to do it manually",
            )
        )
    if is_ops(lease):
        # 运维授权不再预先圈定服务清单：装完软件要启动/重启它，属于同一件事。
        # 单元名仍按正则校验（防止把 "foo; rm -rf /" 拼进 systemctl 的 argv）。
        unit = str(args.get("unit") or "").strip()
        if not UNIT_RE.match(unit):
            raise ToolDenied(i18n.pick("单元名不合法", "Invalid unit name"))
    else:
        unit = _check_unit(lease, str(args.get("unit") or ""))
    _risk_allowed(lease, RISK_CONFIG)
    if not await _ask(
        lease,
        tool="vm_restart_unit",
        target=f"systemctl restart {unit}",
        purpose=i18n.pick(f"重启服务 {unit}", f"Restart the {unit} service"),
        risk=RISK_CONFIG,
        detail=i18n.pick(
            "服务会短暂中断（秒级）。回滚方式：再次重启，或按动作记录里的服务状态恢复。",
            "The service is briefly unavailable. To roll back, restart it again or "
            "restore the state recorded in the action log.",
        ),
    ):
        return {
            "ok": False,
            "output": i18n.pick(
                "用户没有批准重启该服务。",
                "The user did not approve restarting this service.",
            ),
            "command": f"systemctl restart {unit}",
            "exitcode": 1,
        }
    # argv 形式，不过 shell：单元名已经过正则与白名单校验，这里不再拼字符串
    restarted = await _agent_exec(lease, ["systemctl", "restart", unit])
    check = await _agent_exec(lease, ["systemctl", "is-active", unit])
    active = (check["out"] or "").strip()
    ok = restarted["exitcode"] == 0 and active == "active"
    return {
        "ok": ok,
        "output": i18n.pick(
            f"重启后状态：{active or '未知'}"
            + ("" if ok else f"（stderr: {restarted['err'][:200]}）"),
            f"State after restart: {active or 'unknown'}"
            + ("" if ok else f" (stderr: {restarted['err'][:200]})"),
        ),
        "command": f"systemctl restart {unit}",
        "exitcode": restarted["exitcode"],
    }


def _diag_plan(lease: Dict[str, Any]) -> List[tuple]:
    """诊断包要跑的固定命令组。

    命令是**写死在这里的**，不来自模型 —— 所以这里不走 ``_check_read_command``
    （那套白名单是给模型输入用的）。风险边界也在于此：这份清单改一次就该被审一次。
    """
    if str(lease.get("os_type")) == "windows":
        return [
            ("系统信息", ["cmd.exe", "/c", "systeminfo"]),
            ("监听端口", ["cmd.exe", "/c", "netstat -ano"]),
            ("进程", ["cmd.exe", "/c", "tasklist /svc"]),
            ("磁盘", ["cmd.exe", "/c", "wmic logicaldisk get size,freespace,caption"]),
            ("最近错误", ["cmd.exe", "/c", "wevtutil qe System /c:20 /rd:true /f:text"]),
        ]
    return [
        ("负载与运行时长", ["sh", "-c", "uptime"]),
        ("内存", ["sh", "-c", "free -m"]),
        ("磁盘", ["sh", "-c", "df -h"]),
        ("监听端口", ["sh", "-c", "ss -tulpn"]),
        ("CPU 占用前 15", ["sh", "-c", "ps aux --sort=-%cpu | head -15"]),
        ("内核日志", ["sh", "-c", "dmesg | tail -40"]),
        ("系统日志", ["sh", "-c", "journalctl -n 60 --no-pager"]),
    ]


async def _tool_collect_diagnostics(
    lease: Dict[str, Any], args: Dict[str, Any]
) -> Dict[str, Any]:
    """一次性收集常用证据（诊断包）。

    为什么值得单独一个工具：模型逐条调工具时，每条输出都要过一次模型上下文，
    六七条就把预算吃掉一半；这里在**服务端**合并、按段截断，只回一份摘要。
    代价是模型看不到原始全文 —— 需要细节时它仍然可以单独调 read/run。
    """
    _risk_allowed(lease, RISK_READ)
    limit = 9000
    chunks: List[str] = []
    for label, argv in _diag_plan(lease):
        try:
            result = await _agent_exec(lease, list(argv), timeout=15.0)
        except ToolDenied as exc:
            chunks.append(f"## {label}\n(不可用：{exc})")
            continue
        except Exception as exc:  # noqa: BLE001 - 单项失败不该毁掉整份诊断包
            chunks.append(f"## {label}\n(失败：{str(exc)[:120]})")
            continue
        text = (result["out"] or result["err"] or "").strip()
        chunks.append(f"## {label}\n{_truncate(text, 1600)}")
        if sum(len(chunk) for chunk in chunks) >= limit:
            chunks.append("…（诊断包已达上限，其余项略）")
            break
    return {
        "ok": True,
        "output": _truncate("\n\n".join(chunks), limit),
        "command": "collect-diagnostics",
        "exitcode": 0,
    }


def _make_diff(path: str, before: str, after: str) -> str:
    if before == after:
        return i18n.pick("（内容没有变化）", "(no change)")
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile=f"a{path}",
        tofile=f"b{path}",
        lineterm="",
        n=3,
    )
    text = "\n".join(diff)
    return text if len(text) <= 6000 else text[:6000] + "\n…（diff 已截断）"


async def prepare_snapshot(lease: Dict[str, Any]) -> str:
    """签发后立刻打一张快照（策略要求时）。

    写文件的「写前备份」只覆盖那一个文件；运维授权下 AI 是直接跑 apt / dnf 的，
    它改动的是一整片目录与包数据库，只能靠整机快照兜底。打快照失败不阻挡授权
    —— 存储不支持快照是常态，前端会如实显示「无快照」。
    """
    pol = await policy()
    if not pol.get("auto_snapshot", True):
        return ""
    return await _ensure_snapshot(lease)


async def _ensure_snapshot(lease: Dict[str, Any]) -> str:
    """给 VM 打一张快照（尽力而为），同一张票只打一次。

    失败不中断：快照依赖存储能力（LVM-thin / ZFS 可以，dir 存储往往不行），
    而文件级备份已经覆盖了「改了哪个文件」这一层。但要在租约里记下「有没有拿到
    快照」，前端才好如实显示回滚粒度。
    """
    if lease.get("snapshot"):
        return str(lease.get("snapshot"))
    if str(lease.get("guest_type") or "qemu") != "qemu":
        return ""
    name = f"pvecenter-ai-{str(lease.get('id') or '')[:8]}"
    node = str(lease.get("node") or "")
    vmid = int(lease.get("vmid") or 0)
    client = await _client_for(lease)
    try:
        await client.qemu_snapshot_create(
            node, vmid, name, description="pvecenter AI 排查自动快照", vmstate=False
        )
    except Exception:  # noqa: BLE001 - 存储不支持快照是常态，不该挡住修复
        logger.info("AI VM：快照创建失败（存储可能不支持）%s/%s", node, vmid, exc_info=True)
        return ""
    lease["snapshot"] = name
    try:
        async with database.connect() as db:
            await db.execute(
                "UPDATE ai_vm_leases SET snapshot=? WHERE id=?", (name, lease.get("id"))
            )
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("AI VM：记录快照名失败", exc_info=True)
    return name


# ----------------------------------------------------------------------- 回滚

async def rollback(
    lease: Dict[str, Any], *, action_id: str = "", mode: str = "file"
) -> Dict[str, Any]:
    """回滚一次写操作（file）或整台机器（snapshot）。

    回滚本身也是一次动作，同样落审计 —— 否则「用回滚掩盖破坏」就无从追溯。
    """
    seq = await _bump_actions(str(lease.get("id") or ""))
    started = time.time()
    try:
        if mode == "snapshot":
            name = str(lease.get("snapshot") or "")
            if not name:
                raise ToolDenied(
                    i18n.pick("这张租约没有可用的快照", "This lease has no snapshot")
                )
            client = await _client_for(lease)
            await client.qemu_snapshot_rollback(
                str(lease.get("node") or ""), int(lease.get("vmid") or 0), name
            )
            result = {
                "ok": True,
                "output": i18n.pick(
                    f"已回滚到快照 {name}", f"Rolled back to snapshot {name}"
                ),
            }
            await _record(
                lease,
                seq=seq,
                tool="rollback",
                target=f"snapshot:{name}",
                risk=RISK_CONFIG,
                verdict="allow",
                ok=True,
                output=result["output"],
                elapsed_ms=int((time.time() - started) * 1000),
            )
            return result

        if not action_id:
            raise ToolDenied(
                i18n.pick("缺少要回滚的动作 id", "Missing the action id to roll back")
            )
        async with database.connect() as db:
            cursor = await db.execute(
                "SELECT * FROM ai_vm_actions WHERE id=? AND lease_id=?",
                (action_id, str(lease.get("id") or "")),
            )
            row = await cursor.fetchone()
        if not row:
            raise ToolDenied(i18n.pick("找不到这条动作", "Action not found"))
        action = dict(row)
        target = str(action.get("target") or "")
        backup = str(action.get("backup") or "")
        windows = str(lease.get("os_type")) == "windows"
        if not (ntpath.isabs(target) if windows else target.startswith("/")):
            raise ToolDenied(
                i18n.pick("这条动作不能按文件回滚", "This action cannot be rolled back per file")
            )
        if backup:
            if windows:
                done = await _agent_exec(
                    lease, ["cmd.exe", "/c", f'copy /y "{backup}" "{target}" >nul']
                )
                err = done["err"] or done["out"]
            else:
                done = await _agent_exec(
                    lease,
                    ["sh", "-c", f"cp -p {shlex.quote(backup)} {shlex.quote(target)}"],
                )
                err = done["err"]
            if done["exitcode"] != 0:
                raise ToolDenied(
                    i18n.pick("回滚失败：" + err[:200], "Rollback failed: " + err[:200])
                )
        else:
            # 原文件不存在 = 这次是「新建」，回滚就是删掉它
            if windows:
                done = await _agent_exec(lease, ["cmd.exe", "/c", f'del /f /q "{target}"'])
                err = done["err"] or done["out"]
            else:
                done = await _agent_exec(lease, ["rm", "-f", "--", target])
                err = done["err"]
            if done["exitcode"] != 0:
                raise ToolDenied(
                    i18n.pick("回滚失败：" + err[:200], "Rollback failed: " + err[:200])
                )
        result = {
            "ok": True,
            "output": i18n.pick(f"已还原 {target}", f"Restored {target}"),
        }
        await _record(
            lease,
            seq=seq,
            tool="rollback",
            target=target,
            risk=RISK_CONFIG,
            verdict="allow",
            ok=True,
            output=result["output"],
            backup=backup,
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return result
    except ToolDenied as exc:
        await _record(
            lease,
            seq=seq,
            tool="rollback",
            target=mode,
            risk=RISK_CONFIG,
            verdict="deny",
            ok=False,
            output=str(exc),
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return {"ok": False, "output": str(exc)}


# ------------------------------------------------------------------- 工具入口

async def run_tool(
    name: str, args: Any, *, perms: Optional[Iterable[str]] = None
) -> Dict[str, Any]:
    """执行一个 VM 工具。与 ``aitools.run_host_tool`` 同形：返回 ``{ok, output}``。"""
    # 第一道闸是权限位，与能力说明共用 aitools.TOOL_PERMISSIONS：说明页里看不见的
    # 工具，模型就算被提示词注入点名也跑不起来。租约是第二道闸（见下）。
    if not aitools.allowed_by_permissions(perms, name):
        return {
            "ok": False,
            "output": i18n.pick(
                "没有使用该工具的权限", "You do not have permission to use this tool"
            ),
        }
    lease = _LEASE.get()
    if lease is None:
        return {
            "ok": False,
            "output": i18n.pick(
                "本次会话没有生效的虚拟机授权，不能操作虚拟机内部。"
                "请先让用户在助手里为这台虚拟机签发授权。",
                "No active VM grant for this session. Ask the user to grant access "
                "to this VM in the assistant first.",
            ),
        }
    if lease.get("status") != "active":
        return {
            "ok": False,
            "output": i18n.pick(
                "本次授权已失效（到期或被撤销）", "This grant has expired or was revoked"
            ),
        }
    # 每个动作前重读一次状态：撤销必须**立刻**生效。contextvar 里存的是本轮
    # 开始时的快照，用户中途点了「撤销」，不重读就会让这一轮继续跑下去。
    fresh = await get_lease(str(lease.get("id") or ""))
    if not fresh or fresh.get("status") != "active":
        return {
            "ok": False,
            "output": i18n.pick(
                "授权已到期或被撤销，本次排查到此为止。",
                "The grant expired or was revoked; this investigation stops here.",
            ),
        }
    lease = fresh
    _LEASE.set(fresh)

    if int(lease.get("actions_used") or 0) >= int(lease.get("actions_max") or DEFAULT_ACTIONS):
        return {
            "ok": False,
            "output": i18n.pick(
                "本次授权的动作次数已用完，请让用户重新授权",
                "This grant has used up its action budget; ask the user to grant again",
            ),
        }

    payload = args if isinstance(args, dict) else {}
    seq = await _bump_actions(str(lease.get("id") or ""))
    started = time.time()
    risk = RISK_READ
    target = ""
    # 标准模式下 diagd 用这个 id 登记 pid，撤销时才找得到要 kill 的进程。
    lease["_action_id"] = uuid.uuid4().hex[:12]
    try:
        if name == "vm_list_dir":
            target = str(payload.get("path") or "")
            outcome = await _tool_list_dir(lease, payload)
        elif name == "vm_read_file":
            target = str(payload.get("path") or "")
            outcome = await _tool_read_file(lease, payload)
        elif name == "vm_run_command":
            target = str(payload.get("command") or "")
            outcome = await _tool_run_command(lease, payload)
            # 审计里要如实标出这条命令的风险级别：运维授权下既有只读命令也有装包，
            # 都记成同一级，事后翻日志就分不清「看了一眼」和「改了系统」。
            risk = int(outcome.pop("risk", risk) or risk)
        elif name == "vm_collect_diagnostics":
            target = "diagnostics"
            outcome = await _tool_collect_diagnostics(lease, payload)
        elif name == "vm_write_file":
            target = str(payload.get("path") or "")
            risk = RISK_CONFIG
            outcome = await _tool_write_file(lease, payload)
        elif name == "vm_restart_unit":
            target = str(payload.get("unit") or "")
            risk = RISK_CONFIG
            outcome = await _tool_restart_unit(lease, payload)
        else:
            raise ToolDenied(i18n.pick("没有这个工具", "No such tool"))
    except ToolDenied as exc:
        await _record(
            lease,
            seq=seq,
            tool=name,
            target=target,
            risk=risk,
            verdict="deny",
            ok=False,
            output=str(exc),
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return {"ok": False, "output": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 单条动作失败不该中断整轮排查
        logger.warning("AI VM 工具执行失败：%s", exc, exc_info=True)
        await _record(
            lease,
            seq=seq,
            tool=name,
            target=target,
            risk=risk,
            verdict="error",
            ok=False,
            output=str(exc)[:500],
            elapsed_ms=int((time.time() - started) * 1000),
        )
        return {"ok": False, "output": str(exc)[:300]}

    outcome.setdefault("elapsed_ms", int((time.time() - started) * 1000))
    await _record(
        lease,
        seq=seq,
        tool=name,
        target=target,
        risk=risk,
        verdict="allow",
        ok=bool(outcome.get("ok")),
        exitcode=int(outcome.get("exitcode") or 0),
        output=str(outcome.get("output") or ""),
        diff=str(outcome.get("diff") or ""),
        backup=str(outcome.get("backup") or ""),
        elapsed_ms=int(outcome.get("elapsed_ms") or 0),
    )
    return outcome


# --------------------------------------------------------------- 工具声明（LLM）

#: 只在**有租约**时下发给模型。没有授权时这些工具根本不存在 ——
#: 与 ``aitools`` 里「默认不给 exec」是同一个原则：能力不出现，就没法被诱导。
VM_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "vm_collect_diagnostics",
        "description": (
            "一次性收集常用证据（负载 / 内存 / 磁盘 / 监听端口 / 高 CPU 进程 / "
            "内核与系统日志），在服务端合并后返回。排查开始时先用它，比逐条调快得多",
            "Collect common evidence in one shot (load, memory, disk, listeners, "
            "top processes, kernel and system logs), merged server-side. "
            "Use it first — far cheaper than one call per item",
        ),
        "params": {},
    },
    {
        "name": "vm_list_dir",
        "description": (
            "列出虚拟机内一个目录的内容（需授权目录内）",
            "List a directory inside the VM (must be within the granted paths)",
        ),
        "params": {"path": {"required": True, "hint": "绝对路径，例如 /var/log/nginx"}},
    },
    {
        "name": "vm_read_file",
        "description": (
            "读取虚拟机内一个文件的开头若干行（需授权目录内）",
            "Read the first N lines of a file inside the VM (within granted paths)",
        ),
        "params": {
            "path": {"required": True, "hint": "绝对路径"},
            "lines": {"required": False, "hint": "读取行数，默认 200"},
        },
    },
    {
        "name": "vm_run_command",
        "description": (
            "在虚拟机内执行一条**只读**诊断命令（白名单命令，可用 | 串联；"
            "不允许写文件、改配置、重启服务）",
            "Run one **read-only** diagnostic command inside the VM (whitelisted, "
            "pipes allowed; no writes, config changes or service restarts)",
        ),
        "params": {"command": {"required": True, "hint": "例如 ps aux | grep nginx"}},
    },
    {
        "name": "vm_write_file",
        "description": (
            "覆盖写入虚拟机内的一个文件（需在授权的可写目录内）。"
            "会先给用户看 diff 等待批准，并自动备份原文件以便回滚",
            "Overwrite a file inside the VM (must be within a granted writable path). "
            "Shows a diff for approval first and backs up the original for rollback",
        ),
        "params": {
            "path": {"required": True, "hint": "绝对路径"},
            "content": {"required": True, "hint": "完整的新文件内容"},
        },
    },
    {
        "name": "vm_restart_unit",
        "description": (
            "重启虚拟机内的一个 systemd 服务（仅限授权清单内的单元）",
            "Restart a systemd unit inside the VM (only units on the granted list)",
        ),
        "params": {"unit": {"required": True, "hint": "例如 nginx"}},
    },
]

VM_TOOL_MAP = {item["name"]: item for item in VM_TOOLS}


def is_vm_tool(name: str) -> bool:
    return str(name or "") in VM_TOOL_MAP


def prompt_hint(lease: Dict[str, Any]) -> str:
    """把「这次能碰什么」写进系统提示。

    不写会怎样：模型不知道可读目录有哪些，要么凭想象报一个 ``/etc/nginx/conf.d``，
    被策略拒绝后再试一个，两三次就把动作配额耗光 —— 用户看到的是「AI 一直在瞎试」。
    把边界明说，它第一次就会走对。
    """
    ro = "、".join(lease.get("fs_ro") or []) or "无"
    rw = "、".join(lease.get("fs_rw") or []) or "无"
    units = "、".join(lease.get("units") or []) or "无"
    remaining = max(0, int(lease.get("expires_at") or 0) - int(time.time())) // 60
    kind = "容器" if str(lease.get("guest_type")) == "lxc" else "虚拟机"
    quota = int(lease.get("actions_max") or 0) - int(lease.get("actions_used") or 0)
    windows = str(lease.get("os_type")) == "windows"
    os_label = "Windows" if windows else "Linux"

    if is_ops(lease):
        # 运维授权的提示词要回答的是「你能不能自己装」，而不是「哪些目录能看」。
        # 不写清楚，模型会沿用只读模式的口吻，把安装步骤丢回给用户 —— 这正是
        # 用户抱怨「授权了还是不自由」的直接原因。
        ops_mode = (
            "标准模式（客户机内受限执行器，命令受 CPU/内存限制且可被终止）"
            if str(lease.get("mode")) == "standard"
            else "兼容模式（客户机内无沙箱，命令可能跑到超时才结束）"
        )
        ops_policy = str(lease.get("policy_note") or "")
        ops_approval = (
            "装包、写文件、重启服务等变更动作需要**两名用户分别批准**"
            if lease.get("double_approve")
            else "装包、写文件、重启服务等变更动作需要用户当场批准"
        )
        return i18n.pick(
            "\n\n## 虚拟机内运维（用户已授予「运维授权」）\n"
            f"目标：{lease.get('vm_name') or '未命名'}（VMID {lease.get('vmid')}，"
            f"节点 {lease.get('node')}，{kind}，{os_label}）\n"
            "可操作范围：整机（认证材料与不可回滚操作除外）\n"
            f"剩余时间约 {remaining} 分钟，剩余动作 {max(0, quota)} 次；{ops_approval}\n"
            f"执行环境：{ops_mode}" + (f"；{ops_policy}" if ops_policy else "") + "\n"
            "规则：\n"
            "- 用户已经明确授权你动手：需要装软件、改配置、查服务时，直接用 "
            "vm_run_command / vm_write_file / vm_restart_unit 执行，不要只把步骤丢回给用户。\n"
            "- 命令以 root 身份执行，不要加 sudo；变更类命令会先弹审批卡，等用户批准后再继续。\n"
            "- 保持服务可用：能用 reload 就不要 restart；一条命令只做一件事，便于定位失败。\n"
            "- 装完要验证：查版本、看服务状态、必要时看日志，再给结论。\n"
            "- 用户拒绝后不要原样重试，先说明为什么必须做、会影响什么。\n"
            "- 永远不会被执行（服务端硬拦截）：删根、重分区/格式化、关机或重启整机、"
            "修改 shadow / authorized_keys。这类事项请用户手动处理。\n"
            "- 结论里引用实际看到的输出片段。",
            "\n\n## In-guest operations (the user granted “ops” access)\n"
            f"Target: {lease.get('vm_name') or 'unnamed'} (VMID {lease.get('vmid')}, "
            f"node {lease.get('node')}, {kind}, {os_label})\n"
            "Scope: the whole guest (except credential material and irreversible actions)\n"
            f"About {remaining} min left, {max(0, quota)} actions left; "
            + (
                "installs, writes and restarts need two separate approvals\n"
                if lease.get("double_approve")
                else "installs, writes and restarts need approval\n"
            )
            + f"Runtime: {ops_mode}" + (f"; {ops_policy}" if ops_policy else "") + "\n"
            "Rules:\n"
            "- The user explicitly asked you to act: use vm_run_command / vm_write_file / "
            "vm_restart_unit to install packages, edit configs and check services. Do not "
            "hand the steps back to the user.\n"
            "- Commands run as root; do not prefix them with sudo. Mutating commands show an "
            "approval card first — wait for it.\n"
            "- Keep services available: prefer reload over restart, and do one thing per command.\n"
            "- Verify after installing (version, service state, logs when needed), then conclude.\n"
            "- If the user rejects an action, do not retry it verbatim; explain why it is needed.\n"
            "- Never allowed (blocked server-side): removing the root filesystem, repartitioning "
            "or formatting, rebooting/powering off the VM, editing shadow / authorized_keys. "
            "Ask the user to do those manually.\n"
            "- Cite the output snippets you actually saw.",
        )
    # 沙箱模式与策略提示也写进去：模型知道「写操作要两个人点头」就不会催用户点快了
    mode_note = (
        "标准模式（客户机内受限执行器，命令受 CPU/内存限制且可被终止）"
        if str(lease.get("mode")) == "standard"
        else "兼容模式（客户机内无沙箱，安全边界由服务端策略保证）"
    )
    policy_note = str(lease.get("policy_note") or "")
    approval_note = (
        "写文件与重启服务需要两名用户分别批准"
        if lease.get("double_approve")
        else "写文件与重启服务需要用户当场批准"
    )
    shell_hint = (
        "Windows 命令用 cmd 语法（ipconfig / tasklist / netstat / type / dir），"
        "路径写成 C:\\... 形式"
        if windows
        else "只读命令可以用管道（例如 ps aux | grep nginx）"
    )
    return i18n.pick(
        "\n\n## 虚拟机内排查（用户已授权）\n"
        f"目标：{lease.get('vm_name') or '未命名'}（VMID {lease.get('vmid')}，"
        f"节点 {lease.get('node')}，{kind}，{os_label}）\n"
        f"可读目录：{ro}\n可写目录：{rw}\n可重启服务：{units}\n"
        f"剩余时间约 {remaining} 分钟，剩余动作 {max(0, quota)} 次；{approval_note}\n"
        f"执行环境：{mode_note}" + (f"；{policy_note}" if policy_note else "") + "\n"
        "规则：\n"
        "- 先用 vm_collect_diagnostics 拿一份整体状态，再按需 vm_list_dir / vm_read_file，"
        "不要凭经验猜配置内容。\n"
        f"- {shell_hint}；写文件与重启服务会先给用户看 diff 并等待批准。\n"
        "- 用户拒绝后不要重复提交同样的内容，先解释为什么必须改。\n"
        "- 删除文件、重启整机、改网络/防火墙这类操作用不了：需要明确告诉用户手动处理。\n"
        "- 结论里要引用实际看到的证据（文件路径、命令、输出片段）。",
        "\n\n## In-guest investigation (granted by the user)\n"
        f"Target: {lease.get('vm_name') or 'unnamed'} (VMID {lease.get('vmid')}, "
        f"node {lease.get('node')}, {kind}, {os_label})\n"
        f"Readable: {ro}\nWritable: {rw}\nRestartable units: {units}\n"
        f"About {remaining} min left, {max(0, quota)} actions left; "
        + ("writes need two separate approvals\n" if lease.get("double_approve") else "writes need approval\n")
        + f"Runtime: {mode_note}" + (f"; {policy_note}" if policy_note else "") + "\n"
        "Rules:\n"
        "- Start with vm_collect_diagnostics for an overall picture, then narrow down with "
        "vm_list_dir / vm_read_file; do not guess configs.\n"
        f"- {shell_hint}; writes and restarts show a diff and wait for approval.\n"
        "- If the user rejects an action, do not resubmit it; explain why the change is needed.\n"
        "- Deleting files, rebooting the VM, or changing networking is not available: tell the user to do it manually.\n"
        "- Cite the evidence you actually saw (paths, commands, output snippets).",
    )


def _tool_description(item: Dict[str, Any], lease: Optional[Dict[str, Any]]) -> str:
    """按这张票的级别给出工具说明。

    同一个 ``vm_run_command``，只读票和运维票能做的事完全不同。说明写死成「只读」，
    模型就不会尝试装包 —— 用户授权了运维，AI 却仍然只会给建议，问题不在模型，
    在这段提示词。
    """
    name = str(item.get("name") or "")
    if not (lease and is_ops(lease)):
        return i18n.pick(*item["description"])
    if name == "vm_run_command":
        return i18n.pick(
            "在虚拟机内执行任意 shell 命令（以 root 运行）。可安装软件、改配置、"
            "启动/重启服务；会改变状态的命令会先弹审批卡等用户批准。"
            "不可回滚的操作（删根、重分区、整机重启、改认证材料）会被服务端直接拒绝",
            "Run any shell command inside the VM (as root). Use it to install packages, "
            "edit configs, start/restart services; mutating commands show an approval card "
            "first. Irreversible actions (removing the root filesystem, repartitioning, "
            "rebooting the VM, altering credentials) are blocked server-side",
        )
    if name == "vm_restart_unit":
        return i18n.pick(
            "重启虚拟机内的任意 systemd 服务（运维授权不限定服务清单）",
            "Restart any systemd unit inside the VM (ops grants are not limited to a list)",
        )
    if name == "vm_write_file":
        return i18n.pick(
            "覆盖写入虚拟机内任意路径的文件（认证材料与设备节点除外）。"
            "会先给用户看 diff 等待批准，并自动备份原文件以便回滚",
            "Overwrite any file inside the VM (credential material and device nodes "
            "excepted). Shows a diff for approval first and backs up the original "
            "for rollback",
        )
    if name == "vm_list_dir":
        return i18n.pick(
            "列出虚拟机内任意目录的内容（运维授权：整个文件系统，认证材料除外）",
            "List any directory inside the VM (ops grant: whole filesystem, credentials "
            "excepted)",
        )
    if name == "vm_read_file":
        return i18n.pick(
            "读取虚拟机内任意文件的开头若干行（运维授权：整个文件系统，认证材料除外）",
            "Read the first N lines of any file inside the VM (ops grant: whole filesystem, "
            "credentials excepted)",
        )
    return i18n.pick(*item["description"])


def tool_specs(lease: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """转成 OpenAI function calling 的 tools 格式。"""
    specs: List[Dict[str, Any]] = []
    for item in VM_TOOLS:
        props: Dict[str, Any] = {}
        required: List[str] = []
        for key, rule in item["params"].items():
            props[key] = {"type": "string", "description": rule.get("hint") or key}
            if rule.get("required"):
                required.append(key)
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": _tool_description(item, lease),
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    },
                },
            }
        )
    return specs


# ------------------------------------------------------------------- 目标清单

async def list_targets(limit: int = 200) -> List[Dict[str, Any]]:
    """可授权的虚拟机清单（当前连接，排除模板）。

    只列 **运行中** 的 qemu：
    * 容器执行要经过节点侧 ``pct``，写入更没有 Guest Agent 的文件接口可用
      （回滚也就无从谈起）；
    * 已停止的机器 Guest Agent 必然离线，授权出去也只能拿到「连不上」。

    列出来又用不了，比不列更糟 —— 用户会以为授权坏了。Guest Agent 是否应答
    则在选中机器时探测一次（见 ``probe_agent``），不在这里批量 ping：几十台
    机器逐台等超时，下拉框要转十几秒。
    """
    client = get_client()
    items: List[Dict[str, Any]] = []
    try:
        vms = await client.qemu_all() or []
    except Exception:  # noqa: BLE001
        vms = []
    for vm in vms:
        if int(vm.get("template") or 0) == 1:
            continue
        # 只列运行中的：停止的机器连 Guest Agent 都不会应答，授权等于发一张
        # 注定失败的票。状态可能在两次刷新之间变化，签发时还会再探一次。
        if str(vm.get("status") or "") != "running":
            continue
        items.append(
            {
                "node": str(vm.get("node") or ""),
                "vmid": int(vm.get("vmid") or 0),
                "name": str(vm.get("name") or ""),
                "guest_type": "qemu",
                "status": str(vm.get("status") or ""),
                # 带上连接 id：前端要用它把虚拟机与受管主机记录里的 conn_id 对上，
                # 从而在「只选一次虚拟机」时自动带出主机。
                "connection_id": target_connection_id(),
            }
        )
    items.sort(key=lambda item: (item["node"], item["vmid"]))
    return items[:limit]


# --------------------------------------------------------------------- 回收

async def cleanup_expired(limit: int = 10) -> Dict[str, Any]:
    """回收过期租约留下的快照与备份目录。

    不回收会怎样：每次首次写入都会打一个 ``pvecenter-ai-*`` 快照，几次之后存储
    就被这些「没人记得的快照」占满，而用户在界面上看不到它们从哪来。所以回收是
    授权机制的一部分，不是可选的收尾工作。
    """
    pol = await policy()
    keep = max(1, int(pol.get("keep_snapshot_hours") or 24))
    cutoff = int(time.time()) - keep * 3600
    async with database.connect() as db:
        cursor = await db.execute(
            "SELECT * FROM ai_vm_leases WHERE status IN ('expired','revoked')"
            " AND created < ? ORDER BY created ASC LIMIT ?",
            (cutoff, max(1, min(50, int(limit)))),
        )
        rows = await cursor.fetchall()

    removed = 0
    for row in rows:
        lease = _row_to_lease(row)
        windows = str(lease.get("os_type")) == "windows"
        try:
            if lease.get("snapshot") and not windows:
                client = await _client_for(lease)
                await client.qemu_snapshot_delete(
                    lease["node"], lease["vmid"], lease["snapshot"]
                )
        except Exception:  # noqa: BLE001 - 快照可能已被手工删掉，继续往下走
            logger.info("AI VM 清理快照失败 %s/%s", lease.get("vmid"), lease.get("snapshot"))

        # 备份目录名是租约 id（hex），拼进命令是安全的；容器/关机状态删不掉也无所谓，
        # 下次回收还会再试（只要 lease 还是 closed 之前的清理对象）。
        try:
            if windows:
                await _agent_exec(
                    lease,
                    ["cmd.exe", "/c", f'rmdir /s /q "{WINDOWS_BACKUP_DIR}\\{lease["id"]}"'],
                    timeout=10.0,
                )
            else:
                await _agent_exec(
                    lease,
                    ["sh", "-c", f"rm -rf {shlex.quote(BACKUP_DIR + '/' + str(lease['id']))}"],
                    timeout=10.0,
                )
        except Exception:  # noqa: BLE001 - 客户机可能已关机
            logger.debug("AI VM 清理备份目录失败 %s", lease.get("id"), exc_info=True)

        async with database.connect() as db:
            await db.execute(
                "UPDATE ai_vm_leases SET status='closed' WHERE id=?", (lease["id"],)
            )
            await db.commit()
        removed += 1

    return {"removed": removed}
