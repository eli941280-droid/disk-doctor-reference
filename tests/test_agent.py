"""Agent behaviour: routing, two-phase gating, veto ledger, capabilities."""

from __future__ import annotations

import pytest

from disk_doctor.agent.orchestrator import DiskDoctorAgent
from disk_doctor.core.schemas import Intent


@pytest.fixture
def agent(vault, tmp_path):
    return DiskDoctorAgent.create(
        [str(vault)],
        home=tmp_path / "home",
        quarantine_dir=tmp_path / "quarantine",
    )


@pytest.fixture
def cache_dir(vault, make_file):
    """A positively identifiable cache: __pycache__ holding only .pyc files."""

    root = vault / "__pycache__"
    make_file(root / "mod.cpython-311.pyc", 300)
    return root


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我清理一下", Intent.CLEAN_QUICK),
        ("深度清理", Intent.CLEAN_DEEP),
        ("看看我的磁盘状况", Intent.DIAGNOSE),
        ("找找大文件", Intent.FIND_LARGE),
        ("有没有重复文件", Intent.FIND_DUPLICATE),
        ("我的成就", Intent.ACHIEVEMENTS),
        ("回收站还能用吗", Intent.TRASH_INFO),
    ],
)
def test_routes_known_intents(agent, text, expected):
    assert agent.router.route(text).intent is expected


@pytest.mark.parametrize(
    "text",
    ["帮我把 D 盘格式化了", "帮我恢复误删的文件", "看看我有哪些私密照片", "给我 root 权限"],
)
def test_out_of_scope_wins_over_other_keywords(agent, text):
    assert agent.router.route(text).intent is Intent.OUT_OF_SCOPE


def test_unknown_intent_is_reported(agent):
    turn = agent.handle("今天天气怎么样")
    assert turn.intent is Intent.UNKNOWN
    assert "不太确定" in turn.message


# --------------------------------------------------------------------------- #
# Read-only paths
# --------------------------------------------------------------------------- #

def test_refusal_is_three_part(agent):
    turn = agent.handle("帮我把 D 盘格式化了")
    assert turn.intent is Intent.OUT_OF_SCOPE
    assert "无法执行" in turn.message
    assert "→" in turn.message


def test_diagnose_returns_report(agent, vault):
    turn = agent.handle("看看磁盘", scope=str(vault))
    assert turn.intent is Intent.DIAGNOSE
    assert "健康评分" in turn.message
    assert turn.data["days_until_full"] is None


def test_find_large_is_read_only(agent, vault, make_file):
    make_file(vault / "big.bin", 2048)
    turn = agent.handle("找大文件", scope=str(vault))
    assert turn.intent is Intent.FIND_LARGE
    assert (vault / "big.bin").exists()


# --------------------------------------------------------------------------- #
# Two-phase cleanup
# --------------------------------------------------------------------------- #

def test_plan_does_not_touch_files(agent, vault, cache_dir):
    turn = agent.handle("清理", scope=str(vault))

    assert turn.awaiting_confirmation
    assert turn.plan_id and len(turn.plan_id) == 64
    assert (cache_dir / "mod.cpython-311.pyc").exists()
    assert "清理计划" in turn.message


def test_plan_is_persisted(agent, vault, cache_dir):
    turn = agent.handle("清理", scope=str(vault))
    loaded = agent.plan_store.load(turn.plan_id)
    assert loaded.plan_id == turn.plan_id


def test_execute_with_wrong_token_rejected(agent, vault, cache_dir):
    turn = agent.handle("清理", scope=str(vault))
    result = agent.execute_plan(turn.plan_id, confirmation="yes")

    assert (cache_dir / "mod.cpython-311.pyc").exists()
    assert "确认未通过" in result.message


def test_execute_with_partial_id_rejected(agent, vault, cache_dir):
    turn = agent.handle("清理", scope=str(vault))
    partial = turn.plan_id[:16]
    result = agent.execute_plan(partial, confirmation=f"EXECUTE {partial}")

    assert (cache_dir / "mod.cpython-311.pyc").exists()
    assert "无法执行" in result.message


def test_execute_with_exact_id_moves_files(agent, vault, cache_dir):
    turn = agent.handle("清理", scope=str(vault))
    result = agent.execute_plan(
        turn.plan_id, confirmation=f"EXECUTE {turn.plan_id}"
    )

    assert not (cache_dir / "mod.cpython-311.pyc").exists()
    assert "移入回收站" in result.message
    assert result.data["result"]["bytes_moved_to_trash"] > 0


def test_plan_is_consumed_after_execution(agent, vault, cache_dir):
    turn = agent.handle("清理", scope=str(vault))
    agent.execute_plan(turn.plan_id, confirmation=f"EXECUTE {turn.plan_id}")

    again = agent.execute_plan(turn.plan_id, confirmation=f"EXECUTE {turn.plan_id}")
    assert "无法执行" in again.message
    assert again.data["error"] == "plan_consumed"


def test_cleanup_outside_allowed_root_is_refused(agent, outside):
    turn = agent.handle("清理", scope=str(outside))
    assert "不在授权范围内" in turn.message


def test_veto_keeps_item_out_of_later_plans(agent, vault, cache_dir):
    agent.veto(str(cache_dir), "还要用")
    turn = agent.handle("清理", scope=str(vault))
    assert turn.data["items"] == []


# --------------------------------------------------------------------------- #
# Capability truthfulness
# --------------------------------------------------------------------------- #

def test_registry_exposes_no_write_tools(agent):
    """v0.2.2: the model-facing registry holds reads only.

    Execution is not a tool. It is a state transition that requires a stored
    plan and a fresh user confirmation, so it cannot be reached by a model.
    """

    caps = agent.capabilities()
    writers = [t for t in caps["tools"] if not t["read_only"]]
    assert writers == []
    assert caps["write_tools"] == []
    assert caps["execution_entry"].startswith("agent.execute_plan")


def test_narration_is_labelled_as_mock(agent, vault):
    turn = agent.handle("看看磁盘", scope=str(vault), narrate=True)
    assert "[mock:" in turn.message
    assert "未调用任何模型服务" in turn.message
