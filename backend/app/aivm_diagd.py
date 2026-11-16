"""客户机内的受限执行器（pvecenter-diagd）。

它解决什么问题
--------------
Guest Agent 的 ``exec`` 是「以 root 跑任意 argv，只回 pid，没有 kill」。于是：

* 一条写错的命令只能等它自己结束（``EXEC_TIMEOUT`` 只是**停止等待**，进程还在）；
* 用户点了「撤销授权」，屏幕上那条 ``journalctl -f`` 还在转；
* 客户机里没有任何资源上限，AI 一条 ``grep -r /`` 就能把 CPU 打满。

diagd 在客户机内补上这三件事：记录 pid/pgid、施加 CPU 与内存上限、支持按租约终止。

为什么是一个 sh 脚本而不是常驻服务
--------------------------------
客户机可能是最小化安装、可能没有 python、可能连 systemd 都不完整。一个
``#!/bin/sh`` 的脚本能在几乎所有 Linux 上跑；常驻服务要装依赖、要开机自启、
要处理升级 —— 为了「能 kill 一条命令」付出这些代价不值得。

**它不降权**。以 ``pvecenter-ai`` 执行听起来更安全，但写 ``/etc`` 就会
Permission denied，等于把 P1 的写能力废掉。真正的降权要配合文件属主与 sudo
白名单，那是另一件事（见项目待办）。当前的安全边界仍是服务端策略。
"""
from __future__ import annotations

#: 脚本版本：探测时回传，便于确认客户机里装的是哪一版
VERSION = "1"

SCRIPT = r"""#!/bin/sh
# pvecenter-diagd —— 客户机内的受限执行器（由 pvecenter 面板下发）
# 用法：
#   pvecenter-diagd probe                     # 自检，输出 ok
#   pvecenter-diagd install                   # 安装到 /usr/local/sbin
#   pvecenter-diagd run --id ID --lease L --timeout N -- CMD...
#   pvecenter-diagd kill --id ID | --lease L
#
# 它只做三件事：登记 pid、施加资源上限、支持被终止。命令的合法性由服务端判定，
# 这里不做白名单 —— 客户机上的规则改一次要重启，服务端的规则改一次就生效。
set -eu
DIR=/run/pvecenter-diagd
SELF=/usr/local/sbin/pvecenter-diagd
mkdir -p "$DIR" 2>/dev/null || true

case "${1:-help}" in
  probe)
    echo "pvecenter-diagd ok __VERSION__"
    exit 0
    ;;
  install)
    install -m 0755 "$0" "$SELF" 2>/dev/null || cp "$0" "$SELF"
    chmod 0755 "$SELF"
    echo "installed $SELF"
    ;;
  run)
    shift
    rid=""; rtimeout="20"; rlease=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --id) rid="$2"; shift 2 ;;
        --timeout) rtimeout="$2"; shift 2 ;;
        --lease) rlease="$2"; shift 2 ;;
        --) shift; break ;;
        *) break ;;
      esac
    done
    [ "$#" -gt 0 ] || { echo "no command" >&2; exit 64; }
    case "$rtimeout" in
      ''|*[!0-9]*) rtimeout=20 ;;
    esac

    # ulimit 只约束本进程及其子进程，够挡住「一条 grep 把 CPU 吃满」这类事故。
    ulimit -t "$rtimeout" 2>/dev/null || true
    ulimit -v 524288 2>/dev/null || true

    # setsid：新进程组，kill 时按组杀，子进程不会变成孤儿
    setsid "$@" &
    rpid=$!
    echo "$rpid" > "$DIR/$rid.pid"
    if [ -n "$rlease" ]; then
      echo "$rlease" > "$DIR/$rid.lease"
    fi

    # 兜底超时：外面（面板）超时只是停止等待，这里负责真正收口
    ( sleep "$rtimeout"; kill -TERM "-$rpid" 2>/dev/null || true ) &
    rkiller=$!

    set +e
    wait "$rpid"
    rrc=$?
    set -e
    kill "$rkiller" 2>/dev/null || true
    rm -f "$DIR/$rid.pid" "$DIR/$rid.lease"
    exit "$rrc"
    ;;
  kill)
    shift
    kid=""; klease=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --id) kid="$2"; shift 2 ;;
        --lease) klease="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    found=0
    if [ -n "$kid" ] && [ -f "$DIR/$kid.pid" ]; then
      kp=$(cat "$DIR/$kid.pid")
      kill -TERM "-$kp" 2>/dev/null || kill -TERM "$kp" 2>/dev/null || true
      rm -f "$DIR/$kid.pid" "$DIR/$kid.lease"
      found=1
    elif [ -n "$klease" ]; then
      for kf in "$DIR"/*.lease; do
        [ -f "$kf" ] || continue
        if [ "$(cat "$kf")" = "$klease" ]; then
          kbase=${kf%.lease}
          if [ -f "$kbase.pid" ]; then
            kp=$(cat "$kbase.pid")
            kill -TERM "-$kp" 2>/dev/null || kill -TERM "$kp" 2>/dev/null || true
            rm -f "$kbase.pid"
            found=1
          fi
          rm -f "$kf"
        fi
      done
    fi
    if [ "$found" = "1" ]; then
      echo "killed"
    else
      echo "no running task"
    fi
    exit 0
    ;;
  *)
    echo "usage: pvecenter-diagd {probe|install|run|kill}" >&2
    exit 64
    ;;
esac
""".replace("__VERSION__", VERSION)

#: 给用户的安装命令（粘贴到客户机里执行一次）
INSTALL_HINT = (
    "把脚本保存到客户机的 /tmp/pvecenter-diagd，chmod +x 后执行 install；"
    "面板会通过 Guest Agent 自动探测 pvecenter-diagd probe 是否可用"
)
