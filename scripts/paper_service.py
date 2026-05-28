"""虚拟盘观察期后台服务管理。

这个脚本负责安全地启动、查询、停止 `paper_daemon.py`，避免一个月观察期内重复
启动多个守护进程。它只管理虚拟盘，不会连接 QMT 或发送真实委托。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parents[1]
PID_FILE_NAME = "paper_daemon.pid"
SERVICE_LOG_NAME = "paper_daemon_service.log"


@dataclass(frozen=True)
class ServiceConfig:
    """后台服务配置。"""

    root_dir: Path
    watch_interval: int = 4
    scan_interval: int = 600
    top_n: int = 20
    poll_seconds: int = 300
    review_days: int = 30
    ignore_calendar: bool = False


@dataclass(frozen=True)
class ServiceStatus:
    """后台服务状态。"""

    running: bool
    pid: int | None
    pid_file: str
    log_file: str
    stale_pid_file: bool = False
    command: list[str] = field(default_factory=list)
    started_at: str = ""
    message: str = ""


@dataclass(frozen=True)
class ServiceActionResult:
    """后台服务操作结果。"""

    ok: bool
    action: str
    status: ServiceStatus
    message: str


PidChecker = Callable[[int], bool]
PopenFactory = Callable[..., subprocess.Popen[Any]]


def _pid_file(root_dir: Path) -> Path:
    """返回 PID 文件路径。"""
    return root_dir / "data" / PID_FILE_NAME


def _log_file(root_dir: Path) -> Path:
    """返回后台服务日志路径。"""
    return root_dir / "logs" / SERVICE_LOG_NAME


def _now_str() -> str:
    """返回统一时间字符串。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def is_pid_running(pid: int) -> bool:
    """判断 PID 是否仍在运行。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def build_daemon_command(config: ServiceConfig) -> list[str]:
    """构建后台守护命令。"""
    command = [
        sys.executable,
        str(config.root_dir / "scripts" / "paper_daemon.py"),
        "--root",
        str(config.root_dir),
        "--watch-interval",
        str(config.watch_interval),
        "--scan-interval",
        str(config.scan_interval),
        "--top-n",
        str(config.top_n),
        "--poll-seconds",
        str(config.poll_seconds),
        "--review-days",
        str(config.review_days),
    ]
    if config.ignore_calendar:
        command.append("--ignore-calendar")
    return command


def _read_pid_metadata(pid_file: Path) -> dict[str, Any]:
    """读取 PID 元数据，兼容纯数字旧格式。"""
    if not pid_file.exists():
        return {}
    text = pid_file.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        try:
            return {"pid": int(text)}
        except ValueError:
            return {}


def write_pid_metadata(pid_file: Path, pid: int, command: list[str], log_file: Path) -> None:
    """写入 PID 元数据。"""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "command": command,
        "log_file": str(log_file),
        "started_at": _now_str(),
    }
    pid_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_status(root_dir: Path, pid_checker: PidChecker = is_pid_running) -> ServiceStatus:
    """查询后台服务状态。"""
    root_dir = root_dir.resolve()
    pid_file = _pid_file(root_dir)
    log_file = _log_file(root_dir)
    metadata = _read_pid_metadata(pid_file)
    pid = metadata.get("pid")
    pid_value = int(pid) if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()) else None
    running = pid_checker(pid_value) if pid_value is not None else False
    stale = pid_file.exists() and pid_value is not None and not running
    if pid_file.exists() and pid_value is None:
        stale = True

    return ServiceStatus(
        running=running,
        pid=pid_value,
        pid_file=str(pid_file),
        log_file=str(log_file),
        stale_pid_file=stale,
        command=[str(item) for item in metadata.get("command", [])] if isinstance(metadata.get("command"), list) else [],
        started_at=str(metadata.get("started_at", "")),
        message="running" if running else "stopped",
    )


def start_service(
    config: ServiceConfig,
    popen_factory: PopenFactory = subprocess.Popen,
    pid_checker: PidChecker = is_pid_running,
) -> ServiceActionResult:
    """启动后台服务。"""
    root_dir = config.root_dir.resolve()
    status = get_status(root_dir, pid_checker=pid_checker)
    if status.running:
        return ServiceActionResult(
            ok=False,
            action="start",
            status=status,
            message=f"虚拟盘守护进程已在运行: pid={status.pid}",
        )

    pid_file = _pid_file(root_dir)
    if status.stale_pid_file and pid_file.exists():
        pid_file.unlink()

    log_file = _log_file(root_dir)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    command = build_daemon_command(config)
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n[{_now_str()}] START {' '.join(command)}\n")
        log.flush()
        process = popen_factory(
            command,
            cwd=str(root_dir),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    poll = getattr(process, "poll", lambda: None)
    time.sleep(0.2)
    return_code = poll()
    if return_code is not None:
        failed_status = get_status(root_dir, pid_checker=pid_checker)
        return ServiceActionResult(
            ok=False,
            action="start",
            status=failed_status,
            message=f"虚拟盘守护进程启动后立即退出: returncode={return_code}",
        )

    write_pid_metadata(pid_file, int(process.pid), command, log_file)
    new_status = get_status(root_dir, pid_checker=pid_checker)
    return ServiceActionResult(
        ok=True,
        action="start",
        status=new_status,
        message=f"虚拟盘守护进程已启动: pid={process.pid}",
    )


def stop_service(
    root_dir: Path,
    timeout_seconds: int = 10,
    force: bool = False,
    pid_checker: PidChecker = is_pid_running,
) -> ServiceActionResult:
    """停止后台服务。"""
    root_dir = root_dir.resolve()
    status = get_status(root_dir, pid_checker=pid_checker)
    pid_file = _pid_file(root_dir)
    if status.pid is None:
        if pid_file.exists():
            pid_file.unlink()
        return ServiceActionResult(ok=True, action="stop", status=status, message="虚拟盘守护进程未运行")

    if not status.running:
        if pid_file.exists():
            pid_file.unlink()
        return ServiceActionResult(ok=True, action="stop", status=status, message="已清理过期 PID 文件")

    os.kill(status.pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not pid_checker(status.pid):
            if pid_file.exists():
                pid_file.unlink()
            stopped = get_status(root_dir, pid_checker=pid_checker)
            return ServiceActionResult(ok=True, action="stop", status=stopped, message="虚拟盘守护进程已停止")
        time.sleep(0.2)

    if force:
        os.kill(status.pid, signal.SIGKILL)
        if pid_file.exists():
            pid_file.unlink()
        stopped = get_status(root_dir, pid_checker=pid_checker)
        return ServiceActionResult(ok=True, action="stop", status=stopped, message="虚拟盘守护进程已强制停止")

    return ServiceActionResult(
        ok=False,
        action="stop",
        status=get_status(root_dir, pid_checker=pid_checker),
        message=f"停止超时，请检查 pid={status.pid}",
    )


def restart_service(
    config: ServiceConfig,
    timeout_seconds: int = 10,
    force: bool = False,
) -> ServiceActionResult:
    """重启后台服务。"""
    stopped = stop_service(config.root_dir, timeout_seconds=timeout_seconds, force=force)
    if not stopped.ok:
        return ServiceActionResult(ok=False, action="restart", status=stopped.status, message=stopped.message)
    started = start_service(config)
    return ServiceActionResult(ok=started.ok, action="restart", status=started.status, message=started.message)


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="管理 A 股虚拟盘观察期后台服务")
    parser.add_argument("action", choices=["start", "status", "stop", "restart"], help="操作")
    parser.add_argument("--root", default=str(ROOT_DIR), help="项目根目录")
    parser.add_argument("--watch-interval", type=int, default=4, help="盯盘刷新秒数")
    parser.add_argument("--scan-interval", type=int, default=600, help="扫描间隔秒数")
    parser.add_argument("--top-n", type=int, default=20, help="候选股数量")
    parser.add_argument("--poll-seconds", type=int, default=300, help="守护循环轮询秒数")
    parser.add_argument("--review-days", type=int, default=30, help="收盘复盘天数")
    parser.add_argument("--ignore-calendar", action="store_true", help="忽略交易日历，便于联调")
    parser.add_argument("--timeout-seconds", type=int, default=10, help="停止等待秒数")
    parser.add_argument("--force", action="store_true", help="停止超时后强制结束")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def _print_result(result: ServiceActionResult, as_json: bool) -> None:
    """输出操作结果。"""
    payload = asdict(result)
    if as_json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        return
    prefix = "OK" if result.ok else "FAIL"
    sys.stdout.write(f"{prefix} {result.message}\n")
    sys.stdout.write(f"pid={result.status.pid} running={result.status.running}\n")
    sys.stdout.write(f"pid_file={result.status.pid_file}\n")
    sys.stdout.write(f"log_file={result.status.log_file}\n")


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    root_dir = Path(args.root).resolve()
    config = ServiceConfig(
        root_dir=root_dir,
        watch_interval=args.watch_interval,
        scan_interval=args.scan_interval,
        top_n=args.top_n,
        poll_seconds=args.poll_seconds,
        review_days=args.review_days,
        ignore_calendar=args.ignore_calendar,
    )

    if args.action == "status":
        status = get_status(root_dir)
        result = ServiceActionResult(ok=True, action="status", status=status, message=status.message)
    elif args.action == "start":
        result = start_service(config)
    elif args.action == "stop":
        result = stop_service(root_dir, timeout_seconds=args.timeout_seconds, force=args.force)
    else:
        result = restart_service(config, timeout_seconds=args.timeout_seconds, force=args.force)

    _print_result(result, args.json)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
