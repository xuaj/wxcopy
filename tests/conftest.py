"""pytest 公共夹具。

项目是 editable 安装的，正常情况下 `import wxcopy` 直接可用；
这里再加一层 src 兜底，方便没装包、直接跑源码的场景。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
