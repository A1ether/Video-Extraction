"""版本号只有一个事实来源：pyproject.toml。

此前 `__init__.__version__` 停在 0.1.0、FastAPI 里硬编码 0.3.0，两处都能悄悄漂移。
这里把"包版本 / API 版本 / pyproject 版本"钉成同一个值。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import vextract
from vextract.api.app import app


def _pyproject_version() -> str:
    root = Path(__file__).resolve().parent.parent
    with (root / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_versions_do_not_drift() -> None:
    expected = _pyproject_version()
    assert vextract.__version__ == expected
    assert app.version == expected
