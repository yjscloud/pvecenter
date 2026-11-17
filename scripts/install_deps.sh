#!/usr/bin/env bash
# ============================================================================
#  把 backend/requirements.txt 装进指定的虚拟环境。
#
#  为什么单独一个脚本：deploy.sh / start.sh / start-prod.sh 三条入口以前各写一行
#  `pip install -q -r requirements.txt`，于是三个问题一起出现：
#
#    1. **每次都碰一下 PyPI**。已经装齐的机器上重跑部署，仍然要过一次网络；
#       内网 / SSL 被中间设备打断的机器就会打印一屏
#       `WARNING: Retrying ... SSLError(SSL: UNEXPECTED_EOF_WHILE_READING) /simple/pip/`，
#       看起来像部署失败，实际上什么都没坏。
#    2. **那屏告警来自 `--upgrade pip`**。升级 pip 是可选动作，却排在装依赖之前，
#       于是最容易被拦住的第一步成了整个部署的第一印象。
#    3. 失败时只留一句「检查网络」，看不出试过哪些源、也没留日志。
#
#  现在的顺序是：**先离线判一次「已经装齐了没」**（pip --no-index 只比对本地
#  已安装版本，全程零网络），齐了就直接返回；不齐才联网，联网按「显式指定 →
#  默认源 → 国内镜像」依次回退，pip 的输出收进日志，只在真的失败时给最后一段。
#
#  用法：
#      scripts/install_deps.sh .venv/bin/python backend/requirements.txt
#      scripts/install_deps.sh .venv/bin/python backend/requirements.txt --verbose
#      scripts/install_deps.sh .venv/bin/python backend/requirements.txt --lang en
#
#  可选环境变量：
#      PC_PIP_INDEX=https://xxx/simple   指定源，只试它（等价于 pip -i）
#      PIP_INDEX_URL=...                 pip 自己的变量，同样被认
#      PC_PIP_OFFLINE=1                  禁止联网：依赖不齐就直接失败并说明原因
#      PC_UPGRADE_PIP=1                  先升级 pip（默认不升，避免多一次网络访问）
#
#  退出码：0 = 依赖已可用；1 = 装不上（原因与下一步见 stderr）
# ============================================================================
set -uo pipefail

VPY="${1:-}"
REQ="${2:-}"
shift 2 2>/dev/null || true

MODE=""
LANG_UI="zh"
for arg in "$@"; do
  case "$arg" in
    --verbose) MODE="--verbose" ;;
    --lang) : ;;
    zh|en) LANG_UI="$arg" ;;
    *)
      case "$arg" in
        --lang=*) LANG_UI="${arg#--lang=}" ;;
      esac
      ;;
  esac
done
[ "$LANG_UI" = "en" ] || LANG_UI="zh"

# say <中文> <English>
say() {
  if [ "$LANG_UI" = "en" ]; then
    printf '[deps] %s\n' "$2"
  else
    printf '[deps] %s\n' "$1"
  fi
}
say_err() {
  if [ "$LANG_UI" = "en" ]; then
    printf '[deps] [x] %s\n' "$2" >&2
  else
    printf '[deps] [x] %s\n' "$1" >&2
  fi
}
fail() { say_err "$1" "$2"; exit 1; }

if [ -z "$VPY" ] || [ -z "$REQ" ]; then
  fail "用法：install_deps.sh <venv python> <requirements.txt> [--verbose] [--lang zh|en]" \
       "Usage: install_deps.sh <venv python> <requirements.txt> [--verbose] [--lang zh|en]"
fi
[ -x "$VPY" ] || fail "找不到虚拟环境的 python：$VPY" "No python in the virtualenv: $VPY"
[ -f "$REQ" ] || fail "找不到依赖清单：$REQ" "Requirements file not found: $REQ"

# venv 建的时候可能禁用了 ensurepip（--without-pip），这种环境里没有 pip
if ! "$VPY" -m pip --version >/dev/null 2>&1; then
  say "虚拟环境里没有 pip，尝试用 ensurepip 补齐…" \
      "No pip in the virtualenv, trying ensurepip…"
  "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 \
    || fail "虚拟环境缺少 pip 且 ensurepip 失败。请重建：rm -rf .venv && ./deploy.sh" \
            "The virtualenv has no pip and ensurepip failed. Recreate it: rm -rf .venv && ./deploy.sh"
fi

PIP=("$VPY" -m pip)
# --retries/--timeout 压小：源不通时要**快速失败换下一个**，而不是原地重试五分钟
# （那一串 WARNING: Retrying 就是原地重试打出来的）。--no-input 防卡在交互提示。
BASE=(--disable-pip-version-check --no-input --timeout 20 --retries 1)

# ---------------------------------------------------------------------------
# 1. 离线判一次「是否已经装齐」
#
# --no-index 让 pip 完全不看索引，只比对本地已安装的版本：全部满足就直接成功，
# 一个数据包都不发。重复部署、断网机器、SSL 被拦的机器都走这条路 —— 这也正是
# 最不该出现任何告警的场景。
# ---------------------------------------------------------------------------
if "${PIP[@]}" install -q --no-index "${BASE[@]}" -r "$REQ" >/dev/null 2>&1; then
  say "依赖已满足，跳过联网安装" "Dependencies already satisfied, skipping the network"
  exit 0
fi

if [ "${PC_PIP_OFFLINE:-0}" = "1" ]; then
  fail "PC_PIP_OFFLINE=1 但依赖未装齐。请在有网环境装好依赖后再离线运行。" \
       "PC_PIP_OFFLINE=1 but the dependencies are not satisfied. Install them online first."
fi

if [ "${PC_UPGRADE_PIP:-0}" = "1" ]; then
  say "升级 pip（PC_UPGRADE_PIP=1）…" "Upgrading pip (PC_UPGRADE_PIP=1)…"
  "${PIP[@]}" install -q "${BASE[@]}" --upgrade pip \
    || say "[!] pip 升级失败，继续用现有版本" "[!] pip upgrade failed, keeping the current version"
fi

LOG="${TMPDIR:-/tmp}/pc-pip.$$.log"
LAST_LOG=""
trap 'rm -f "$LOG"' EXIT

# 源列表：显式指定 > 默认（pip 自己读 PIP_INDEX_URL）> 国内镜像回退。
# 用两个平行数组而不是「数组套数组」：bash 3.2（macOS 自带）没有数组引用。
NAMES=()
ARGS=()
if [ -n "${PC_PIP_INDEX:-}" ]; then
  NAMES+=("PC_PIP_INDEX")
  ARGS+=("-i ${PC_PIP_INDEX}")
elif [ -n "${PIP_INDEX_URL:-}" ]; then
  NAMES+=("PIP_INDEX_URL")
  ARGS+=("")
else
  NAMES+=("默认源/default index")
  ARGS+=("")
  NAMES+=("清华镜像/Tsinghua mirror")
  ARGS+=("-i https://pypi.tuna.tsinghua.edu.cn/simple")
  NAMES+=("阿里云镜像/Aliyun mirror")
  ARGS+=("-i https://mirrors.aliyun.com/pypi/simple")
fi

LAST_STATUS=1
IMPORTANT_SOURCE=""
for index in "${!NAMES[@]}"; do
  source_name="${NAMES[$index]}"
  extra_text="${ARGS[$index]}"
  extra=()
  if [ -n "$extra_text" ]; then
    # 只有 `-i <url>` 这一种形态，按空白切开即可
    read -r -a extra <<< "$extra_text"
  fi

  say "安装依赖（$source_name）…" "Installing dependencies ($source_name)…"
  # 注意：不能靠 `if pip…; then …; fi` 之后的 `$?` 判成败 —— 没有命中分支的 if
  # 语句退出码是 0，会把失败当成功。这里显式记一个变量。
  ok=1
  if [ "$MODE" = "--verbose" ]; then
    # 手工排查时用它：pip 的进度与告警原样可见
    "${PIP[@]}" install "${BASE[@]}" "${extra[@]}" -r "$REQ" && ok=0
  else
    # 默认把 pip 的输出收进日志：正常路径上「什么都没有」才是对的，
    # 那一串重试告警只有在**真的失败**时才有价值。
    "${PIP[@]}" install -q "${BASE[@]}" "${extra[@]}" -r "$REQ" >"$LOG" 2>&1 && ok=0
  fi

  if [ "$ok" -eq 0 ]; then
    LAST_STATUS=0
    IMPORTANT_SOURCE="$source_name"
    break
  fi

  LAST_STATUS=1
  LAST_LOG="$(tail -n 20 "$LOG" 2>/dev/null || true)"
  say "[!] $source_name 没成功" "[!] $source_name did not work"
done

if [ "$LAST_STATUS" -eq 0 ]; then
  say "依赖安装完成（$IMPORTANT_SOURCE）" "Dependencies installed ($IMPORTANT_SOURCE)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 全部源都失败：把「最后一段真实输出」和可执行的下一步一起给出来，
# 而不是只丢一句「检查网络」。
# ---------------------------------------------------------------------------
if [ "$LANG_UI" = "en" ]; then
  {
    printf '[deps] [x] Installing dependencies failed (tried: %s)\n' "${NAMES[*]}"
    [ -n "$LAST_LOG" ] && printf '[deps] ---- last pip output ----\n%s\n' "$LAST_LOG"
    printf '%s\n' \
      "[deps] Usual cause: this machine cannot reach PyPI (TLS broken by a middlebox / proxy needed / isolated network). Try:" \
      "[deps]   1) point at a reachable mirror: PC_PIP_INDEX=https://your-mirror/simple ./deploy.sh" \
      "[deps]   2) go through a proxy: export https_proxy=http://proxy:port ./deploy.sh" \
      "[deps]   3) offline: pip download -r backend/requirements.txt -d wheels on a connected machine, then" \
      "[deps]      .venv/bin/python -m pip install --no-index --find-links wheels -r backend/requirements.txt" \
      "[deps]   4) reproduce by hand: $VPY -m pip install -r $REQ"
  } >&2
else
  {
    printf '[deps] [x] 依赖安装失败（已尝试：%s）\n' "${NAMES[*]}"
    [ -n "$LAST_LOG" ] && printf '[deps] ---- 最后一段 pip 输出 ----\n%s\n' "$LAST_LOG"
    printf '%s\n' \
      "[deps] 常见原因：这台机器访问不了 PyPI（SSL 被中间设备打断 / 需要代理 / 纯内网）。可以按顺序试：" \
      "[deps]   1) 换一个能通的源：PC_PIP_INDEX=https://你的镜像/simple ./deploy.sh" \
      "[deps]   2) 走代理：export https_proxy=http://代理地址:端口 ./deploy.sh" \
      "[deps]   3) 离线：在能上网的机器上 pip download -r backend/requirements.txt -d wheels，拷贝后用" \
      "[deps]      .venv/bin/python -m pip install --no-index --find-links wheels -r backend/requirements.txt" \
      "[deps]   4) 手工复现：$VPY -m pip install -r $REQ"
  } >&2
fi
exit 1
