# A 股量化回测与虚拟盘观察系统

这是一个面向学习、验证和观察期演练的 A 股量化项目。默认账户策略为低频
`robust_v2`：宽基 ETF 中期趋势、沪深主板盈利收益率/适度规模/低波增强、现金不低于
20%，使用 SQLite 单账本。
旧 Combo、RPS 和小市值策略只保留为研究基线，不再默认写虚拟盘账户。

本项目默认只运行虚拟盘，不会发送真实委托。任何实盘接入都必须先经过一个月以上观察期、健康检查和人工确认。

## 适用场景

- 在 OpenClaw、终端或本地 IDE 中快速运行 A 股回测。
- 盘中使用虚拟盘观察策略信号、风控拒绝、成交记录和账户快照。
- 用一个月观察期验证策略稳定性，再决定是否进入 QMT dry-run 联调。
- 复盘每日交易、收益、回撤、日志异常和事件可追溯性。

## 功能概览

- 免费数据源：BaoStock 股票历史行情、腾讯股票实时行情、AKShare 交易日历/ETF/行业指数。
- 回测能力：双均线策略、手续费、印花税、滑点、夏普比率、最大回撤。
- 策略能力：宽基 ETF 绝对趋势、主板多因子增强、ETF / 行业 RPS 研究基线、全市场扫描候选池。
- 虚拟盘：SQLite 单账本、分批 T+1、幂等订单、单实例写租约、整手和分证券费用。
- 风控：标的范围、白名单买入、ST / 停牌过滤、资金检查、回撤和单日亏损控制。
- 观测：`trade_events.jsonl` 记录信号、风控、成交和账户快照。
- Web：本地仪表盘查看账户、交易、日志、健康检查和观察期状态。
- QMT：当前只保留 dry-run 适配层，真实下单路径默认阻断。

## 标的范围

AKShare 只是 Python 数据接口库，不是股票标的。本项目当前交易标的限定为沪深 A 股股票和 ETF：

- 沪市股票：`600`、`601`、`603`、`605`、`688`、`689` 开头。
- 深市股票：`000`、`001`、`002`、`003`、`300`、`301` 开头。
- 沪市 ETF：`51`、`56`、`58` 开头；深市 ETF：`15` 开头。
- 默认 5 万资金 `robust_v2` 账户只直接买入沪深主板股票和宽基 ETF，不直接买入创业板
  `300/301`、科创板 `688/689`、可转债、港股通或融资融券标的。
- 如账户已确认开通额外权限，可复制 `config/permissions.example.yaml` 为
  `config/permissions.yaml`，或设置 `ALLOW_CHINEXT_STOCKS=true`、
  `ALLOW_STAR_MARKET_STOCKS=true` 等环境变量；`robust_v2` 仍不会直接买入这些板块股票。
- `robust_v2` 的 ETF 执行池只含沪深 300、中证 500、中证 1000、创业板和科创 50
  等宽基品种；半导体等行业 ETF、行业指数和 RPS 结果只做研究观察，不接入该账户。
- 实时全市场扫描只扫描沪深 A 股股票。腾讯批量行情用于全市场当日价格、成交量和
  停牌状态预筛，BaoStock 用于候选历史行情和估值字段；扫描审计会记录
  `tencent_quote_count`，不再出现“配置了腾讯行情但无法证明实际使用”的情况。
- 实时虚拟盘会过滤指数、基金、港股、美股、B 股、北交所和市场前缀不一致的代码。

## OpenClaw 快速使用

在 OpenClaw 中打开该仓库后，建议固定在项目根目录执行命令：

```bash
cd /Users/xueds/Python/quant-a-stock
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
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
读取 reports/daily_v2_YYYYMMDD.txt 和 logs/robust_v2.log，按时间线总结风控拒绝、成交和异常。
```

```text
检查虚拟盘观察期状态，执行 python scripts/paper_status.py --json 并解释风险项。
```

注意事项：

- 观察期默认使用 `BROKER_MODE=paper_v2`，不要在 OpenClaw 中直接开启真实交易。
- `.env`、`logs/`、缓存和事件流水属于本地运行环境，不应提交。
- `data/paper_v2.db` 是当前唯一虚拟盘账户；拉取代码或切换分支不会覆盖运行卷，但操作前仍应执行 `git status` 并确认守护进程状态。

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

仪表盘的“策略回测对比”只展示 `robust_v2` 的版本化历史股票池滚动样本外结果。
部署后如果 `reports/backtest_latest.json` 缺失或过期，Web 会调用
`scripts.robust_walk_forward`；历史股票池不足约三年时明确报错，不再展示旧策略或用
当前股票池回填历史。手动刷新命令：

```bash
python -m scripts.robust_walk_forward \
  --data-root data/robust_research \
  --output reports/backtest_latest.json
```

可通过环境变量调整自动回测：

- `BACKTEST_AUTO_GENERATE=false`：关闭自动生成。
- `BACKTEST_AUTO_UNIVERSE_SIZE=120`：旧兼容参数，`robust_v2` 不做当前股票池抽样。
- `BACKTEST_AUTO_MAX_AGE_HOURS=168`：回测结果最大缓存时间。

## robust_v2 虚拟盘运行

首次启用前先预览旧 JSON/日志归档；确认后只复制到只读目录，原文件不会删除或清空：

```bash
python scripts/paper_v2_init.py --json
python scripts/paper_v2_init.py --confirm --json
```

前台运行唯一守护实例：

```bash
./start_live.sh
# 或
python robust_runner.py daemon
# 固定一个月观察期；结束日次日会正常退出
python robust_runner.py daemon --observation-end-date 20260823
```

部署应由 Docker、systemd、supervisor 或 tmux 托管。进程必须先获得 SQLite 写租约；
第二个实例会直接拒绝启动，不再使用跨目录 `pkill`。Docker 入口已经切换为
`robust_runner.py daemon`。

macOS 可使用仓库内的一月观察期 LaunchAgent；模板固定使用 50,000 元虚拟盘，并在
`2026-08-23` 结束日之后正常退出：

```bash
cp deploy/launchd/com.xueds.quant-a-stock.paper-v2.plist \
  ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.xueds.quant-a-stock.paper-v2.plist
```

也可以直接使用生产化容器编排；它会先初始化单账本，再启动守护进程和只读面板：

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f paper-live
```

容器以非 root 用户运行，代码目录只读，`data/logs/reports` 使用独立持久卷；面板默认
只绑定 `127.0.0.1:8888`。如通过 HTTPS 反向代理公开访问，请设置
`DASHBOARD_COOKIE_SECURE=true`。

运行时序固定如下：

1. 每个交易日 15:05 后更新一次只读个股候选观察；默认只在每月最后一个交易日用
   T 日复权数据生成正式 `TargetPortfolio`，不在收盘直接下单。
2. T+1 日 09:35 后，统一分配器把目标权重转换为整手订单并保留目标现金。
3. 盘中每 60 秒只检查行情健康和 7% 股票/10% ETF 灾难止损。
4. 普通退出必须由收盘目标确认；旧 `COMBO_DEFENSIVE_EXIT`、追涨和盘中补仓不接入账户。
5. 前收盘偏差超过 2%、缺少上一交易日或数据陈旧时，该标的停止交易。
6. 暂时性行情、停牌、涨跌停或委托失败不会吃掉信号，当日按状态机继续重试；跨日旧
   信号自动失效，避免追单。

常用的单步诊断命令：

```bash
python robust_runner.py status
python robust_runner.py preview-scan
python robust_runner.py signal --date 20260710 --force
python robust_runner.py execute --date 20260713
python robust_runner.py report --date 20260713
```

`preview-scan` 自动使用最近一个已完成收盘的交易日，只更新 `data/scans/` 和
`data/universe/` 审计文件，不写 `signals`、订单或持仓；它也是面板“安全预览扫描”
按钮调用的命令。`signal --force` 会生成正式待执行信号，不能用于只读预览。

旧入口默认拒绝写账户。只有复现研究基线时，才可临时设置
`ENABLE_LEGACY_ACCOUNT_WRITERS=true`；不要与 `paper_v2` 守护实例同时部署。

## robust_v2 样本外参数选择

研究数据目录必须包含按代码保存的 `etf/`、`stock/` 行情，按交易日保存的
`universe/robust_v2_YYYYMMDD.json`，以及必须提供的 `benchmark/000300.csv`；
`benchmark/000905.csv` 可选。股票池版本不足约 3 年时脚本会拒绝运行：

```bash
python scripts/robust_walk_forward.py \
  --data-root data/robust_research \
  --legacy-baseline reports/backtest_latest.json
```

脚本固定评估 32 组参数，使用 24 个月训练后接 6 个月滚动验证，最后 12 个月
完全锁定；门槛不通过或双倍滑点下落后于基准时，自动降级为最多 48% 宽基 ETF +
现金。
通过后会写入 `data/robust_v2_selected.json`，下次启动时由 `robust_runner` 自动加载；
文件缺失时使用本计划的推荐默认参数。

策略取舍依据：

- 中国 A 股因子研究显示，盈利收益率比账面市值比更能解释本地价值效应，并建议排除
  最小 30% 公司：[Size and value in China](https://www.sciencedirect.com/science/article/pii/S0304405X19300625)。
- 纳入交易成本后，市场、规模和按月更新的盈利收益率是更简洁的组合：
  [Factor models for Chinese A-shares](https://www.sciencedirect.com/science/article/pii/S105752192300491X)。
- 中国市场周/月价格动量并不稳定，较明显的是短周期日内延续，因此本项目不把个股
  中期价格动量或深度反转作为主排序因子：
  [Daily Momentum and New Investors in an Emerging Stock Market](https://www.nber.org/papers/w31839)。

## Web 仪表盘

启动本地仪表盘：

```bash
python web/app.py 8888
```

浏览器访问：

```text
http://127.0.0.1:8888
```

“候选股雷达”直接读取结构化扫描快照，展示主板总量、粗筛数量、完整历史数量、
合格候选和主要淘汰原因；“系统状态”使用 `paper_v2` SQLite 写租约作为跨容器
守护心跳，不再依赖旧扫描线程日志。

仪表盘接口：

- `/`：页面入口。
- `/healthz`：无需登录的容器存活探针，不返回账户数据。
- `/api/status`：账户、持仓、交易和日志摘要。
- `/api/observation`：后台服务、健康检查、30 日复盘、QMT dry-run 验收和最新日志。

### React 前端开发

仪表盘前端位于 `web/frontend`，生产构建输出到 `web/dist`，由 `python web/app.py 8888` 托管。

首次安装依赖：

```bash
cd web/frontend
npm install
```

本地开发时先启动 Python 后端，再启动 Vite：

```bash
python web/app.py 8888
cd web/frontend
npm run dev
```

生产构建：

```bash
cd web/frontend
npm run build
python ../../web/app.py 8888
```

前端测试：

```bash
cd web/frontend
npm run test
```

## 每日巡检

开盘前建议执行：

```bash
python robust_runner.py status
```

盘中出现异常时查看：

```bash
tail -n 120 logs/robust_v2.log
```

收盘后执行严格检查：

```bash
python scripts/monthly_review.py --start 20260710 --end 20260810 --json
python scripts/paper_v2_acceptance.py --start 20260710 --end 20260810
```

健康检查关注点：

- 零 T+1/限制板块违规、零重复幂等订单、零旧策略来源成交。
- 同时活动的账户写会话不得超过一个，净值不得疑似重置到 50,000 元。
- 每月成交不超过 24 笔，成本不超过平均净值 0.5%。
- 满 60 个交易日后最大回撤目标不超过 10%，并与沪深 300 基准比较。

## 月度观察期验收

进入 QMT dry-run 前建议至少满足：

- 连续运行 20 个以上交易日。
- `paper_v2_acceptance.py` 20 日运维门槛全部通过。
- 再完成至少 60 个交易日绩效观察，并比较净收益、Calmar、换手和成本。
- `monthly_review.py` 必须指定起止日期；容器重启产生的多个运行批次仍按同一账户连续复盘。
- 未完成复盘前不切换真实资金。

## 配置说明

核心配置在 `config/settings.py`：

- `INITIAL_CAPITAL`：回测和虚拟盘初始资金。
- `ROBUST_V2_MAX_TOTAL_POSITION`：V2 总仓位上限，默认 80%。
- `ROBUST_V2_ETF_TARGET` / `ROBUST_V2_STOCK_TARGET`：默认 48% / 32%。
- `ROBUST_V2_MAX_SINGLE_ETF` / `ROBUST_V2_MAX_SINGLE_STOCK`：默认 24% / 16%。
- `ROBUST_V2_MIN_STOCK_ORDER_AMOUNT`：V2 个股最低订单，默认 4000 元，确保
  5 万账户的 16% 目标经整手取整后仍可执行。
- `ROBUST_V2_REBALANCE_DAYS`：低频调仓档位，默认 `20`（月末）。
- `ROBUST_V2_DAILY_JOB_RETRY_SECONDS`：收盘数据任务失败后的重试退避，默认
  900 秒，避免每分钟重复请求上游。
- `ROBUST_V2_MIN_CASH`：现金下限，默认 20%。
- `ROBUST_V2_LEDGER_PATH`：唯一 SQLite 账本。
- `MIN_STOCK_ORDER_AMOUNT`：旧策略股票最低建议买入成交额，默认 8000 元。
- `MIN_ETF_ORDER_AMOUNT`：ETF 最低建议买入成交额，默认 5000 元。
- `PERMISSIONS_FILE`：可选账户权限配置文件，默认读取 `config/permissions.yaml`。
- `CASH_BUFFER`：现金缓冲。
- `DAILY_LOSS_THRESHOLD`：单日最大亏损阈值。
- `MAX_DRAWDOWN_THRESHOLD`：最大回撤阈值。
- `DEFAULT_UNIVERSE`：普通策略默认白名单。

环境变量配置见 `.env.example`：

```bash
BROKER_MODE=paper_v2
ROBUST_V2_LEDGER_PATH=data/paper_v2.db
ENFORCE_T1=true
LIVE_TRADING_ENABLED=false
ROBUST_V2_MONITOR_INTERVAL_SECONDS=60
ROBUST_V2_ETF_TARGET=0.48
ROBUST_V2_STOCK_TARGET=0.32
ROBUST_V2_MIN_STOCK_ORDER_AMOUNT=4000
ROBUST_V2_REBALANCE_DAYS=20
ROBUST_V2_DAILY_JOB_RETRY_SECONDS=900
PERMISSIONS_FILE=config/permissions.yaml
ALLOW_CHINEXT_STOCKS=false
ALLOW_STAR_MARKET_STOCKS=false
MIN_STOCK_ORDER_AMOUNT=8000
MIN_ETF_ORDER_AMOUNT=5000
QMT_ACCOUNT_ID=
QMT_CLIENT_PATH=
DASHBOARD_COOKIE_SECURE=false
```

安全默认值：

- `BROKER_MODE=paper_v2`：只使用 SQLite 虚拟盘。
- `ENFORCE_T1=true`：V2 和 QMT dry-run 的强制启动条件。
- `LIVE_TRADING_ENABLED=false`：禁止真实下单。
- 即使误设 `LIVE_TRADING_ENABLED=true`，当前 `QmtBrokerAdapter` 也会拒绝连接真实通道。

## 关键文件

```text
config/settings.py              全局配置
main.py                         旧研究回测入口
robust_runner.py                robust_v2 唯一虚拟盘入口
live_runner.py                  旧 Combo/RPS 研究入口（默认禁写）
daily_runner.py                 每日执行入口
paper_trading.py                虚拟盘示例入口
data/ak_loader.py               AKShare / 腾讯 / BaoStock 数据加载
data/bs_worker.py               BaoStock 子进程隔离
risk/control.py                 风控模块
trading/ledger.py               SQLite 单账本、T+1、幂等和租约
trading/market.py               交易时段、行情时效、停牌与涨跌停执行校验
trading/schedule.py             回测和实时共用调仓日程
trading/allocator.py            唯一目标组合订单分配器
rules/position.py               旧 JSON 持仓基线
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
data/paper_v2.db                V2 唯一账户账本
data/universe/                  每交易日股票池和财务字段版本
data/scans/                     个股扫描候选、过滤统计和最新快照
data/backups/paper_v2/          每日 SQLite 在线备份
data/backups/paper_v2_*/        首次启用时复制的旧 JSON/日志只读归档
logs/robust_v2.log              V2 轮转运行日志
reports/daily_v2_YYYYMMDD.txt   含毛/净收益、成本、换手和版本的日报
```

## 策略与风控摘要

双均线策略：

- 金叉买入：短均线上穿长均线。
- 死叉卖出：短均线下穿长均线。

`robust_v2` 组合策略：

- 宽基 ETF 必须位于 MA200 之上、60 日收益为正且未出现 20 日急跌，再按
  20/60/120 日风险调整收益排序。
- 主板个股先校验盈利收益率、历史完整性、流动性、MA200、20 日急跌和 120 日波动，
  再按盈利收益率、适度规模和低波动评分；不再用 PB 或“越跌越买”的短期反转因子。
- 默认目标为 2 只宽基 ETF 各不超过 24%、2 只主板股各不超过 16%，至少保留 20%
  现金；没有合格标的时自动持有更多现金。
- 盘中只允许灾难止损，普通调仓由月末收盘目标在 T+1 执行。

风控规则：

- 默认只允许当前账户权限覆盖的沪深 A 股股票和场内 ETF。
- 普通策略买入受默认白名单限制。
- 全市场扫描策略买入可绕过固定白名单。
- 卖出不受买入白名单限制，但仍受 T+1、停牌、跌停和持仓数量限制。
- 买入前检查现金、最低建议成交额、单票仓位、总仓位和现金缓冲。

## QMT / miniQMT 接入状态

策略、数据校验、调度、风控、`OrderIntent` 和账户审计已经与交易通道解耦；当前运行使用
`SQLitePaperBrokerAdapter`，不会发送真实委托。`QmtBrokerAdapter` 目前只做 dry-run 接口
边界验证，真实连接仍被硬阻断。

虚拟盘已经模拟实盘能在本地可靠复现的约束：官方交易日、连续竞价时段、当日行情时间
戳、停牌、涨跌停、整手、T+1、费用、滑点、资金/仓位、信号重试、幂等和每日对账。
交易所排队、网络延迟、真实部分成交和券商拒单只能在 QMT 联调后由真实回报确认，不能
用本地模型伪装成完全一致。

接入真实 QMT 前必须完成：

- 资金、持仓、委托、成交回报查询的 dry-run 对齐。
- QMT 返回字段映射到 `OrderIntent`、`ExecutionReport`、`PortfolioSnapshot`。
- 对 `submitted/partially_filled/cancelled` 回报进行异步对账，禁止未确认时重复委托。
- 实盘开关、账户、资金规模、风控阈值和人工确认流程。
- 至少一个月虚拟盘观察期验收通过。

## 开发与测试

提交前统一验证：

```bash
pytest -q
mypy .
git diff --check
```

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

注意：如果虚拟盘正在观察期运行，不要覆盖或删除 `data/paper_v2.db` 及其每日备份。

## 常见问题

### 为什么卖出被 T+1 拒绝？

A 股股票当日买入不能当日卖出。日志中出现 `T+1 限制（买入日: YYYYMMDD）` 属于正常风控结果。

### 为什么买入被白名单拒绝？

普通策略买入会受 `DEFAULT_UNIVERSE` 限制。全市场扫描策略使用 `全市场扫描+组合策略`，可以绕过固定白名单，但仍会经过 A 股代码、资金、仓位、ST、停牌和涨跌停检查。

### 为什么总仓位限制拒绝买入？

旧策略的 `MAX_TOTAL_POSITION` 默认是 90%；`robust_v2` 使用独立的
`ROBUST_V2_MAX_TOTAL_POSITION=0.80`。当持仓市值已经接近或超过对应阈值时，新增买入会被拒绝。

### BaoStock 或行情获取失败怎么办？

先看日志和缓存回退情况：

```bash
tail -n 120 logs/robust_v2.log
python scripts/paper_v2_healthcheck.py --ledger data/paper_v2.db --json
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
