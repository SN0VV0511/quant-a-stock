"""证券交易元数据。

旧实现把所有 ETF 当作 10% 涨跌幅证券，并在卖出 ETF 时收取股票印花税。
本模块把交易制度集中为一个可覆盖的证券画像，供虚拟盘、回测和风控共享。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from config.settings import (
    LIMIT_CHINEXT,
    LIMIT_MAINBOARD,
    LIMIT_ST,
    STAMP_TAX_RATE,
    TRANSFER_FEE_RATE,
    get_stock_board,
    is_etf,
    normalize_a_share_code,
)

InstrumentType = Literal["stock", "etf"]

# 代码本身无法完整表达 ETF 跟踪指数的涨跌幅制度，因此只维护策略池中已核验的
# 20% 品种；新增 ETF 必须在券商能力预检或证券主数据中显式覆盖。
ETF_PRICE_LIMIT_OVERRIDES: dict[str, float] = {
    "159915": LIMIT_CHINEXT,
    "588000": LIMIT_CHINEXT,
}


@dataclass(frozen=True)
class InstrumentProfile:
    """单个证券的交易制度画像。"""

    code: str
    instrument_type: InstrumentType
    board: str
    price_limit_pct: float
    t_plus_one: bool
    stamp_tax_rate: float
    transfer_fee_rate: float
    lot_size: int = 100

    @property
    def is_etf(self) -> bool:
        """是否为 ETF。"""
        return self.instrument_type == "etf"


def normalized_security_code(code: str) -> str:
    """返回六位数字证券代码。"""
    try:
        return normalize_a_share_code(code)
    except ValueError as exc:
        raise ValueError(f"不支持的沪深证券代码: {code}") from exc


def get_instrument_profile(
    code: str,
    *,
    name: str = "",
    price_limit_override: float | None = None,
) -> InstrumentProfile:
    """构建证券交易画像。

    Args:
        code: 六位、腾讯或 BaoStock 格式证券代码。
        name: 股票名称，用于识别 ST。
        price_limit_override: 来自券商证券主数据的涨跌幅覆盖值。

    Returns:
        InstrumentProfile: 可供撮合和风控直接使用的制度参数。
    """
    raw_code = normalized_security_code(code)
    if is_etf(code):
        limit = price_limit_override or ETF_PRICE_LIMIT_OVERRIDES.get(
            raw_code, LIMIT_MAINBOARD
        )
        return InstrumentProfile(
            code=raw_code,
            instrument_type="etf",
            board="etf",
            price_limit_pct=limit,
            t_plus_one=True,
            stamp_tax_rate=0.0,
            transfer_fee_rate=0.0,
        )

    board = get_stock_board(code)
    if board is None:
        raise ValueError(f"不支持的沪深 A 股证券代码: {code}")
    if "ST" in name.upper():
        limit = LIMIT_ST
    elif board in {"chinext", "star"}:
        limit = LIMIT_CHINEXT
    else:
        limit = LIMIT_MAINBOARD
    if price_limit_override is not None:
        limit = price_limit_override
    return InstrumentProfile(
        code=raw_code,
        instrument_type="stock",
        board=board,
        price_limit_pct=limit,
        t_plus_one=True,
        stamp_tax_rate=STAMP_TAX_RATE,
        transfer_fee_rate=TRANSFER_FEE_RATE,
    )
