# 📈 A 股量化回测系统

小白友好的 A 股量化回测项目，开箱即用。

## 功能

- ✅ 免费获取沪深 A 股股票行情数据（BaoStock 历史、腾讯实时、AKShare 交易日历）
- ✅ 双均线交叉策略（金叉买入 / 死叉卖出）
- ✅ 完整回测引擎（手续费、印花税、夏普比率、最大回撤）
- ✅ 自动生成图表（净值曲线、回撤图、K线图）
- ✅ 输出 CSV 结果
- ✅ 虚拟盘实时盯盘、风控审批、结构化成交记录
- ✅ QMT / miniQMT dry-run 适配器预留，默认阻断真实下单

## 标的范围

AKShare 不是股票标的，它只是 Python 数据接口库。本项目当前交易标的限定为沪深 A 股股票：

- 沪市股票：`600`、`601`、`603`、`605`、`688`、`689` 开头。
- 深市股票：`000`、`001`、`002`、`003`、`300`、`301` 开头。
- 当前实时虚拟盘会过滤指数、ETF、基金、港股、美股、B 股、北交所和市场前缀不一致的代码。

## 快速开始

```bash
cd quant-a-stock
pip install -r requirements.txt
python main.py              # 默认回测平安银行
python main.py 600519       # 回测贵州茅台
python main.py 000858 20230101 20240101  # 指定时间范围
```

## 虚拟盘实时运行

先安装依赖，并确认 `.env` 沿用虚拟盘配置：

```bash
pip install -r requirements.txt
cp .env.example .env
python scripts/paper_reset.py --json          # 只预览将备份哪些旧状态文件
python scripts/paper_reset.py --confirm --json # 开始新观察期前再执行
python live_runner.py --broker paper
python web/app.py 8888
```

无人值守观察期可以用守护脚本托管交易日运行：

```bash
python scripts/paper_daemon.py --once --dry-run --json
python scripts/paper_daemon.py
```

更适合长期观察期的后台管理入口：

```bash
python scripts/paper_service.py status --json
python scripts/paper_service.py start --json
python scripts/paper_service.py stop --json
python scripts/paper_service.py restart --json
python scripts/paper_status.py --json
```

虚拟盘默认做盘中低频运行：
- 盯盘线：每 `LIVE_WATCH_INTERVAL_SECONDS` 秒刷新持仓和候选股。
- 扫描线：每 `LIVE_SCAN_INTERVAL_SECONDS` 秒做一次候选池扫描。
- 所有订单都会先经过风控，再交给 `PaperBrokerAdapter` 虚拟成交。

运行期间关键文件：
- `data/portfolio_state.json`: 当前现金、持仓、交易记录、日快照。
- `data/trade_log.json`: Web 仪表盘读取的交易流水。
- `data/trade_events.jsonl`: 信号、风控、成交事件流水。
- `data/portfolio_snapshots.jsonl`: 账户快照流水。
- `data/paper_daemon.pid`: 后台守护进程 PID 元数据。
- `logs/live.log` 和 `logs/live_today.log`: 实时运行日志。
- `logs/paper_daemon_service.log`: 后台服务启动和守护输出日志。

Web 仪表盘同时提供 `/api/observation`，返回后台服务、健康检查、30 日复盘、QMT dry-run 验收和最新日志的统一状态。

## 每日巡检与月度复盘

建议每天开盘前、盘中、收盘后各跑一次健康检查：

```bash
python scripts/paper_smoke_run.py --json
python scripts/paper_healthcheck.py
python scripts/paper_healthcheck.py --strict-snapshot --strict-events --strict-report --max-snapshot-age-minutes 1440
```

虚拟盘观察一个月后，用复盘脚本检查收益、回撤、交易次数和胜率：

```bash
python scripts/monthly_review.py --days 30
python scripts/monthly_review.py --days 30 --json
python scripts/paper_acceptance.py --days 30 --min-snapshot-days 20 --json
```

进入 QMT 前的最低门槛建议：
- 连续运行 20 个以上交易日无状态损坏、无负现金、无超仓。
- `trade_events.jsonl` 能串起每笔成交对应的信号、风控通过和成交回报。
- 收盘日报 `reports/daily_YYYYMMDD.txt` 的总资产与最新账户快照一致。
- 月度最大回撤、单日亏损、交易次数与策略预期一致。
- `paper_healthcheck.py` 通过，`monthly_review.py` 指标可解释，`paper_acceptance.py` 给出 `ready_for_qmt_dry_run=true`。

## 项目结构

```
quant-a-stock/
├── main.py                  # 主入口
├── config/settings.py       # 配置文件
├── data/data_loader.py      # 数据加载
├── strategies/ma_cross.py   # 均线策略
├── backtest/engine.py       # 回测引擎
├── backtest/visualizer.py   # 可视化
├── trading/                 # Broker 适配器与标准交易模型
├── risk/                    # 风控模块
├── rules/                   # A 股交易规则和持仓状态
├── scripts/                 # 健康检查和复盘工具
├── web/                     # 虚拟盘仪表盘
├── reports/                 # 输出报告
└── requirements.txt         # 依赖
```

## 策略说明

**双均线交叉策略 (MA5/MA20)**:
- 🟢 金叉买入：5日均线上穿20日均线
- 🔴 死叉卖出：5日均线下穿20日均线

## 配置

编辑 `config/settings.py` 修改：
- 默认股票、回测时间
- 初始资金、手续费率
- 均线周期

实时虚拟盘和 QMT 预留参数也可通过环境变量配置：
- `BROKER_MODE=paper`: 默认只使用虚拟盘。
- `LIVE_TRADING_ENABLED=false`: 默认禁止真实下单。
- `LIVE_WATCH_INTERVAL_SECONDS=4`: 盯盘刷新间隔。
- `LIVE_SCAN_INTERVAL_SECONDS=600`: 候选股扫描间隔。
- `QMT_ACCOUNT_ID`、`QMT_CLIENT_PATH`: QMT dry-run 预留参数。

## QMT / miniQMT 接入说明

当前版本只实现 QMT dry-run 适配器，不会发送真实委托。即使设置
`LIVE_TRADING_ENABLED=true`，`QmtBrokerAdapter` 也会拒绝连接真实通道，避免观察期前误下单。

后续接入实盘前，需要先完成：
- QMT 查询资金、持仓、委托、成交回报的 dry-run 对齐。
- 虚拟盘和 QMT 返回字段映射到同一套 `OrderIntent`、`ExecutionReport`、`PortfolioSnapshot`。
- 人工确认实盘开关、账户、资金规模和风控阈值。

## 注意

⚠️ 本项目仅供学习研究，不构成投资建议
