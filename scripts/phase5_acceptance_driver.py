"""Run the Phase 5 acceptance driver from an uninstalled source checkout."""

from __future__ import annotations

from pathlib import Path
import sys


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _source_root in (
    _REPOSITORY_ROOT / "apps/core/src",
    _REPOSITORY_ROOT / "apps/model_provider/src",
    _REPOSITORY_ROOT / "packages/contracts/src",
):
    if str(_source_root) not in sys.path:
        sys.path.insert(0, str(_source_root))

from superlily_core.phase5_acceptance_driver import main


if __name__ == "__main__":
    raise SystemExit(main())
