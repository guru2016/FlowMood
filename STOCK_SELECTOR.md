# 午盘/收盘个股测试

## 输入

`stock_samples.json` 是标准输入格式，每个股票快照包含：

```json
{
  "symbol": "600000.SH",
  "name": "示例",
  "price": 12.30,
  "prev_close": 12.00,
  "high": 12.40,
  "low": 12.05,
  "amount_ratio": 1.85,
  "money_flow_pct": 3.2,
  "sector_score": 76,
  "sentiment_score": 70,
  "turnover_pct": 4.6,
  "atr14": 0.32
}
```

字段来源对应：

- `price/high/low/prev_close`: 普通行情或 StockCJMX/分钟行情
- `amount_ratio/turnover_pct`: 行情快照计算
- `money_flow_pct`: `level2CapitalFree` 或主力监控聚合
- `sector_score`: 题材内涨跌、成交额、涨停扩散计算
- `sentiment_score`: H5 市场温度和题材情绪
- `atr14`: 本地历史 K 线计算

## 运行

午盘：

```bash
python stock_selector.py \
  --input stock_samples.json \
  --market-score 58.35 \
  --session midday \
  --output stock_report_midday.md
```

收盘：

```bash
python stock_selector.py \
  --input stock_samples.json \
  --market-score 58.35 \
  --session close \
  --output stock_report_close.md
```

## 输出

每个候选股输出：

```text
结论：条件买入 / 重点观察 / 放弃
评分
买入区间
目标价和预期空间
止损价和止损比例
买入理由
失效条件
```

当前为条件信号，不自动下单。实际运行时必须用当天实时快照替换样本，并在竞价/开盘承接、题材强弱和资金流方向确认后执行。

## 当前测试

[stock_report_close.md](./stock_report_close.md) 已用 3 个本地样本验证，评分、买入区间、空间、止损和理由均正常生成。
