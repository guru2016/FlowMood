# Tonghuashun APK recovered data specification

Sample: `com.hexin.plat.android`, V11.65.02

## Large fund trend

Request body assembled by `PageLargeFundTrend`:

```text
key=level2CapitalFree       # no permission branch
stockcode=600000
marketcode=<market id>
action=subscribe
```

With permission, the key is `level2Capital`.

Response shape:

```json
{
  "level2CapitalFree": [
    {
      "stockcode": "600000",
      "value": "{\"data\":[{\"time\":\"09:35:00\",\"type\":0,\"money\":1000000,\"vol\":10000,\"price\":12.34}]}"
    }
  ]
}
```

Recovered fields:

- `time`: event time
- `type`: `0` buy, `1` sell
- `detailType`: `0` active, `1` passive, present in the paid branch
- `money`: amount
- `vol`: volume
- `price`: price

Derived buckets:

```text
active_buy = type=0, detailType=0
passive_buy = type=0, detailType=1
active_sell = type=1, detailType=0
passive_sell = type=1, detailType=1
```

## MMDL buy/sell queue table

`Level2MMDL` subscribes through `frame/service=1276` and reads five table columns:

```text
0x8537 = 34103
0x8538 = 34104
0x8539 = 34105
0x853a = 34106
0x000a = 10
```

The app builds a row matrix and separates rows by the flag embedded in column `34106`:

```text
flag bit 0x1000 -> buy-side list
flag without 0x1000 -> sell-side list
```

The row parser also extracts integer values from columns `34103..34106` and derives:

```text
order count display (笔)
volume display (手/笔)
side totals
buy/sell queue rows
```

Exact column labels remain subject to one real `StuffTableStruct` capture because the table protocol carries values by numeric field id rather than JSON names.

## Queue auxiliary capital text

`LaTingZijinTextView` reads field `0x8636` (`34358`) from a `StuffTableStruct` and displays its first value as a capital/large-order text indicator.

Request details recovered from the same class:

```text
frame/service = 1261 (0x4ed)
stockcode=<stock code>
marketcode=<market id>
```



The main-forces presenter calls the resource string `main_forces`:

```text
https://vaserviece.10jqka.com.cn/Level2/index.php
```

It sends an HTTP request with:

```text
op=mainMonitorDetail
userid=<app user id>
stockcode=<stock code>
```

The response model is JSON:

```json
{
  "errorcode": 0,
  "msg": "",
  "notice": {"ntime": "09:35:00", "tips": ""},
  "pricechange": [{"1": "...", "2525646": 123.4}],
  "list": [
    {
      "nature": "主",
      "volume": "1000手",
      "tradetype": "1",
      "avgprice": "12.34",
      "value": "...",
      "money": 1234567.0,
      "ctime": "09:35:00",
      "otime": "09:34:58"
    }
  ],
  "title": {}
}
```

Recovered semantic helpers:

```text
tradetype=1 -> primary/active category in the app filter
tradetype=2 -> secondary/passive category in the app filter
ctime        -> commit/trade time
otime        -> order time
avgprice     -> average price
volume       -> volume display string
money        -> amount
```

The app polls this endpoint at a 60-second interval. The response parser is available as `parse_main_forces()` in [recovered_data.py](./recovered_data.py).


`Level2Grade500.getRequestText()` builds:

```text
stockcode=600000
marketcode=<market id>
startrow=<visible-row-window start>
rowcount=<visible-row-window count>
isVolumeFloat=1       # only for markets requiring it
```

The request is dispatched through:

```text
frame=0x92a (2346)
service=0x50d (1293)
```

The table uses four field identifiers:

```text
0x8660 = 34400
0x8661 = 34401
0x8662 = 34402
0x8663 = 34403
```

The response is a `StuffTableStruct`, not JSON. Each field returns:

```text
getData(field_id)      -> string[] per row
getDataColor(field_id) -> int[] per row
```

The 500-level row matrix has four columns after loading fields `34400..34403`:

```text
matrix[0], matrix[1] -> buy-side vv9
matrix[2], matrix[3] -> sell-side vv9
```

For each `vv9`, the first string is rendered as price and the second as quantity/order display. This is confirmed by `Level2Grade500$f` binding the buy pair to the buy price/quantity views and the sell pair to the sell price/quantity views.

## Ordinary trade-detail table (StockCJMX)

The non-Level2 trade-detail component uses a `StuffTableStruct` with six fields:

```text
1
10
49 (0x31)
2515 (0x9d3)
71 (0x47)
56 (0x38)
```

For each row, `fillDataAndColor()` reads all six fields and preserves both value and color maps. Field `56` is also used as the row cursor/time value for incremental updates.

Request body:

```text
stockcode=<stock code>
marketcode=<market id>
isVolumeFloat=1       # market-dependent
nopush=1              # first-page/no-push mode
rowcount=40
endtime=<last row cursor>
```

First-page mode adds:

```text
currentpage=1
```

The request uses runtime frame/page/instance ids. The default page mode is `1002` (`0x3ea`), and no-push mode is `1003` (`0x3eb`).


`Level2ZbwtRecyclerView` creates `phc` with request type:

```text
all | buy | sell
```

The request body assembled by `phc.e()` is:

```text
orderid=<cursor>
marketcode=<market id>
rowcount=40
reqtype=all|buy|sell
stockcode=<stock code>
endtime=<cursor time>
```

Dispatch:

```text
frame/service = 0xfbb (4027)
```

The response is a `StuffTableStruct`. `phc.g()` reads these fields:

```text
field 56 (0x38)  -> formatted time/raw display field
field 10 (0x0a)  -> price, normalized to two decimals
field 13 (0x0d)  -> volume/amount display
field 12 (0x0c)  -> direction flag, `1` renders 买, otherwise 卖
field 1  (0x01)  -> auxiliary value
```

The single-list UI confirms the rendered columns:

```text
时间
买/卖
价格
手/成交量
```

The client requests 40 rows per page and uses the last order id/time as the next-page cursor.


`BuyAndSellQueueModelView` builds:

```text
host=500order
url=/v2/orders-by-price?
market=<market id>
&code=600000
&price=12.34
&version=2&rettime=0&rettype=json
&all_orders=1
```

or the filtered form:

```text
&min_orders=100
```

Response shape:

```json
{
  "status_code": 0,
  "data": {
    "market": 1,
    "orderlevels": [
      {
        "price": 12.34,
        "total_count": 20,
        "total_quantity": 180000,
        "total_large_quantity": 120000,
        "ordersque": [10000, 20000, 30000]
      }
    ]
  }
}
```

## Other recovered frame identifiers

```text
1276 (0x4fc): buy/sell queue MMDL request
2310 (0x906): trade-detail navigation / detail view
2346 (0x92a): 500-level quote table frame
1293 (0x50d): 500-level quote table service
```

## Bundled H5 market package

The APK bundles `assets/webapp/hqMarket_V4.49.zip`, declared in [webapp.json](./apktool/assets/webapp/webapp.json) as business `hqMarket` version `4.49`. Its JavaScript API inventory includes:

```text
https://eq.10jqka.com.cn/open/api/dapan_v3/chart
https://eq.10jqka.com.cn/open/api/dapan_v3/custom/quotation_function
https://eq.10jqka.com.cn/open/api/etf_sentiment/v1/config
https://eq.10jqka.com.cn/open/api/etf_sentiment/v1/kyc/
https://eq.10jqka.com.cn/open/api/etf_sentiment/v1/sentiment_index?code=
https://dq.10jqka.com.cn/fuyao/north_fund/default/v5/fuyao/app/dapan/north_fund_interpret/v1/get
https://eq.10jqka.com.cn/webpage/market-quotations-review/index.html
```

The H5 package is separate from the native `9528` quote socket. In the emulator, routing through the host proxy allowed the H5 `hqMarket` WebView to load, while native quote TLS continued to reset. This makes the H5 endpoints the first dynamic path to validate for ordinary market/sentiment data.

A real public response was fetched through the host proxy from:

```text
GET https://eq.10jqka.com.cn/open/api/etf_sentiment/v1/sentiment_index?code=1B0016
```

Response data contains aligned arrays:

```text
data["1B0016"].time       -> 1903 trade dates
 data["1B0016"].price      -> index prices
 data["1B0016"].sentiment  -> sentiment score
```

The latest saved sample was:

```text
trade_date=20261109
price=2789.15
sentiment=52.42
```

Samples are stored under `dynamic/h5_api/`; [recovered_data.py](./recovered_data.py) provides `parse_h5_sentiment()` and `insert_h5_sentiment()`.

The generic chart endpoint was also validated:

```text
sjdp_market_capital -> 大盘资金
point_key_list = [date, net_inflow, szzz]
latest = [20261110, 185.09, 3663.72]

sjdp_north_capital -> 北向资金
point_key_list = [date, net_purchase, net_inflow, szzz]

sjdp_temperature_hs -> 市场情绪
point_key_list = [date, temperature]
latest = [20261110, 76]
```

The H5 chart response is normalized by `parse_h5_chart()` and stored in `ths_h5_chart` through `insert_h5_chart()`.

## Local parser

Use [recovered_data.py](./recovered_data.py) for the JSON branches:

```python
from recovered_data import parse_large_fund, summarize_large_fund, parse_order_queue
```

The 500-level `StuffTableStruct` branch still needs one real captured table response to map fields `34400..34403` to exact labels such as price, bid volume, ask volume, and order count. No device is currently connected (`adb devices` is empty).
