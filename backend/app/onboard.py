"""把「一键接入」渲染成一段可直接粘贴执行的安装脚本。

为什么脚本由面板生成、而不是写进前端
------------------------------------
面板域名、注册码、是否强制更新，这三样每次都不一样；把脚本放在后端用模板渲染，
前端只需要展示一条命令，运维复制粘贴即可 —— 不需要先下载文件、也不需要手工改
``PANEL=`` 与 ``CODE=`` 两行。前端那份内联脚本（v0.5.9 的做法）已删除：两处
维护同一段 bash，迟早会漂移成「网页上的一套能跑、链接下载的那套不能跑」。

脚本自己要完成的事（按执行顺序）
--------------------------------
1. 环境自检：root、``pveum`` / ``pvesh``、``/etc/pve``、``curl``、``python3``；
2. 面板连通性：先打 ``/api/health``，连不上面板就**什么都别改**；
3. 本机信息：节点名 + 面板回连地址（可用 ``PVE_ONBOARD_HOST=`` 覆盖）；
4. 只读账号：建 ``pvecenter@pve`` 并授予 ``PVEAuditor``（幂等）；
5. API 令牌：``--privsep 0`` 建令牌；已存在则轮换（旧 secret 不可恢复）；
6. 注册：把地址与令牌 POST 给面板，按返回码区分「码失效 / 冲突 / 连不上」；
7. 结果确认：核对面板回报的集群与节点指标，打印接入凭据。

失败处理
--------
``ERR`` 陷阱 + 显式 ``die`` 都会走 :func:`_rollback`：只回滚**本次脚本创建的**
东西（新建的账号 / 新建或轮换过的令牌）。已经成功注册（``DONE=1``）之后不再
回滚 —— 那时删令牌只会把面板里刚建好的连接弄坏。

安全提醒
--------
注册码写在 URL 查询串里，反向代理的访问日志可能留下它。因此它必须只是**一次性、
短时效**的凭据：消费即失效，最长 24 小时。这里不做额外隐藏 —— 明文脚本本身就
要带着它，藏起来只是自欺。
"""
from __future__ import annotations

#: 渲染占位符。用 ``@@NAME@@`` 而不是 ``$NAME``：bash 里 ``$`` 满地都是，
#: 用 ``str.format`` 或 ``Template`` 迟早会替错一个变量。
PANEL_TOKEN = "@@PANEL@@"
CODE_TOKEN = "@@CODE@@"
SELF_TOKEN = "@@SELF@@"
FORCE_TOKEN = "@@FORCE@@"

#: 脚本本体。``r"""`` 是必须的：里面全是 ``\033`` / ``\n`` 这类要被 bash
#: 原样收到的转义，普通字符串会先吃掉一层。
_SCRIPT = r"""#!/usr/bin/env bash
# ============================================================================
#  PVE Center · 一键接入
#
#  在 PVE 节点上执行（root）。脚本会：自检环境 -> 建只读账号与令牌 ->
#  把本节点所在的集群注册到面板 -> 自检接入结果。
#
#  同一个集群只需要在其中一台节点上执行；重复执行是安全的（会轮换令牌并同步
#  更新面板里的连接，且「什么都没改」的那次不消耗链接的可用次数）。任何一步失败
#  都会回滚本脚本创建的账号 / 令牌。
# ============================================================================
set -Eeuo pipefail

# pveum / pvesh 在 /usr/sbin，进不了某些受限 PATH（cron、非登录 shell、sudo 的
# secure_path 被裁剪过的机器）；补上，找不到时再报错。
export PATH="$PATH:/usr/sbin:/sbin"

PANEL='@@PANEL@@'
CODE='@@CODE@@'
SELF_URL='@@SELF@@'
FORCE=@@FORCE@@                 # 1 = 该集群已接入时改用本节点地址
HOST_OVERRIDE="${PVE_ONBOARD_HOST:-}"

PVE_USER='pvecenter@pve'
PVE_TOKEN='pvecenter'

# 有终端才上色：写进日志文件时不带 ANSI 转义
if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
    C_OK=''; C_ERR=''; C_WARN=''; C_DIM=''; C_END=''
fi

STEP=0
step() { STEP=$((STEP + 1)); printf '\n%s[%d/7]%s %s\n' "$C_DIM" "$STEP" "$C_END" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_END" "$*"; }

# ---- 回滚登记 -------------------------------------------------------------
# 只登记「本脚本改了什么」，回滚时也只动这些，绝不碰面板已有的配置。
CREATED_USER=0
TOKEN_TOUCHED=0
DONE=0

_rollback() {
    if [ "$DONE" = 1 ]; then
        return 0
    fi
    if [ "$CREATED_USER" = 0 ] && [ "$TOKEN_TOUCHED" = 0 ]; then
        return 0
    fi
    printf '\n%s==> 回滚本机改动%s\n' "$C_DIM" "$C_END" >&2
    if [ "$TOKEN_TOUCHED" = 1 ]; then
        if pveum user token remove "$PVE_USER" "$PVE_TOKEN" >/dev/null 2>&1; then
            printf '  已删除 API 令牌 %s!%s\n' "$PVE_USER" "$PVE_TOKEN" >&2
        else
            printf '  令牌删除失败，请手动执行：pveum user token remove %s %s\n' "$PVE_USER" "$PVE_TOKEN" >&2
        fi
    fi
    if [ "$CREATED_USER" = 1 ]; then
        if pveum user delete "$PVE_USER" >/dev/null 2>&1; then
            printf '  已删除只读账号 %s\n' "$PVE_USER" >&2
        else
            printf '  账号删除失败，请手动执行：pveum user delete %s\n' "$PVE_USER" >&2
        fi
    fi
    printf '  本机已回到接入前的状态；排除故障后重新执行同一条命令即可。\n' >&2
}

die() {
    printf '\n%s✗ %s%s\n' "$C_ERR" "$*" "$C_END" >&2
    _rollback
    exit 1
}

_on_error() {
    local code=$? line=$1
    trap - ERR
    printf '\n%s✗ 第 %s 行执行失败（退出码 %s）%s\n' "$C_ERR" "$line" "$code" "$C_END" >&2
    _rollback
    exit "$code"
}
trap '_on_error $LINENO' ERR

# ============================================================================
step '检查运行环境'

if [ "$(id -u)" != '0' ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
        printf '  当前不是 root，改用 sudo 继续\n'
        # 重新取一次脚本文本再交给 sudo。不写成 ``exec sudo bash -c "$(curl ...)"``：
        # 命令替换里失败时，错误码会随着空字符串一起被丢掉（表现是「报了一行错，
        # 但退出码 0」），那种半成功比直接失败更难查。
        _body=$(curl -fsSL "$SELF_URL") || die "重新下载脚本失败：$SELF_URL"
        exec sudo -E bash -c "$_body"
    fi
    die "需要 root 权限。请改用：sudo bash -c \"\$(curl -fsSL '$SELF_URL')\""
fi

for _cmd in pveum pvesh curl python3; do
    if ! command -v "$_cmd" >/dev/null 2>&1; then
        die "找不到 $_cmd：这台机器不是 Proxmox VE（或环境不完整）。本脚本只能跑在 PVE 节点上"
    fi
done

if [ ! -d /etc/pve ]; then
    die '/etc/pve 不存在或未挂载：Proxmox 的集群文件系统异常，先修复 PVE 再接入'
fi

PVE_VERSION=$(pveversion 2>/dev/null | head -n1 || true)
if [ -z "$PVE_VERSION" ]; then
    die 'pveversion 没有输出：Proxmox 环境异常，先确认 pve-manager 正常'
fi
ok "Proxmox：$PVE_VERSION"

# ============================================================================
step '检查面板连通性'

CURL_TLS=''
if [ "${PANEL_INSECURE:-0}" = '1' ]; then
    CURL_TLS='-k'
    warn 'PANEL_INSECURE=1：已跳过面板证书校验（仅用于自签证书的临时排查）'
fi

HEALTH=$(curl -fsS $CURL_TLS -m 10 "$PANEL/api/health" 2>/dev/null || true)
case "$HEALTH" in
    *ok*)
        ok "面板可达：$PANEL"
        ;;
    *)
        die "连不上面板 $PANEL 。请依次确认：本节点能访问该地址（防火墙 / DNS / VPN）、面板证书受信任（自签证书可加 PANEL_INSECURE=1 重试）、地址没有写错"
        ;;
esac

# ============================================================================
step '读取本机信息'

NODE_NAME=$(hostname -s)
HOST_IP="$HOST_OVERRIDE"
if [ -z "$HOST_IP" ]; then
    HOST_IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)
fi
if [ -z "$HOST_IP" ]; then
    HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
fi
if [ -z "$HOST_IP" ]; then
    die "识别不出本机 IP。请手动指定后再执行：PVE_ONBOARD_HOST=10.0.0.5 bash -c \"\$(curl -fsSL '$SELF_URL')\""
fi

CLUSTER_NAME=''
CLUSTER_NAME=$(pvesh get /cluster/status --output-format json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin) or []
except Exception:
    data = []
for item in data:
    if isinstance(item, dict) and item.get("type") == "cluster":
        print(item.get("name") or "")
        break
' 2>/dev/null || true)

ok "节点名：$NODE_NAME"
ok "接入地址：$HOST_IP:8006（面板从这里连回来；不对就加 PVE_ONBOARD_HOST= 指定）"
if [ -n "$CLUSTER_NAME" ]; then
    ok "集群名：$CLUSTER_NAME"
else
    warn '读不到集群名，按单机接入（面板侧会自行识别集群指纹）'
fi

# ============================================================================
step "准备只读账号 $PVE_USER"

if pveum user list --output-format json 2>/dev/null | grep -q "\"$PVE_USER\""; then
    ok '账号已存在，跳过创建'
else
    pveum user add "$PVE_USER" --comment 'PVE Center 面板只读账号' >/dev/null
    CREATED_USER=1
    ok '已创建账号（仅用于面板读取）'
fi

# 幂等：重复授予同一角色不会报错，也顺便修好被手工改坏的权限
pveum acl modify / --users "$PVE_USER" --roles PVEAuditor >/dev/null
ok '已在 / 上授予 PVEAuditor（只读）'

# ============================================================================
step "准备 API 令牌 $PVE_USER!$PVE_TOKEN"

TOKEN_JSON=$(pveum user token add "$PVE_USER" "$PVE_TOKEN" --privsep 0 --output-format json 2>/dev/null || true)
if [ -z "$TOKEN_JSON" ]; then
    warn '令牌已存在：PVE 不会再给出明文，只能轮换（旧的 secret 无法找回）'
    pveum user token remove "$PVE_USER" "$PVE_TOKEN" >/dev/null 2>&1 || true
    TOKEN_JSON=$(pveum user token add "$PVE_USER" "$PVE_TOKEN" --privsep 0 --output-format json 2>/dev/null || true)
    # 凭据变了，必须让面板同步覆盖，否则它会拿着旧 secret 一直 401
    FORCE=1
fi
if [ -n "$TOKEN_JSON" ]; then
    TOKEN_TOUCHED=1
else
    die '创建令牌失败：pveum user token add 没有返回结果'
fi

TOKEN_SECRET=$(printf '%s' "$TOKEN_JSON" | tr -d ' \n' | grep -o '"value":"[^"]*"' | cut -d'"' -f4)
if [ -z "$TOKEN_SECRET" ]; then
    die '拿不到令牌明文，无法写入面板（pveum 的输出格式可能已变）'
fi
ok '令牌就绪（--privsep 0：权限随账号，避免「特权分离」导致读不到指标）'

# ============================================================================
step '注册到面板'

PAYLOAD=$(python3 - "$CODE" "$HOST_IP" "$NODE_NAME" "$PVE_USER" "$PVE_TOKEN" "$TOKEN_SECRET" "$FORCE" <<'PY'
import json, sys

code, host, node, user, token, secret, force = sys.argv[1:8]
print(json.dumps({
    "code": code,
    "host": host,
    "port": 8006,
    "token_id": "%s!%s" % (user, token),
    "token_secret": secret,
    "node_name": node,
    "force_update": force == "1",
}))
PY
) || die '组装注册请求失败'

RESP=$(curl -sS $CURL_TLS -m 60 -X POST "$PANEL/api/connections/register" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" -w '\n%{http_code}' 2>/dev/null || true)
if [ -z "$RESP" ]; then
    die "注册请求没有响应：面板 $PANEL 不可达或被中断（本机改动已回滚）"
fi

HTTP_CODE=$(printf '%s' "$RESP" | tail -n1)
BODY=$(printf '%s' "$RESP" | sed '$d')

jget() {
    printf '%s' "$BODY" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
value = data
for part in sys.argv[1].split("."):
    value = value.get(part) if isinstance(value, dict) else None
print("" if value is None else value)
' "$1" 2>/dev/null || true
}

if [ "$HTTP_CODE" != '200' ]; then
    DETAIL=$(jget detail.message)
    if [ -z "$DETAIL" ]; then
        DETAIL=$(jget detail)
    fi
    if [ -z "$DETAIL" ]; then
        DETAIL="$BODY"
    fi
    case "$HTTP_CODE" in
        403) die "注册码不可用（已使用 / 已过期 / 已撤销）：$DETAIL" ;;
        409)
            _taken_name=$(jget detail.name)
            _taken_id=$(jget detail.connection_id)
            die "这个地址已被连接「${_taken_name:-未命名}」（${_taken_id:-?}）占用，且集群指纹不同。请确认是不是接错了机器；确定要替换那条连接就在面板里先删掉它，再重跑本命令"
            ;;
        400) die "面板连不上本节点：$DETAIL" ;;
        *)   die "注册失败（HTTP $HTTP_CODE）：$DETAIL" ;;
    esac
fi

DONE=1   # 面板已建好连接：此后不再回滚，否则会把刚接好的连接弄坏

STATUS=$(jget status)
ok "面板已受理（$STATUS）"

# ============================================================================
step '确认接入结果'

CONN_NAME=$(jget name)
CLUSTER_KEY=$(jget cluster_key)
NODE_COUNT=$(jget node_count)
NODE_METRICS=$(jget node_metrics)
PVE_VER=$(jget pve_version)
IS_ACTIVE=$(jget active)
MESSAGE=$(jget message)
MAX_USES=$(jget max_uses)
USED_COUNT=$(jget used_count)
REMAINING=$(jget remaining)

case "$STATUS" in
    created)   ok '已新建连接' ;;
    updated)   ok '已更新已有连接（地址 / 凭据已同步）' ;;
    unchanged) ok '该集群之前已接入，本次未做改动' ;;
    *)         ok "面板返回状态：$STATUS" ;;
esac
if [ -n "$MESSAGE" ]; then
    printf '  %s\n' "$MESSAGE"
fi

# 注意 ``user token list`` 里的 tokenid **只有令牌名**，不带 ``user!`` 前缀
if pveum user token list "$PVE_USER" --output-format json 2>/dev/null | grep -q "\"$PVE_TOKEN\""; then
    ok "本机令牌在册：$PVE_USER!$PVE_TOKEN"
else
    warn '本机没读到刚创建的令牌（面板侧已注册，可稍后在面板核对）'
fi

if [ "$NODE_METRICS" = 'True' ]; then
    ok '面板已读到节点指标（CPU / 内存 / 磁盘），权限正常'
else
    warn '面板读不到节点指标：请给该账号授予 PVEAuditor 及以上只读角色后重试'
fi

printf '\n%s================ 接入完成 ================%s\n' "$C_OK" "$C_END"
printf '  面板      %s\n' "$PANEL"
printf '  连接      %s\n' "${CONN_NAME:-未命名}"
printf '  集群      %s（节点数 %s）\n' "${CLUSTER_KEY:-未知}" "${NODE_COUNT:-?}"
printf '  PVE       %s\n' "${PVE_VER:-$PVE_VERSION}"
printf '  本节点    %s @ %s:8006\n' "$NODE_NAME" "$HOST_IP"
if [ "$MAX_USES" = '0' ]; then
    printf '  次数      不限次数（已用 %s 次）\n' "${USED_COUNT:-0}"
elif [ -n "$MAX_USES" ]; then
    printf '  次数      %s / %s（剩余 %s 次）\n' "${USED_COUNT:-0}" "$MAX_USES" "${REMAINING:-0}"
fi
if [ "$IS_ACTIVE" = 'True' ]; then
    printf '  状态      该连接是面板当前的活动连接\n'
else
    printf '  状态      已接入（面板的活动连接是另一条，可在面板切换）\n'
fi
printf '\n  回到面板「节点」页即可看到这台主机。\n'
"""

#: 注册码不可用时的「脚本」。仍然回 200 + 脚本体：``curl ... | bash`` 只看
#: 退出码与屏幕输出，回一段 JSON 只会让 bash 报一堆语法错误，而真正的原因
#: （码过期了）反而看不见。
_ERROR_SCRIPT = r"""#!/usr/bin/env bash
# PVE Center 一键接入 —— 这条链接已失效
if [ -t 1 ]; then
    C_ERR=$'\033[31m'; C_END=$'\033[0m'
else
    C_ERR=''; C_END=''
fi
printf '\n%s✗ 注册码不可用%s：%s\n\n' "$C_ERR" "$C_END" "@@REASON@@" >&2
printf '  请回到面板「节点 · 连接配置 → 一键接入」重新生成一条链接。\n' >&2
printf '  注册码有可用次数与有效期：次数用尽或过了有效期就会失效。\n\n' >&2
exit 1
"""


def render(*, panel: str, code: str, self_url: str, force: bool = False) -> str:
    """渲染安装脚本。

    ``self_url`` 用于在「非 root」时提示用户可以``sudo bash -c "$(curl ...)"``
    重跑 —— 脚本得知道自己的地址，否则只能让用户回面板再复制一次。
    """
    return (
        _SCRIPT.replace(PANEL_TOKEN, str(panel or "").rstrip("/"))
        .replace(CODE_TOKEN, str(code or "").strip())
        .replace(SELF_TOKEN, str(self_url or ""))
        .replace(FORCE_TOKEN, "1" if force else "0")
    )


def error_script(reason: str) -> str:
    """把失败原因包成一段脚本：由节点上的 bash 打印，而不是面板回 JSON。"""
    return _ERROR_SCRIPT.replace("@@REASON@@", str(reason or "注册码不可用"))
