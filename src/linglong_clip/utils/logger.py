"""日志：同时写轮转文件与标准错误。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Optional

from .paths import log_dir

_CONFIGURED = False


def setup_logging(level: int = logging.INFO, quiet: bool = False) -> logging.Logger:
    global _CONFIGURED
    logger = logging.getLogger("linglong")
    if _CONFIGURED:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir() / "linglong.log", maxBytes=1 << 20, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # 日志目录不可写不应该阻断应用启动。
        pass

    if not quiet:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    _CONFIGURED = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(f"linglong.{name}" if name else "linglong")
