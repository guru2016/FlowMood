#!/usr/bin/env python3
"""盘中实时行情 —— 用 curl 做传输层，避开东财对 Python TLS 连接的间歇性拒绝。

【为什么不用 url/lib 直连】

  实测（2026-11-11 09:40 盘中，同一台机器、同一 URL、同一时刻）：
    curl                          -> 3/3 成功
    urllib / http.client 直连      -> 约 1/3 成功，失败为 RemoteDisconnected
                                      （TCP 建连成功，发出请求后对端直接断开）
  换 host（82.push2 / push2delay）无改善，关闭 keep-alive 反而更差。
  推断是 TLS 指纹层面的风控，改 header 无效。curl 作为子进程调用最稳。

【性能】

  56 页并发 10 -> 全市场 5563 条 1.1 秒。比 Python 直连更快，且不失败。

【用法】

    from live_quotes import fetch_quotes, apply_quotes
    quotes = fetch_quotes()                       # {code: {price, change_pct, ...}}
    rows   = apply_quotes(base_snapshot, quotes)  # 用实时行情覆盖价格类字段
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from pathlib import Path

# 交易时段判断（is_trading_now / elapsed_ratio）依赖本地时间。本模块是采集与实时
# 链路的公共底层，在这里统一对齐北京时区，VPS 为 UTC 时也不会误判「未开盘/已收盘」。
# 各入口（auto_pipeline / dashboard_server）会再设一次，重复设置是幂等的。
os.environ["TZ"] = os.environ.get("THS_TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    time.tzset()

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120"

EM_HOST = "push2.eastmoney.com"
EM_PATH = ("/api/qt/clist/get?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
           "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
           "&fields=f12,f14,f2,f3,f5,f6,f8,f15,f16,f17,f18,f62,f184,f100,f13")
PAGE_SIZE = 100

# A 股交易时段（不含集合竞价，判断"现在该不该刷实时"够用）
MORNING = (dtime(9, 30), dtime(11, 30))
AFTERNOON = (dtime(13, 0), dtime(15, 0))

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": {}, "source": "eastmoney"}
CACHE_TTL = 8.0          # 秒；东财正常时的刷新粒度
FALLBACK_TTL = 60.0      # 秒；同花顺兜底要逐只请求，降为分钟级，别把上游打爆
MIN_QUOTES = 4000        # 少于这个数视为「半份行情」，不能覆盖缓存
QUOTE_MAX_AGE = 150.0    # 秒；超过则后端降级为快照，绝不把陈旧数据标成实时
LAST_SOURCE = "eastmoney"  # eastmoney / ths_fallback / unavailable
THS_HOST = "d.10jqka.com.cn"


def num(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-", "--"):
            return default
        out = float(value)
        return out if out == out and abs(out) != float("inf") else default
    except (TypeError, ValueError):
        return default


def is_trading_now(now: datetime | None = None) -> tuple[bool, str]:
    """返回 (是否在交易时段, 原因)。周末直接判否，节假日靠日K判断由调用方决定。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False, "周末"
    t = now.time()
    if MORNING[0] <= t <= MORNING[1]:
        return True, "早盘"
    if AFTERNOON[0] <= t <= AFTERNOON[1]:
        return True, "午盘"
    if t < MORNING[0]:
        return False, "未开盘"
    if MORNING[1] < t < AFTERNOON[0]:
        return False, "午间休市"
    return False, "已收盘"


def _curl_page(page: int, timeout: float = 8.0, retries: int = 3) -> tuple[list[dict], int]:
    """返回 (该页行情, 市场总数)。失败返回 ([], 0)，绝不抛异常。

    东财 clist 会间歇性返回 Empty reply（本机与 VPS 均复现，连续 10 次可全失败，
    隔几分钟又全通），单次请求不可靠，必须重试。退避 0.12/0.24 秒足够跨过短抖动，
    又不会在彻底不可用时拖慢整轮拉取。
    """
    url = f"https://{EM_HOST}" + EM_PATH.format(page=page)
    for attempt in range(retries):
        try:
            proc = subprocess.run(
                ["curl", "-s", "-m", str(int(timeout)), "-A", UA, url],
                capture_output=True, text=True, timeout=timeout + 5)
            data = json.loads(proc.stdout).get("data") or {}
            rows = list(data.get("diff") or [])
            if rows:
                return rows, int(data.get("total") or 0)
        except Exception:  # noqa: BLE001
            pass
        if attempt < retries - 1:
            time.sleep(0.12 * (attempt + 1))
    return [], 0


# ---------------------------------------------------------------- 同花顺兜底
# 东财 clist 会整条链路间歇性不可用（本机与 VPS 均复现过连续 10 次全失败，
# 且备用域名/明文 HTTP 同时失效）。同花顺 d.10jqka.com.cn 同期 10/10 稳定，
# 因此用它逐只补实时价格。字段经交叉验证：6=昨收 10=现价 8/9/7=高/低/开
# 19=成交额 199112=涨跌幅% 1968584=换手率%。同花顺资金流字段口径不可信，
# 故意不映射 f62/f184，由 apply_quotes 保留快照值，避免全市场资金维度归零。
_ths_local = threading.local()


def _snapshot_codes() -> list[str]:
    """从最近一次全市场快照取代码表。列表本身日间几乎不变，不必实时拉。"""
    try:
        path = Path(__file__).resolve().parent / "normalized_snapshots.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [str(r["code"]) for r in rows
                if len(str(r.get("code") or "")) == 6]
    except Exception:  # noqa: BLE001
        return []


def _ths_one(code: str) -> tuple[str, dict | None]:
    for _ in range(2):
        try:
            conn = getattr(_ths_local, "conn", None)
            if conn is None:
                conn = _ths_local.conn = http.client.HTTPSConnection(THS_HOST, timeout=8)
            conn.request("GET", f"/v2/realhead/hs_{code}/last.js",
                         headers={"User-Agent": UA, "Accept": "*/*",
                                  "Connection": "keep-alive"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200 or len(body) < 100:
                raise ValueError(resp.status)
            text = body.decode("utf-8", errors="replace")
            it = json.loads(text[text.index("(") + 1: text.rindex(")")])["items"]
            price, prev = num(it.get("10")), num(it.get("6"))
            if price <= 0 or prev <= 0:
                return code, None
            return code, {
                "f12": code, "f14": str(it.get("name") or ""),
                "f2": price, "f18": prev,
                "f3": num(it.get("199112")),
                "f15": num(it.get("8")) or price,
                "f16": num(it.get("9")) or price,
                "f17": num(it.get("7")) or price,
                "f6": num(it.get("19")),
                "f8": num(it.get("1968584")),
            }
        except Exception:  # noqa: BLE001
            try:
                _ths_local.conn.close()
            except Exception:  # noqa: BLE001
                pass
            _ths_local.conn = None
    return code, None


def fetch_quotes_ths(codes: list[str], workers: int = 48) -> dict[str, dict]:
    """同花顺 realhead 逐只兜底，返回与东财 clist 同构的字段字典。"""
    out: dict[str, dict] = {}
    if not codes:
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for code, item in pool.map(_ths_one, codes):
            if item:
                out[code] = item
    return out


def fetch_quotes(workers: int = 10, max_pages: int = 60) -> dict[str, dict]:
    """拉全市场实时行情，返回 {code: 行情字段}。

    东财为主、同花顺为备：东财页失败先重试补齐，仍不足 MIN_QUOTES 时
    用同花顺 realhead 补缺代码。返回结构对上层完全一致，调用方无感。
    """
    global LAST_SOURCE
    first, total = _curl_page(1)
    total = total or len(first)

    rows = list(first)
    failed: list[int] = []
    if first and total:
        pages = min(max_pages, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        if pages > 1:
            todo = list(range(2, pages + 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for page, (chunk, _) in zip(todo, pool.map(_curl_page, todo)):
                    if chunk:
                        rows.extend(chunk)
                    else:
                        failed.append(page)
            # 一轮失败多为上游短时抖动，降并发再补一次
            if failed:
                time.sleep(0.3)
                with ThreadPoolExecutor(max_workers=4) as pool:
                    for chunk, _ in pool.map(_curl_page, failed):
                        rows.extend(chunk)

    out: dict[str, dict] = {}
    for item in rows:
        code = str(item.get("f12") or "")
        if len(code) != 6:
            continue
        out[code] = item

    if len(out) >= MIN_QUOTES:
        LAST_SOURCE = "eastmoney"
        return out

    codes = [c for c in _snapshot_codes() if c not in out]
    fallback = fetch_quotes_ths(codes)
    for code, item in fallback.items():
        out.setdefault(code, item)
    LAST_SOURCE = "ths_fallback" if len(out) >= MIN_QUOTES else "unavailable"
    return out


def fetch_quotes_cached(ttl: float = CACHE_TTL) -> tuple[dict[str, dict], float]:
    """带短缓存的版本，给 HTTP 接口用，避免并发请求把上游打爆。

    第二个返回值是**行情真正拉取成功的时间**，不是调用时间。拉取失败时返回旧数据
    + 旧时间戳，上层据此算出滞后秒数并降级，绝不把陈旧数据标成「实时」。
    """
    with _lock:
        # 兜底源是逐只请求，缓存放宽到分钟级，否则 20 秒轮询会把上游打爆
        effective = ttl if _cache.get("source") != "ths_fallback" else max(ttl, FALLBACK_TTL)
        if _cache["data"] and (time.time() - _cache["at"]) < effective:
            return _cache["data"], _cache["at"]
    data = fetch_quotes()
    if len(data) >= MIN_QUOTES:
        with _lock:
            _cache["data"] = data
            _cache["at"] = time.time()
            _cache["source"] = LAST_SOURCE
        return data, _cache["at"]
    # 空数据或半份数据都不覆盖缓存，避免把残缺行情当成一次成功采集
    with _lock:
        return _cache["data"], _cache["at"]


def elapsed_ratio(now: datetime | None = None, floor: float = 0.15) -> float:
    """当日已交易时长占全天 240 分钟的比例，用于把盘中成交额折算成全天口径。

    为什么必须折算：系统的 amount_ratio 语义是「今日成交额 / 20 日均额」。
    收盘时这个比值约等于 1，但 09:45 只交易了 15 分钟，成交额自然只有全天的 6%，
    全市场量比中位数会掉到 0.09，流动性维度几乎全灭、评分系统性下跌十几分。
    折算后量比中位回到 0.6 左右，与收盘口径可比。

    下限 0.15（约 36 分钟）是刻意的：开盘头几分钟分母太小，折算会把量比放大到失真。
    非交易时段一律返回 1.0——那时拿到的是全天数据，不需要折算。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return 1.0
    t = now.time()
    if t < MORNING[0] or t > AFTERNOON[1]:
        return 1.0
    if t <= MORNING[1]:
        raw = (t.hour * 60 + t.minute) - (MORNING[0].hour * 60 + MORNING[0].minute)
    elif t < AFTERNOON[0]:
        raw = 120
    else:
        raw = 120 + (t.hour * 60 + t.minute) - (AFTERNOON[0].hour * 60 + AFTERNOON[0].minute)
    return max(floor, min(1.0, raw / 240))


def apply_quotes(rows: list[dict], quotes: dict[str, dict],
                 elapsed: float | None = None) -> tuple[list[dict], dict]:
    """把实时行情覆盖到历史快照上。

    只覆盖价格类字段；历史统计（atr14 / ma20 / high20 / avg_amount20 /
    trend_consistency）保持快照里的值——这些要日K才能算，盘中重拉成本高且当天不变。
    amount_ratio 用「实时成交额 / (20 日均额 × 已交易占比)」重算，即预估全天量比。
    """
    if elapsed is None:
        elapsed = elapsed_ratio()
    out: list[dict] = []
    hit = miss = 0
    for base in rows:
        code = str(base.get("code") or "")
        item = quotes.get(code)
        if item is None:
            miss += 1
            out.append(base)
            continue
        price = num(item.get("f2"))
        prev = num(item.get("f18"))
        if price <= 0 or prev <= 0:
            miss += 1
            out.append(base)
            continue

        hit += 1
        amount = num(item.get("f6"))
        change_pct = num(item.get("f3"))
        if "f184" in item or "f62" in item:
            money_flow_pct = num(item.get("f184"))
            if not money_flow_pct:
                net = num(item.get("f62"))
                money_flow_pct = net / amount * 100 if amount > 0 else 0.0
        else:
            # 同花顺兜底源不提供资金流（口径不可信）。保留快照值，
            # 否则全市场资金维度会集体归零、排名被整体扭曲。
            money_flow_pct = num(base.get("money_flow_pct"))
        avg_amount20 = num(base.get("avg_amount20"))
        amount_ratio = amount / (avg_amount20 * elapsed) if avg_amount20 > 0 else 1.0
        cap = 19.5 if code.startswith(("30", "68")) else 9.5

        row = dict(base)
        row.update({
            "price": round(price, 4),
            "prev_close": round(prev, 4),
            "high": round(num(item.get("f15")) or price, 4),
            "low": round(num(item.get("f16")) or price, 4),
            "open": round(num(item.get("f17")) or price, 4),
            "amount": round(amount, 2),
            "change_pct": round(change_pct, 4),
            "turnover_pct": round(num(item.get("f8")), 4),
            "money_flow_pct": round(money_flow_pct, 4),
            "amount_ratio": round(amount_ratio, 4),
            "breakout_volume_ratio": round(amount_ratio, 4),
            "sector": str(item.get("f100") or base.get("sector") or "未分类"),
            "limit_up": change_pct >= cap,
            "collected_at": datetime.now().isoformat(timespec="seconds"),
        })
        out.append(row)

    return out, {"hit": hit, "miss": miss, "total": len(rows), "elapsed": round(elapsed, 3)}


def main() -> None:
    import time
    t0 = time.time()
    trading, why = is_trading_now()
    quotes = fetch_quotes()
    dt = time.time() - t0
    print(f"交易时段: {'是' if trading else '否'}（{why}）")
    print(f"实时行情: {len(quotes)} 只, {dt:.2f}s")
    for code in list(quotes)[:5]:
        q = quotes[code]
        print(f"  {code} {q.get('f14')} 价{q.get('f2')} 涨{q.get('f3')}% 资金{q.get('f184')} {q.get('f100')}")


if __name__ == "__main__":
    main()
