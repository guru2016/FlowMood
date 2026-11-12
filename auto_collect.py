#!/usr/bin/env python3
"""全市场行情自动采集器 —— 把原来手工准备个股快照的环节彻底自动化。

【数据源分工，全部经实测 HTTP 200 验证，非推断】

  东财 clist      -> 全市场 5563 只股票列表（56 页并发）+ 实时行情 + 资金流 + 行业归属
                     同花顺列表接口 q.10jqka.com.cn 需要 hexin-v 签名未打通，列表层借用东财
  同花顺 v6 line  -> 个股日K全历史，用于 20 日均额、ATR14、MA20、20 日高点、趋势一致性

【为什么资金流用东财而不是同花顺】

  实测同一日 300133 华策影视（涨 16.39%）：
    东财 f62 主力净流入 = +3.73 亿（超大单 +5.10 亿 大单 -1.38 亿，内部自洽）
    同花顺 237/238 推算 = -1.10 亿（方向相反，且 223+225 与 237 对不上，口径不明）
  money_flow_pct 在 stock_selector 里值 15 分权重，方向错会把错票送进条件买入，
  因此资金流采用可交叉验证的东财口径。

【字段映射，已用东财交叉验证】

  同花顺 6      = 昨收       （浦发 10.10，东财 f60=1010）
  同花顺 199112 = 涨跌幅%    （N钶锐 184.77，东财 f170=18477）
  同花顺 1968584= 换手率%    （佳创 11.567，东财 f168=11.57）
  同花顺 24     = 现价复权口径，不是昨收，切勿混用

【产出】

  normalized_snapshots.json      纯列表，字段与 ingest_snapshots.py 输出兼容，
                                 可直接喂给 sector_scoring.py / stock_selector.py
  normalized_snapshots_meta.json 采集元数据：耗时、成功/失败/跳过数、是否交易日

用法：
    python auto_collect.py                          # 全市场采集
    python auto_collect.py --limit 50 --workers 8   # 调试
    python auto_collect.py --force                  # 非交易日强制采集
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from live_quotes import elapsed_ratio

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"

# 深主板 + 创业板 + 沪主板 + 科创板
EM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
EM_LIST = ("https://push2.eastmoney.com/api/qt/clist/get"
           "?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3&fs=" + EM_FS +
           "&fields=f12,f14,f2,f3,f5,f6,f8,f15,f16,f17,f18,f62,f184,f100,f13")

THS_LINE = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"

_lock = threading.Lock()
_stats = {"ok": 0, "fail": 0, "skip": 0}


def bump(key: str, n: int = 1) -> None:
    with _lock:
        _stats[key] += n


def http_get(url: str, timeout: float = 10.0, retries: int = 3) -> str:
    """兼容旧调用：单次请求，内部走连接池。"""
    return POOL.get(url, timeout=timeout, retries=retries)


class HttpPool:
    """每线程一条 keep-alive 长连接。

    实测对比（500 个不同股票代码拉日K）：
      urllib 每次新建 TLS 连接 -> 74 秒 / 500 只
      连接复用 + 32 并发      ->  0.97 秒 / 500 只（全量 5028 只约 10 秒）
    差 70 倍，瓶颈全在 TLS 握手，不在带宽。
    """

    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout
        self._local = threading.local()

    def _conn(self, host: str) -> http.client.HTTPSConnection:
        bucket = getattr(self._local, "conns", None)
        if bucket is None:
            bucket = self._local.conns = {}
        conn = bucket.get(host)
        if conn is None:
            conn = bucket[host] = http.client.HTTPSConnection(host, timeout=self.timeout)
        return conn

    def _drop(self, host: str, conn) -> None:
        bucket = getattr(self._local, "conns", {}) or {}
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        bucket.pop(host, None)

    def get(self, url: str, timeout: float | None = None, retries: int = 2) -> str:
        """失败返回空串，绝不抛异常打断全局采集。"""
        without_scheme = url.split("://", 1)[-1]
        host, _, path = without_scheme.partition("/")
        path = "/" + path
        headers = {
            "User-Agent": UA,
            "Accept": "*/*",
            "Referer": "https://eq.10jqka.com.cn/",
            "Connection": "keep-alive",
        }
        for attempt in range(retries):
            conn = self._conn(host)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    raise http.client.HTTPException(f"status {resp.status}")
                return body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                self._drop(host, conn)
                if attempt < retries - 1:
                    time.sleep(0.25 * (2 ** attempt) + random.random() * 0.2)
        return ""


POOL = HttpPool()


def parse_body(text: str) -> dict:
    """东财返回裸 JSON，同花顺返回 func({...}) 形式的 JSONP，两种都吃。"""
    if not text:
        return {}
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    start, end = text.find("("), text.rfind(")")
    if start < 0 or end <= start:
        return {}
    try:
        return json.loads(text[start + 1:end])
    except json.JSONDecodeError:
        return {}


def num(value, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-", "--"):
            return default
        out = float(value)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 市场列表
def fetch_market_list(workers: int = 10) -> list[dict]:
    """东财 clist 每页硬上限 100 条（实测 pz=6000 也只返回 100），5563 只要翻 56 页。

    传输层走 curl（见 live_quotes 模块）：Python 直连 push2 会被对端在返回前断开，
    实测成功率只有 1/3，而 curl 是 56 页全成功、1.1 秒拉完。
    """
    from live_quotes import fetch_quotes

    quotes = fetch_quotes(workers=workers)
    if not quotes:
        raise SystemExit("东财列表请求失败，无法确定市场股票总数")
    return list(quotes.values())


def restore_flow_sector(targets: list[dict], prev_path: Path) -> tuple[list[dict], dict]:
    """行情源降级时，用上一份完整定稿快照补回「主力净占比 / 行业」。

    live_quotes.fetch_quotes() 在东财 clist 被限流时会切同花顺 realhead 兜底，
    那份数据只有价格/涨跌/换手，没有 f184（主力净占比）和 f100（行业）。
    fetch_market_list() 直接拿它当全市场列表用，build_row 只能写成
    money_flow_pct=0、sector=未分类 —— 七维评分里的资金(20)、板块(18)
    集体归零，2026-11-12 午盘就出现过「最高分 85.68→65.63、条件买入 7→0、
    板块数 128→1」的整体塌陷。

    对齐 A 股作战系统 emotion_flow 的口径：盘中不伪造资金流，主力净占比与
    行业沿用最近一份完整定稿快照，只让价格/涨跌/量比随实时行情走。
    返回 (补齐后的列表, 诊断信息)。
    """
    info = {"degraded": False, "flow_ratio": 1.0, "sector_ratio": 1.0,
            "flow_restored": 0, "sector_restored": 0, "prev_count": 0}

    def present(item: dict, key: str) -> bool:
        try:
            return abs(float(item.get(key) or 0)) > 1e-6
        except (TypeError, ValueError):
            return False

    n = len(targets) or 1
    flow_ratio = sum(1 for t in targets if present(t, "f184") or present(t, "f62")) / n
    sector_ratio = sum(
        1 for t in targets
        if str(t.get("f100") or "").strip() not in ("", "未分类")
    ) / n
    info["flow_ratio"] = round(flow_ratio, 3)
    info["sector_ratio"] = round(sector_ratio, 3)
    flow_bad, sector_bad = flow_ratio < 0.2, sector_ratio < 0.2
    if not (flow_bad or sector_bad):
        return targets, info

    info["degraded"] = True
    prior: dict[str, dict] = {}
    try:
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
        if isinstance(prev, list):
            prior = {str(r.get("code") or ""): r for r in prev if isinstance(r, dict)}
    except (OSError, ValueError, TypeError):
        prior = {}
    # 定稿字段基准由 auto_rank 维护，比「上一份快照」可靠——上一份可能同样
    # 已被残缺采集覆盖过，所以基准优先。
    base_path = (Path(__file__).resolve().parent / "auto_output"
                 / "base_flow_sector.json")
    try:
        base = json.loads(base_path.read_text(encoding="utf-8"))
        if isinstance(base, dict):
            prior.update({str(k): v for k, v in base.items() if isinstance(v, dict)})
    except (OSError, ValueError, TypeError):
        pass
    info["prev_count"] = len(prior)

    out: list[dict] = []
    for raw in targets:
        item = dict(raw)
        old = prior.get(str(item.get("f12") or "")) or {}
        restored = False
        if flow_bad and not (present(item, "f184") or present(item, "f62")):
            try:
                v = float(old.get("money_flow_pct") or 0)
            except (TypeError, ValueError):
                v = 0.0
            if abs(v) > 1e-6:
                item["f184"] = v
                info["flow_restored"] += 1
                restored = True
        if sector_bad:
            cur = str(item.get("f100") or "").strip()
            old_sector = str(old.get("sector") or "").strip()
            if cur in ("", "未分类") and old_sector not in ("", "未分类"):
                item["f100"] = old_sector
                info["sector_restored"] += 1
                restored = True
        if restored:
            item["_restored"] = True
        out.append(item)
    return out, info


def is_tradable(item: dict) -> bool:
    """剔除停牌、退市、ST、北交所、无报价标的。"""
    code = str(item.get("f12") or "")
    name = str(item.get("f14") or "")
    if len(code) != 6 or num(item.get("f2")) <= 0:
        return False
    if "ST" in name.upper() or "退" in name:
        return False
    if code[0] in ("4", "8"):  # 北交所不在本次 fs 范围，双保险
        return False
    return True


# ---------------------------------------------------------------- 日K历史
def fetch_kline(code: str) -> list[tuple]:
    """返回 [(date, open, high, low, close, volume, amount, turnover), ...] 全历史。"""
    body = parse_body(http_get(THS_LINE.format(code=code)))
    raw = body.get("data") or ""
    out: list[tuple] = []
    if not isinstance(raw, str):
        return out
    for line in raw.split(";"):
        parts = line.split(",")
        if len(parts) < 8:
            continue
        try:
            out.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3]),
                        float(parts[4]), float(parts[5]), float(parts[6]), float(parts[7])))
        except (TypeError, ValueError):
            continue
    return out


def atr(bars: list[tuple], period: int = 14) -> float:
    """真实波幅均值。"""
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(bars)):
        high, low = bars[i][2], bars[i][3]
        prev_close = bars[i - 1][4]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def trend_consistency(bars: list[tuple], span: int = 10) -> float:
    """近 span 日收盘高于前一日收盘的比例，0~1。"""
    tail = bars[-(span + 1):]
    if len(tail) < 3:
        return 0.0
    wins = sum(1 for i in range(1, len(tail)) if tail[i][4] > tail[i - 1][4])
    return round(wins / (len(tail) - 1), 4)


# ---------------------------------------------------------------- 单只组装
def build_row(item: dict, bars: list[tuple], session: str) -> dict | None:
    """东财实时字段 + 同花顺历史统计，压成 stock_selector 认识的一行快照。"""
    code = str(item.get("f12") or "")
    name = str(item.get("f14") or "")
    price = num(item.get("f2"))
    prev_close = num(item.get("f18"))
    if price <= 0 or prev_close <= 0:
        return None

    amount = num(item.get("f6"))
    change_pct = num(item.get("f3"))
    turnover = num(item.get("f8"))
    high = num(item.get("f15")) or price
    low = num(item.get("f16")) or price
    open_ = num(item.get("f17")) or price

    # 主力净流入占比（%），东财 f184。缺失时用 f62/f6 反算。
    money_flow_pct = num(item.get("f184"))
    if not money_flow_pct:
        net = num(item.get("f62"))
        money_flow_pct = net / amount * 100 if amount > 0 else 0.0

    today = datetime.now().strftime("%Y%m%d")
    # 历史统计一律排除当日K线，否则当日放量/新高会污染基准
    hist = [b for b in bars if b[0] != today]
    if len(hist) < 20:
        return None

    prior20 = hist[-20:]
    avg_amount20 = sum(b[6] for b in prior20) / len(prior20)
    # 量比 = 今日成交额 / 20 日均额。盘中只交易了一部分时间，直接相除会把量比
    # 压到 0.1 以下，流动性维度几乎全灭、全市场评分系统性下跌十几分（实测最高分
    # 从 81 掉到 60、≥72 分归零）。按已交易时长折算成全天口径，收盘时比例=1
    # 与原口径完全一致，不影响历史可比性。
    er = elapsed_ratio()
    amount_ratio = (amount / er) / avg_amount20 if avg_amount20 > 0 else 1.0
    ma20 = sum(b[4] for b in prior20) / len(prior20)
    high20 = max(b[2] for b in prior20)

    # 涨停判定：涨幅达到各板阈值（主板 10%、双创 20%、ST 已剔除）
    cap = 19.5 if code.startswith(("30", "68")) else 9.5
    limit_up = change_pct >= cap

    return {
        "symbol": f"{code}.{'SH' if code[0] == '6' else 'SZ'}",
        "code": code,
        "name": name or code,
        "price": round(price, 4),
        "prev_close": round(prev_close, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "open": round(open_, 4),
        "amount": round(amount, 2),
        "avg_amount20": round(avg_amount20, 2),
        "turnover_pct": round(turnover, 4),
        "change_pct": round(change_pct, 4),
        "money_flow_pct": round(money_flow_pct, 4),
        "amount_ratio": round(amount_ratio, 4),
        "atr14": round(atr(hist), 4),
        "ma20": round(ma20, 4),
        "high20": round(high20, 4),
        "pivot_price": round(high20, 4),
        "breakout_volume_ratio": round(amount_ratio, 4),
        "trend_consistency": trend_consistency(hist),
        "sector": str(item.get("f100") or "未分类"),
        "limit_up": limit_up,
        "session": session,
        "source": ("em_clist+ths_line+prev_defined" if item.get("_restored")
                   else "em_clist+ths_line"),
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }


def collect_one(item: dict, session: str) -> dict | None:
    code = str(item.get("f12") or "")
    row = build_row(item, fetch_kline(code), session)
    if row is None:
        bump("skip")
    else:
        bump("ok")
    return row


# ---------------------------------------------------------------- 交易日判定
def check_trading_day() -> tuple[bool, str]:
    """用上证指数当日日K是否已更新来判断，天然覆盖周末与节假日。"""
    body = parse_body(http_get(THS_LINE.format(code="1A0001")))
    last = str(body.get("today") or "")
    return last == datetime.now().strftime("%Y%m%d"), last


# ---------------------------------------------------------------- 主流程
def collect(workers: int = 32, limit: int | None = None, session: str = "close",
            output: Path = Path("normalized_snapshots.json"),
            list_cache: Path | None = Path("cache/market_list.json"),
            force: bool = False) -> Path:

    trading, last_day = check_trading_day()
    if not trading and not force:
        raise SystemExit(f"今天不是交易日（上证最新日K={last_day or '读取失败'}）。--force 可强制运行。")

    t0 = time.time()

    rows_raw: list[dict] = []
    # 缓存的是东财 clist 原始返回，里面含实时价格和成交额——绝对不能长缓存。
    # 原来设的 6 小时会让盘中 11:35 的运行读到早上 07:34 的价格，等于白跑一次。
    # 60 秒只用于挡住同一秒内的重复调用，跨过一分钟就必须重新拉。
    if list_cache and list_cache.exists() and (time.time() - list_cache.stat().st_mtime) < 60:
        try:
            rows_raw = json.loads(list_cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rows_raw = []
    if not rows_raw:
        rows_raw = fetch_market_list()
        if list_cache and rows_raw:
            list_cache.parent.mkdir(parents=True, exist_ok=True)
            list_cache.write_text(json.dumps(rows_raw, ensure_ascii=False), encoding="utf-8")

    targets = [x for x in rows_raw if is_tradable(x)]
    print(f"[collect] 市场 {len(rows_raw)} 只，可采集 {len(targets)} 只，并发 {workers}", flush=True)
    if limit:
        targets = targets[:limit]

    # 必须在写新快照之前读旧文件：降级行情缺的资金流/行业从这里沿用。
    targets, restore = restore_flow_sector(targets, output)
    if restore["degraded"]:
        print(f"[collect] 行情源缺资金流/行业（覆盖 {restore['flow_ratio']:.0%}/"
              f"{restore['sector_ratio']:.0%}），沿用上一份定稿快照：资金流 "
              f"{restore['flow_restored']} 只 / 行业 {restore['sector_restored']} 只", flush=True)

    out: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(collect_one, item, session) for item in targets]
        for fut in as_completed(futures):
            done += 1
            try:
                row = fut.result()
                if row:
                    out.append(row)
            except Exception:  # noqa: BLE001
                bump("fail")
            if done % 500 == 0:
                print(f"[collect] {done}/{len(targets)} 成功 {_stats['ok']} 跳过 {_stats['skip']} "
                      f"失败 {_stats['fail']} 耗时 {time.time() - t0:.0f}s", flush=True)

    out.sort(key=lambda r: r["amount"], reverse=True)

    # 下游 daily_runner.py 拿到文件后直接当 list 用，必须保持纯列表格式
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, output)

    meta = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "session": session, "trading_day": trading,
        "count": len(out), "target": len(targets),
        "ok": _stats["ok"], "skip": _stats["skip"], "fail": _stats["fail"],
        "elapsed_sec": round(time.time() - t0, 1),
        "elapsed_ratio": round(elapsed_ratio(), 3),
        "source": "auto_collect",
        "restored_fields": restore,
    }
    output.with_name(output.stem + "_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[collect] 完成 {len(out)}/{len(targets)} | 跳过 {_stats['skip']} 失败 {_stats['fail']} "
          f"| {meta['elapsed_sec']}s -> {output}", flush=True)
    if not out:
        raise SystemExit("采集结果为空，下游无法运行")
    return output


def main() -> None:
    ap = argparse.ArgumentParser(description="全市场行情自动采集")
    ap.add_argument("--output", type=Path, default=Path("normalized_snapshots.json"))
    ap.add_argument("--session", choices=["midday", "close"], default="close")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--limit", type=int, help="只采前 N 只（按涨幅排序），调试用")
    ap.add_argument("--force", action="store_true", help="非交易日也强制采集")
    ap.add_argument("--no-cache", action="store_true", help="不读写列表缓存")
    args = ap.parse_args()
    collect(workers=args.workers, limit=args.limit, session=args.session,
            output=args.output,
            list_cache=None if args.no_cache else Path("cache/market_list.json"),
            force=args.force)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
