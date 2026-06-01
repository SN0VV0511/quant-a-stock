"""统一日志配置工具。

集中管理项目日志的格式、文件轮转与控制台输出,避免各入口脚本各写一套
``FileHandler`` / ``basicConfig`` 导致配置不一致、累计日志无限增长的问题。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from config.settings import LOG_DIR

_DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s"

# 累计日志单文件上限与保留份数:10MB × 5,约束磁盘占用上限 ~50MB
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def setup_logger(
    name: str,
    *,
    rotating_files: tuple[str, ...] = (),
    plain_files: tuple[str, ...] = (),
    level: int = logging.INFO,
    console: bool = True,
    fmt: str = _DEFAULT_FORMAT,
) -> logging.Logger:
    """配置并返回一个命名 logger(幂等,重复调用不会重复挂 handler)。

    Args:
        name: logger 名称。
        rotating_files: 需要按大小轮转的日志文件名(累计日志,如 ``live.log``)。
        plain_files: 不轮转的日志文件名(如当日日志 ``live_today.log``,
            按运行周期由外部覆盖,无需轮转)。
        level: 日志级别。
        console: 是否同时输出到控制台。
        fmt: 日志格式。

    Returns:
        已配置好的 logger。
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # 幂等:已配置过则直接返回,避免重复添加 handler 造成日志重复
    if logger.handlers:
        return logger

    formatter = logging.Formatter(fmt)

    for filename in rotating_files:
        handler = RotatingFileHandler(
            os.path.join(LOG_DIR, filename),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    for filename in plain_files:
        handler = logging.FileHandler(os.path.join(LOG_DIR, filename), encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    return logger
