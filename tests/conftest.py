"""测试共用夹具路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture() -> callable:
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8", errors="ignore")

    return _read