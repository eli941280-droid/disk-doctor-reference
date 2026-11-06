"""Plan building: scope enforcement, filtering, ordering, sealing."""

from __future__ import annotations

from disk_doctor.core.clean_plan import PlanBuilder
from disk_doctor.core.schemas import RiskLevel


def test_rejects_candidate_outside_scope(policy, vault, outside, make_file, make_candidate):
    other = outside / "cache"
    make_file(other / "x.tmp", 10)
    builder = PlanBuilder(policy)
    plan = builder.build(str(vault), [make_candidate(other)])
    assert plan.items == []
    assert plan.rejections[0].rule == "outside_scope_root"


def test_protected_file_is_excluded_and_counted(policy, vault, make_file, make_candidate):
    cache = vault / "cache"
    make_file(cache / "a.tmp", 10)
    make_file(cache / "thesis.docx", 10)
    plan = PlanBuilder(policy).build(str(vault), [make_candidate(cache)])

    assert plan.total_files == 1
    assert plan.protected_files == 1
    assert all(not f.path.endswith(".docx") for f in plan.all_files())


def test_candidate_with_only_protected_files_is_rejected(
    policy, vault, make_file, make_candidate
):
    cache = vault / "cache2"
    make_file(cache / "secret.key", 10)
    plan = PlanBuilder(policy).build(str(vault), [make_candidate(cache)])
    assert plan.items == []
    assert plan.rejections[0].rule == "no_eligible_files"


def test_deduplicates_same_directory(policy, vault, make_file, make_candidate):
    cache = vault / "cache"
    make_file(cache / "a.tmp", 10)
    cand = make_candidate(cache)
    plan = PlanBuilder(policy).build(str(vault), [cand, cand])
    assert len(plan.items) == 1


def test_safe_items_sort_before_caution(policy, vault, make_file, make_candidate):
    safe = vault / "safe_cache"
    risky = vault / "risky_cache"
    make_file(safe / "a.tmp", 10)
    make_file(risky / "b.tmp", 9999)
    plan = PlanBuilder(policy).build(
        str(vault),
        [make_candidate(risky, risk=RiskLevel.CAUTION), make_candidate(safe)],
    )
    assert plan.items[0].risk is RiskLevel.SAFE


def test_within_same_risk_larger_first(policy, vault, make_file, make_candidate):
    small = vault / "small_cache"
    large = vault / "large_cache"
    make_file(small / "a.tmp", 10)
    make_file(large / "b.tmp", 5000)
    plan = PlanBuilder(policy).build(
        str(vault), [make_candidate(small), make_candidate(large)]
    )
    assert plan.items[0].size_bytes == 5000


def test_totals_come_from_files(policy, vault, make_file, make_candidate):
    a = vault / "ca"
    b = vault / "cb"
    make_file(a / "1.tmp", 100)
    make_file(a / "2.tmp", 100)
    make_file(b / "3.tmp", 250)
    plan = PlanBuilder(policy).build(str(vault), [make_candidate(a), make_candidate(b)])
    assert plan.total_bytes == 450
    assert plan.total_files == 3


def test_item_ids_are_deterministic_by_order(policy, vault, make_file, make_candidate):
    a = vault / "ca"
    b = vault / "cb"
    make_file(a / "1.tmp", 10)
    make_file(b / "2.tmp", 20)
    plan = PlanBuilder(policy).build(str(vault), [make_candidate(a), make_candidate(b)])
    assert plan.items[0].item_id.startswith("I001")
    assert plan.items[1].item_id.startswith("I002")


def test_truncates_and_notes(policy, vault, make_ref):
    """Truncation is tested with synthetic refs; no files needed on disk."""

    from disk_doctor.core.schemas import Candidate

    cands = []
    for i in range(5):
        directory = vault / f"c{i}"
        directory.mkdir()
        ref = make_ref(directory / "x.tmp", 10 + i)
        cands.append(
            Candidate(
                path=str(directory),
                category="t",
                risk=RiskLevel.SAFE,
                size_bytes=ref.size_bytes,
                file_count=1,
                files=(ref,),
            )
        )
    plan = PlanBuilder(policy, max_items=3).build(str(vault), cands)
    assert len(plan.items) == 3
    assert any("截断" in n for n in plan.notes)


def test_second_confirm_threshold_by_file_count(policy, vault, make_ref):
    from disk_doctor.core.schemas import Candidate

    directory = vault / "bulk"
    directory.mkdir()
    refs = tuple(make_ref(directory / f"f{i}.tmp", 1, inode=i) for i in range(51))
    cand = Candidate(
        path=str(directory),
        category="t",
        risk=RiskLevel.SAFE,
        size_bytes=51,
        file_count=51,
        files=refs,
    )
    plan = PlanBuilder(policy).build(str(vault), [cand])
    assert plan.total_files == 51
    assert plan.requires_second_confirm


def test_second_confirm_threshold_by_size(policy, vault, make_ref):
    from disk_doctor.core.schemas import Candidate

    directory = vault / "huge"
    directory.mkdir()
    ref = make_ref(directory / "big.tmp", 1024**3 + 1)
    cand = Candidate(
        path=str(directory),
        category="t",
        risk=RiskLevel.SAFE,
        size_bytes=ref.size_bytes,
        file_count=1,
        files=(ref,),
    )
    plan = PlanBuilder(policy).build(str(vault), [cand])
    assert plan.requires_second_confirm


def test_small_plan_needs_no_second_confirm(policy, vault, make_file, make_candidate):
    cache = vault / "tiny"
    make_file(cache / "a.tmp", 1024)
    plan = PlanBuilder(policy).build(str(vault), [make_candidate(cache)])
    assert not plan.requires_second_confirm


def test_plan_is_sealed_on_build(policy, vault, make_file, make_candidate):
    cache = vault / "cache"
    make_file(cache / "a.tmp", 10)
    plan = PlanBuilder(policy).build(str(vault), [make_candidate(cache)])
    assert len(plan.plan_id) == 64
    ok, reason = plan.verify_integrity()
    assert ok, reason


def test_plan_ttl_is_stamped(policy, vault):
    plan = PlanBuilder(policy, ttl_seconds=60).build(str(vault), [])
    assert plan.ttl_seconds == 60
