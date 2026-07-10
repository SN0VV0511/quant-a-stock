"""归档旧虚拟盘证据并初始化 5 万元 ``paper_v2`` 单账本。

默认只预览。显式传入 ``--confirm`` 后会复制旧 JSON/日志到带时间戳的只读目录，
原文件仍保留不动；随后创建新的 SQLite 账本。已有账本不会被静默重置。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config.settings import INITIAL_CAPITAL, ROBUST_V2_ACCOUNT_ID, ROBUST_V2_LEDGER_PATH
from config.time_utils import format_local
from trading.ledger import PaperLedger

LOGGER = logging.getLogger("paper_v2_init")

LEGACY_EVIDENCE = (
    Path("data/portfolio_state.json"),
    Path("data/trade_log.json"),
    Path("data/trade_events.jsonl"),
    Path("data/portfolio_snapshots.jsonl"),
    Path("data/rps_state.json"),
    Path("data/small_cap_state.json"),
    Path("logs/live.log"),
    Path("logs/live_today.log"),
    Path("logs/smallcap_runner.log"),
    Path("logs/smallcap_today.log"),
)


@dataclass(frozen=True)
class InitResult:
    """归档和账本初始化结果。"""

    changed: bool
    root_dir: str
    ledger_path: str
    archive_dir: str | None
    initial_cash: float
    archived_files: tuple[str, ...]
    skipped_files: tuple[str, ...]
    message: str


def _sha256(path: Path) -> str:
    """流式计算归档文件哈希，避免大日志一次读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_evidence(
    root_dir: Path, archive_dir: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """复制旧证据、写入校验清单并把归档副本设为只读。"""
    archived: list[str] = []
    skipped: list[str] = []
    manifest: list[dict[str, object]] = []
    for relative in LEGACY_EVIDENCE:
        source = root_dir / relative
        if not source.exists() or not source.is_file():
            skipped.append(str(relative))
            continue
        target = archive_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        target.chmod(0o444)
        archived.append(str(relative))
        manifest.append(
            {
                "path": str(relative),
                "size": target.stat().st_size,
                "sha256": _sha256(target),
            }
        )
    manifest_path = archive_dir / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {"created_at": format_local(), "files": manifest},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o444)
    return tuple(archived), tuple(skipped)


def initialize_paper_v2(
    root_dir: Path,
    *,
    ledger_path: Path,
    initial_cash: float = INITIAL_CAPITAL,
    confirm: bool = False,
) -> InitResult:
    """归档旧证据并创建不可重复初始化的 SQLite 账本。"""
    if initial_cash <= 0:
        raise ValueError("初始资金必须大于 0")
    root = root_dir.expanduser().resolve()
    ledger = ledger_path.expanduser().resolve()
    existing = tuple(str(path) for path in LEGACY_EVIDENCE if (root / path).is_file())
    skipped = tuple(
        str(path) for path in LEGACY_EVIDENCE if not (root / path).is_file()
    )
    if ledger.exists():
        return InitResult(
            changed=False,
            root_dir=str(root),
            ledger_path=str(ledger),
            archive_dir=None,
            initial_cash=initial_cash,
            archived_files=(),
            skipped_files=skipped,
            message="paper_v2 账本已存在；为保护流水，拒绝重置",
        )
    if not confirm:
        return InitResult(
            changed=False,
            root_dir=str(root),
            ledger_path=str(ledger),
            archive_dir=None,
            initial_cash=initial_cash,
            archived_files=existing,
            skipped_files=skipped,
            message="预览完成；传入 --confirm 才会归档并初始化",
        )

    timestamp = format_local("%Y%m%d_%H%M%S")
    archive_dir = root / "data" / "backups" / f"paper_v2_{timestamp}"
    archive_dir.mkdir(parents=True, exist_ok=False)
    archived, skipped_files = _archive_evidence(root, archive_dir)
    paper_ledger = PaperLedger(
        ledger,
        account_id=ROBUST_V2_ACCOUNT_ID,
        initial_cash=initial_cash,
        enforce_t1=True,
    )
    try:
        paper_ledger.connect()
        cash = paper_ledger.query_cash()
    finally:
        paper_ledger.close()
    if abs(cash - initial_cash) > 0.01:
        raise RuntimeError(f"账本初始现金校验失败: {cash} != {initial_cash}")
    return InitResult(
        changed=True,
        root_dir=str(root),
        ledger_path=str(ledger),
        archive_dir=str(archive_dir),
        initial_cash=initial_cash,
        archived_files=archived,
        skipped_files=skipped_files,
        message="旧证据已只读归档，paper_v2 账本已初始化",
    )


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="初始化 robust_v2 虚拟盘单账本")
    parser.add_argument("--root", default=str(ROOT_DIR), help="项目根目录")
    parser.add_argument(
        "--ledger", default=ROBUST_V2_LEDGER_PATH, help="SQLite 账本路径"
    )
    parser.add_argument("--cash", type=float, default=INITIAL_CAPITAL, help="初始资金")
    parser.add_argument("--confirm", action="store_true", help="确认执行归档和初始化")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()
    result = initialize_paper_v2(
        Path(args.root),
        ledger_path=Path(args.ledger),
        initial_cash=args.cash,
        confirm=args.confirm,
    )
    if args.json:
        sys.stdout.write(
            json.dumps(asdict(result), ensure_ascii=False, indent=2) + "\n"
        )
    else:
        LOGGER.info("%s", result.message)
        LOGGER.info("账本: %s", result.ledger_path)
        if result.archive_dir:
            LOGGER.info("归档: %s", result.archive_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
