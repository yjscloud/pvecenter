"""证书状态的统一口径。

腾讯云的整数状态码被沿用为内部标准（历史数据、日志与前端都按它渲染），阿里云 /
华为云等厂商的字符串状态在各自的适配器里映射到这套值。这样上层只认一种状态，
换厂商不会让「已签发 / 审核中」的判断失效。

单独成模块是为了避免 ``certproviders`` 反向依赖某一家厂商的客户端。
"""
from __future__ import annotations

from typing import Any

#: 内部状态码 → 中文短句（出口处再按请求语言翻译）
STATUS_TEXT = {
    0: "审核中",
    1: "已签发",
    2: "审核失败",
    3: "已过期",
    4: "等待 DNS 验证",
    5: "待提交资料",
    6: "订单取消中",
    7: "已取消",
    8: "待上传确认函",
    9: "吊销中",
    10: "已吊销",
    11: "重颁发中",
    12: "待上传吊销确认函",
    13: "免费证书待提交资料",
    14: "已退款",
    15: "证书迁移中",
}


#: 允许的状态跃迁（文档 + 排障用）。厂商的流程并不完全一致（重颁发、审核
#: 退回、订单取消…），所以这里不追求穷举，而是作为「正常路径」的说明；真正的
#: 硬约束见 :func:`set_status` 里的不可逆规则。
TRANSITIONS: dict = {
    -1: (0, 1, 10),          # 未申请 → 审核中 / 已签发 / 已吊销
    0: (1, 2, 3, 10),        # 审核中 → 签发 / 失败 / 过期 / 吊销
    1: (3, 4, 9, 10, 11),    # 已签发 → 过期 / 待验证 / 吊销中 / 已吊销 / 重颁发
    2: (0, 1, 10),           # 审核失败 → 重新提交
    3: (0, 1, 10),           # 已过期 → 重新申请 / 吊销
    4: (1, 2, 10),
    9: (10,),                # 吊销中 → 已吊销
    10: (0, 1),              # 已吊销 → 重新申请（必须有新的证书材料）
    11: (1, 2, 10),          # 重颁发中
}


class StatusTransitionError(ValueError):
    """非法的证书状态跃迁。"""


def _as_int(status: Any) -> int:
    try:
        return int(status)
    except (TypeError, ValueError):
        return -1


def set_status(site: Any, status: Any, *, changed_at: int = 0) -> int:
    """写入证书状态并守住不可逆约束。

    唯一一条硬规则：**已吊销（10）不能在没有新证书材料的情况下回到活动状态**。
    吊销后本地私钥已被销毁，若某条路径（定时同步、并发写、旧快照回写）把状态
    改回「已签发」，界面会显示一张实际上不可用的证书 —— 这类矛盾态一旦出现，
    排障与审计都会失真。

    其余跃迁按 :data:`TRANSITIONS` 记录 `status_prev` 后放行：厂商侧的
    「重颁发 / 审核退回 / 订单取消」并不遵循单一状态机，强行拦截反而会把
    正常的同步流程打断。
    """
    code = _as_int(status)
    if code != -1 and code not in STATUS_TEXT:
        raise StatusTransitionError(f"未知的证书状态：{status}")

    previous = _as_int(site.get("cert_status"))
    if code == previous:
        # 状态没变（定时同步的绝大多数情况）：只刷新文案，不覆盖「上次变更时间」
        site["cert_status_text"] = "" if code == -1 else status_text(code)
        return code
    if previous == 10 and code != 10:
        has_new_cert = bool(
            str(site.get("pending_cert_id") or "").strip()
            or str(site.get("cert_id") or "").strip()
        )
        if not has_new_cert:
            raise StatusTransitionError("已吊销的证书没有新的签发材料，不能改回活动状态")

    site["status_prev"] = previous
    site["status_changed_at"] = int(changed_at or 0)
    site["cert_status"] = code
    site["cert_status_text"] = "" if code == -1 else status_text(code)
    return code


def status_text(status: Any) -> str:
    try:
        return STATUS_TEXT.get(int(status), "状态 " + str(status))
    except (TypeError, ValueError):
        return "未知状态"
