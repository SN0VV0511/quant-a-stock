# 📈 A 股量化回测系统

小白友好的 A 股量化回测项目，开箱即用。

## 功能

- ✅ 免费获取 A 股行情数据（AKShare）
- ✅ 双均线交叉策略（金叉买入 / 死叉卖出）
- ✅ 完整回测引擎（手续费、印花税、夏普比率、最大回撤）
- ✅ 自动生成图表（净值曲线、回撤图、K线图）
- ✅ 输出 CSV 结果

## 快速开始

```bash
cd quant-a-stock
pip install -r requirements.txt
python main.py              # 默认回测平安银行
python main.py 600519       # 回测贵州茅台
python main.py 000858 20230101 20240101  # 指定时间范围
```

## 项目结构

```
quant-a-stock/
├── main.py                  # 主入口
├── config/settings.py       # 配置文件
├── data/data_loader.py      # 数据加载
├── strategies/ma_cross.py   # 均线策略
├── backtest/engine.py       # 回测引擎
├── backtest/visualizer.py   # 可视化
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

## 注意

⚠️ 本项目仅供学习研究，不构成投资建议
