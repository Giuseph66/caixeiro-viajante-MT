"""Start experiment runners inside a hard cgroup memory limit."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.memory_limit import MAX_MEMORY_BYTES, enforce_memory_limit
