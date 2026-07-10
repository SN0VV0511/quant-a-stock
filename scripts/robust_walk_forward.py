"""运行 robust_v2 的 32 组滚动样本外选择。

脚本只接受按交易日保存的研究数据，不会用当前固定股票池回填历史，因此缺少足够
point-in-time 股票池时会明确失败，而不是生成带幸存者偏差的“最优参数”。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backtest.robust_v2 import (  # noqa: E402
    RobustPortfolioBacktester,
    RobustWalkForwardSelector,
    selection_to_dict,
)

LOGGER = logging.getLogger("robust_walk_forward")


def _load_frame(path: Path) -> pd.DataFrame:
    """读取 CSV 或 pickle 行情文件。"""
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    elif path.suffix.lower() in {".pkl", ".pickle"}:
        frame = pd.read_pickle(path)
    else:
        raise ValueError(f"不支持的行情文件格式: {path}")
    if "date" not in frame.columns or "close" not in frame.columns:
        raise ValueError(f"行情文件缺少 date/close 列: {path}")
    return frame


def _load_history_directory(directory: Path) -> dict[str, pd.DataFrame]:
    """按文件名代码加载证券历史。"""
    if not directory.is_dir():
        raise FileNotFoundError(f"历史行情目录不存在: {directory}")
    result: dict[str, pd.DataFrame] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".csv", ".pkl", ".pickle"}:
            continue
        result[path.stem] = _load_frame(path)
    if not result:
        raise ValueError(f"历史行情目录为空: {directory}")
    return result


def _load_universe_snapshots(directory: Path) -> dict[str, set[str]]:
    """读取 ``robust_v2_YYYYMMDD.json`` 可交易池版本。"""
    if not directory.is_dir():
        raise FileNotFoundError(f"历史股票池目录不存在: {directory}")
    result: dict[str, set[str]] = {}
    for path in sorted(directory.glob("robust_v2_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshot = payload.get("snapshot", {})
        date = str(snapshot.get("trade_date") or path.stem.rsplit("_", 1)[-1])
        stocks = payload.get("stocks", [])
        result[date] = {
            str(stock["code"])
            for stock in stocks
            if isinstance(stock, dict) and stock.get("code")
        }
    if not result:
        raise ValueError("没有可用的 point-in-time 股票池版本")
    dates = sorted(result)
    span_days = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[0])).days
    if span_days < 365 * 3:
        raise ValueError(
            f"股票池版本仅覆盖 {span_days} 天，至少需要约 3 年以执行 24+6+12 月验证"
        )
    return result


def _index_return(
    frame: pd.DataFrame | None, start_date: str, end_date: str
) -> float | None:
    """计算指定基准在锁定测试区间的收益。"""
    if frame is None:
        return None
    data = frame.copy()
    data["date"] = data["date"].astype(str).str.replace("-", "", regex=False).str[:8]
    close = pd.to_numeric(
        data.loc[(data["date"] >= start_date) & (data["date"] <= end_date), "close"],
        errors="coerce",
    ).dropna()
    if len(close) < 2 or float(close.iloc[0]) <= 0:
        return None
    return round(float(close.iloc[-1] / close.iloc[0] - 1), 6)


def _legacy_metrics(path: Path | None) -> dict[str, Any] | None:
    """读取可选旧策略基线指标，不推断缺失数据。"""
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    series = payload.get("series", [])
    for row in series:
        if row.get("name") in {"组合(sleeve)", "旧策略", "legacy"}:
            return row.get("metrics")
    return None


def run_selection(
    data_root: Path,
    *,
    output: Path,
    selected_config_output: Path | None = None,
    legacy_baseline: Path | None = None,
) -> dict[str, Any]:
    """装载版本化数据、执行选择并原子写入结果。"""
    etfs = _load_history_directory(data_root / "etf")
    stocks = _load_history_directory(data_root / "stock")
    universes = _load_universe_snapshots(data_root / "universe")
    hs300_path = data_root / "benchmark" / "000300.csv"
    zz500_path = data_root / "benchmark" / "000905.csv"
    hs300 = _load_frame(hs300_path) if hs300_path.exists() else None
    if hs300 is None:
        raise FileNotFoundError("缺少沪深 300 基准文件 benchmark/000300.csv")
    zz500 = _load_frame(zz500_path) if zz500_path.exists() else None
    backtester = RobustPortfolioBacktester(
        etfs,
        stocks,
        universes,
        benchmark_history=hs300,
    )
    selection = RobustWalkForwardSelector(backtester).select()
    payload = selection_to_dict(selection)
    start = selection.locked_test.start_date
    end = selection.locked_test.end_date
    payload["comparisons"] = {
        "legacy": _legacy_metrics(legacy_baseline),
        "pure_etf": selection.pure_etf_test.metrics,
        "hs300_return": _index_return(hs300, start, end),
        "zz500_return": _index_return(zz500, start, end),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=output.parent, delete=False
    ) as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(output)
    if selected_config_output is not None:
        selected_config_output.parent.mkdir(parents=True, exist_ok=True)
        selected_payload = {
            "selected_params": payload["selected_params"],
            "fallback_to_etf": payload["fallback_to_etf"],
            "locked_test": payload["locked_test"],
            "stress_test": payload["stress_test"],
        }
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=selected_config_output.parent,
            delete=False,
        ) as stream:
            json.dump(
                selected_payload, stream, ensure_ascii=False, indent=2, sort_keys=True
            )
            stream.write("\n")
            selected_temporary = Path(stream.name)
        selected_temporary.replace(selected_config_output)
    return payload


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(description="robust_v2 32 组滚动样本外选择")
    parser.add_argument(
        "--data-root",
        default=str(ROOT_DIR / "data" / "robust_research"),
        help="含 etf/stock/universe/benchmark 的版本化研究数据目录",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT_DIR / "reports" / "robust_v2_walk_forward.json"),
        help="选择结果 JSON",
    )
    parser.add_argument("--legacy-baseline", help="可选旧策略 backtest_latest.json")
    parser.add_argument(
        "--selected-config",
        default=str(ROOT_DIR / "data" / "robust_v2_selected.json"),
        help="供 robust_runner 加载的已选择参数文件",
    )
    return parser.parse_args()


def main() -> int:
    """命令行入口。"""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    args = _parse_args()
    payload = run_selection(
        Path(args.data_root),
        output=Path(args.output),
        selected_config_output=Path(args.selected_config),
        legacy_baseline=Path(args.legacy_baseline) if args.legacy_baseline else None,
    )
    LOGGER.info(
        "选择完成: fallback=%s params=%s locked_return=%s",
        payload["fallback_to_etf"],
        payload["selected_params"],
        payload["locked_test"]["metrics"].get("total_return"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
