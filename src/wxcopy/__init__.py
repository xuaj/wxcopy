"""wxcopy - 视频号对标素材处理流水线。"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # 单一事实来源：版本号只写在 pyproject.toml 里。
    # 这里曾经手写过一个常量，结果 pyproject 升到 0.4.0 了它还是 0.2.0，
    # `wxcopy version` 一直在报错的版本号。
    __version__ = _pkg_version("wxcopy")
except PackageNotFoundError:  # 源码直跑、没 pip install
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
