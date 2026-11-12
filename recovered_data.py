"""Parse recovered Tonghuashun market-data response shapes.

This module is intentionally offline: it parses response bodies saved from an
authorized app session and does not perform network requests.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass
class LargeFundEvent:
    stock_code: str
    event_time: str
    event_type: int
    money: float
    volume: int
    price: float
    detail_type: int | None = None

    @property
    def direction(self) -> str:
        """type=0 is buy; type=1 is sell."""
        return "buy" if self.event_type == 0 else "sell"

    @property
    def execution(self) -> str:
        """detailType=0 is active; detailType=1 is passive."""
        return "active" if self.detail_type == 0 else "passive"

    @property
    def factor_key(self) -> str:
        return f"{self.execution}_{self.direction}"


@dataclass
class MainForceTrade:
    nature: str
    volume: str
    trade_type: str
    average_price: str
    value: str
    money: float
    commit_time: str
    order_time: str


@dataclass
class MainForceResponse:
    error_code: int
    message: str
    notice_time: str | None
    notice_tips: str | None
    trades: list[MainForceTrade]
    price_changes: list[dict[str, Any]]


@dataclass
class MmdlRow:
    stock_code: str
    side: str
    field_34103: int
    field_34104: int
    field_34105: int
    field_34106: int
    field_10: str


@dataclass
class QueueLevel:
    stock_code: str
    market: int | None
    price: float
    total_count: int
    total_quantity: int
    total_large_quantity: int
    orders_queue: list[int]


def normalize_mmdl_rows(stock_code: str, rows: Iterable[Iterable[str]]) -> list[MmdlRow]:
    """Normalize the five-column MMDL table while preserving raw field ids."""
    result: list[MmdlRow] = []
    for raw in rows:
        values = list(raw)
        if len(values) < 5:
            continue
        try:
            f34103, f34104, f34105, f34106 = (int(values[i] or 0) for i in range(4))
        except (TypeError, ValueError):
            continue
        side = "buy" if (f34106 & 0x1000) else "sell"
        result.append(MmdlRow(stock_code, side, f34103, f34104, f34105, f34106, str(values[4] or "")))
    return result


@dataclass
class TickOrder:
    stock_code: str
    event_time: str
    side: str
    price: str
    volume: str
    order_id: str = ""


def normalize_tick_rows(
    stock_code: str,
    field_56: Iterable[str],
    field_10: Iterable[str],
    field_13: Iterable[str],
    field_12: Iterable[str],
    field_1: Iterable[str] = (),
) -> list[TickOrder]:
    """Normalize ZBWT table columns recovered from StuffTableStruct."""
    times, prices, volumes, sides, order_ids = map(list, (field_56, field_10, field_13, field_12, field_1))
    count = min(len(times), len(prices), len(volumes), len(sides))
    return [
        TickOrder(
            stock_code=stock_code,
            event_time=str(times[i] or ""),
            side="buy" if str(sides[i]) == "1" else "sell",
            price=str(prices[i] or ""),
            volume=str(volumes[i] or ""),
            order_id=(str(order_ids[i] or "") if i < len(order_ids) else ""),
        )
        for i in range(count)
    ]


def normalize_stock_cjmx_rows(
    stock_code: str,
    field_56: Iterable[str],
    field_10: Iterable[str],
    field_49: Iterable[str],
    field_2515: Iterable[str] = (),
) -> list[TickOrder]:
    """Normalize ordinary StockCJMX rows; preserve direction when available."""
    times, prices, volumes, directions = map(list, (field_56, field_10, field_49, field_2515))
    count = min(len(times), len(prices), len(volumes))
    return [
        TickOrder(
            stock_code=stock_code,
            event_time=str(times[i] or ""),
            side=("buy" if str(directions[i]) == "1" else "sell") if i < len(directions) else "unknown",
            price=str(prices[i] or ""),
            volume=str(volumes[i] or ""),
        )
        for i in range(count)
    ]


def parse_main_forces(body: str | bytes | dict[str, Any]) -> MainForceResponse:
    root = _load_body(body)
    notice = root.get("notice") if isinstance(root.get("notice"), dict) else {}
    raw_trades = root.get("list") if isinstance(root.get("list"), list) else []
    trades: list[MainForceTrade] = []
    for item in raw_trades:
        if not isinstance(item, dict):
            continue
        trades.append(
            MainForceTrade(
                nature=str(item.get("nature", "")),
                volume=str(item.get("volume", "")),
                trade_type=str(item.get("tradetype", "")),
                average_price=str(item.get("avgprice", "")),
                value=str(item.get("value", "")),
                money=float(item.get("money", 0) or 0),
                commit_time=str(item.get("ctime", "")),
                order_time=str(item.get("otime", "")),
            )
        )
    raw_changes = root.get("pricechange") if isinstance(root.get("pricechange"), list) else []
    price_changes = [item for item in raw_changes if isinstance(item, dict)]
    return MainForceResponse(
        error_code=int(root.get("errorcode", 0) or 0),
        message=str(root.get("msg", "") or ""),
        notice_time=(str(notice["ntime"]) if "ntime" in notice else None),
        notice_tips=(str(notice["tips"]) if "tips" in notice else None),
        trades=trades,
        price_changes=price_changes,
    )


def _load_body(body: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("response root must be an object")
    return value


@dataclass
class H5ChartPoint:
    chart_key: str
    values: dict[str, Any]


def parse_h5_chart(body: str | bytes | dict[str, Any]) -> list[H5ChartPoint]:
    root = _load_body(body)
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    keys = data.get("point_key_list") if isinstance(data.get("point_key_list"), list) else []
    points = data.get("point_list") if isinstance(data.get("point_list"), list) else []
    chart_key = str(data.get("key", ""))
    result: list[H5ChartPoint] = []
    for point in points:
        if not isinstance(point, list):
            continue
        result.append(H5ChartPoint(chart_key, {str(k): (point[i] if i < len(point) else None) for i, k in enumerate(keys)}))
    return result


@dataclass
class H5SentimentPoint:
    index_code: str
    trade_date: str
    price: float | None
    sentiment: float | None


def parse_h5_sentiment(body: str | bytes | dict[str, Any]) -> list[H5SentimentPoint]:
    root = _load_body(body)
    data = root.get("data") if isinstance(root.get("data"), dict) else {}
    result: list[H5SentimentPoint] = []
    for index_code, item in data.items():
        if not isinstance(item, dict):
            continue
        times = item.get("time") if isinstance(item.get("time"), list) else []
        prices = item.get("price") if isinstance(item.get("price"), list) else []
        sentiments = item.get("sentiment") if isinstance(item.get("sentiment"), list) else []
        for i, day in enumerate(times):
            price = prices[i] if i < len(prices) else None
            sentiment = sentiments[i] if i < len(sentiments) else None
            result.append(H5SentimentPoint(
                index_code=str(index_code),
                trade_date=str(day),
                price=(float(price) if price is not None else None),
                sentiment=(float(sentiment) if sentiment is not None else None),
            ))
    return result


def parse_large_fund(body: str | bytes | dict[str, Any], stock_code: str | None = None) -> list[LargeFundEvent]:
    root = _load_body(body)
    rows: list[LargeFundEvent] = []
    arrays: list[Any] = []
    for key in ("level2CapitalFree", "level2Capital"):
        value = root.get(key)
        if isinstance(value, list):
            arrays = value
            break

    for item in arrays:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stockcode", stock_code or ""))
        if stock_code and code != stock_code:
            continue
        value = item.get("value", "{}")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                continue
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            continue
        for point in value["data"]:
            if not isinstance(point, dict):
                continue
            rows.append(
                LargeFundEvent(
                    stock_code=code,
                    event_time=str(point.get("time", "")),
                    event_type=int(point.get("type", 0)),
                    money=float(point.get("money", 0) or 0),
                    volume=int(point.get("vol", 0) or 0),
                    price=float(point.get("price", 0) or 0),
                    detail_type=(int(point["detailType"]) if "detailType" in point else None),
                )
            )
    return rows


def summarize_large_fund(events: Iterable[LargeFundEvent]) -> dict[str, float]:
    """Aggregate money and volume by active/passive buy/sell buckets."""
    result = {
        "active_buy_money": 0.0,
        "passive_buy_money": 0.0,
        "active_sell_money": 0.0,
        "passive_sell_money": 0.0,
        "active_buy_volume": 0.0,
        "passive_buy_volume": 0.0,
        "active_sell_volume": 0.0,
        "passive_sell_volume": 0.0,
    }
    for event in events:
        key = f"{event.factor_key}_money"
        volume_key = f"{event.factor_key}_volume"
        if key in result:
            result[key] += event.money
            result[volume_key] += event.volume
    result["buy_money"] = result["active_buy_money"] + result["passive_buy_money"]
    result["sell_money"] = result["active_sell_money"] + result["passive_sell_money"]
    result["net_money"] = result["buy_money"] - result["sell_money"]
    result["active_net_money"] = result["active_buy_money"] - result["active_sell_money"]
    return result


def parse_order_queue(
    body: str | bytes | dict[str, Any], stock_code: str, market: int | None = None
) -> list[QueueLevel]:
    root = _load_body(body)
    if int(root.get("status_code", -1)) != 0:
        return []
    data = root.get("data")
    if not isinstance(data, dict):
        return []
    response_market = data.get("market", market)
    levels = data.get("orderlevels")
    if not isinstance(levels, list):
        return []
    rows: list[QueueLevel] = []
    for item in levels:
        if not isinstance(item, dict):
            continue
        queue = item.get("ordersque", [])
        if not isinstance(queue, list):
            queue = []
        rows.append(
            QueueLevel(
                stock_code=stock_code,
                market=(int(response_market) if response_market is not None else None),
                price=float(item.get("price", 0) or 0),
                total_count=int(item.get("total_count", 0) or 0),
                total_quantity=int(item.get("total_quantity", 0) or 0),
                total_large_quantity=int(item.get("total_large_quantity", 0) or 0),
                orders_queue=[int(x or 0) for x in queue],
            )
        )
    return rows


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS ths_h5_chart (
            chart_key TEXT NOT NULL,
            point_date TEXT,
            values_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'ths_h5'
        );
        CREATE TABLE IF NOT EXISTS ths_h5_sentiment (
            index_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            price REAL,
            sentiment REAL,
            source TEXT NOT NULL DEFAULT 'ths_h5'
        );
        CREATE TABLE IF NOT EXISTS ths_large_fund (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            event_time TEXT NOT NULL,
            event_type INTEGER NOT NULL,
            money REAL NOT NULL,
            volume INTEGER NOT NULL,
            price REAL NOT NULL,
            detail_type INTEGER,
            source TEXT NOT NULL DEFAULT 'ths'
        );
        CREATE TABLE IF NOT EXISTS ths_main_force (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            nature TEXT,
            volume TEXT,
            trade_type TEXT,
            average_price TEXT,
            value TEXT,
            money REAL NOT NULL,
            commit_time TEXT,
            order_time TEXT,
            source TEXT NOT NULL DEFAULT 'ths'
        );
        CREATE TABLE IF NOT EXISTS ths_tick_order (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            event_time TEXT,
            side TEXT NOT NULL,
            price TEXT,
            volume TEXT,
            order_id TEXT,
            source TEXT NOT NULL DEFAULT 'ths'
        );
        CREATE TABLE IF NOT EXISTS ths_order_queue (
            trade_date TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            market INTEGER,
            price REAL NOT NULL,
            total_count INTEGER NOT NULL,
            total_quantity INTEGER NOT NULL,
            total_large_quantity INTEGER NOT NULL,
            orders_queue_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'ths'
        );
        CREATE INDEX IF NOT EXISTS idx_h5_chart_key_date ON ths_h5_chart(chart_key, point_date);
        CREATE INDEX IF NOT EXISTS idx_h5_sentiment_code_date ON ths_h5_sentiment(index_code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_large_fund_stock_date ON ths_large_fund(stock_code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_main_force_stock_date ON ths_main_force(stock_code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_tick_order_stock_date ON ths_tick_order(stock_code, trade_date);
        CREATE INDEX IF NOT EXISTS idx_order_queue_stock_date ON ths_order_queue(stock_code, trade_date);
        """
    )


def insert_h5_chart(connection: sqlite3.Connection, rows: Iterable[H5ChartPoint]) -> int:
    values = list(rows)
    connection.executemany(
        """INSERT INTO ths_h5_chart (chart_key, point_date, values_json)
        VALUES (?, ?, ?)""",
        [
            (row.chart_key, str(row.values.get("date", "")), json.dumps(row.values, ensure_ascii=False))
            for row in values
        ],
    )
    return len(values)


def insert_h5_sentiment(
    connection: sqlite3.Connection,
    rows: Iterable[H5SentimentPoint],
) -> int:
    values = list(rows)
    connection.executemany(
        """INSERT INTO ths_h5_sentiment (index_code, trade_date, price, sentiment)
        VALUES (?, ?, ?, ?)""",
        [(row.index_code, row.trade_date, row.price, row.sentiment) for row in values],
    )
    return len(values)


def insert_large_fund(
    connection: sqlite3.Connection,
    trade_date: str,
    events: Iterable[LargeFundEvent],
) -> int:
    rows = list(events)
    connection.executemany(
        """INSERT INTO ths_large_fund
        (trade_date, stock_code, event_time, event_type, money, volume, price, detail_type)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                trade_date,
                row.stock_code,
                row.event_time,
                row.event_type,
                row.money,
                row.volume,
                row.price,
                row.detail_type,
            )
            for row in rows
        ],
    )
    return len(rows)


def insert_main_force(
    connection: sqlite3.Connection,
    trade_date: str,
    stock_code: str,
    response: MainForceResponse,
) -> int:
    rows = response.trades
    connection.executemany(
        """INSERT INTO ths_main_force
        (trade_date, stock_code, nature, volume, trade_type, average_price,
         value, money, commit_time, order_time)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                trade_date,
                stock_code,
                row.nature,
                row.volume,
                row.trade_type,
                row.average_price,
                row.value,
                row.money,
                row.commit_time,
                row.order_time,
            )
            for row in rows
        ],
    )
    return len(rows)


def insert_tick_orders(
    connection: sqlite3.Connection,
    trade_date: str,
    rows: Iterable[TickOrder],
) -> int:
    values = list(rows)
    connection.executemany(
        """INSERT INTO ths_tick_order
        (trade_date, stock_code, event_time, side, price, volume, order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (trade_date, row.stock_code, row.event_time, row.side, row.price, row.volume, row.order_id)
            for row in values
        ],
    )
    return len(values)


def insert_order_queue(
    connection: sqlite3.Connection,
    trade_date: str,
    snapshot_time: str,
    rows: Iterable[QueueLevel],
) -> int:
    values = list(rows)
    connection.executemany(
        """INSERT INTO ths_order_queue
        (trade_date, stock_code, snapshot_time, market, price, total_count,
         total_quantity, total_large_quantity, orders_queue_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                trade_date,
                row.stock_code,
                snapshot_time,
                row.market,
                row.price,
                row.total_count,
                row.total_quantity,
                row.total_large_quantity,
                json.dumps(row.orders_queue, ensure_ascii=False),
            )
            for row in values
        ],
    )
    return len(values)


def as_json(rows: Iterable[LargeFundEvent | QueueLevel]) -> str:
    return json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2)
