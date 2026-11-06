"""Capability modules exposed by the agent."""

from .achievements import MILESTONES, AchievementStore, Milestone
from .base import Tool, ToolSpec
from .cache_analyzer import CacheAnalyzer, Signature
from .disk_scanner import DiskScanner
from .duplicate_finder import DuplicateFinder
from .registry import ToolNotFound, ToolRegistry
from .trash_manager import TrashManager

__all__ = [
    "MILESTONES",
    "AchievementStore",
    "CacheAnalyzer",
    "DiskScanner",
    "DuplicateFinder",
    "Milestone",
    "Signature",
    "Tool",
    "ToolNotFound",
    "ToolRegistry",
    "ToolSpec",
    "TrashManager",
]
