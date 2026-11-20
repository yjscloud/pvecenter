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
3. 本机信息：节点名 + 面板回连地址（可用 ``PVE_ONBOARD_HOST=`` 覆盖），顺手抓
   本机 pveproxy 正在用的证书（含链）—— 面板回连是 https，PVE 出厂自签，不把证书
   交给面板就必然 ``CERTIFICATE_VERIFY_FAILED``；同时先问面板「这个集群是不是已经
   接过了」，是就原地停下：同一集群共享账号与令牌，重复执行会轮换掉别人在用的那份；
4. API 令牌：**直接用 PVE 内置超级用户** ``root@pam``（``--privsep 0``）建令牌，
   已存在则轮换（旧 secret 不可恢复）。不建中间账号、不建自定义角色、不写 ACL：
   面板要的就是 root 的全权，PVE 升级新增的权限也自动覆盖，不会因为漏抄一项权限
   在某个操作上 403；
5. 控制台账号（**始终创建**）：先确保自定义角色 ``CenterConsole`` 存在
   （``VM.Audit,VM.Console``；名字不能以 ``PVE`` 开头，那是 PVE 的保留命名空间），
   再建 ``pvecenter-console@pve``，随机密码 —— PVE 的 VNC 要「账号 + 密码」换票据，
   API Token 换不了；密码只在注册请求里交给面板（落库加密），不打印、不进 shell
   历史；账号与面板账号分开，回收时互不影响；
7. 注册：把地址、令牌、本机证书（``ca_cert`` 钉扎）与控制台凭据 POST 给面板，
   按返回码区分「码失效 / 冲突 / 连不上」；
8. 结果确认：核对面板回报的集群与节点指标，打印接入凭据。

失败处理
--------
``ERR`` 陷阱 + 显式 ``die`` 都会走 :func:`_rollback`：只回滚**本次脚本创建的**
东西（新建的角色 / 新建或轮换过的令牌 / 本次建的控制台账号）。已经成功注册
（``DONE=1``）之后不再回滚 —— 那时删令牌只会把面板里刚建好的连接弄坏。

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
#  在 PVE 节点上执行（root）。脚本会：自检环境 -> 给 root@pam 建 API 令牌 ->
#  建控制台账号（VNC 用）-> 把本节点所在的集群注册到面板 -> 自检接入结果。
#
#  面板直接用 PVE 的 root 权限接入：令牌属于内置超级用户 root@pam，不建中间账号、
#  不建自定义角色、不写 ACL，PVE 上的每一处操作面板都能做。
#
#  同一个集群只需要在其中一台节点上执行；重复执行是安全的（会轮换令牌并同步
#  更新面板里的连接，且「什么都没改」的那次不消耗链接的可用次数）。任何一步失败
#  都会回滚本脚本创建的令牌 / 角色 / 控制台账号。
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

# 面板账号 = PVE 内置超级用户 root@pam。为什么不再建 pvecenter@pve + 自定义角色：
# 面板要管的是整台 PVE —— 创建 / 克隆 / 开关机 / 改配置 / 快照 / 备份恢复 / 迁移 /
# 建盘 / 上传 ISO / 防火墙 / 节点网络 / 任务中止 / 访问 shell……手写权限清单一定会
# 漏（漏一项就在那个操作上 403），而 root@pam 天然是 Administrator，PVE 升级新增
# 的权限也自动覆盖。代价是这份令牌等于 root 凭据，所以它只落在面板的加密存储里。
PVE_USER='root@pam'
PVE_TOKEN='pvecenter'
# 控制台账号独立于面板账号：登录取票据的账号一旦持有，就等于能操作客户机键盘，
# 与面板的 API 令牌分开放，出问题时也好单独回收
CONSOLE_USER='pvecenter-console@pve'
# 角色名不能以 PVE 开头（大小写不敏感）：那是 PVE 自己的保留命名空间，
# ``pveum role add`` 会直接 400「cannot use role ID starting with 'PVE'」。
# 所以叫 CenterConsole —— 别改回 PVECenterConsole，那名字在 PVE 里根本建不出来。
CONSOLE_ROLE='CenterConsole'
# privs 用**逗号**分隔：这是 PVE 里角色权限的规范写法（/etc/pve/user.cfg 里就是
# 逗号存着）。写成空格那版会被 pveum 判成「一个叫 VM.Audit VM.Console 的非法
# 权限」而失败 —— 早先这里连 stderr 都丢了，于是角色没建成，下一步 ACL 才报
# 「role does not exist」，错误点离真正的原因隔了一步。
CONSOLE_PRIVS='VM.Audit,VM.Console'

# 有终端才上色：写进日志文件时不带 ANSI 转义
if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
else
    C_OK=''; C_ERR=''; C_WARN=''; C_DIM=''; C_END=''
fi

STEP=0
STEP_TOTAL=7
step() { STEP=$((STEP + 1)); printf '\n%s[%d/%d]%s %s\n' "$C_DIM" "$STEP" "$STEP_TOTAL" "$C_END" "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_END" "$*"; }

# ---- 回滚登记 -------------------------------------------------------------
# 只登记「本脚本改了什么」，回滚时也只动这些，绝不碰面板已有的配置。
# 注意 root@pam 是 PVE 的内置用户，任何情况下都不删它，只删脚本建的令牌。
TOKEN_TOUCHED=0
CONSOLE_CREATED=0
ROLE_CREATED=0
DONE=0

_rollback() {
    if [ "$DONE" = 1 ]; then
        return 0
    fi
    if [ "$TOKEN_TOUCHED" = 0 ] \
        && [ "$CONSOLE_CREATED" = 0 ] && [ "$ROLE_CREATED" = 0 ]; then
        return 0
    fi
    printf '\n%s==> 回滚本机改动%s\n' "$C_DIM" "$C_END" >&2
    if [ "$CONSOLE_CREATED" = 1 ]; then
        if pveum user delete "$CONSOLE_USER" >/dev/null 2>&1; then
            printf '  已删除控制台账号 %s\n' "$CONSOLE_USER" >&2
        else
            printf '  控制台账号删除失败，请手动执行：pveum user delete %s\n' "$CONSOLE_USER" >&2
        fi
    fi
    if [ "$ROLE_CREATED" = 1 ]; then
        # 角色是本次建的才有得删；删角色会连带清掉引用它的 ACL，所以放在账号之后
        if pveum role delete "$CONSOLE_ROLE" >/dev/null 2>&1; then
            printf '  已删除控制台角色 %s\n' "$CONSOLE_ROLE" >&2
        else
            printf '  控制台角色删除失败，请手动执行：pveum role delete %s\n' "$CONSOLE_ROLE" >&2
        fi
    fi
    if [ "$TOKEN_TOUCHED" = 1 ]; then
        if pveum user token remove "$PVE_USER" "$PVE_TOKEN" >/dev/null 2>&1; then
            printf '  已删除 API 令牌 %s!%s\n' "$PVE_USER" "$PVE_TOKEN" >&2
        else
            printf '  令牌删除失败，请手动执行：pveum user token remove %s %s\n' "$PVE_USER" "$PVE_TOKEN" >&2
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

# 动手之前先问面板一句：这个集群（或这台单机）是不是已经接过了？
# 同一个 PVE 集群共用同一份账号与令牌（/etc/pve 是集群级配置），在第二个节点上再跑
# 一遍会把**共享的**令牌轮换掉，面板里那条连接当场失效 —— 而后面任何一步失败还会
# 回滚，等于白拆了正在用的凭据。所以一旦答案是「已接入」，就地停下，什么都不改。
CLUSTER_KEY="$CLUSTER_NAME"
if [ -z "$CLUSTER_KEY" ]; then
    CLUSTER_KEY="standalone:$NODE_NAME"
fi
PREFLIGHT=$(curl -sS $CURL_TLS -m 15 -G "$PANEL/api/connections/onboard/preflight" \
    --data-urlencode "code=$CODE" \
    --data-urlencode "cluster_key=$CLUSTER_KEY" \
    --data-urlencode "host=$HOST_IP" \
    --data-urlencode 'port=8006' 2>/dev/null || true)

_pf_get() {
    printf '%s' "$PREFLIGHT" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin) or {}
except Exception:
    data = {}
value = data.get(sys.argv[1]) if isinstance(data, dict) else None
print("" if value is None else value)
' "$1" 2>/dev/null || true
}

# 面板不可达 / 老版本面板没有这个接口时一律当作「没接过」，继续走正常流程
if [ "$(_pf_get registered)" = 'True' ] && [ "$FORCE" != '1' ]; then
    _pf_name=$(_pf_get name)
    _pf_host=$(_pf_get host)
    _pf_port=$(_pf_get port)
    ok "这个集群已经接在面板里了：连接「${_pf_name:-未命名}」（${_pf_host:-?}:${_pf_port:-8006}）"
    printf '\n%s================ 无需重复接入 ================%s\n' "$C_OK" "$C_END"
    printf '  同一个 PVE 集群只需要接入一次：其他节点会自动出现在面板里，本机不用再跑脚本。\n'
    printf '  本机没有做任何改动（账号、令牌、角色都没有动）。\n\n'
    printf '  确实要改用本机地址接入，或者面板现在读不到该集群、需要轮换令牌：\n'
    printf '    在上面那条链接末尾加上 &force=1 再执行一次。\n'
    printf '  （面板报权限不足时也这样重跑：会换成 root 令牌，权限一次到位）\n\n'
    exit 0
fi

# 面板回连本节点走 https://<IP>:8006，而 pveproxy 出厂用的是 PVE 自签的证书（CN 是
# 节点名、没有 IP SAN）。不把证书交上去，注册那步必然 CERTIFICATE_VERIFY_FAILED。
# 这里抓本机 8006 上**真正在用**的证书链，随注册请求一起交给面板做钉扎：面板按这段
# PEM 校验、不比对主机名，效果等同 SSH 的 known_hosts —— 既不是关掉校验，也不用运维
# 去面板表单里手工粘贴 PEM。
NODE_CA_PEM=''
# 拼接每一段时都必须自己补换行：命令替换会把结尾那个换行吃掉，写成
# "${A}$(cat f)" 就会把上一段的 -----END CERTIFICATE----- 和下一段的
# -----BEGIN CERTIFICATE----- 粘在同一行，OpenSSL 于是一个证书都读不出来
# （表现为面板侧 500：no start line / cadata does not contain a certificate）。
_pem_add() {
    if [ -n "$1" ]; then
        NODE_CA_PEM="${NODE_CA_PEM}${1}"$'\n'
    fi
}
if command -v openssl >/dev/null 2>&1; then
    # -showcerts 必须带：s_client 默认只打印**叶子**，而叶子是被 PVE 的根 CA 签的
    # （不是自签）。只钉叶子，面板侧建不出链，报的正是 unable to get local issuer
    # certificate —— 缺的就是签发它的那张。
    # timeout 兜底：pveproxy 不响应时 s_client 会一直等，不能把整个接入卡死。
    _pem_add "$(timeout 10 openssl s_client -connect '127.0.0.1:8006' -showcerts \
        -servername "$NODE_NAME" </dev/null 2>/dev/null \
        | sed -n '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/p' || true)"
fi
# 再补上本机文件里的根 CA 与节点证书：s_client 不一定吐得出根（有的版本只给到中间
# 证书），而 pve-root-ca.pem 就是签发节点证书的那张根。重复的证书对信任锚没影响。
for _pem in /etc/pve/pve-root-ca.pem /etc/pve/local/pve-ssl.pem; do
    if [ -r "$_pem" ]; then
        _pem_add "$(cat "$_pem" 2>/dev/null || true)"
    fi
done
if [ -n "$NODE_CA_PEM" ]; then
    _ca_blocks=$(printf '%s' "$NODE_CA_PEM" | grep -c 'BEGIN CERTIFICATE' || true)
    ok "已取到本机证书 ${_ca_blocks:-0} 张，注册时交给面板钉扎（自签也连得上，且不关校验）"
else
    warn '没取到本机证书（openssl 与 /etc/pve 都读不到）：面板若开了 PVE_VERIFY_SSL，回连会因自签证书失败'
fi
if [ "${PVE_ONBOARD_INSECURE:-0}" = '1' ]; then
    warn 'PVE_ONBOARD_INSECURE=1：注册时不再带证书，面板回连将不校验证书（仅建议用于临时排查）'
fi

# ---- 角色工具 ---------------------------------------------------------------
# 角色是否已在册：``pveum role add`` 遇到同名角色会直接报错退出，重复执行前必须先查
_role_exists() {
    pveum role list --output-format json 2>/dev/null | grep -q "\"$1\""
}

# ============================================================================
step "准备 root 令牌 $PVE_USER!$PVE_TOKEN"

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
ok '令牌就绪（--privsep 0：令牌继承 root 的全部权限，面板不再受最小权限限制）'

# ============================================================================
step "准备控制台账号 $CONSOLE_USER"

# 控制台账号始终创建：PVE 的 VNC 要先「账号 + 密码」换票据再调 vncproxy，API Token
# 换不了票据 —— 面板手里握着 root 令牌也一样，没有密码账号就点不开控制台。
# 密码随机生成，只在注册请求里交给面板（落库加密），不打印在屏幕上，也不留在
# shell 历史里。
CONSOLE_PW=$(python3 -c '
import secrets, string
alphabet = string.ascii_letters + string.digits
print("".join(secrets.choice(alphabet) for _ in range(24)))
')
if [ -z "$CONSOLE_PW" ]; then
    die '生成控制台密码失败'
fi

if pveum user list --output-format json 2>/dev/null | grep -q "\"$CONSOLE_USER\""; then
    # 已存在：轮换密码。改密码不能走 `pveum user modify` —— 它没有 --password
    # 选项（PVE 9 上直接报 Unknown option: password），`pveum passwd` 又是交互式
    # 的（问两遍，喂不进管道）。可脚本化的是 API：PUT /access/password。
    # 轮换失败就**不带**密码上去，让面板继续用已存的那份，而不是把旧密码写死。
    if pvesh set /access/password --userid "$CONSOLE_USER" --password "$CONSOLE_PW" >/dev/null 2>&1 \
        || pvesh set /access/password --userid "$CONSOLE_USER" --password "$CONSOLE_PW" \
            --confirmation-password "$CONSOLE_PW" >/dev/null 2>&1; then
        warn '控制台账号已存在，密码已轮换（面板会同步更新）'
    else
        CONSOLE_PW=''
        warn '控制台账号已存在，但轮换密码失败：本次沿用面板里已存的密码（若提示凭据失效，用 &force=1 重跑）'
    fi
else
    pveum user add "$CONSOLE_USER" --password "$CONSOLE_PW" \
        --comment 'PVE Center 控制台账号（仅供面板打开 VNC）' >/dev/null
    CONSOLE_CREATED=1
    ok '已创建控制台账号（密码随机，不需要记）'
fi

# 控制台角色只给这两个权限：够开控制台，仍然不能改配置、不能开关机 —— 万一控制台
# 密码泄漏，损失面也只到「能看、能敲键盘」。建角色这一步以前是 ``|| true`` + 丢弃
# stderr：角色建失败也照样往下走，然后 ACL 才报「role does not exist」，把真正的
# 原因藏起来了。现在把 pveum 的原话一起打出来，失败就当场回滚。
if _role_exists "$CONSOLE_ROLE"; then
    ok "角色已存在，跳过创建：$CONSOLE_ROLE"
else
    if ! _role_err=$(pveum role add "$CONSOLE_ROLE" --privs "$CONSOLE_PRIVS" 2>&1 >/dev/null); then
        die "创建角色 $CONSOLE_ROLE 失败（pveum role add）：${_role_err:-pveum 没有输出}"
    fi
    ROLE_CREATED=1
    ok "已创建角色 $CONSOLE_ROLE（$CONSOLE_PRIVS）"
fi

pveum acl modify / --users "$CONSOLE_USER" --roles "$CONSOLE_ROLE" >/dev/null
ok "已在 / 上授予 $CONSOLE_ROLE（VM.Audit + VM.Console）"

# ============================================================================
step '注册到面板'

# 证书走环境变量而不是 argv：几 KB 的 PEM 塞进命令行会出现在 ps 里，也顶长度上限
PAYLOAD=$(NODE_CA_PEM="$NODE_CA_PEM" ONBOARD_INSECURE="${PVE_ONBOARD_INSECURE:-0}" \
    python3 - "$CODE" "$HOST_IP" "$NODE_NAME" "$PVE_USER" "$PVE_TOKEN" "$TOKEN_SECRET" "$FORCE" "$CONSOLE_USER" "${CONSOLE_PW:-}" <<'PY'
import json, os, re, ssl, sys

_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S
)


def normalize_pem(raw):
    # 把抓到的证书规整成一段能被 ssl 读进去的 PEM，并当场自证可用。
    #
    # 抓证是两次文本拼接（s_client 输出 + /etc/pve 下的 pem 文件），漏一个换行、或者
    # 文件里混了别的东西，PEM 就会失效；而失效的 PEM 发到面板只会变成一句 500
    # （no start line: cadata does not contain a certificate），运维完全看不懂。
    # 所以在节点上就地抽块、去重、重排换行，再用 ssl 自己验一遍：验不过就不发，
    # 宁可按面板策略校验，也不发一段坏数据过去。
    seen, blocks = set(), []
    for block in _CERT_RE.findall(raw or ""):
        body = "\n".join(line.strip() for line in block.strip().splitlines())
        if body in seen:
            continue
        seen.add(body)
        blocks.append(body + "\n")
    pem = "".join(blocks)
    if not pem:
        return ""
    try:
        ssl.create_default_context(cadata=pem)
    except Exception:
        return ""
    return pem


code, host, node, user, token, secret, force, console_user, console_pw = sys.argv[1:10]
data = {
    "code": code,
    "host": host,
    "port": 8006,
    "token_id": "%s!%s" % (user, token),
    "token_secret": secret,
    "node_name": node,
    "force_update": force == "1",
}
# 面板侧的 ca_cert 语义是「拿这段 PEM 当信任锚，且不比对主机名」（见 pve.build_ssl_context）：
# 自签证书因此能过，而校验本身没关。取不到证书时不要自作主张关校验 —— 除非运维显式
# 要求（PVE_ONBOARD_INSECURE=1），否则就交给面板按它自己的 PVE_VERIFY_SSL 策略决定。
raw_pem = os.environ.get("NODE_CA_PEM") or ""
ca_pem = normalize_pem(raw_pem)
if os.environ.get("ONBOARD_INSECURE") == "1":
    data["verify_ssl"] = False
elif ca_pem:
    data["ca_cert"] = ca_pem
    data["verify_ssl"] = True
elif raw_pem.strip():
    # 抓到了东西但解析不出来：明说，别让面板那边报一句看不懂的 500
    print(
        "  ! 抓到的证书无法解析，已放弃钉扎（本次按面板的 PVE_VERIFY_SSL 策略校验）",
        file=sys.stderr,
    )

# 控制台凭据始终带上：VNC 只能靠账号密码换票据，令牌在这里顶不上
if console_pw:
    data["console_user"] = console_user
    data["console_password"] = console_pw
print(json.dumps(data))
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

# 旧版脚本建过 pvecenter@pve（最小权限账号 + CenterPanel 角色）。本版改用 root@pam
# 令牌，那个账号不再需要；不自动删（可能还有别的系统在用），只提示一句。
if pveum user list --output-format json 2>/dev/null | grep -q '"pvecenter@pve"'; then
    warn '本机还留着旧版的 pvecenter@pve 账号；确认没有别处使用后可清理：pveum user delete pvecenter@pve'
fi

if [ "$NODE_METRICS" = 'True' ]; then
    ok '面板已读到节点指标（CPU / 内存 / 磁盘），权限正常'
else
    warn '面板读不到节点指标：root 令牌不该缺权限，先查面板到本机 8006 的连通性与证书'
fi

printf '\n%s================ 接入完成 ================%s\n' "$C_OK" "$C_END"
printf '  面板      %s\n' "$PANEL"
printf '  连接      %s\n' "${CONN_NAME:-未命名}"
printf '  集群      %s（节点数 %s）\n' "${CLUSTER_KEY:-未知}" "${NODE_COUNT:-?}"
printf '  PVE       %s\n' "${PVE_VER:-$PVE_VERSION}"
printf '  本节点    %s @ %s:8006\n' "$NODE_NAME" "$HOST_IP"
if [ "${PVE_ONBOARD_INSECURE:-0}" = '1' ]; then
    printf '  证书      未校验（PVE_ONBOARD_INSECURE=1）\n'
elif [ -n "$NODE_CA_PEM" ]; then
    printf '  证书      已钉扎本机证书（按证书校验，不比对主机名）\n'
else
    printf '  证书      按面板的 PVE_VERIFY_SSL 策略\n'
fi
printf '  权限      创建 / 克隆 / 开关机 / 快照 / 备份 / 防火墙 / 节点网络\n'
printf '  控制台    %s（随机密码已写入面板，可直接开 VNC）\n' "$CONSOLE_USER"
printf '  提醒      这份令牌等同 PVE 最高权限 —— 登录面板的账户请务必使用强密码；面板若对公网开放，请务必为账户开启二次验证（TOTP）\n'
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


def render(
    *,
    panel: str,
    code: str,
    self_url: str,
    force: bool = False,
) -> str:
    """渲染安装脚本。

    ``self_url`` 用于在「非 root」时提示用户可以``sudo bash -c "$(curl ...)"``
    重跑 —— 脚本得知道自己的地址，否则只能让用户回面板再复制一次。

    权限与控制台都不再有开关：接入一律用 ``root@pam`` 令牌（全权），控制台账号
    一律创建（VNC 要账号密码换票据）。注册码里那两个字段已经删除，持码者也无法
    通过查询串改变这一点。
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
