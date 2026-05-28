"""
A 股量化交易系统 - 全局配置
"""

import os

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ==================== 资金配置 ====================
INITIAL_CAPITAL = 10000.0  # 初始资金（元）

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
CACHE_DIR = os.path.join(DATA_DIR, "cache")
REPORT_DIR = os.path.join(BASE_DIR, "reports")
LOG_DIR = os.path.join(BASE_DIR, "logs")

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


def is_chinext(code):
    """判断是否为创业板股票（30xxxx）"""
    raw = code[2:] if code[:2] in ("sh", "sz") else code
    return raw.startswith("300")


def is_shenzhen(code):
    """判断是否为深市股票"""
    return code.startswith("sz") or code.startswith("0") or code.startswith("3")
