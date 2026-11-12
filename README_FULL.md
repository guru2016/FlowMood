# 资金流情绪交易研究系统

> 基于同花顺 APK 静态恢复结果和已验证的 H5 行情接口，构建的午盘/收盘研究系统。系统输出候选标的、买入区间、目标空间、止损、交易阶段、下一动作和交易原则审计，不自动下单。

## 1. 系统目标

系统每天围绕以下链路运行：

```text
H5 市场资金/情绪
  -> 市场分和市场状态
  -> 个股行情快照导入
  -> 板块强弱评分
  -> 资金流/情绪/趋势/突破评分
  -> 利弗莫尔交易原则审计
  -> 午盘或收盘候选计划
  -> 买入区间、目标价、止损价
  -> SQLite 记录
  -> 下一交易日收益回填
  -> 命中率、收益、回撤和分组表现
```

支持两种运行时段：

```text
midday  午盘测试
close   收盘测试
```

## 2. 目录位置

```bash
cd /path/to/ths_apk_analysis
```

核心文件：

| 文件 | 用途 |
|---|---|
| `daily_runner.py` | 采集 H5 指标并生成每日资金流报告 |
| `stock_selector.py` | 个股评分、买入区间、目标、止损和原则审计 |
| `stock_pipeline.py` | 板块评分和个股评分一键流水线 |
| `run_all.py` | 快照导入、H5 采集、选股、入库、表现统计一键运行 |
| `run_watch_all.py` | 监控快照目录并自动触发完整流水线 |
| `ingest_snapshots.py` | CSV/JSON 快照校验、去重、格式标准化 |
| `snapshot_builder.py` | 从行情和历史 K 线计算 `amount_ratio`、`atr14` |
| `sector_scoring.py` | 计算板块扩散、资金流、成交额和涨停强度 |
| `backtest_selector.py` | 单次成本感知回测 |
| `rolling_backtest.py` | 按交易日滚动回测 |
| `performance_tracker.py` | 计划入库和下一交易日收益回填 |
| `performance_daily.py` | 按日期生成表现日报 |
| `rolling_windows.py` | 5 日/20 日滚动收益和回撤 |
| `report_index.py` | 报告、板块和个股表现索引 |
| `report_monitor.py` | 异常数据和候选变化告警 |
| `alert_webhook.py` | 通用 webhook 告警发送 |
| `health_check.py` | 监控器健康检查和静默检测 |
| `system_summary.py` | 运行状态和数据量摘要 |
| `maintenance.py` | 原始数据清理和 SQLite VACUUM |
| `recovered_data.py` | 同花顺响应解析器和 SQLite schema |
| `strategy_config.json` | 评分和风险参数 |

## 3. 环境要求

```text
Python 3.10+
SQLite 3
macOS/Linux/Windows 均可运行离线部分
```

可选：

```text
HTTP/HTTPS 代理：例如 http://127.0.0.1:7897
```

安装依赖：

```bash
# 当前 MVP 只依赖 Python 标准库，不需要额外 pip 包
python -m py_compile *.py
```

## 4. H5 数据来源

APK 内置 `hqMarket_V4.49.zip`，已验证的 H5 接口包括：

```text
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_market_capital.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_north_capital.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_temperature_hs.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_rzrq.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_new_investors.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_market_financing.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_lift_the_ban.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_increase_or_decrease.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_new_fund_size.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_reverse_repurchase.json
https://eq.10jqka.com.cn/open/api/dapan_v3/chart/sjdp_dollar_rmb.json
```

情绪时间序列：

```text
https://eq.10jqka.com.cn/open/api/etf_sentiment/v1/sentiment_index?code=1B0016
```

`1B0016` 是上证 50 情绪序列示例。接口返回的 `time`、`price`、`sentiment` 三个数组按位置对齐。

H5 原始样本在：

```text
dynamic/h5_api/
```

网络不可用时，可以使用该目录作为离线回退。

## 5. 导入行情快照

### 5.1 英文列名格式

```csv
symbol,name,price,prev_close,high,low,amount,avg_amount20,money_flow_pct,turnover_pct,sector
600000.SH,浦发银行,12.3,12.0,12.4,12.05,185000000,100000000,3.2,4.6,银行
```

### 5.2 中文列名格式

支持：

```csv
代码,名称,现价,昨收,最高,最低,成交额,20日均成交额,换手率,资金流强度,板块
600000.SH,浦发银行,12.3,12.0,12.4,12.05,1.85亿,1亿,4.6%,3.2%,银行
```

支持的中文别名包括：

```text
代码/证券代码/股票代码 -> symbol
名称/股票名称 -> name
现价/最新价 -> price
昨收/昨收价 -> prev_close
最高/最高价 -> high
最低/最低价 -> low
成交额 -> amount
20日均成交额 -> avg_amount20
换手率 -> turnover_pct
涨跌幅 -> change_pct
资金流强度 -> money_flow_pct
板块 -> sector
```

数值清洗支持：

```text
4.6%       -> 4.6
1.85亿     -> 185000000
5000万     -> 50000000
1,234,567  -> 1234567
```

### 5.3 导入命令

```bash
python ingest_snapshots.py \
  --input realtime_snapshots.csv \
  --output normalized_snapshots.json \
  --errors snapshot_errors.json
```

导入器会检查：

```text
必填字段
价格大于0
最高价不低于最低价
价格相对昨收不能异常偏离
涨跌幅与价格/昨收是否一致
重复股票代码
非法数字
```

## 6. 快照标准化和 ATR

当已有实时行情和历史 K 线时：

```bash
python snapshot_builder.py \
  --quotes quotes.json \
  --bars daily_bars.json \
  --output normalized_snapshots.json
```

历史 K 线格式：

```json
{
  "600000.SH": [
    {"high": 12.4, "low": 12.0, "close": 12.3}
  ]
}
```

标准化器计算：

```text
amount_ratio = 当日成交额 / 20日平均成交额
atr14        = 最近14个交易日真实波幅均值
```

## 7. 市场分和仓位

自动计算市场分：

```bash
python market_score.py \
  --capital dynamic/h5_api/sjdp_market_capital.json \
  --temperature dynamic/h5_api/sjdp_temperature_hs.json \
  --output market_state.json
```

市场状态：

```text
市场分 >= 60       偏强 / 标准仓 / 最大总仓位100%
42 <= 市场分 < 60  震荡 / 轻仓 / 最大总仓位30%
市场分 < 42        偏弱 / 空仓 / 最大总仓位0%
```

示例：

```json
{
  "market_score": 58.35,
  "state": "震荡",
  "position": "轻仓",
  "max_exposure": 0.3
}
```

## 8. 板块评分

```bash
python sector_scoring.py \
  --input normalized_snapshots.json \
  --output sector_enriched.json \
  --sectors-output sector_scores.json
```

板块评分考虑：

```text
上涨扩散率
平均资金流强度
平均成交额放大
涨停数量
```

输出 `sector_score` 回写到个股快照。

## 9. 个股评分和交易计划

```bash
python stock_selector.py \
  --input normalized_snapshots.json \
  --market-score 58.35 \
  --session close \
  --config strategy_config.json \
  --output stock_report_close.md
```

午盘：

```bash
python stock_selector.py \
  --input normalized_snapshots.json \
  --market-score 58.35 \
  --session midday \
  --config strategy_config.json \
  --output stock_report_midday.md
```

输出：

```text
条件买入/重点观察/放弃
买入区间
目标价
预期空间
止损价和止损比例
关键枢轴价
20日高点
回踩确认价
交易阶段
下一动作
交易日记
失效条件
```

## 10. 利弗莫尔交易原则

当前规则已落成代码，不是只写在说明里。

### 10.1 顺势交易

```text
trend_ok
trend_consistency
```

趋势未确认时：

```text
扣分
不直接条件买入
下一动作通常为等待
```

### 10.2 关键点和突破

```text
pivot_price
high20
breakout_volume_ratio
```

必须满足：

```text
接近/站上关键位
日内高点确认
突破量能达到阈值
```

否则输出：

```text
突破前观察
观察突破和量能
```

### 10.3 交易领涨股

```text
relative_strength
sector_avg_change_pct
sector_leader
```

板块领涨股优先，非领涨股无法直接进入条件买入。

### 10.4 市场和板块共同确认

```text
strong_sector_count
min_strong_sectors
```

单只股票或单个题材孤立上涨时，信号自动降级。

### 10.5 不凭消息交易

```text
independent_confirmation
signal_source
```

没有独立行情/资金数据确认时：

```text
不直接买入
输出等待独立确认
```

### 10.6 亏损止损，不摊平

```text
holding_loss
trend_break
relative_strength_reversal
sector_fading
```

失效条件：

```text
跌破止损价
趋势破坏
相对强度反转
板块退潮
亏损后禁止向下摊平
```

### 10.7 只向盈利仓加仓

```text
add_on
existing_profit_pct
```

只有满足：

```text
已有盈利
趋势确认
枢轴突破
量能确认
```

才允许顺势加仓。

### 10.8 情绪流过滤

```text
sentiment_score
sentiment_delta
crowding_score
```

规则：

```text
情绪升温：加分
情绪退潮：降级
情绪过热：避免追高
情绪拥挤：禁止追高
资金流与题材共振、情绪尚未拥挤：加分
```

### 10.9 风险收益和仓位

```text
风险收益比 = 目标空间 / 止损幅度
建议仓位 = 单笔风险预算 / 止损距离
```

同时受以下限制：

```text
市场最大总仓位
max_exposure
risk_budget_pct
min_risk_reward
max_stop_pct
```

## 11. 一键运行

```bash
python run_all.py \
  --input realtime_snapshots.csv \
  --session close \
  --auto-market-score \
  --out close_output \
  --db research.sqlite3 \
  --offline-dir dynamic/h5_api \
  --config strategy_config.json \
  --proxy http://127.0.0.1:7897
```

`--auto-market-score` 会自动读取 H5 大盘资金和市场温度，不再手填市场分。

## 12. 持续监控目录

```bash
python run_watch_all.py \
  --watch incoming_snapshots \
  --output watch_output \
  --auto-market-score \
  --session close \
  --offline-dir dynamic/h5_api \
  --interval 30
```

监控器支持：

```text
文件稳定检测
SHA-256 去重
跨重启状态保存
失败重试
失败记录
运行耗时
自动市场分
完整流水线
系统摘要
```

状态文件：

```text
watch_output/watcher_state.json
```

## 13. 健康检查和告警

```bash
python health_check.py \
  --state watch_output/watcher_state.json \
  --max-silence-min 60 \
  --webhook "$RESEARCH_WEBHOOK"
```

告警条件：

```text
状态文件损坏
最近一次运行失败
超过指定时间没有新快照
```

报告异常告警：

```bash
python report_monitor.py \
  --current close_output/stock_report_close.json \
  --previous previous_output/stock_report_close.json \
  --output alerts.json
```

## 14. 回测

单次成本回测：

```bash
python backtest_selector.py \
  --input backtest_samples.json \
  --market-score 58.35 \
  --session close \
  --commission-bps 5 \
  --slippage-bps 5 \
  --output backtest_result.json
```

滚动多日回测：

```bash
python rolling_backtest.py \
  --input rolling_samples.json \
  --session close \
  --commission-bps 5 \
  --slippage-bps 5 \
  --output rolling_result.json
```

正式回测必须按交易日切分，不能把未来数据放入当日快照。

## 15. 表现跟踪

保存计划：

```bash
python performance_tracker.py \
  --db research.sqlite3 \
  --plan-json stock_report_close.json
```

回填下一交易日收益：

```csv
symbol,realized_return_pct
600000.SH,2.4
300000.SZ,-2.0
```

```bash
python performance_tracker.py \
  --db research.sqlite3 \
  --returns-csv realized_returns.csv
```

统计：

```bash
python performance_daily.py --db research.sqlite3
python rolling_windows.py --db research.sqlite3
python report_index.py --root . --db research.sqlite3
```

## 16. 数据库表

```text
ths_h5_chart
ths_h5_sentiment
ths_large_fund
ths_main_force
ths_order_queue
ths_tick_order
stock_plan
plan_performance
```

主要字段：

```text
ths_h5_chart：chart_key, point_date, values_json
ths_h5_sentiment：index_code, trade_date, price, sentiment
ths_large_fund：stock_code, event_time, event_type, money, volume, price
ths_main_force：stock_code, trade_type, money, commit_time, order_time
ths_order_queue：stock_code, price, total_quantity, total_large_quantity
ths_tick_order：stock_code, event_time, side, price, volume, order_id
stock_plan：session, symbol, action, score, buy_price, target_price, stop_price
plan_performance：session, symbol, score, realized_return_pct
```

## 17. 数据维护

预览清理30天前原始 JSON：

```bash
python maintenance.py --root . --days 30 --dry-run
```

正式清理并压缩数据库：

```bash
python maintenance.py --root . --days 30 --db research.sqlite3
```

## 18. 动态观测证据

同花顺 APK 动态观测记录：

[DYNAMIC_OBSERVATION.md](./dynamic/DYNAMIC_OBSERVATION.md)

已确认：

```text
AVD hongguo_test
emulator-5554
com.hexin.plat.android
H5 hqMarket WebView
H5 情绪/市场资金接口
native 9528 行情链
```

native 行情链曾出现 TLS `Connection reset`，H5 路径已通过宿主代理获取真实响应。H5 原始样本位于：

```text
dynamic/h5_api/
```

## 19. 常见问题

### 没有实时行情文件怎么办？

使用已验证样本测试：

```bash
--offline-dir dynamic/h5_api
```

### 网络请求失败怎么办？

使用代理：

```bash
--proxy http://127.0.0.1:7897
```

系统也支持 H5 JSON 离线回退。

### 为什么个股分数高却不是条件买入？

检查报告中的：

```text
交易阶段
下一动作
交易日记
core_unmet_conditions
unmet_conditions
```

常见原因：

```text
趋势未确认
枢轴未突破
量能不足
非板块领涨
缺少独立确认
市场分过低
情绪拥挤
风险收益比不足
刚止损冷却
已有亏损仓
```

### 是否自动下单？

不会。系统只输出研究计划和风控条件，不连接券商交易接口，不自动执行订单。

## 20. 最小验收标准

每个交易日应至少检查：

```text
1. 快照导入错误数
2. H5 市场分和市场状态
3. 板块排名
4. 条件买入数量
5. 每只候选的交易日记
6. 买入区间/目标/止损
7. 告警 JSON
8. watcher_state.json 健康状态
9. SQLite 数据量
10. 下一交易日收益回填
```
