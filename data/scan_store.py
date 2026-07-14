"""robust_v2 个股扫描快照的结构化持久化。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

ScanMode = Literal["scheduled", "daily_observation", "manual_preview"]
ScanStatus = Literal["completed", "failed"]


@dataclass(frozen=True)
class StockScanSnapshot:
    """一次个股扫描的完整审计快照。"""

    strategy_version: str
    trade_date: str
    generated_at: str
    mode: ScanMode
    status: ScanStatus
    account_value: float
    source_snapshot_hash: str
    universe: dict[str, int]
    input_count: int
    eligible_count: int
    prefilter_counts: dict[str, int]
    prefilter_labels: dict[str, str]
    filter_counts: dict[str, int]
    filter_labels: dict[str, str]
    candidates: tuple[dict[str, Any], ...]
    selected_codes: tuple[str, ...]
    next_scheduled_scan_at: str
    error: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        if len(self.trade_date) != 8 or not self.trade_date.isdigit():
            raise ValueError(f"扫描交易日必须为 YYYYMMDD: {self.trade_date}")
        if self.account_value < 0:
            raise ValueError("扫描账户净值不能为负数")
        if self.input_count < 0 or self.eligible_count < 0:
            raise ValueError("扫描数量不能为负数")
        if self.eligible_count != len(self.candidates):
            raise ValueError("合格数量必须与候选明细数量一致")
        if self.status == "failed" and not self.error.strip():
            raise ValueError("失败扫描必须包含错误信息")

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 可序列化字典。"""
        return asdict(self)


class StockScanStore:
    """使用原子替换保存最新扫描与历史扫描快照。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.latest_path = self.directory / "robust_v2_latest.json"

    def save(self, snapshot: StockScanSnapshot) -> Path:
        """保存历史快照并原子更新 latest 指针文件。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex[:8]
        history_path = self.directory / (
            f"robust_v2_{snapshot.trade_date}_{snapshot.mode}_{suffix}.json"
        )
        payload = snapshot.to_dict()
        self._write_atomic(history_path, payload)
        self._write_atomic(self.latest_path, payload)
        return history_path

    def load_latest(self) -> dict[str, Any] | None:
        """读取最新扫描；文件不存在时返回 None，损坏时显式报错。"""
        if not self.latest_path.exists():
            return None
        try:
            payload = json.loads(self.latest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"读取个股扫描快照失败: {self.latest_path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"个股扫描快照根节点必须是对象: {self.latest_path}")
        return payload

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        """在目标目录内写临时文件并原子替换，避免面板读到半截 JSON。"""
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.directory,
                delete=False,
            ) as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                temporary_path = Path(stream.name)
            temporary_path.replace(path)
            path.chmod(0o644)
        except (OSError, TypeError, ValueError):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
