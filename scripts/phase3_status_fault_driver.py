"""从未安装源码树运行正式的 Phase 3 status fault driver。"""

from __future__ import annotations

from pathlib import Path
import sys


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _source_root in (
    _REPOSITORY_ROOT / "apps/core/src",
    _REPOSITORY_ROOT / "apps/status_provider/src",
    _REPOSITORY_ROOT / "packages/contracts/src",
    _REPOSITORY_ROOT / "packages/provider_sdk/src",
):
    if str(_source_root) not in sys.path:
        sys.path.insert(0, str(_source_root))

from superlily_core.phase3_status_fault_driver import main


if __name__ == "__main__":
    raise SystemExit(main())
