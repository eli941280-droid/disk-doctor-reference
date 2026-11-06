"""Command-line interface.

v0.2.1 replaces the single-command flow with two phases:

    disk-doctor plan    --root ... --allow-root ...
    disk-doctor execute --plan-id <64 hex> --allow-root ...

Why: ``--yes`` cannot express *which* file list was approved. The two-phase
form makes the approved artefact an explicit, persisted object, and requires the
user to type the complete plan id back.

``execute`` refuses to run when stdin is not an interactive terminal. There is
deliberately no non-interactive bypass: an approval that can be piped in is not
an approval.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .agent.orchestrator import DiskDoctorAgent
from .core.clean_plan import PlanStoreError
from .core.utils import human_bytes
from .llm import LLMConfigError, build_llm
from .reporting.preview import render_confirmation_summary
from .tools.duplicate_finder import DuplicateFinder
from .tools.trash_manager import TrashManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="disk-doctor",
        description="磁盘健康诊断与安全清理 Agent（参考实现）。",
    )
    parser.add_argument("--version", action="version", version=f"disk-doctor {__version__}")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "mock", "openai", "deepseek"],
        help="语言模型服务商；auto 有 Key 就用真实模型，否则用本地 mock（默认）",
    )
    parser.add_argument("--home", default=None, help="状态、计划与审计日志目录")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--narrate", action="store_true", help="追加一段模型生成的说明")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("diagnose", help="磁盘健康诊断（只读）")
    p.add_argument("--path", default=None, help="要检查的位置，默认当前目录")

    p = sub.add_parser("scan", help="目录体积与大文件排名（只读）")
    p.add_argument("--root", default=".")
    p.add_argument("--top", type=int, default=10)

    p = sub.add_parser("dupes", help="查找重复文件（只读）")
    p.add_argument("--root", default=".")
    p.add_argument("--min-size", type=int, default=1024, help="最小字节数，默认 1024")

    p = sub.add_parser("plan", help="生成清理计划（不执行）")
    p.add_argument("--root", required=True)
    p.add_argument("--allow-root", action="append", default=[], help="授权根目录，可重复")
    p.add_argument("--mode", choices=["quick", "deep"], default="quick")
    p.add_argument(
        "--allow-optional-cache",
        action="store_true",
        help="显式启用可选缓存类别（node_modules / npm 缓存），始终为 CAUTION",
    )

    p = sub.add_parser("execute", help="执行已保存的计划（需要交互式确认）")
    p.add_argument("--plan-id", required=True, help="完整的 64 位 plan_id")
    p.add_argument("--allow-root", action="append", default=[])
    p.add_argument("--quarantine", default=None, help="把文件移入该目录而不是回收站")

    p = sub.add_parser("clean", help="已弃用：等价于 plan，不会执行任何操作")
    p.add_argument("--root", required=True)
    p.add_argument("--allow-root", action="append", default=[])
    p.add_argument("--mode", choices=["quick", "deep"], default="quick")
    p.add_argument("--no-dry-run", action="store_true", help="已弃用且被忽略")
    p.add_argument("--yes", action="store_true", help="已弃用且被忽略")

    p = sub.add_parser("ask", help="用自然语言发起请求（只生成计划）")
    p.add_argument("text", nargs="+")
    p.add_argument("--allow-root", action="append", default=[])
    p.add_argument("--quarantine", default=None)

    sub.add_parser("trash", help="查看回收站后端状态")
    sub.add_parser("achievements", help="查看累计清理成就")
    sub.add_parser("tools", help="列出全部能力与安全策略")
    return parser


def _make_agent(args, allowed_roots: list[str], *, quarantine: str | None = None) -> DiskDoctorAgent:
    try:
        llm = build_llm(args.provider)
    except LLMConfigError:
        llm = None
    return DiskDoctorAgent.create(
        allowed_roots,
        home=args.home,
        quarantine_dir=quarantine,
        llm=llm,
    )


def _is_authorized(root: str, allowed: list[str]) -> bool:
    """Prefix check with a separator, so ``D:\\foo`` never authorises ``D:\\foobar``."""

    key = os.path.normcase(os.path.abspath(root))
    for entry in allowed:
        base = os.path.normcase(os.path.abspath(entry))
        if key == base or key.startswith(base + os.sep):
            return True
    return False


def _require_authorization(root: str, allowed: list[str]) -> bool:
    if _is_authorized(root, allowed):
        return True
    print(
        "拒绝执行：目标目录不在授权范围内。\n"
        f"  目标：{root}\n"
        "  请用 --allow-root 显式授权该目录后再试。",
        file=sys.stderr,
    )
    return False


def _emit(args, turn) -> int:
    if args.json:
        print(json.dumps(turn.data, ensure_ascii=False, indent=2))
    else:
        print(turn.message)
    if turn.intent.value == "out_of_scope":
        return 1
    return 0


def _run_plan(args, *, allow_optional: bool) -> int:
    allowed = args.allow_root or []
    root = os.path.abspath(args.root)
    if not _require_authorization(root, allowed):
        return 2
    agent = _make_agent(args, allowed)
    word = "深度清理" if args.mode == "deep" else "清理"
    turn = agent.handle(
        f"{word} {root}",
        scope=root,
        narrate=args.narrate,
        allow_optional_cache=allow_optional,
    )
    return _emit(args, turn)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cwd = os.getcwd()

    if args.command == "tools":
        agent = _make_agent(args, [cwd])
        caps = agent.capabilities()
        if args.json:
            print(json.dumps(caps, ensure_ascii=False, indent=2))
        else:
            print("可用能力")
            print("━" * 18)
            for tool in caps["tools"]:
                flag = "只读" if tool["read_only"] else f"写操作/{tool['danger']}"
                print(f"  {tool['name']:<32} [{flag}]  {tool['summary']}")
            print("\n授权根目录：")
            for root in caps["policy"]["allowed_roots"]:
                print(f"  · {root}")
            print(f"\n模型：{caps['llm']}")
        return 0

    if args.command == "trash":
        manager = TrashManager()
        info = manager.backend_info()
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"回收站后端：{info['backend']}\n可用：{'是' if info['available'] else '否'}")
            print(info["reason"])
        return 0 if info["available"] else 1

    if args.command == "dupes":
        finder = DuplicateFinder(min_size=args.min_size)
        groups = finder.find([args.root])
        total = sum(g.wasted_bytes for g in groups)
        if args.json:
            print(
                json.dumps(
                    {
                        "groups": [g.model_dump(mode="json") for g in groups],
                        "wasted_bytes": total,
                        "wasted_human": human_bytes(total),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"发现 {len(groups)} 组重复，冗余约 {human_bytes(total)}")
            for idx, g in enumerate(groups[:10], 1):
                print(f"  {idx}. {human_bytes(g.size_bytes)} × {len(g.paths)}")
                for path in g.paths:
                    print(f"       {path}")
        return 0

    if args.command == "scan":
        agent = _make_agent(args, [cwd])
        volume, dirs, files = agent.scanner.scan(args.root, top_n=args.top)
        if args.json:
            print(
                json.dumps(
                    {
                        "volume": volume.model_dump(mode="json"),
                        "directories": [d.model_dump(mode="json") for d in dirs],
                        "files": [f.model_dump(mode="json") for f in files],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        print(f"位置：{volume.path}")
        print(
            f"总 {human_bytes(volume.total_bytes)} / 已用 {human_bytes(volume.used_bytes)} "
            f"({volume.percent_used:.0f}%) / 剩余 {human_bytes(volume.free_bytes)}"
        )
        if dirs:
            print("\n目录占用：")
            for d in dirs:
                print(f"  {human_bytes(d.size_bytes):>10}  {d.path}")
        if files:
            print("\n最大文件：")
            for f in files:
                print(f"  {human_bytes(f.size_bytes):>10}  {f.path}")
        return 0

    if args.command == "diagnose":
        agent = _make_agent(args, [cwd])
        target = os.path.abspath(args.path) if args.path else cwd
        turn = agent.handle("看看磁盘状况", scope=target, narrate=args.narrate)
        return _emit(args, turn)

    if args.command == "achievements":
        agent = _make_agent(args, [cwd])
        return _emit(args, agent.handle("我的成就"))

    if args.command == "plan":
        return _run_plan(args, allow_optional=args.allow_optional_cache)

    if args.command == "clean":
        print(
            "提示：clean 已弃用，本命令只生成计划，不会执行任何文件移动。\n"
            "      要真正执行，请使用：disk-doctor execute --plan-id <plan_id> --allow-root <目录>",
            file=sys.stderr,
        )
        return _run_plan(args, allow_optional=False)

    if args.command == "execute":
        return _run_execute(args, parser)

    if args.command == "ask":
        text = " ".join(args.text)
        allowed = args.allow_root or [cwd]
        agent = _make_agent(args, allowed, quarantine=args.quarantine)
        routed = agent.router.route(text)
        target = routed.scope or (allowed[0] if allowed else None)
        turn = agent.handle(text, scope=target, narrate=args.narrate)
        return _emit(args, turn)

    parser.print_help()
    return 2


def _run_execute(args, parser: argparse.ArgumentParser) -> int:
    if not sys.stdin.isatty():
        print(
            "拒绝执行：确认必须在交互式终端中完成。\n"
            "  非交互环境下不提供任何绕过确认的路径。",
            file=sys.stderr,
        )
        return 2

    allowed = args.allow_root or []
    if not allowed:
        print("拒绝执行：execute 需要 --allow-root 指定授权目录。", file=sys.stderr)
        return 2

    agent = _make_agent(args, allowed, quarantine=args.quarantine)

    try:
        plan = agent.plan_store.load(args.plan_id)
    except PlanStoreError as exc:
        print(f"无法加载计划：{exc}", file=sys.stderr)
        return 2

    if not _require_authorization(plan.root, allowed):
        return 2

    print(render_confirmation_summary(plan))
    print()
    print(f"文件将移入回收站，共 {plan.total_files} 个文件。")
    print(f"要确认执行，请完整输入：{agent.authority.execute_token(plan.plan_id)}")

    typed = input("> ").strip()
    second = None
    if plan.requires_second_confirm:
        print(f"该计划超过阈值，需要二次确认。请完整输入：{agent.authority.second_token(plan.plan_id)}")
        second = input("> ").strip()

    turn = agent.execute_plan(
        args.plan_id,
        confirmation=typed,
        second_confirmation=second,
        narrate=args.narrate,
    )
    return _emit(args, turn)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
