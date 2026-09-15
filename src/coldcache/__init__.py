"""Find the GitHub Actions caches that never hit."""

from .core import (
    FROZEN_KEY,
    KEY_COLLISION,
    NEVER_HITS,
    NEVER_ON_DEFAULT_BRANCH,
    NOTHING_SAVES_THIS,
    RESTORE_KEY_DEAD,
    SAVE_NEVER_RESTORED,
    Finding,
    Report,
    check,
    scan,
)
from .globs import Tree

__all__ = [
    "FROZEN_KEY",
    "KEY_COLLISION",
    "NEVER_HITS",
    "NEVER_ON_DEFAULT_BRANCH",
    "NOTHING_SAVES_THIS",
    "RESTORE_KEY_DEAD",
    "SAVE_NEVER_RESTORED",
    "Finding",
    "Report",
    "Tree",
    "check",
    "scan",
]

__version__ = "0.1.0"
