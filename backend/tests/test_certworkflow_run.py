"""工作流运行状态机的纯函数测试（不依赖数据库）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-unit-tests-only")

from app import certworkflow_run as run  # noqa: E402


DEFINITION = {
    "name": "发布流程",
    "nodes": [
        {"id": "deploy", "type": "deploy_upload", "title": "部署", "needs": ["apply"]},
        {"id": "monitor", "type": "domain_monitor", "title": "", "needs": []},
        {"id": "notify", "type": "notification", "title": "通知", "needs": ["deploy"]},
        {"id": "apply", "type": "cert_apply", "title": "申请", "needs": ["monitor"]},
    ],
}


def _new() -> dict:
    steps = run.build_steps(DEFINITION)
    return {
        "id": "run_test",
        "status": run.STATUS_PENDING,
        "current_index": -1,
        "steps": steps,
        "approver": "",
        "approval_note": "",
        "started_at": 0,
        "finished_at": 0,
        "updated_at": 0,
    }


def test_build_steps_follows_needs_not_definition_order():
    steps = run.build_steps(DEFINITION)
    assert [step["node_id"] for step in steps] == [
        "monitor",
        "apply",
        "deploy",
        "notify",
    ]
    assert all(step["status"] == run.STEP_PENDING for step in steps)


def test_approve_starts_first_step():
    approved = run.apply_action(_new(), "approve", note="同意", operator="alice", now=100)
    assert approved["status"] == run.STATUS_RUNNING
    assert approved["current_index"] == 0
    assert approved["approver"] == "alice"
    assert approved["approval_note"] == "同意"
    assert approved["steps"][0]["status"] == run.STEP_RUNNING
    assert approved["steps"][0]["started_at"] == 100
    assert approved["started_at"] == 100


def test_advance_through_all_steps_finishes_run():
    current = run.apply_action(_new(), "approve", operator="alice", now=100)
    current = run.apply_action(current, "advance", note="监控完成", now=200)
    assert current["steps"][0]["status"] == run.STEP_DONE
    assert current["steps"][0]["note"] == "监控完成"
    assert current["steps"][1]["status"] == run.STEP_RUNNING
    assert current["current_index"] == 1

    current = run.apply_action(current, "advance", now=300)
    current = run.apply_action(current, "advance", now=400)
    assert current["status"] == run.STATUS_RUNNING
    assert current["current_index"] == 3

    current = run.apply_action(current, "advance", now=500)
    assert current["status"] == run.STATUS_SUCCEEDED
    assert current["current_index"] == 4
    assert current["finished_at"] == 500
    assert all(step["status"] == run.STEP_DONE for step in current["steps"])


def test_reject_only_from_pending():
    rejected = run.apply_action(_new(), "reject", note="参数不全", operator="bob", now=10)
    assert rejected["status"] == run.STATUS_REJECTED
    assert rejected["approval_note"] == "参数不全"
    assert rejected["finished_at"] == 10
    with pytest.raises(run.RunError) as exc:
        run.apply_action(rejected, "approve")
    assert exc.value.code == "status_conflict"


def test_cancel_running_marks_current_step_skipped():
    current = run.apply_action(_new(), "approve", now=10)
    cancelled = run.apply_action(current, "cancel", note="人工终止", now=20)
    assert cancelled["status"] == run.STATUS_CANCELLED
    assert cancelled["steps"][0]["status"] == run.STEP_SKIPPED
    assert cancelled["steps"][0]["note"] == "人工终止"
    assert cancelled["finished_at"] == 20


def test_invalid_action_and_unknown_action():
    with pytest.raises(run.RunError) as exc:
        run.apply_action(_new(), "advance")
    assert exc.value.code == "status_conflict"
    with pytest.raises(run.RunError) as exc:
        run.apply_action(_new(), "unknown")
    assert exc.value.code == "invalid_action"


def test_no_nodes_cannot_approve():
    empty = _new()
    empty["steps"] = []
    with pytest.raises(run.RunError) as exc:
        run.apply_action(empty, "approve")
    assert exc.value.code == "no_nodes"


def test_cycle_fallback_keeps_all_nodes():
    cyclic = {
        "nodes": [
            {"id": "a", "type": "domain_monitor", "needs": ["b"]},
            {"id": "b", "type": "cert_apply", "needs": ["a"]},
        ]
    }
    steps = run.build_steps(cyclic)
    assert [step["node_id"] for step in steps] == ["a", "b"]
