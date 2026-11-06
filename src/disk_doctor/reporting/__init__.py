"""Report rendering: diagnosis, cleanup preview, completion, errors."""

from .completion import render_completion, render_error
from .diagnosis import build_diagnosis, health_score, render_diagnosis, score_verdict
from .preview import render_preview

__all__ = [
    "build_diagnosis",
    "health_score",
    "render_completion",
    "render_diagnosis",
    "render_error",
    "render_preview",
    "score_verdict",
]
