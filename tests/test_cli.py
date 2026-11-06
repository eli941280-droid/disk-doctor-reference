"""CLI: two-phase execution and the flags that must refuse."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from disk_doctor.cli import build_parser, main


@pytest.fixture
def args_home(tmp_path):
    return ["--home", str(tmp_path / "home")]


@pytest.fixture
def cache_dir(vault, make_file):
    root = vault / "__pycache__"
    make_file(root / "mod.cpython-311.pyc", 200)
    return root


def _plan(args_home, vault, capsys) -> str:
    code = main([*args_home, "--json", "plan", "--root", str(vault), "--allow-root", str(vault)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    return payload["plan_id"]


def test_version_flag_exits_cleanly():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--version"])
    assert exc.value.code == 0


def test_tools_lists_capabilities(args_home, capsys):
    assert main([*args_home, "tools"]) == 0
    out = capsys.readouterr().out
    assert "disk_scanner.scan" in out
    # v0.2.2: execution is no longer listed as a callable tool.
    assert "safe_executor.execute" not in out
    assert "写操作" not in out


def test_plan_requires_explicit_allow_root(args_home, vault, capsys):
    code = main([*args_home, "plan", "--root", str(vault)])
    assert code == 2
    assert "不在授权范围内" in capsys.readouterr().err


def test_plan_does_not_move_files(args_home, vault, cache_dir, capsys):
    plan_id = _plan(args_home, vault, capsys)
    assert len(plan_id) == 64
    assert (cache_dir / "mod.cpython-311.pyc").exists()


def test_plan_text_shows_execute_instruction(args_home, vault, cache_dir, capsys):
    code = main([*args_home, "plan", "--root", str(vault), "--allow-root", str(vault)])
    assert code == 0
    out = capsys.readouterr().out
    assert "EXECUTE " in out
    assert "清理计划" in out


def test_execute_rejected_when_not_interactive(args_home, vault, cache_dir, capsys):
    plan_id = _plan(args_home, vault, capsys)
    code = main(
        [*args_home, "execute", "--plan-id", plan_id, "--allow-root", str(vault)]
    )
    assert code == 2
    assert "交互式终端" in capsys.readouterr().err
    assert (cache_dir / "mod.cpython-311.pyc").exists()


def test_execute_with_wrong_token_refused(args_home, vault, cache_dir, capsys, monkeypatch):
    plan_id = _plan(args_home, vault, capsys)

    monkeypatch.setattr("disk_doctor.cli.sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")

    code = main(
        [*args_home, "execute", "--plan-id", plan_id, "--allow-root", str(vault)]
    )
    assert code == 0
    assert "确认未通过" in capsys.readouterr().out
    assert (cache_dir / "mod.cpython-311.pyc").exists()


def test_execute_with_exact_token_moves_file(
    args_home, vault, cache_dir, capsys, monkeypatch, tmp_path
):
    plan_id = _plan(args_home, vault, capsys)

    monkeypatch.setattr("disk_doctor.cli.sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _prompt="": f"EXECUTE {plan_id}")

    code = main(
        [
            *args_home,
            "execute",
            "--plan-id",
            plan_id,
            "--allow-root",
            str(vault),
            "--quarantine",
            str(tmp_path / "q"),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "移入回收站" in out
    assert not (cache_dir / "mod.cpython-311.pyc").exists()


def test_legacy_clean_cannot_execute(args_home, vault, cache_dir, capsys):
    code = main(
        [
            *args_home,
            "clean",
            "--root",
            str(vault),
            "--allow-root",
            str(vault),
            "--no-dry-run",
            "--yes",
        ]
    )
    assert code == 0
    assert (cache_dir / "mod.cpython-311.pyc").exists()
    assert "已弃用" in capsys.readouterr().err


def test_sibling_directory_is_not_authorized_by_prefix(tmp_path, capsys):
    base = tmp_path / "work"
    sibling = tmp_path / "workshop"
    base.mkdir()
    sibling.mkdir()

    code = main(
        [
            "--home",
            str(tmp_path / "h"),
            "plan",
            "--root",
            str(sibling),
            "--allow-root",
            str(base),
        ]
    )
    assert code == 2
    assert "不在授权范围内" in capsys.readouterr().err


def test_diagnose_json(args_home, vault, capsys):
    assert main([*args_home, "--json", "diagnose", "--path", str(vault)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert 0 <= payload["health_score"] <= 100
    assert payload["days_until_full"] is None


def test_ask_cannot_execute(args_home, vault, cache_dir, capsys):
    code = main(
        [*args_home, "ask", "清理", "--allow-root", str(vault)]
    )
    assert code == 0
    assert (cache_dir / "mod.cpython-311.pyc").exists()


def test_achievements_command(args_home, capsys):
    assert main([*args_home, "achievements"]) == 0
    assert "累计" in capsys.readouterr().out


def test_dupes_command(args_home, vault, make_file, capsys):
    make_file(vault / "a.bin", 4096, fill=b"z")
    make_file(vault / "b.bin", 4096, fill=b"z")
    assert main([*args_home, "dupes", "--root", str(vault)]) == 0
    assert "重复" in capsys.readouterr().out
