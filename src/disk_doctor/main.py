"""Module entry point: ``python -m disk_doctor``."""

from __future__ import annotations

import sys

from .cli import main


def run() -> int:
    return main(sys.argv[1:])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
