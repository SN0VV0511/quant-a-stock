# A 股量化回测与虚拟盘观察系统

这是一个面向学习、验证和观察期演练的 A 股量化项目。当前能力覆盖历史回测、全市场候选扫描、虚拟盘实时盯盘、五层风控、结构化交易事件、Web 仪表盘，以及 QMT / miniQMT 接入前的 dry-run 适配器。

本项目默认只运行虚拟盘，不会发送真实委托。任何实盘接入都必须先经过一个月以上观察期、健康检查和人工确认。

## 适用场景

- 在 OpenClaw、终端或本地 IDE 中快速运行 A 股回测。
- 盘中使用虚拟盘观察策略信号、风控拒绝、成交记录和账户快照。
- 用一个月观察期验证策略稳定性，再决定是否进入 QMT dry-run 联调。
- 复盘每日交易、收益、回撤、日志异常和事件可追溯性。

## 功能概览

- 免费数据源：BaoStock 股票历史行情、腾讯股票实时行情、AKShare 交易日历/ETF/行业指数。
- 回测能力：双均线策略、手续费、印花税、滑点、夏普比率、最大回撤。
- 策略能力：组合信号、RSI、ETF 动量代理、ETF / 行业 RPS 轮动、全市场扫描候选池。
- 虚拟盘：本地 JSON 持仓、交易流水、T+1、整手、涨跌停和仓位限制。
- 风控：标的范围、白名单买入、ST / 停牌过滤、资金检查、回撤和单日亏损控制。
- 观测：`trade_events.jsonl` 记录信号、风控、成交和账户快照。
- Web：本地仪表盘查看账户、交易、日志、健康检查和观察期状态。
- QMT：当前只保留 dry-run 适配层，真实下单路径默认阻断。

## 标的范围

AKShare 只是 Python 数据接口库，不是股票标的。本项目当前交易标的限定为沪深 A 股股票和 ETF：

- 沪市股票：`600`、`601`、`603`、`605`、`688`、`689` 开头。
- 深市股票：`000`、`001`、`002`、`003`、`300`、`301` 开头。
- 沪市 ETF：`51`、`56`、`58` 开头；深市 ETF：`15` 开头。
- 实时全市场扫描仍只扫描沪深 A 股股票；ETF 走日频 RPS/动量模块；行业指数只做强弱观察，不直接下单。
- 实时虚拟盘会过滤指数、基金、港股、美股、B 股、北交所和市场前缀不一致的代码。

## OpenClaw 快速使用

在 OpenClaw 中打开该仓库后，建议固定在项目根目录执行命令：

```bash
cd /Users/xueds/Python/quant-a-stock
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

先跑一次测试和烟测，确认环境可用：

```bash
pytest -q
python scripts/paper_smoke_run.py --json
```

OpenClaw 里常用的 Agent 指令可以直接写成：

```text
在 /Users/xueds/Python/quant-a-stock 中运行 pytest -q，若失败请定位原因并只改相关文件。
```

```text
读取 reports/2026-05-29_full.txt 和 logs/live_today.log，按时间线总结风控拒绝、成交和异常。
```

```text
检查虚拟盘观察期状态，执行 python scripts/paper_status.py --json 并解释风险项。
```

注意事项：

- 观察期默认使用 `BROKER_MODE=paper`，不要在 OpenClaw 中直接开启真实交易。
- `.env`、`logs/`、缓存和事件流水属于本地运行环境，不应提交。
- `data/portfolio_state.json` 和 `data/trade_log.json` 当前是虚拟盘状态文件，拉取代码或切换分支前先执行 `git status`，避免覆盖正在观察的持仓状态。

## 快速回测

```bash
python main.py
python main.py 600519
python main.py 000858 20230101 20240101
```

输出内容通常包括：

- 回测指标：收益率、最大回撤、夏普比率、交易次数。
- 图表文件：净值曲线、回撤图、K 线图。
- CSV 结果：交易明细和回测序列。

仪表盘的“策略回测对比”会读取 `reports/backtest_latest.json`。部署后如果该文件缺失或超过
`BACKTEST_AUTO_MAX_AGE_HOURS`，Web 服务会自动在后台触发 `scripts.strategy_ab` 生成；观察期
守护脚本收盘后也会同步刷新一次。需要手动强制刷新时再执行：

```bash
python -m scripts.strategy_ab 120
```

可通过环境变量调整自动回测：

- `BACKTEST_AUTO_GENERATE=false`：关闭自动生成。
- `BACKTEST_AUTO_UNIVERSE_SIZE=120`：自动回测抽样股票数。
- `BACKTEST_AUTO_MAX_AGE_HOURS=168`：回测结果最大缓存时间。

## 虚拟盘实时运行

开始新观察期前，先预览会备份哪些旧状态文件：

```bash
python scripts/paper_reset.py --json
```

确认开始新的虚拟盘观察期时再执行：

```bash
python scripts/paper_reset.py --confirm --json
```

盘中手动运行：

```bash
python live_runner.py --broker paper
```

常用参数：

- `--watch-interval 4`：盯盘刷新秒数。
- `--scan-interval 600`：候选池扫描间隔。
- `--top-n 30`：候选股数量。
- `--ignore-calendar`：忽略交易日历，便于非交易时段联调。

虚拟盘运行流程：

1. 开盘后先执行一次 ETF/RPS 日频轮动，使用 AKShare 拉取 ETF 与行业指数历史数据。
2. ETF/RPS 生成的买卖订单先经过风控，再进入 `PaperBrokerAdapter` 虚拟成交。
3. 开盘后延迟确认全市场扫描，形成股票候选池。
4. 盯盘线程持续更新持仓价格和候选股价格。
5. 持仓触发止损、止盈或策略卖出时生成卖出订单。
6. 候选股触发组合策略买入信号时生成买入订单。
7. 所有信号、风控、成交、RPS 状态和账户快照写入结构化文件，供 1-2 个月观察期验收。

ETF/RPS 观察文件：

- `data/rps_state.json`：每日 ETF/RPS 数据源状态、ETF 入选、行业强弱和订单结果。
- `data/trade_events.jsonl`：包含 `rps_rotation_completed`、`signal`、`risk_approved`、`execution` 等事件。
- `reports/daily_YYYYMMDD.txt`：收盘日报会包含 ETF/RPS 摘要。

观察期核心流水文件：

- `data/trade_events.jsonl`：启动 `live_runner.py` 创建；每次扫描、RPS 轮动、信号、风控、成交、账户快照事件都会追加。
- `data/portfolio_snapshots.jsonl`：初始化虚拟盘或创建 `PositionManager` 时先创建空文件；盘中每 5 分钟快照、收盘快照和手动 `save_snapshot()` 时追加。
- 空文件表示“系统已初始化但还没产生对应事件/快照”；文件缺失通常表示数据目录未挂载或初始化没有跑。

## 后台守护运行

无人值守观察期建议使用后台服务管理脚本，避免重复启动多个守护进程：

```bash
python scripts/paper_service.py status --json
python scripts/paper_service.py start --json
python scripts/paper_service.py stop --json
python scripts/paper_service.py restart --json
```

守护脚本会在交易日运行窗口内启动 `live_runner.py --broker paper`，收盘后执行健康检查和复盘摘要：

```bash
python scripts/paper_daemon.py --once --dry-run --json
python scripts/paper_daemon.py
```

如果只想在 OpenClaw 中联调流程，可以加 `--ignore-calendar`：

```bash
python scripts/paper_service.py start --ignore-calendar --json
```

## Web 仪表盘

启动本地仪表盘：

```bash
python web/app.py 8888
```

浏览器访问：

```text
http://127.0.0.1:8888
```

仪表盘接口：

- `/`：页面入口。
- `/api/status`：账户、持仓、交易和日志摘要。
- `/api/observation`：后台服务、健康检查、30 日复盘、QMT dry-run 验收和最新日志。

## 每日巡检

开盘前建议执行：

```bash
python scripts/paper_healthcheck.py --json
python scripts/paper_service.py status --json
```

盘中出现异常时查看：

```bash
python scripts/paper_status.py --json
tail -n 120 logs/live_today.log
tail -n 120 logs/paper_daemon_service.log
```

收盘后执行严格检查：

```bash
python scripts/paper_healthcheck.py --strict-snapshot --strict-events --strict-report --max-snapshot-age-minutes 1440
python scripts/monthly_review.py --days 30
python scripts/paper_acceptance.py --days 30 --min-snapshot-days 20 --json
```

健康检查关注点：

- 现金不能为负。
- 持仓必须是沪深 A 股且为整手。
- 总仓位不能超过配置阈值。
- 快照、日报和账户状态要一致。
- 每笔成交都能追溯到信号、风控通过和成交回报。

## 月度观察期验收

进入 QMT dry-run 前建议至少满足：

- 连续运行 20 个以上交易日。
- `paper_healthcheck.py` 无失败项。
- `data/rps_state.json` 最近一次状态为 `ok` 且 ETF 数据成功加载。
- `monthly_review.py` 指标可解释，最大回撤和交易次数符合策略预期。
- `paper_acceptance.py` 输出 `ready_for_qmt_dry_run=true`。
- `trade_events.jsonl` 能串起每笔成交对应的信号、风控和成交。
- 收盘日报 `reports/daily_YYYYMMDD.txt` 的总资产与最新账户快照一致。

## 配置说明

核心配置在 `config/settings.py`：

- `INITIAL_CAPITAL`：回测和虚拟盘初始资金。
- `MAX_TOTAL_POSITION`：总仓位上限。
- `MAX_SINGLE_STOCK`：单只股票仓位上限。
- `CASH_BUFFER`：现金缓冲。
- `DAILY_LOSS_THRESHOLD`：单日最大亏损阈值。
- `MAX_DRAWDOWN_THRESHOLD`：最大回撤阈值。
- `DEFAULT_UNIVERSE`：普通策略默认白名单。

环境变量配置见 `.env.example`：

```bash
BROKER_MODE=paper
LIVE_TRADING_ENABLED=false
LIVE_WATCH_INTERVAL_SECONDS=4
LIVE_SCAN_INTERVAL_SECONDS=600
QMT_ACCOUNT_ID=
QMT_CLIENT_PATH=
```

安全默认值：

- `BROKER_MODE=paper`：只使用虚拟盘。
- `LIVE_TRADING_ENABLED=false`：禁止真实下单。
- 即使误设 `LIVE_TRADING_ENABLED=true`，当前 `QmtBrokerAdapter` 也会拒绝连接真实通道。

## 关键文件

```text
config/settings.py              全局配置
main.py                         历史回测入口
live_runner.py                  虚拟盘实时盯盘入口
daily_runner.py                 每日执行入口
paper_trading.py                虚拟盘示例入口
data/ak_loader.py               AKShare / 腾讯 / BaoStock 数据加载
data/bs_worker.py               BaoStock 子进程隔离
risk/control.py                 风控模块
rules/position.py               持仓状态管理
rules/engine.py                 A 股交易规则
strategies/                     策略模块
trading/                        Broker 适配器和交易模型
scripts/                        巡检、守护、复盘、验收脚本
web/                            Web 仪表盘
reports/                        日报、策略总结和回测输出
tests/                          单元测试和集成测试
```

运行态文件：

```text
data/portfolio_state.json       当前现金、持仓、交易记录和日快照
data/trade_log.json             Web 仪表盘读取的交易流水
data/trade_events.jsonl         信号、风控、成交事件流水
data/portfolio_snapshots.jsonl  账户快照流水
data/paper_daemon.pid           后台守护进程 PID 元数据
logs/live.log                   实时运行累计日志
logs/live_today.log             当日实时日志
logs/paper_daemon_service.log   后台服务日志
```

## 策略与风控摘要

双均线策略：

- 金叉买入：短均线上穿长均线。
- 死叉卖出：短均线下穿长均线。

实时组合策略：

- 使用全市场扫描生成候选池。
- 对候选股进行组合信号判断。
- 对持仓执行止损、止盈和策略卖出检查。

风控规则：

- 只允许沪深 A 股股票代码。
- 普通策略买入受默认白名单限制。
- 全市场扫描策略买入可绕过固定白名单。
- 卖出不受买入白名单限制，但仍受 T+1、停牌、跌停和持仓数量限制。
- 买入前检查现金、单票仓位、总仓位和现金缓冲。

## QMT / miniQMT 接入状态

当前版本只实现 QMT dry-run 适配器，不会发送真实委托。`QmtBrokerAdapter` 用于统一接口和字段映射验证，不用于实盘。

接入真实 QMT 前必须完成：

- 资金、持仓、委托、成交回报查询的 dry-run 对齐。
- QMT 返回字段映射到 `OrderIntent`、`ExecutionReport`、`PortfolioSnapshot`。
- 实盘开关、账户、资金规模、风控阈值和人工确认流程。
- 至少一个月虚拟盘观察期验收通过。

## 开发与测试

运行全部测试：

```bash
pytest -q
```

运行关键集成测试：

```bash
pytest -q tests/test_paper_broker_and_risk.py
pytest -q tests/test_scripts.py
```

语法检查：

```bash
python -m py_compile live_runner.py tests/test_paper_broker_and_risk.py
```

提交前建议：

```bash
git status --short
pytest -q
```

## Git 协作建议

拉取最新代码：

```bash
git status --short --branch
git pull --ff-only
```

只提交文档：

```bash
git add README.md
git commit -m "docs: 完善 OpenClaw 使用说明"
git push origin main
```

只提交代码修复：

```bash
git add live_runner.py tests/test_paper_broker_and_risk.py
git commit -m "fix: 降低虚拟盘重复拒单日志"
git push origin main
```

注意：如果虚拟盘正在观察期运行，不建议随意提交或覆盖 `data/portfolio_state.json`、`data/trade_log.json` 等运行态文件。

## 常见问题

### 为什么卖出被 T+1 拒绝？

A 股股票当日买入不能当日卖出。日志中出现 `T+1 限制（买入日: YYYYMMDD）` 属于正常风控结果。

### 为什么买入被白名单拒绝？

普通策略买入会受 `DEFAULT_UNIVERSE` 限制。全市场扫描策略使用 `全市场扫描+组合策略`，可以绕过固定白名单，但仍会经过 A 股代码、资金、仓位、ST、停牌和涨跌停检查。

### 为什么总仓位限制拒绝买入？

`MAX_TOTAL_POSITION` 默认是 90%。当持仓市值已经接近或超过该阈值时，新增买入会被拒绝。

### BaoStock 或行情获取失败怎么办？

先看日志和缓存回退情况：

```bash
tail -n 120 logs/live_today.log
python scripts/paper_healthcheck.py --json
```

如果是网络、数据源或子进程超时问题，优先保持虚拟盘不真实下单，再检查 `data/ak_loader.py` 和 `data/bs_worker.py` 的错误日志。

### Web 仪表盘没有数据怎么办？

确认虚拟盘状态文件和日志存在：

```bash
python scripts/paper_status.py --json
ls data logs reports
```

如果刚初始化观察期，空仓和无交易是正常状态。

## 风险声明

本项目仅供学习、研究和工程验证使用，不构成投资建议。任何策略表现都不代表未来收益。真实交易前必须进行充分测试、人工复核和风险评估。
