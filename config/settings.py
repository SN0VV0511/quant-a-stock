"""
A 股量化交易系统 - 全局配置
"""

from __future__ import annotations

import os

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==================== 资金配置 ====================
INITIAL_CAPITAL = 50000.0  # 初始资金（元）

# ==================== 默认回测配置 ====================
DEFAULT_STOCK = "000001"
BACKTEST_START = "20240101"
BACKTEST_END = "20250101"
MA_SHORT = 5
MA_LONG = 20

# ==================== 交易成本 ====================
COMMISSION_RATE = 0.0003       # 佣金费率（万三）
COMMISSION_MIN = 5.0           # 最低佣金（元）
STAMP_TAX_RATE = 0.0005        # 印花税率（千分之五，仅卖出）
TRANSFER_FEE_RATE = 0.00001    # 过户费率（万分之0.1，双向）

# ==================== 滑点 ====================
SLIPPAGE_STOCK = 0.0010   # 股票滑点 10bp
SLIPPAGE_ETF = 0.0003     # ETF 滑点 3bp

# ==================== 交易单位 ====================
LOT_SIZE = 100  # 每手 100 股/份

# ==================== 涨跌停规则 ====================
LIMIT_MAINBOARD = 0.10    # 主板 ±10%
LIMIT_ST = 0.05           # ST 板块 ±5%
LIMIT_CHINEXT = 0.20      # 创业板 ±20%

# ==================== 持仓限制 ====================
MAX_TOTAL_POSITION = 0.90   # 总仓位上限 90%
MAX_SINGLE_ETF = 0.45       # 单只 ETF 上限 45%
MAX_SINGLE_STOCK = 0.25     # 单只股票上限 25%
CASH_BUFFER = 0.10          # 现金缓冲 10%

# ==================== 风控参数 ====================
DAILY_LOSS_THRESHOLD = 0.025    # 单日最大亏损 2.5% 触发降仓
MAX_DRAWDOWN_THRESHOLD = 0.06   # 最大回撤 6% 暂停开仓

# ==================== 默认标的池 ====================
# ETF 动量轮动标的（BaoStock 不支持 ETF，暂用宽基指数成分股代替）
# 待接入 Tushare/AKShare 后替换为真实 ETF
DEFAULT_ETF_PROXY_POOL = {
    "sh601988": {"name": "中国银行(ETF代替)", "code": "sh601988", "raw_code": "601988"},
    "sh600519": {"name": "贵州茅台(ETF代替)", "code": "sh600519", "raw_code": "600519"},
    "sz000858": {"name": "五粮液(ETF代替)",   "code": "sz000858", "raw_code": "000858"},
    "sh601318": {"name": "中国平安(ETF代替)", "code": "sh601318", "raw_code": "601318"},
}

# 兼容旧版回测引擎命名；当前仍使用代理标的作为 ETF 池。
DEFAULT_ETF_POOL = DEFAULT_ETF_PROXY_POOL

DEFAULT_STOCK_POOL = {
    "sh601988": {"name": "中国银行", "code": "sh601988", "raw_code": "601988"},
    "sz000651": {"name": "格力电器", "code": "sz000651", "raw_code": "000651"},
    "sz000725": {"name": "京东方A",  "code": "sz000725", "raw_code": "000725"},
    "sz002415": {"name": "海康威视", "code": "sz002415", "raw_code": "002415"},
    "sz300750": {"name": "宁德时代", "code": "sz300750", "raw_code": "300750"},
}

# 合并标的池
DEFAULT_UNIVERSE = {}
DEFAULT_UNIVERSE.update(DEFAULT_ETF_PROXY_POOL)
DEFAULT_UNIVERSE.update(DEFAULT_STOCK_POOL)

# ==================== 文件路径 ====================
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "portfolio_state.json")
TRADE_LOG_FILE = os.path.join(DATA_DIR, "trade_log.json")
TRADE_EVENTS_FILE = os.path.join(DATA_DIR, "trade_events.jsonl")
SNAPSHOT_LOG_FILE = os.path.join(DATA_DIR, "portfolio_snapshots.jsonl")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ==================== 实时运行配置 ====================
LIVE_WATCH_INTERVAL_SECONDS = int(os.getenv("LIVE_WATCH_INTERVAL_SECONDS", "4"))
LIVE_SCAN_INTERVAL_SECONDS = int(os.getenv("LIVE_SCAN_INTERVAL_SECONDS", "600"))

# ==================== Broker / QMT 配置 ====================
BROKER_MODE = os.getenv("BROKER_MODE", "paper").lower()
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
QMT_ACCOUNT_ID = os.getenv("QMT_ACCOUNT_ID", "")
QMT_CLIENT_PATH = os.getenv("QMT_CLIENT_PATH", "")

# 确保目录存在
for d in [DATA_DIR, CACHE_DIR, REPORT_DIR, LOG_DIR]:
    os.makedirs(d, exist_ok=True)


def get_etf_codes():
    """返回 ETF 代理标的代码列表（BaoStock 格式）"""
    return list(DEFAULT_ETF_PROXY_POOL.keys())


def get_stock_codes():
    """返回股票标的代码列表（BaoStock 格式）"""
    return list(DEFAULT_STOCK_POOL.keys())


def get_all_codes():
    """返回所有标的代码列表"""
    return list(DEFAULT_UNIVERSE.keys())


def is_etf(code):
    """判断是否为 ETF（当前用股票代替，始终返回 False）"""
    # TODO: 接入真实 ETF 数据源后恢复
    return False


SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688", "689")
SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")


def _split_market_prefix(code: str) -> tuple[str | None, str]:
    """拆分市场前缀和 6 位证券代码。"""
    normalized = str(code).strip().lower().replace(".", "")
    if normalized.startswith(("sh", "sz")):
        return normalized[:2], normalized[2:]
    return None, normalized


def normalize_a_share_code(code: str) -> str:
    """归一化为 6 位 A 股股票代码。

    Args:
        code: 支持 `600519`、`sh600519`、`sh.600519` 等常见格式。

    Returns:
        6 位数字代码。

    Raises:
        ValueError: 当输入不是 6 位数字证券代码时抛出。
    """
    _, raw_code = _split_market_prefix(code)
    if not raw_code.isdigit() or len(raw_code) != 6:
        raise ValueError(f"无效证券代码: {code}")
    return raw_code


def get_a_share_market(code: str) -> str | None:
    """返回沪深 A 股股票所属市场，非沪深 A 股股票返回 None。

    当前实时虚拟盘仅覆盖沪深 A 股股票，不包含 ETF、指数、港美股、B 股和北交所。
    带市场前缀的代码必须与 6 位代码本身的交易所归属一致。
    """
    prefix, raw_code = _split_market_prefix(code)
    if not raw_code.isdigit() or len(raw_code) != 6:
        return None

    if raw_code.startswith(SH_A_SHARE_PREFIXES):
        market = "sh"
    elif raw_code.startswith(SZ_A_SHARE_PREFIXES):
        market = "sz"
    else:
        return None

    if prefix is not None and prefix != market:
        return None
    return market


def is_a_share_stock(code: str) -> bool:
    """判断代码是否为当前系统支持的沪深 A 股股票。"""
    return get_a_share_market(code) is not None


def to_tencent_code(code: str) -> str:
    """转换为腾讯实时行情代码格式，例如 `sh600519`。"""
    market = get_a_share_market(code)
    if market is None:
        raise ValueError(f"非沪深 A 股股票代码，无法请求腾讯行情: {code}")
    return f"{market}{normalize_a_share_code(code)}"


def to_baostock_code(code: str) -> str:
    """转换为 BaoStock 代码格式，例如 `sh.600519`。"""
    market = get_a_share_market(code)
    if market is None:
        raise ValueError(f"非沪深 A 股股票代码，无法请求 BaoStock 行情: {code}")
    return f"{market}.{normalize_a_share_code(code)}"


def is_chinext(code):
    """判断是否为创业板股票（300/301 开头）。"""
    try:
        raw = normalize_a_share_code(code)
    except ValueError:
        return False
    return get_a_share_market(code) == "sz" and raw.startswith(("300", "301"))


def is_shenzhen(code):
    """判断是否为深市股票"""
    return get_a_share_market(code) == "sz"
