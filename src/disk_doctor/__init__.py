"""Disk Doctor Reference — a clean-room, safety-first disk health agent.

v0.2.3 closes two release blockers that were about *where* a check happens:
``reconcile_interrupted`` now re-reads state while holding the plan lock, and
every scan entry refuses a root that is itself a symlink or reparse point.

Inherited from earlier versions: one write entry point (``SafeExecutor``), one
execution owner per plan (cross-process claim), receipts burned before the first
byte moves, and a shared symlink / reparse-point guard.

This package is an independent implementation. It is reconstructed from the
*publicly observable* behaviour of a commercial expert product, not from any
private configuration, prompt text, or source code.
"""

__version__ = "0.2.3"
__all__ = ["__version__"]
