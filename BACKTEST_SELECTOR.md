# 个股计划回测

给每个历史快照加入 `next_return_pct` 作为下一交易日收益标签：

```bash
python backtest_selector.py \
  --input backtest_samples.json \
  --market-score 58.35 \
  --session close \
  --output backtest_result.json
```

输出：

```text
active_count
win_rate_pct
avg_return_pct
profit_factor
max_drawdown_pct
```

同时保留每只股票的：

```text
评分
结论
下一交易日收益
目标空间
止损比例
命中/亏损
```

正式回测时必须使用按交易日切分的历史快照，不能把未来数据放入当日因子；手续费、滑点、涨跌停无法成交等因素需要另行加入成本模型。
