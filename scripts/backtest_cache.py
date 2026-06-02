"""策略回测对比缓存维护。

Web 仪表盘和观察期守护脚本共用本模块,确保部署后能自动生成
``reports/backtest_latest.json``,而不是依赖人工执行命令。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import (  # noqa: E402
    BACKTEST_AUTO_GENERATE,
    BACKTEST_AUTO_MAX_AGE_HOURS,
    BACKTEST_AUTO_UNIVERSE_SIZE,
)


LOGGER = logging.getLogger("backtest_cache")
_THREAD_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


@dataclass(frozen=True)
class BacktestCacheStatus:
    """回测缓存状态。"""

    available: bool
    generating: bool
    stale: bool
    path: str
    age_hours: float | None = None
    generated_at: str = ""
    error: str = ""
    message: str = ""
    auto_generate: bool = BACKTEST_AUTO_GENERATE

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 可序列化字典。"""
        return asdict(self)


def _artifact_path(root_dir: Path) -> Path:
    """返回 Web 仪表盘读取的回测产物路径。"""
    return root_dir / "reports" / "backtest_latest.json"


def _lock_path(root_dir: Path) -> Path:
    """返回回测生成锁文件路径。"""
    return root_dir / "data" / "backtest_refresh.lock"


def _error_path(root_dir: Path) -> Path:
    """返回最近一次自动回测错误记录路径。"""
    return root_dir / "data" / "backtest_refresh_error.json"


def _read_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象,失败返回空字典。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _is_generating(root_dir: Path) -> bool:
    """根据锁文件判断是否已有自动回测任务在运行。"""
    path = _lock_path(root_dir)
    if not path.exists():
        return False
    try:
        started = float(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    # strategy_ab 正常不应跑超过 2 小时;超过视为过期锁,允许新任务覆盖。
    return time.time() - started < 7200


def get_backtest_cache_status(
    root_dir: Path,
    max_age_hours: int = BACKTEST_AUTO_MAX_AGE_HOURS,
) -> BacktestCacheStatus:
    """获取当前回测缓存状态。"""
    root_dir = root_dir.resolve()
    path = _artifact_path(root_dir)
    generating = _is_generating(root_dir)
    error_data = _read_json(_error_path(root_dir))

    if not path.exists():
        return BacktestCacheStatus(
            available=False,
            generating=generating,
            stale=False,
            path=str(path),
            error=str(error_data.get("error", "")),
            message="暂无回测结果",
        )

    age_hours = (time.time() - path.stat().st_mtime) / 3600
    stale = age_hours > max_age_hours
    payload = _read_json(path)
    return BacktestCacheStatus(
        available=True,
        generating=generating,
        stale=stale,
        path=str(path),
        age_hours=round(age_hours, 2),
        generated_at=str(payload.get("generated_at", "")),
        error=str(error_data.get("error", "")),
        message="回测结果过期，后台刷新中" if stale and generating else "",
    )


def _run_generation(root_dir: Path, universe_size: int) -> None:
    """同步执行一次策略 A/B 回测并维护锁/错误文件。"""
    root_dir = root_dir.resolve()
    lock = _lock_path(root_dir)
    err = _error_path(root_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(time.time()), encoding="utf-8")
    try:
        command = [sys.executable, "-m", "scripts.strategy_ab", str(universe_size)]
        LOGGER.info("自动生成回测对比: %s", " ".join(command))
        completed = subprocess.run(
            command,
            cwd=str(root_dir),
            check=False,
            capture_output=True,
            text=True,
            timeout=7200,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"strategy_ab 退出码 {completed.returncode}: "
                f"{(completed.stderr or completed.stdout)[-1000:]}"
            )
        if err.exists():
            err.unlink()
    except Exception as exc:  # noqa: BLE001 - 需要记录后台任务失败原因
        LOGGER.warning("自动生成回测失败: %s", exc)
        err.write_text(
            json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "error": str(exc),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def ensure_backtest_cache(
    root_dir: Path,
    *,
    async_run: bool = True,
    force: bool = False,
    universe_size: int = BACKTEST_AUTO_UNIVERSE_SIZE,
    max_age_hours: int = BACKTEST_AUTO_MAX_AGE_HOURS,
) -> BacktestCacheStatus:
    """确保回测产物存在且不过期。

    Args:
        root_dir: 项目根目录。
        async_run: True 时后台生成,False 时当前进程同步生成。
        force: 是否忽略缓存强制刷新。
        universe_size: ``strategy_ab`` 抽样股票数量。
        max_age_hours: 缓存最大可接受年龄。

    Returns:
        BacktestCacheStatus: 触发前后的状态。
    """
    status = get_backtest_cache_status(root_dir, max_age_hours=max_age_hours)
    if not BACKTEST_AUTO_GENERATE:
        return status
    if status.generating:
        return status
    if not force and status.available and not status.stale:
        return status

    if async_run:
        global _WORKER
        with _THREAD_LOCK:
            if _WORKER is not None and _WORKER.is_alive():
                return get_backtest_cache_status(root_dir, max_age_hours=max_age_hours)
            _WORKER = threading.Thread(
                target=_run_generation,
                args=(root_dir.resolve(), universe_size),
                name="backtest-cache-refresh",
                daemon=True,
            )
            _WORKER.start()
        return get_backtest_cache_status(root_dir, max_age_hours=max_age_hours)

    _run_generation(root_dir, universe_size)
    return get_backtest_cache_status(root_dir, max_age_hours=max_age_hours)
