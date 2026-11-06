"""Path safety policy — the single gate every destructive action must pass.

Design notes (and why this module looks the way it does):

1. The policy is a *whitelist*. A path is only allowed if it sits underneath a
   root the caller explicitly authorised. Nothing is allowed "unless it looks
   dangerous" — that inversion is how cleaners end up deleting things.
2. Containment is decided by walking path components (``os.path.relpath``),
   never by string prefix. ``D:\\work`` must not authorise ``D:\\workshop``.
3. **Every directory between the required root and the target is inspected.**
   A symlink or Windows junction placed on a *parent* directory redirects the
   whole subtree somewhere else. Checking only the file itself misses that
   entirely (v0.2.1, P0-1).

Trust boundary, stated explicitly: the ancestors of ``required_root`` are
*outside* the tool's scope. If a caller authorises a root that is itself
reached through a redirected profile directory, that is the caller's context,
not an escape discovered by this tool. What is enforced is that nothing
*below* the root redirects anywhere, and that the fully resolved target stays
inside the fully resolved root.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Sequence
from pathlib import Path

from .schemas import POLICY_VERSION, ChainVerdict, PolicyVerdict, RiskLevel
from .utils import is_reparse_point, is_symlink_or_reparse

# Exact path components that are never touched, regardless of allow-list.
PROTECTED_COMPONENTS: frozenset[str] = frozenset(
    {
        # Windows
        "windows",
        "system32",
        "syswow64",
        "winsxs",
        "program files",
        "program files (x86)",
        "programdata",
        "$recycle.bin",
        "system volume information",
        "recovery",
        "msocache",
        "perflogs",
        "boot",
        "efi",
        # POSIX
        "system",
        "library",
        "usr",
        "bin",
        "sbin",
        "etc",
        "private",
        "applications",
        "cores",
        # Credentials
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
    }
)

# File types treated as irreplaceable user data rather than junk.
PROTECTED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".key",
        ".pem",
        ".pfx",
        ".p12",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".pdf",
        ".pages",
        ".numbers",
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".tif",
        ".tiff",
        ".raw",
        ".cr2",
        ".nef",
        ".psd",
        ".ai",
        ".sketch",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".mdb",
        ".kdbx",
        ".env",
        ".bak",
        # archives / media / code are protected by default
        ".zip",
        ".7z",
        ".rar",
        ".tar",
        ".gz",
        ".mp4",
        ".mov",
        ".mkv",
        ".py",
        ".js",
        ".ts",
        ".go",
        ".rs",
        ".java",
    }
)

PROTECTED_NAMES: frozenset[str] = frozenset(
    {"ntuser.dat", "pagefile.sys", "hiberfil.sys", "swapfile.sys", "bootmgr"}
)

# ``is_reparse_point`` and ``is_symlink_or_reparse`` are imported from
# ``core.utils`` and re-exported here: v0.2.2 gave the project one
# implementation instead of three slightly different ones.
__all__ = [
    "PROTECTED_COMPONENTS",
    "PROTECTED_NAMES",
    "PROTECTED_SUFFIXES",
    "PolicyEngine",
    "is_reparse_point",
    "is_symlink_or_reparse",
    "is_within",
]


def is_within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    """Component-wise containment. Different volumes fail closed."""

    try:
        rel = os.path.relpath(
            os.path.normcase(str(child)), os.path.normcase(str(parent))
        )
    except ValueError:
        return False
    if rel == os.curdir:
        return True
    return not (rel == os.pardir or rel.startswith(os.pardir + os.sep))


class PolicyEngine:
    """Decides whether a path may be modified.

    Parameters
    ----------
    allowed_roots:
        Directories the user has explicitly authorised. Empty is invalid.
    protected_components / protected_suffixes / protected_names:
        Overrides for tests or for platform-specific policy.
    """

    version = POLICY_VERSION

    def __init__(
        self,
        allowed_roots: Sequence[str | os.PathLike[str]],
        *,
        protected_components: Iterable[str] | None = None,
        protected_suffixes: Iterable[str] | None = None,
        protected_names: Iterable[str] | None = None,
    ) -> None:
        roots: list[Path] = []
        for raw in allowed_roots:
            p = self.normalize(raw)
            if p not in roots:
                roots.append(p)
        if not roots:
            raise ValueError("PolicyEngine requires at least one allowed root")

        self._allowed_roots: tuple[Path, ...] = tuple(roots)
        self._protected_components = frozenset(
            c.lower() for c in (protected_components or PROTECTED_COMPONENTS)
        )
        self._protected_suffixes = frozenset(
            s.lower() for s in (protected_suffixes or PROTECTED_SUFFIXES)
        )
        self._protected_names = frozenset(
            n.lower() for n in (protected_names or PROTECTED_NAMES)
        )

    # -- path helpers ------------------------------------------------------ #

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._allowed_roots

    @staticmethod
    def normalize(raw: str | os.PathLike[str]) -> Path:
        """Absolute, variable-expanded, non-resolving normal form."""

        text = os.path.expandvars(os.path.expanduser(str(raw)))
        p = Path(text)
        if not p.is_absolute():
            p = Path.cwd() / p
        return Path(os.path.abspath(p))

    @staticmethod
    def _key(p: str | os.PathLike[str]) -> str:
        return os.path.normcase(str(p))

    @staticmethod
    def volume_of(p: str | os.PathLike[str]) -> str:
        drive, _ = os.path.splitdrive(os.path.abspath(str(p)))
        return os.path.normcase(drive)

    def contains(self, path: str | os.PathLike[str]) -> bool:
        """True if *path* is one of, or nested under, an allowed root."""

        key = self.normalize(path)
        return any(is_within(key, root) for root in self._allowed_roots)

    def contains_in(
        self, path: str | os.PathLike[str], root: str | os.PathLike[str]
    ) -> bool:
        """Containment against one specific root, not "any allowed root"."""

        return is_within(self.normalize(path), self.normalize(root))

    def same_volume(self, a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
        return self.volume_of(a) == self.volume_of(b)

    def is_filesystem_root(self, path: Path) -> bool:
        return path.parent == path

    # -- the chain walk (P0-1) --------------------------------------------- #

    def validate_path_chain(
        self,
        path: str | os.PathLike[str],
        required_root: str | os.PathLike[str],
    ) -> ChainVerdict:
        """Walk every component between *required_root* and *path*.

        Rejects if any component is a symlink or reparse point, if the pair
        spans volumes, or if the fully resolved target escapes the fully
        resolved root.
        """

        try:
            root = self.normalize(required_root)
            target = self.normalize(path)
        except (OSError, ValueError) as exc:  # pragma: no cover - exotic input
            return ChainVerdict(
                allowed=False, rule="unparsable", reason=f"路径无法解析：{exc}"
            )

        if not self.same_volume(root, target):
            return ChainVerdict(
                allowed=False,
                rule="cross_volume",
                reason="目标与授权根不在同一卷",
                resolved_root=str(root),
                resolved_target=str(target),
            )

        if self.is_filesystem_root(root):
            return ChainVerdict(
                allowed=False,
                rule="filesystem_root",
                reason="拒绝把磁盘根目录作为授权根",
            )

        if not root.exists() or not root.is_dir():
            return ChainVerdict(
                allowed=False, rule="root_missing", reason=f"授权根不存在：{root}"
            )

        if os.path.islink(root):
            return ChainVerdict(
                allowed=False,
                rule="symlink_root",
                reason="授权根本身是符号链接",
                resolved_root=str(os.path.realpath(root)),
                resolved_target=str(target),
            )

        if is_reparse_point(root):
            return ChainVerdict(
                allowed=False,
                rule="reparse_root",
                reason="授权根本身是重解析点（Junction）",
            )

        if not is_within(target, root):
            return ChainVerdict(
                allowed=False,
                rule="outside_required_root",
                reason=f"目标不在本次授权根 {root} 内",
                resolved_root=str(root),
                resolved_target=str(target),
            )

        # Walk each component strictly below the root.
        try:
            rel_parts = Path(os.path.relpath(target, root)).parts
        except ValueError:  # pragma: no cover - covered by volume check above
            return ChainVerdict(
                allowed=False, rule="cross_volume", reason="无法计算相对路径"
            )

        current = root
        for part in rel_parts:
            if part in (os.curdir,):
                continue
            current = current / part
            is_target = self._key(current) == self._key(target)

            if os.path.islink(current):
                return ChainVerdict(
                    allowed=False,
                    rule="symlink_in_path",
                    reason=f"路径中存在符号链接：{current}",
                    resolved_root=str(root),
                    resolved_target=str(target),
                )
            if is_reparse_point(current):
                return ChainVerdict(
                    allowed=False,
                    rule="reparse_in_path",
                    reason=f"路径中存在重解析点：{current}",
                    resolved_root=str(root),
                    resolved_target=str(target),
                )

            # Intermediate directories must exist: a missing parent means the
            # path is not what it claims to be. The *target* may legitimately be
            # absent at plan time — the executor checks that separately, and a
            # file deleted between scan and plan is a normal race, not an escape.
            if not is_target and not current.exists():
                return ChainVerdict(
                    allowed=False,
                    rule="missing_component",
                    reason=f"路径组件不存在：{current}",
                    resolved_root=str(root),
                    resolved_target=str(target),
                )

        resolved_root = Path(os.path.realpath(root))
        resolved_target = Path(os.path.realpath(target))

        if not is_within(resolved_target, resolved_root):
            return ChainVerdict(
                allowed=False,
                rule="resolved_outside_root",
                reason="解析后的真实路径落在本次授权根之外",
                resolved_root=str(resolved_root),
                resolved_target=str(resolved_target),
            )

        return ChainVerdict(
            allowed=True,
            rule="allow",
            reason="路径链校验通过",
            resolved_root=str(resolved_root),
            resolved_target=str(resolved_target),
        )

    # -- file shape -------------------------------------------------------- #

    @staticmethod
    def inspect_file(path: str | os.PathLike[str]) -> tuple[bool, str]:
        """Is this a plain, regular, non-redirecting file we may move?"""

        try:
            info = os.lstat(path)
        except OSError as exc:
            return False, f"missing_or_unreadable:{exc.__class__.__name__}"
        if stat.S_ISLNK(info.st_mode):
            return False, "symlink"
        if is_reparse_point(path):
            return False, "reparse_point"
        if not stat.S_ISREG(info.st_mode):
            return False, "not_regular_file"
        return True, "ok"

    # -- the gate ---------------------------------------------------------- #

    def verdict(
        self,
        raw: str | os.PathLike[str],
        *,
        required_root: str | os.PathLike[str] | None = None,
    ) -> PolicyVerdict:
        try:
            p = self.normalize(raw)
        except (OSError, ValueError) as exc:  # pragma: no cover - exotic input
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="unparsable",
                reason=f"路径无法解析：{exc}",
            )

        if self.is_filesystem_root(p):
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="filesystem_root",
                reason="拒绝操作磁盘根目录",
            )

        if os.path.islink(p):
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="symlink_denied",
                reason="符号链接一律跳过，避免指向允许范围之外",
            )

        if is_reparse_point(p):
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="reparse_point_denied",
                reason="Windows 重解析点（Junction）一律跳过",
            )

        resolved = Path(os.path.realpath(p))
        if self._key(resolved) != self._key(p) and not self.contains(resolved):
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="escape_via_link",
                reason="解析后的真实路径落在白名单之外",
            )

        for part in p.parts:
            name = part.lower().rstrip("\\/")
            if name in self._protected_components:
                return PolicyVerdict(
                    allowed=False,
                    risk=RiskLevel.FORBIDDEN,
                    rule="protected_component",
                    reason=f"路径包含受保护目录：{part}",
                )

        if p.name.lower() in self._protected_names:
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="protected_name",
                reason=f"受保护的系统文件：{p.name}",
            )

        if p.suffix.lower() in self._protected_suffixes:
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="protected_suffix",
                reason=f"受保护的文件类型：{p.suffix}",
            )

        # Containment is checked against the *required* root when one is given.
        scope_ok = (
            self.contains_in(p, required_root)
            if required_root is not None
            else self.contains(p)
        )
        if not scope_ok:
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="outside_allowed_roots",
                reason="不在用户授权的目录范围内",
            )

        if required_root is not None:
            chain = self.validate_path_chain(p, required_root)
            if not chain.allowed:
                return PolicyVerdict(
                    allowed=False,
                    risk=RiskLevel.FORBIDDEN,
                    rule=chain.rule,
                    reason=chain.reason,
                )

        if not self.contains(resolved):
            return PolicyVerdict(
                allowed=False,
                risk=RiskLevel.FORBIDDEN,
                rule="resolved_outside_roots",
                reason="真实路径不在授权范围内",
            )

        return PolicyVerdict(
            allowed=True, risk=RiskLevel.SAFE, rule="allow", reason="通过白名单校验"
        )

    def allows(
        self,
        raw: str | os.PathLike[str],
        *,
        required_root: str | os.PathLike[str] | None = None,
    ) -> bool:
        return self.verdict(raw, required_root=required_root).allowed

    def describe(self) -> dict[str, object]:
        """Machine-readable summary, used by docs generation and ``tools``."""

        return {
            "version": self.version,
            "allowed_roots": [str(r) for r in self._allowed_roots],
            "protected_components": sorted(self._protected_components),
            "protected_suffixes": sorted(self._protected_suffixes),
            "protected_names": sorted(self._protected_names),
        }
