"""版本号一致性：`wxcopy version` 报的必须是真版本。

这里之所以单拉一个文件，是因为踩过：`__init__.py` 里手写的常量常年停在 0.2.0，
而 pyproject 已经到 0.4.0 了，谁都没发现。现在版本号从安装元数据读，
这个用例负责把「元数据和 pyproject 一致」钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import wxcopy

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - 3.10
    tomllib = None

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_is_not_empty():
    assert wxcopy.__version__
    assert wxcopy.__version__ != "0.0.0+source", "本目录是装了包的，不该走到源码兜底分支"


@pytest.mark.skipif(tomllib is None, reason="需要 Python 3.11+ 的 tomllib")
def test_version_matches_pyproject():
    with open(PYPROJECT, "rb") as fh:
        want = tomllib.load(fh)["project"]["version"]
    assert wxcopy.__version__ == want, (
        f"包元数据是 {wxcopy.__version__}，pyproject 是 {want}——多半是改了 pyproject 没重装（pip install -e . ）"
    )
