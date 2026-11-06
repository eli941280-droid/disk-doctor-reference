"""Policy engine: the whitelist must fail closed in every direction."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from disk_doctor.core.policy_engine import PolicyEngine


def test_allows_path_inside_root(policy, vault):
    target = vault / "cache"
    target.mkdir()
    verdict = policy.verdict(target)
    assert verdict.allowed
    assert verdict.rule == "allow"


def test_allows_the_root_itself(policy, vault):
    assert policy.verdict(vault).allowed


def test_denies_path_outside_root(policy, outside):
    assert not policy.verdict(outside).allowed


def test_denies_parent_traversal(policy, vault, tmp_path):
    escape = vault / ".." / "outside" / "secret.txt"
    verdict = policy.verdict(escape)
    assert not verdict.allowed
    assert verdict.rule == "outside_allowed_roots"


def test_denies_protected_component(policy, vault):
    target = vault / "Windows" / "System32"
    target.mkdir(parents=True)
    verdict = policy.verdict(target)
    assert not verdict.allowed
    assert verdict.rule == "protected_component"


def test_denies_protected_suffix(policy, vault, make_file):
    doc = make_file(vault / "thesis.docx", 10)
    verdict = policy.verdict(doc)
    assert not verdict.allowed
    assert verdict.rule == "protected_suffix"


def test_denies_protected_name(policy, vault, make_file):
    page = make_file(vault / "pagefile.sys", 10)
    assert not policy.verdict(page).allowed


def test_denies_filesystem_root(policy):
    root = Path(os.path.abspath(os.sep))
    verdict = policy.verdict(root)
    assert not verdict.allowed
    assert verdict.rule == "filesystem_root"


def test_symlink_is_denied_even_when_name_is_harmless(policy, vault, outside, make_file):
    secret = make_file(outside / "target.txt", 64)
    link = vault / "innocent"
    try:
        os.symlink(secret, link, target_is_directory=False)
    except (OSError, NotImplementedError):  # pragma: no cover - needs privileges
        pytest.skip("当前环境不允许创建符号链接")
    verdict = policy.verdict(link)
    assert not verdict.allowed
    assert verdict.rule == "symlink_denied"


def test_symlink_branch_is_covered_without_privileges(policy, vault, monkeypatch):
    """Windows needs SeCreateSymbolicLinkPrivilege, which CI may lack.

    The branch itself still has to be tested, so we force the detection rather
    than skipping the guarantee entirely.
    """

    import disk_doctor.core.policy_engine as policy_module

    target = vault / "looks_normal"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        policy_module.os.path, "islink", lambda p: str(p) == str(target)
    )

    verdict = policy.verdict(target)
    assert not verdict.allowed
    assert verdict.rule == "symlink_denied"


def test_requires_at_least_one_root():
    with pytest.raises(ValueError):
        PolicyEngine([])


def test_normalize_is_absolute(policy):
    normalized = policy.normalize("some" + os.sep + "relative" + os.sep + "path")
    assert normalized.is_absolute()


def test_describe_exposes_rules(policy, vault):
    payload = policy.describe()
    assert payload["allowed_roots"] == [str(vault)]
    assert "windows" in payload["protected_components"]
    assert ".docx" in payload["protected_suffixes"]


def test_case_insensitive_root_match(tmp_path):
    root = tmp_path / "Data"
    root.mkdir()
    engine = PolicyEngine([root])
    assert engine.verdict(root / "sub").allowed


def test_allows_deep_nesting(policy, vault, make_file):
    deep = make_file(vault / "a" / "b" / "c" / "d" / "cache.tmp", 8)
    assert policy.verdict(deep).allowed
