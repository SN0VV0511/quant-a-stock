"""初始化虚拟盘观察期状态。

开始一个新的一个月观察期前，建议先用本脚本备份旧的虚拟盘状态、交易流水和运行日志，
再初始化 1 万元空仓账户。默认只预览，不会修改文件；必须显式传入 `--confirm`。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from config.time_utils import format_local

ROOT_DIR = Path(__file__).resolve().parents[1]

STATE_FILES = (
    Path("data/portfolio_state.json"),
    Path("data/trade_log.json"),
    Path("data/trade_events.jsonl"),
    Path("data/portfolio_snapshots.jsonl"),
    Path("logs/live.log"),
    Path("logs/live_today.log"),
)


@dataclass(frozen=True)
class ResetResult:
    """初始化结果。"""

    changed: bool
    root_dir: str
    backup_dir: str | None
    initialized_state: str
    backed_up_files: list[str]
    skipped_files: list[str]


def _new_state(cash: float) -> dict[str, object]:
    """生成新的虚拟盘状态。"""
    return {
        "cash": round(cash, 2),
        "positions": {},
        "trades": [],
        "daily_snapshots": {},
        "updated_at": format_local(),
    }


def reset_paper_state(root_dir: Path, cash: float = 10000.0, confirm: bool = False) -> ResetResult:
    """备份旧文件并初始化虚拟盘账户。

    Args:
        root_dir: 项目根目录。
        cash: 新账户初始资金。
        confirm: 是否真正执行修改。False 时只返回预览。

    Returns:
        ResetResult: 备份和初始化结果。
    """
    if cash <= 0:
        raise ValueError(f"初始资金必须大于 0: {cash}")

    root_dir = root_dir.resolve()
    existing_files = [path for path in STATE_FILES if (root_dir / path).exists()]
    skipped_files = [str(path) for path in STATE_FILES if not (root_dir / path).exists()]
    state_path = root_dir / "data" / "portfolio_state.json"

    if not confirm:
        return ResetResult(
            changed=False,
            root_dir=str(root_dir),
            backup_dir=None,
            initialized_state=str(state_path),
            backed_up_files=[str(path) for path in existing_files],
            skipped_files=skipped_files,
        )

    timestamp = format_local("%Y%m%d_%H%M%S")
    backup_dir = root_dir / "data" / "backups" / f"paper_state_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backed_up: list[str] = []
    for relative_path in existing_files:
        source = root_dir / relative_path
        target = backup_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
        backed_up.append(str(relative_path))

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(_new_state(cash), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (root_dir / "data" / "trade_log.json").write_text("", encoding="utf-8")
    (root_dir / "data" / "trade_events.jsonl").write_text("", encoding="utf-8")
    (root_dir / "data" / "portfolio_snapshots.jsonl").write_text("", encoding="utf-8")
    (root_dir / "logs").mkdir(parents=True, exist_ok=True)
    (root_dir / "logs" / "live.log").write_text("", encoding="utf-8")
    (root_dir / "logs" / "live_today.log").write_text("", encoding="utf-8")

    return ResetResult(
        changed=True,
        root_dir=str(root_dir),
        backup_dir=str(backup_dir),
        initialized_state=str(state_path),
        backed_up_files=backed_up,
        skipped_files=skipped_files,
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="备份并初始化虚拟盘观察期状态")
    parser.add_argument("--root", default=str(ROOT_DIR), help="项目根目录")
    parser.add_argument("--cash", type=float, default=10000.0, help="新虚拟盘初始资金")
    parser.add_argument("--confirm", action="store_true", help="确认执行备份和初始化")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    args = _parse_args()
    result = reset_paper_state(Path(args.root), cash=args.cash, confirm=args.confirm)
    payload = asdict(result)
    if args.json:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        mode = "已执行" if result.changed else "预览"
        sys.stdout.write(f"虚拟盘初始化{mode}: {result.initialized_state}\n")
        if result.backup_dir:
            sys.stdout.write(f"备份目录: {result.backup_dir}\n")
        for path in result.backed_up_files:
            sys.stdout.write(f"BACKUP {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
