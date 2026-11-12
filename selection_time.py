#!/usr/bin/env python3
"""记录每只标的「入选时间」——首次进入榜单的时间，精确到秒。

语义：某个交易日里，标的第一次出现在榜单（排名 Top100 / 候选计划）中的时刻。
只要它连续在榜，时间就保持不变；掉出榜单超过 GRACE_SEC 后再回来，重新计时。
跨交易日自动清空，避免把昨天的入选时间带到今天。

状态落在 auto_output/selection_state.json，文件很小（最多几百条），
实时排名每 10 秒重算一次也只会顺序写它，用线程锁挡住并发。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "auto_output" / "selection_state.json"

# 实时排名每次重算名次会有抖动，给掉出榜单一小段宽限期，
# 否则会在 Top100 边缘反复「重新入选」，时间一直跳。
GRACE_SEC = 120
# 用于前端「新入榜」高亮：入选后这段时间内算新
NEW_SEC = 180

_lock = threading.Lock()


def _load() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except Exception:  # noqa: BLE001
        # 入选时间只是展示信息，写盘失败不该让排名接口 500
        pass


def annotate(rows: list[dict], scope: str = "rank", keep: int = 100,
             now: datetime | None = None) -> list[dict]:
    """给 rows 写入 selected_at / selected_new，并返回 rows 本身。

    - rows 需按榜单顺序排好；只把前 keep 名视为「在榜」。
    - scope 区分不同的榜单（rank=全市场排名，plans=候选计划），互不干扰。
    - selected_at 为 ISO 8601 字符串（秒精度），前端自行截取 HH:MM:SS。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    stamp = now.isoformat(timespec="seconds")
    ts = now.timestamp()
    on_list = {str(r.get("symbol")) for r in rows[:keep] if r.get("symbol")}

    with _lock:
        state = _load()
        if state.get("date") != today:
            state = {"date": today}
        book = state.get(scope)
        if not isinstance(book, dict):
            book = {}
            state[scope] = book

        for symbol in on_list:
            item = book.get(symbol)
            if isinstance(item, dict) and item.get("at"):
                item["seen"] = ts
            elif isinstance(item, str):     # 兼容早期只存时间的格式
                book[symbol] = {"at": item, "seen": ts}
            else:
                book[symbol] = {"at": stamp, "seen": ts}

        # 掉出榜单超过宽限期才真正移除；宽限期内保留原入选时间
        for symbol in list(book):
            if symbol in on_list:
                continue
            item = book[symbol]
            seen = item.get("seen", 0) if isinstance(item, dict) else 0
            if ts - seen > GRACE_SEC:
                del book[symbol]

        state["date"] = today
        _save(state)
        snapshot = {k: dict(v) if isinstance(v, dict) else {"at": v, "seen": ts}
                    for k, v in book.items()}

    for r in rows:
        symbol = str(r.get("symbol") or "")
        item = snapshot.get(symbol)
        if not item or symbol not in on_list:
            r.pop("selected_at", None)
            r.pop("selected_new", None)
            continue
        r["selected_at"] = item["at"]
        try:
            born = datetime.fromisoformat(item["at"]).timestamp()
            r["selected_new"] = (ts - born) <= NEW_SEC
        except Exception:  # noqa: BLE001
            r["selected_new"] = False
    return rows


def reset(scope: str | None = None) -> None:
    """手动清空（例如新交易日开始、或用户想重来）。"""
    with _lock:
        state = _load()
        if scope:
            state.pop(scope, None)
        else:
            state = {"date": datetime.now().strftime("%Y-%m-%d")}
        _save(state)
