"""Planning: from discovered candidates to a reviewable, policy-clean plan."""

from __future__ import annotations

import os
from collections.abc import Iterable

from ..core.clean_plan import PlanBuilder
from ..core.policy_engine import PolicyEngine
from ..core.schemas import Candidate, CleanPlan, RiskLevel
from ..tools.cache_analyzer import CacheAnalyzer


class CleanupPlanner:
    def __init__(
        self,
        policy: PolicyEngine,
        analyzer: CacheAnalyzer,
        builder: PlanBuilder | None = None,
    ) -> None:
        self.policy = policy
        self.analyzer = analyzer
        self.builder = builder or PlanBuilder(policy)

    def plan(
        self,
        root: str,
        *,
        mode: str = "quick",
        dry_run: bool = True,
        vetoed: Iterable[str] = (),
        extra_candidates: Iterable[Candidate] = (),
    ) -> CleanPlan:
        """Build a plan for *root*.

        ``mode="quick"`` keeps only SAFE candidates; ``mode="deep"`` also allows
        CAUTION ones, which always require an explicit confirmation step.
        """

        vetoed_keys = {os.path.normcase(str(self.policy.normalize(v))) for v in vetoed}
        candidates = list(self.analyzer.discover([root]))
        candidates.extend(extra_candidates)

        if mode == "quick":
            candidates = [c for c in candidates if c.risk is RiskLevel.SAFE]

        candidates = [
            c
            for c in candidates
            if os.path.normcase(str(self.policy.normalize(c.path))) not in vetoed_keys
        ]

        notes = [
            f"模式：{'快速（仅安全项）' if mode == 'quick' else '深度（含需注意项）'}",
        ]
        return self.builder.build(root, candidates, dry_run=dry_run, notes=notes)
