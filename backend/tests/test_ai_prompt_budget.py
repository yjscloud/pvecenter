"""prompt 体积与稳定前缀：工具表每一步都要重发，省下来的就是首字与成本。

两条约束容易被后来的改动悄悄破坏：

* **说明一句话就够**。模型选工具看的是「这个工具做什么」；「，用于…」那半句是
  给人读的说明书，随 34 个工具每步重发一次，一年下来是纯浪费。
* **tools + system 必须是逐字节稳定的前缀**。上游的 prefix cache 按前缀命中：
  把随轮次变化的租约提示、摘要拼进 system，等于每一步都让缓存整段失效。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import ai, aitools  # noqa: E402


def _names(mode: str = "") -> list[str]:
    return [
        item["function"]["name"]
        for item in aitools.openai_tools(
            include_exec=True, include_write=True, mode=mode
        )
    ]


def test_every_description_is_one_short_line() -> None:
    for item in aitools.openai_tools(include_exec=True, include_write=True):
        description = item["function"]["description"]
        assert "\n" not in description, item["function"]["name"]
        assert "，用于" not in description, item["function"]["name"]
        assert len(description) <= 80, (item["function"]["name"], description)


def test_inspect_mode_skips_tools_already_in_evidence() -> None:
    inspect_names = set(_names("inspect"))
    chat_names = set(_names("chat"))
    for name in aitools.INSPECT_EVIDENCE_DUPLICATED:
        assert name not in inspect_names
        assert name in chat_names


def test_tool_schema_is_byte_stable_across_calls() -> None:
    """顺序与文案固定：前缀缓存靠的就是这个，抖动一下缓存就没了。"""
    first = json.dumps(
        aitools.openai_tools(include_exec=True, include_write=True), ensure_ascii=False
    )
    second = json.dumps(
        aitools.openai_tools(include_exec=True, include_write=True), ensure_ascii=False
    )
    assert first == second
    assert _names() == _names()


def test_group_members_are_not_sent_to_the_model() -> None:
    """被分组覆盖的原子工具不再下发 —— 它们的内容组里一次就拿全了。"""
    exposed = {
        item["function"]["name"]
        for item in aitools.openai_tools(include_exec=True, include_write=True)
    }
    assert not (exposed & aitools.HOST_GROUP_MEMBERS)
    assert {group["name"] for group in aitools.HOST_GROUPS} <= exposed


def test_every_atomic_tool_is_still_reachable() -> None:
    """能执行 ≠ 能下发：原子工具要么被组覆盖，要么仍单列，不能两头落空。"""
    exposed = {
        item["function"]["name"]
        for item in aitools.openai_tools(include_exec=True, include_write=True)
    }
    names = {item["name"] for item in aitools.HOST_TOOLS}
    assert names <= (aitools.HOST_GROUP_MEMBERS | exposed)


def test_group_steps_follow_requires_and_unless() -> None:
    service = aitools.HOST_TOOL_MAP["host_service_diag"]
    without = [item["name"] for item, _ in aitools._group_steps(service, {})]
    with_unit = [item["name"] for item, _ in aitools._group_steps(service, {"unit": "nginx"})]
    assert without == ["host_failed_units"]
    assert with_unit == ["host_unit_status", "host_read_log"]


def test_group_preview_lists_every_command() -> None:
    """用户核对的命令清单必须是真跑的那几条，而不是工具名。"""
    preview = aitools.command_preview("host_net_diag", {"address": "1.1.1.1"})
    assert "ip -br addr" in preview
    assert "ip route" in preview
    assert "ping -c 4 -W 2 1.1.1.1" in preview
    # 没给 name 就不该出现 DNS 查询
    assert "getent" not in preview


def test_group_never_forwards_unregistered_params() -> None:
    """分组工具不是参数通道：原子工具没登记的键不会进到命令模板里。"""
    service = aitools.HOST_TOOL_MAP["host_service_diag"]
    picked = aitools._group_steps(service, {"unit": "sshd", "path": "/etc/shadow"})
    for atomic, step_args in picked:
        for key in step_args:
            assert key in (atomic.get("params") or {}), (atomic["name"], key)


def test_tool_schema_shrank_against_the_atomic_catalog() -> None:
    """合并之后函数表要明显变小，否则这一层改造没有意义。"""
    exposed = aitools.openai_tools(include_exec=True, include_write=True)
    assert len(exposed) < len(aitools.HOST_TOOLS) + len(aitools.INTERNAL_TOOLS)


def test_summary_stream_reassembles_split_chunks() -> None:
    """报告模式的「先给结论」：summary 是 JSON 第一个字段，可以边生成边看。"""
    stream = ai._JsonSummaryStream()
    pieces = [
        '{"summary": "磁盘',
        '分区 /var 已用 92%',
        '，需要清理", "findings": []}',
    ]
    assert "".join(stream.feed(piece) for piece in pieces) == "磁盘分区 /var 已用 92%，需要清理"


def test_summary_stream_stops_at_the_closing_quote() -> None:
    stream = ai._JsonSummaryStream()
    assert stream.feed('{"summary":"a\\"b", "findings":[{"title":"x"}]}') == 'a"b'
    # 收口之后不再往外吐：后面的 findings 不该混进叙述气泡
    assert stream.feed('"findings"') == ""


def test_summary_stream_waits_for_escapes_split_across_chunks() -> None:
    stream = ai._JsonSummaryStream()
    assert stream.feed('{"summary":"line1\\') == "line1"
    assert stream.feed('nline2"}') == "\nline2"


def test_summary_stream_gives_up_on_non_string_summary() -> None:
    stream = ai._JsonSummaryStream()
    assert stream.feed('{"summary": null,') == ""
    assert stream.feed('"x"') == ""


def test_truncate_keeps_the_tail_of_long_output() -> None:
    """日志的结论在末尾：只截头会让模型看到「前面风平浪静」而误判。"""
    body = "A" * 200 + "\n最后一行：Out of memory: Killed process 1234 (nginx)"
    cut = aitools._truncate(body, 120)
    assert len(cut) <= 200  # 正文受控，只多出省略标记
    assert "Killed process 1234" in cut
    assert "省略" in cut


def test_truncate_leaves_short_output_alone() -> None:
    assert aitools._truncate("  短输出  ", 100) == "短输出"


def test_timeout_is_split_between_connect_and_read() -> None:
    """连不上要 10 秒内报错；长回答仍按总时长等。"""
    timeout = ai.httpx_timeout(90)
    assert timeout.connect == 10.0
    assert timeout.read == 90.0
    # 配得比连接超时还短时，连接阶段跟着总时长走
    assert ai.httpx_timeout(5).connect == 5.0
    # 脏配置回落到 90，不抛异常
    assert ai.httpx_timeout("oops").read == 90.0


def test_last_user_message_stays_last_when_a_note_is_added() -> None:
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "答"},
        {"role": "user", "content": "这一轮的问题"},
    ]
    ai._insert_system_note(messages, "本轮租约：只读")
    assert messages[0] == {"role": "system", "content": "SYSTEM"}
    assert messages[-1] == {"role": "user", "content": "这一轮的问题"}
    assert messages[-2] == {"role": "system", "content": "本轮租约：只读"}


def test_empty_note_is_not_inserted() -> None:
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
    ai._insert_system_note(messages, "   ")
    assert len(messages) == 2
