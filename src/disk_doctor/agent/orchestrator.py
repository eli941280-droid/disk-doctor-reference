"""The agent: one entry point that ties routing, planning, execution together.

Design stance
-------------
The orchestrator is a state machine, not a chat loop. Every branch either reads
(read-only, always allowed) or proposes a plan (never mutates). The only path to
a filesystem change is ``execute_plan``, which loads a stored plan, obtains an
approval receipt bound to that exact plan id, and hands it to a ``SafeExecutor``.

That single choke point is what makes the safety claims checkable in code review
rather than only in prose.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..core.audit_logger import AuditLogger
from ..core.clean_plan import (
    ApprovalError,
    ConfirmationAuthority,
    PlanBuilder,
    PlanStore,
    PlanStoreError,
)
from ..core.policy_engine import PolicyEngine
from ..core.safe_executor import (
    QuarantineBackend,
    SafeExecutor,
    Send2TrashBackend,
    TrashBackend,
)
from ..core.schemas import (
    AgentTurn,
    ExecutionState,
    Intent,
    RiskLevel,
)
from ..core.utils import human_bytes
from ..core.validator import Validator
from ..llm.base import LLMClient, LLMError
from ..llm.mock import MockLLM
from ..reporting.completion import render_completion, render_error
from ..reporting.diagnosis import build_diagnosis, render_diagnosis
from ..reporting.preview import render_confirmation_summary, render_preview
from ..tools.achievements import AchievementStore
from ..tools.cache_analyzer import CacheAnalyzer
from ..tools.disk_scanner import DiskScanner
from ..tools.duplicate_finder import DuplicateFinder
from ..tools.registry import ToolRegistry
from ..tools.trash_manager import TrashManager
from .context_manager import ContextManager
from .intent_router import IntentRouter, RoutedIntent
from .planner import CleanupPlanner

SCOPE_SLOT = "scope"


class DiskDoctorAgent:
    """Composition root plus the task state machine."""

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        scanner: DiskScanner | None = None,
        analyzer: CacheAnalyzer | None = None,
        duplicates: DuplicateFinder | None = None,
        planner: CleanupPlanner | None = None,
        executor: SafeExecutor | None = None,
        validator: Validator | None = None,
        achievements: AchievementStore | None = None,
        context: ContextManager | None = None,
        llm: LLMClient | None = None,
        router: IntentRouter | None = None,
        audit: AuditLogger | None = None,
        registry: ToolRegistry | None = None,
        plan_store: PlanStore | None = None,
        authority: ConfirmationAuthority | None = None,
    ) -> None:
        self.policy = policy
        self.scanner = scanner or DiskScanner()
        self.analyzer = analyzer or CacheAnalyzer(policy)
        self.duplicates = duplicates or DuplicateFinder()
        self.planner = planner or CleanupPlanner(policy, self.analyzer)
        self.audit = audit or AuditLogger()
        self.validator = validator or Validator()
        self.achievements = achievements or AchievementStore()
        self.context = context or ContextManager()
        self.llm = llm or MockLLM()
        self.router = router or IntentRouter()
        self.builder = PlanBuilder(policy)
        self.authority = authority or ConfirmationAuthority()
        self.plan_store = plan_store or PlanStore(Path.cwd() / ".disk_doctor-plans")
        self.executor = executor or SafeExecutor(
            policy,
            Send2TrashBackend(),
            audit=self.audit,
            authority=self.authority,
            cleanable=self.analyzer.is_cleanable,
        )
        self.registry = registry or self._build_registry()

    # -- construction ------------------------------------------------------ #

    @classmethod
    def create(
        cls,
        allowed_roots: list[str] | tuple[str, ...],
        *,
        home: str | os.PathLike[str] | None = None,
        trash: TrashBackend | None = None,
        quarantine_dir: str | os.PathLike[str] | None = None,
        llm: LLMClient | None = None,
    ) -> DiskDoctorAgent:
        policy = PolicyEngine(allowed_roots)
        analyzer = CacheAnalyzer(policy)

        if quarantine_dir is not None:
            backend: TrashBackend = QuarantineBackend(quarantine_dir)
        else:
            backend = trash or Send2TrashBackend()

        home_path = Path(home) if home else Path.home() / ".disk_doctor"
        audit = AuditLogger(home_path / "audit.jsonl")
        authority = ConfirmationAuthority()
        plan_store = PlanStore(home_path / "plans")
        agent = cls(
            policy=policy,
            analyzer=analyzer,
            llm=llm or MockLLM(),
            audit=audit,
            plan_store=plan_store,
            authority=authority,
            executor=SafeExecutor(
                policy,
                backend,
                audit=audit,
                authority=authority,
                cleanable=analyzer.is_cleanable,
            ),
            achievements=AchievementStore(home_path / "achievements.json"),
        )
        return agent

    def _build_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(
            "disk_scanner.scan",
            "读取磁盘用量、目录体积与大文件排名（只读）",
            self.scanner.scan,
            inputs={"root": "str", "top_n": "int", "dir_limit": "int"},
        )
        reg.register(
            "disk_scanner.top_files",
            "找出指定目录下最大的 N 个文件（只读）",
            self.scanner.top_files,
            inputs={"root": "str", "limit": "int", "min_size": "int"},
        )
        reg.register(
            "cache_analyzer.discover",
            "识别已知缓存目录并估算可释放空间（只读）",
            self.analyzer.discover,
            inputs={"roots": "list[str]"},
        )
        reg.register(
            "duplicate_finder.find",
            "按大小预筛 + SHA-256 查找重复文件（只读）",
            self.duplicates.find,
            inputs={"roots": "list[str]", "max_groups": "int"},
        )
        reg.register(
            "planner.plan",
            "生成清理计划（不写盘）",
            self.planner.plan,
            inputs={"root": "str", "mode": "str"},
        )
        reg.register(
            "achievements.summary",
            "读取累计清理统计",
            lambda: self.achievements.summary(),
        )
        # Deliberately absent: safe_executor.execute. Moving files is not a tool
        # the model can call; it is a user-driven transition (see execute_plan).
        reg.assert_read_only()
        return reg

    def capabilities(self) -> dict:
        return {
            "tools": self.registry.describe(),
            "write_tools": self.registry.write_tools(),
            "execution_entry": "agent.execute_plan(plan_id, confirmation)",
            "policy": self.policy.describe(),
            "llm": self.llm.describe(),
        }

    # -- public API -------------------------------------------------------- #

    def handle(
        self,
        text: str,
        *,
        scope: str | None = None,
        narrate: bool = False,
        limit: int = 10,
        allow_optional_cache: bool = False,
    ) -> AgentTurn:
        self.context.log("user", text)
        routed = self.router.route(text)
        self.context.log("routed", f"{routed.intent.value} matched={','.join(routed.matched)}")

        # An explicit scope from the caller (CLI flag) outranks anything parsed
        # out of prose: a path with spaces would otherwise be silently truncated.
        resolved_scope = (
            scope or routed.scope or self.context.get_slot(SCOPE_SLOT) or os.getcwd()
        )
        self.context.set_slot(SCOPE_SLOT, resolved_scope)

        turn = self._dispatch(
            routed,
            scope=resolved_scope,
            limit=limit,
            allow_optional_cache=allow_optional_cache,
        )

        if narrate:
            turn.message = self._narrate(turn, text)

        self.context.log("responded", turn.intent.value)
        return turn

    def _dispatch(
        self,
        routed: RoutedIntent,
        *,
        scope: str,
        limit: int,
        allow_optional_cache: bool,
    ) -> AgentTurn:
        intent = routed.intent

        if intent is Intent.OUT_OF_SCOPE:
            return self._refuse(routed)
        if intent is Intent.ACHIEVEMENTS:
            return self._achievements()
        if intent is Intent.TRASH_INFO:
            return self._trash_info()
        if intent is Intent.DIAGNOSE:
            return self._diagnose(scope)
        if intent is Intent.FIND_LARGE:
            return self._find_large(scope, limit=limit)
        if intent is Intent.FIND_DUPLICATE:
            return self._find_duplicates(scope)
        if intent.is_cleanup:
            mode = "quick" if intent is Intent.CLEAN_QUICK else "deep"
            return self._cleanup(
                scope,
                mode=mode,
                allow_optional_cache=allow_optional_cache,
            )
        return self._unknown(routed, scope)

    # -- intents ----------------------------------------------------------- #

    def _refuse(self, routed: RoutedIntent) -> AgentTurn:
        hint = {
            "破坏性磁盘操作不在能力范围内": "分区与格式化请使用系统自带的磁盘管理工具；本工具只做文件级清理。",
            "数据恢复不在能力范围内": "请立即停止对该磁盘的写入，然后使用专业恢复工具。继续扫描会降低恢复成功率。",
            "内容审查不在能力范围内": "我只能处理体积与路径元数据，不会读取或枚举你的私人文件内容。",
            "不请求提升权限": "我不会请求管理员权限。没有权限的目录会被直接跳过。",
        }.get(routed.reason, "该请求超出我的职责范围。")
        return AgentTurn(
            intent=Intent.OUT_OF_SCOPE,
            message=render_error(f"无法执行：{routed.reason}", hint=hint),
            data={"matched": list(routed.matched)},
        )

    def _achievements(self) -> AgentTurn:
        summary = self.achievements.summary()
        lines = [
            "🏆 清理成就（仅统计移入回收站的文件）",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"累计移入回收站：{summary['total_moved_human']}",
            f"累计文件：{summary['total_files']} 个",
            f"清理次数：{summary['runs']} 次",
        ]
        if summary["unlocked"]:
            lines += ["", "已解锁："] + [f"  · {t}" for t in summary["unlocked"]]
        else:
            lines += ["", "尚未解锁成就，完成一次真实清理即可。"]
        if summary["next_milestone"]:
            lines.append(
                f"下一个目标：{summary['next_milestone']}（还差 {summary['next_milestone_remaining']}）"
            )
        return AgentTurn(intent=Intent.ACHIEVEMENTS, message="\n".join(lines), data=summary)

    def _trash_info(self) -> AgentTurn:
        info = TrashManager(self.executor.trash).backend_info()
        ok = bool(info["available"])
        lines = [
            "🗑️ 回收站状态",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"后端：{info['backend']}",
            f"可用：{'是' if ok else '否'}",
        ]
        if not ok:
            lines += ["", "回收站不可用时，本工具会直接停止，不会退化为永久删除。"]
        return AgentTurn(
            intent=Intent.TRASH_INFO,
            message="\n".join(lines),
            data={"backend": info["backend"], "available": ok},
        )

    def _diagnose(self, scope: str) -> AgentTurn:
        root = Path(scope)
        if not root.exists():
            return AgentTurn(
                intent=Intent.DIAGNOSE,
                message=render_error(
                    f"路径不存在：{scope}", hint="确认盘符或目录是否正确。"
                ),
            )
        volume = self.scanner.volume(root)
        dirs = self.scanner.directory_sizes(root, limit=5) if root.is_dir() else []
        files = self.scanner.top_files(root, limit=5) if root.is_dir() else []
        diagnosis = build_diagnosis(volume, dirs, files)
        return AgentTurn(
            intent=Intent.DIAGNOSE,
            message=render_diagnosis(diagnosis),
            data=diagnosis.model_dump(mode="json"),
        )

    def _find_large(self, scope: str, *, limit: int) -> AgentTurn:
        root = Path(scope)
        if not root.is_dir():
            return AgentTurn(
                intent=Intent.FIND_LARGE,
                message=render_error(f"目录不存在：{scope}"),
            )
        files = self.scanner.top_files(root, limit=limit)
        if not files:
            return AgentTurn(
                intent=Intent.FIND_LARGE,
                message="没有找到可统计的文件。",
                data={"files": []},
            )
        lines = ["📄 最大的文件", "━━━━━━━━━━━━━━━━━━", ""]
        for idx, f in enumerate(files, 1):
            lines.append(f"  {idx:>2}. {human_bytes(f.size_bytes):>10}  {f.path}")
        lines += ["", "这些文件不会自动清理：单个大文件多为用户数据，需要你自行判断。"]
        return AgentTurn(
            intent=Intent.FIND_LARGE,
            message="\n".join(lines),
            data={"files": [f.model_dump(mode="json") for f in files]},
        )

    def _find_duplicates(self, scope: str) -> AgentTurn:
        root = Path(scope)
        if not root.is_dir():
            return AgentTurn(
                intent=Intent.FIND_DUPLICATE,
                message=render_error(f"目录不存在：{scope}"),
            )
        groups = self.duplicates.find([root], policy=self.policy)
        if not groups:
            return AgentTurn(
                intent=Intent.FIND_DUPLICATE,
                message="没有发现重复文件。",
                data={"groups": []},
            )
        total = sum(g.wasted_bytes for g in groups)
        lines = [
            "🔁 重复文件",
            "━━━━━━━━━━━━━━━━━━",
            "",
            f"发现 {len(groups)} 组重复，冗余约 {human_bytes(total)}",
            "",
        ]
        for idx, g in enumerate(groups[:10], 1):
            lines.append(f"  {idx}. {human_bytes(g.size_bytes)} × {len(g.paths)} 份")
            for p in g.paths:
                lines.append(f"       {p}")
        lines += ["", "⚠️ 重复不等于可删。本工具不会自动删除重复文件。"]
        return AgentTurn(
            intent=Intent.FIND_DUPLICATE,
            message="\n".join(lines),
            data={"groups": [g.model_dump(mode="json") for g in groups], "wasted_bytes": total},
        )

    def _cleanup(
        self,
        scope: str,
        *,
        mode: str,
        allow_optional_cache: bool = False,
    ) -> AgentTurn:
        """Build a plan and persist it. Never touches the filesystem."""

        intent = Intent.CLEAN_QUICK if mode == "quick" else Intent.CLEAN_DEEP
        root = Path(scope)
        if not root.exists() or not root.is_dir():
            return AgentTurn(
                intent=intent,
                message=render_error(
                    f"目录不存在：{scope}",
                    hint="清理必须指定一个真实存在的目录。",
                ),
            )

        verdict = self.policy.verdict(root)
        if not verdict.allowed:
            return AgentTurn(
                intent=intent,
                message=render_error(
                    f"该目录不在授权范围内：{scope}",
                    detail=verdict.reason,
                    hint="出于安全考虑，只处理通过 --allow-root 显式授权的目录。",
                ),
                data={"rule": verdict.rule},
            )

        candidates = self.analyzer.discover(
            [str(root)], allow_optional=allow_optional_cache
        )
        if mode == "quick":
            candidates = [c for c in candidates if c.risk is RiskLevel.SAFE]

        vetoed = {
            os.path.normcase(str(self.policy.normalize(v)))
            for v in self.context.vetoed_keys()
        }
        candidates = [
            c
            for c in candidates
            if os.path.normcase(str(self.policy.normalize(c.path))) not in vetoed
        ]

        plan = self.builder.build(
            str(root),
            candidates,
            notes=[
                f"模式：{'快速（仅安全项）' if mode == 'quick' else '深度（含需注意项）'}"
            ],
        )
        self.context.store_plan(plan)

        try:
            self.plan_store.save(plan)
        except PlanStoreError as exc:
            return AgentTurn(
                intent=intent,
                message=render_error(
                    "计划无法保存",
                    detail=str(exc),
                    hint="检查应用数据目录是否可写。",
                ),
                data={"error": exc.code},
            )

        return AgentTurn(
            intent=intent,
            message=render_preview(plan),
            data=plan.model_dump(mode="json"),
            awaiting_confirmation=bool(plan.items),
            plan_id=plan.plan_id,
        )

    def execute_plan(
        self,
        plan_id: str,
        *,
        confirmation: str,
        second_confirmation: str | None = None,
        narrate: bool = False,
    ) -> AgentTurn:
        """Execute a previously stored plan after exact-id confirmation."""

        try:
            plan = self.plan_store.load(plan_id)
        except PlanStoreError as exc:
            return AgentTurn(
                intent=Intent.CLEAN_QUICK,
                message=render_error(
                    "无法执行该计划",
                    detail=str(exc),
                    hint="请重新生成计划，并使用新的完整 plan_id。",
                ),
                data={"error": exc.code},
            )

        summary = render_confirmation_summary(plan)

        try:
            receipt = self.authority.issue(
                plan,
                confirmation=confirmation,
                second_confirmation=second_confirmation,
            )
        except ApprovalError as exc:
            return AgentTurn(
                intent=Intent.CLEAN_QUICK,
                message=render_error(
                    "确认未通过",
                    detail=exc.detail or str(exc),
                    hint=f"必须完整输入：{self.authority.execute_token(plan.plan_id)}",
                ),
                data={"error": exc.code},
            )

        try:
            with self.plan_store.claim_for_execution(plan_id) as claim:
                plan = claim.plan
                result = self.executor.execute(plan, receipt)
                claim.finish(
                    ExecutionState.FAILED
                    if result.blocked_reason
                    else ExecutionState.COMPLETED
                )
        except PlanStoreError as exc:
            return AgentTurn(
                intent=Intent.CLEAN_QUICK,
                message=render_error(
                    "无法执行该计划",
                    detail=str(exc),
                    hint="同一计划只能执行一次；请重新扫描并生成新计划。",
                ),
                data={"error": exc.code},
            )

        verification = self.validator.verify(plan, result)

        unlocked: list[str] = []
        if result.bytes_moved_to_trash > 0:
            unlocked = self.achievements.record(
                result.bytes_moved_to_trash, result.success_count
            )

        message = render_completion(
            result,
            verification,
            achievements=self.achievements.summary(),
            unlocked=unlocked,
        )
        turn = AgentTurn(
            intent=Intent.CLEAN_QUICK,
            message=summary + "\n\n" + message,
            data={
                "plan": plan.model_dump(mode="json"),
                "result": result.model_dump(mode="json"),
                "verification": verification.model_dump(mode="json"),
            },
            plan_id=plan.plan_id,
        )
        if narrate:
            turn.message = self._narrate(turn, "执行计划")
        return turn

    def _unknown(self, routed: RoutedIntent, scope: str) -> AgentTurn:
        lines = [
            "我不太确定你想做什么。可以说：",
            "",
            "  · 看看我的磁盘状况        → 生成诊断报告",
            "  · 帮我清理一下            → 生成清理计划（不会直接执行）",
            "  · 找找大文件 / 重复文件   → 只读扫描",
            "  · 我的成就                → 累计统计",
            "",
            f"当前范围：{scope}",
        ]
        return AgentTurn(
            intent=Intent.UNKNOWN,
            message="\n".join(lines),
            data={"matched": list(routed.matched)},
        )

    # -- extras ------------------------------------------------------------ #

    def veto(self, path: str, reason: str = "") -> None:
        """Remember that a path must never enter a plan again."""

        self.context.record_veto(str(self.policy.normalize(path)), reason)

    def _narrate(self, turn: AgentTurn, user_text: str) -> str:
        """Append a model-written comment. Never replaces measured output."""

        try:
            comment = self.llm.complete(
                system="你是磁盘健康助手，用简洁中文解释下面这份结果的重点。不要改动任何数字。",
                user=f"用户诉求：{user_text}\n\n结构化结果：\n{turn.message}",
            )
        except LLMError as exc:
            return turn.message + f"\n\n（模型不可用：{exc}）"
        return turn.message + f"\n\n🤖 说明（{self.llm.name}）：{comment}"
