"""统一日志：rich 渲染 + 文件落盘。"""
from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console(stderr=False)


def setup_logging(level: str = "INFO", log_file: str | Path | None = None) -> logging.Logger:
    logger = logging.getLogger("wxcopy")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    handler = RichHandler(console=console, rich_tracebacks=True, show_path=False, markup=True)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    if log_file:
        p = Path(log_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))
        logger.addHandler(fh)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("wxcopy")
