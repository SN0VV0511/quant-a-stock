"""统一本地时间工具。

容器默认时区可能是 UTC,但 A 股交易时段、日志和观察期统计都必须按中国时区
计算。这里集中使用 Asia/Shanghai,避免依赖宿主机或 Docker 镜像的系统时区。
"""

from __future__ import annotations

import os
from datetime import datetime
from time import struct_time
from zoneinfo import ZoneInfo

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
APP_TZ = ZoneInfo(APP_TIMEZONE)


def now_local() -> datetime:
    """返回应用本地时间。

    返回值去掉 tzinfo,以兼容项目中现有的 naive datetime 比较和格式化逻辑。
    """
    return datetime.now(APP_TZ).replace(tzinfo=None)


def format_local(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """按应用本地时区格式化当前时间。"""
    return now_local().strftime(fmt)


def today_yyyymmdd() -> str:
    """返回应用本地日期 YYYYMMDD。"""
    return format_local("%Y%m%d")


def logging_time_converter(timestamp: float) -> struct_time:
    """logging.Formatter.converter 兼容函数。"""
    return datetime.fromtimestamp(timestamp, APP_TZ).timetuple()
