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
STAMP_TAX_RATE = 0.0005        # 印花税率（万分之五=0.05%，仅卖出，2023-08 减半后现行税率）
TRANSFER_FEE_RATE = 0.00001    # 过户费率（万分之0.1，双向）

# ==================== 滑点 ====================
SLIPPAGE_STOCK = 0.0010   # 股票滑点 10bp
SLIPPAGE_ETF = 0.0003     # ETF 滑点 3bp

# ==================== 交易单位 ====================
LOT_SIZE = 100  # 每手 100 股/份

# ==================== T+1 交易限制 ====================
# A 股买入次日才可卖出。默认强制(贴近真实成交、防止虚拟盘出现现实中不可能的
# 日内反复买卖)。若需当日买卖联调虚拟盘,运行前设环境变量 ENFORCE_T1=false。
ENFORCE_T1 = os.getenv("ENFORCE_T1", "true").strip().lower() not in ("false", "0", "no", "off")

# ==================== 涨跌停规则 ====================
LIMIT_MAINBOARD = 0.10    # 主板 ±10%
LIMIT_ST = 0.05           # ST 板块 ±5%
LIMIT_CHINEXT = 0.20      # 创业板 ±20%

# ==================== 持仓限制 ====================
MAX_TOTAL_POSITION = 0.60   # 总仓位上限 60%,小资金观察期优先控制回撤
MAX_SINGLE_ETF = 0.45       # 单只 ETF 上限 45%
MAX_SINGLE_STOCK = 0.15     # 单只股票上限 15%,避免单票波动吞噬账户
CASH_BUFFER = 0.10          # 现金缓冲 10%

# ==================== 风控参数 ====================
DAILY_LOSS_THRESHOLD = 0.025    # 单日最大亏损 2.5% 触发降仓
MAX_DRAWDOWN_THRESHOLD = 0.06   # 最大回撤 6% 暂停开仓

# 卖出后再买冷却:同一标的被(策略/止损/止盈)卖出后,此秒数内不再开新仓。
# 防止"卖出→信号又翻多→立刻买回"的日内刷单(每个来回都白白消耗佣金+滑点)。
REBUY_COOLDOWN_SECONDS = 1800   # 默认 30 分钟

# ==================== 策略参数(集中管理,便于回测调参) ====================
# 择时:止损止盈(live_runner 持仓退出逻辑读取)
STOP_LOSS_PCT = 0.07            # 固定止损线:亏损达 7% 离场
TAKE_PROFIT_PCT = 0.10          # 止盈触发线:盈利达 10% 且跌破 MA20 离场
ATR_PERIOD = 14                 # ATR 回看周期
ENABLE_ATR_STOP = True          # 启用 ATR 动态止损,固定止损仅作为最大亏损保护
ATR_STOP_MULTIPLIER = 1.2       # ATR 止损倍数,小资金默认取 1~1.5 中间偏稳健
ATR_STOP_MAX_PCT = STOP_LOSS_PCT  # ATR 止损最宽不超过固定止损线
ENABLE_ATR_TAKE_PROFIT = True   # 达到 ATR 目标后,跌破 MA20 时锁定利润
ATR_TAKE_PROFIT_MULTIPLIER = 2.5  # ATR 止盈目标倍数,对应 2~3 倍 ATR 区间
TRAILING_STOP_PCT = 0.08        # 移动止损:自持仓最高点回撤 8% 离场
TRAILING_ACTIVATE_PCT = 0.05    # 移动止损激活线:峰值相对成本盈利达 5% 后才启用,避免买入即被噪声扫出
ENABLE_TRAILING_STOP = True     # 是否启用移动止损
TIME_STOP_DAYS = 0              # 时间止损:持有(自然日)达此值且仍不达预期则清仓;0=禁用
TIME_STOP_MIN_PROFIT = 0.0      # 时间止损的"达标"盈亏阈值,低于此值才触发

# 择时:大盘择时(系统性风险过滤)
# 回测结论(scripts/ab_backtest.py,2023~2025 与 2024H2 两段):简单的"价格≥MA20"择时
# 在弱市与 V 型反弹窗口均跑输基线——A 股反弹多为急拉,该过滤入场滞后、错过主升段且
# 把交易数砍半。因此默认关闭,功能保留待改进(如改用 MA20<MA60 且下行的更严判据)。
ENABLE_MARKET_REGIME = False    # 指数处于弱势(收盘价跌破其 MA)时暂停开新仓,只允许卖出/止损
MARKET_INDEX_CODE = "sh000300"  # 基准指数:沪深300
MARKET_REGIME_MA = 20           # 大盘择时均线周期

# 择时:ComboSignal 动量追涨参数
MOMENTUM_CHASE_GAP = 0.10       # MA 短长价差超过此值视为强势趋势,放宽 RSI 上限追涨
RSI_MOMENTUM_MAX = 85           # 动量追涨模式下的 RSI 上限
RSI_OVERBOUGHT = 70             # RSI 超买线
RSI_OVERSOLD = 35               # RSI 超卖线(脱离超卖区下限)
ENABLE_VOLUME_FILTER = True     # Combo 买入信号必须通过成交量确认
VOLUME_FILTER_LOOKBACK = 20     # 成交量确认使用 20 日均量
VOLUME_FILTER_MIN_RATIO = 1.0   # 当前量需不低于 20 日均量,过滤缩量假突破

# 选股:全市场扫描粗筛阈值
SCAN_MIN_PRICE = 5.0            # 最低股价(元),排除低价股
SCAN_MIN_VOLUME = 1_000_000    # 实时成交量下限(股),排除僵尸股
SCAN_MIN_AVG_VOLUME = 500_000  # 20 日均量下限(股)
SCAN_LIMIT_PCT = 9.8           # 实时涨跌幅绝对值达此值视为涨跌停,剔除
SCAN_MAX_HIST_FETCH = 2000     # 粗筛后最多拉取历史数据的标的数量

# 选股:横截面动量打分权重(对 z-score 标准化后的因子加权)
# 历史问题:旧版直接用 动量*0.5 + 短动量*0.3 - 年化波动率*0.2,三者量纲不一致,
# 波动率项(年化 0.3~0.6)长期压制动量项(0.1~0.5),导致打分偏向低波动而非高动量。
# 现改为先做横截面 z-score 标准化再加权,消除量纲差异。
SCORE_WEIGHT_MOMENTUM = 0.5     # 60 日动量权重
SCORE_WEIGHT_MOMENTUM_20 = 0.3  # 20 日动量权重
SCORE_WEIGHT_VOLATILITY = 0.2   # 波动率惩罚权重(从得分中减去)

# ==================== 小市值价值策略参数 ====================
# 依据:A股价格动量长期负 IC(短期反转主导),真正有效的是小市值 + 低估值 + 短期反转。
# 2024「国九条」退市新规后,必须叠加严格风控过滤,规避退市/ST/面值/财务风险。
# 调仓:周度(每 5 个交易日)。仓位:等权,持有 SMALLCAP_TOP_N 只。
SMALLCAP_TOP_N = 12              # 持仓数量(等权)
SMALLCAP_REBALANCE_DAYS = 5     # 调仓周期(交易日),5≈周度
# "折中:小市值为主"——设市值下限规避最小微盘(国九条退市/流动性高风险区),设上限保持小盘暴露
SMALLCAP_MIN_MKTCAP = 20e8      # 流通市值下限(元),约 20 亿,排除最小微盘
SMALLCAP_MAX_MKTCAP = 200e8     # 流通市值上限(元),约 200 亿,保持小盘风格
# 因子权重(对横截面分位 rank 加权,小市值为主)
SMALLCAP_W_SIZE = 0.5           # 小市值(市值越小越优)
SMALLCAP_W_PB = 0.25            # 低估值(PB 越低越优)
SMALLCAP_W_REVERSAL = 0.25      # 短期反转(过去 N 日跌得多者下期反弹)
SMALLCAP_W_OBV = 0.10           # OBV 资金累积加分,只增强排序,不作为硬过滤
SMALLCAP_REVERSAL_DAYS = 20     # 短期反转回看天数
# 国九条风控过滤阈值
SMALLCAP_MIN_PRICE = 2.0        # 最低股价(元),缓冲 1 元面值退市风险
SMALLCAP_MAX_PRICE = 30.0       # 最高股价(元),小资金避免单手金额过大
SMALLCAP_MIN_PB = 0.0           # PB 必须为正(净资产为负有退市风险)

# ==================== ETF / 行业 RPS 轮动参数 ====================
RPS_LOOKBACK_DAYS = 20          # RPS 回看周期(日频)
RPS_MIN_SCORE = 85.0            # 入选最低相对强弱分位
RPS_TOP_N = 2                   # 每日最多持有/买入数量
RPS_MIN_AVG_VOLUME = 500_000    # 20 日均量下限


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
LIVE_INITIAL_SCAN_DELAY_MINUTES = int(os.getenv("LIVE_INITIAL_SCAN_DELAY_MINUTES", "5"))

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
    """判断是否为 ETF。

    当前系统**仅交易沪深 A 股股票**,不交易 ETF,因此本函数有意恒返回 False。
    这意味着 risk/control、rules/engine、rules/position 中所有 ETF 专属分支
    (3bp 滑点、单票上限 45%、ETF 印花税豁免)目前都不会生效——这是当前
    标的范围下的预期行为,并非遗漏。

    中期 TODO:接入真实 ETF 数据源后,按代码前缀实现真正判断
    (沪市 ETF:51/56/58 开头;深市 ETF:15 开头),并恢复上述分支。
    """
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
