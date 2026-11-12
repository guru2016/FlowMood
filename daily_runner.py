"""Daily zero-cost H5 market collector and next-day research report.

Uses only the verified public H5 endpoints discovered in the APK. It stores raw
JSON and normalized rows in SQLite, then emits a Markdown research report.
This is a research signal report, not an automatic order executor.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import urllib.request
from datetime import datetime
from pathlib import Path

from recovered_data import create_schema, insert_h5_chart, insert_h5_sentiment, parse_h5_chart, parse_h5_sentiment
from stock_selector import select, render
from sector_scoring import enrich

BASE = "https://eq.10jqka.com.cn/open/api/dapan_v3/chart/"
METRICS = {
    "sjdp_market_capital": "大盘资金",
    "sjdp_north_capital": "北向资金",
    "sjdp_temperature_hs": "市场情绪",
    "sjdp_rzrq": "融资融券",
    "sjdp_new_investors": "新增开户",
    "sjdp_market_financing": "市场融资",
    "sjdp_lift_the_ban": "限售解禁",
    "sjdp_increase_or_decrease": "增减持",
    "sjdp_new_fund_size": "新增基金规模",
    "sjdp_reverse_repurchase": "逆回购",
    "sjdp_dollar_rmb": "美元人民币",
}
SENTIMENT_URL = "https://eq.10jqka.com.cn/open/api/etf_sentiment/v1/sentiment_index?code=1B0016"


def fetch_json(url: str, proxy: str | None = None) -> dict:
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "ths-research/1.0", "Accept": "application/json"})
    with opener.open(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def latest_chart(body: dict) -> dict:
    data = body.get("data") or {}
    keys = data.get("point_key_list") or []
    points = data.get("point_list") or []
    return dict(zip(keys, points[-1])) if points else {}


def score(metrics: dict[str, dict]) -> tuple[float, str, list[str]]:
    reasons: list[str] = []
    total = 50.0
    capital = metrics.get("sjdp_market_capital", {})
    temp = metrics.get("sjdp_temperature_hs", {})
    rzrq = metrics.get("sjdp_rzrq", {})
    try:
        net = float(capital.get("net_inflow"))
        total += max(-20, min(20, net / 100))
        reasons.append(f"大盘资金净流入 {net:.2f}")
    except (TypeError, ValueError):
        pass
    try:
        t = float(temp.get("temperature"))
        total += (t - 50) * 0.25
        reasons.append(f"市场温度 {t:.0f}")
    except (TypeError, ValueError):
        pass
    if rzrq:
        reasons.append("融资融券数据已更新")
    total = max(0.0, min(100.0, total))
    regime = "偏强" if total >= 60 else "震荡" if total >= 42 else "偏弱"
    return round(total, 2), regime, reasons


def run(out_dir: Path, db_path: Path, proxy: str | None, stocks_path: Path | None = None, session: str = "close", offline_dir: Path | None = None, config: dict | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_dir = out_dir / "raw" / stamp
    raw_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    create_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS stock_plan (
        generated_at TEXT NOT NULL, session TEXT NOT NULL, symbol TEXT NOT NULL,
        name TEXT, action TEXT, score REAL, buy_price TEXT, target_price TEXT,
        space_pct REAL, stop_price REAL, stop_pct REAL, reasons_json TEXT,
        invalid_json TEXT
    )""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_plan_run ON stock_plan(generated_at, session, symbol)")
    latest: dict[str, dict] = {}
    for key in METRICS:
        try:
            body = fetch_json(f"{BASE}{key}.json", proxy)
        except Exception:
            fallback = (offline_dir / f"{key}.json") if offline_dir else None
            if not fallback or not fallback.exists():
                raise
            body = json.loads(fallback.read_text(encoding="utf-8"))
        (raw_dir / f"{key}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        latest[key] = latest_chart(body)
        insert_h5_chart(conn, parse_h5_chart(body))
    try:
        sentiment_body = fetch_json(SENTIMENT_URL, proxy)
    except Exception:
        fallback = (offline_dir / "sentiment_1B0016.json") if offline_dir else None
        if not fallback or not fallback.exists():
            raise
        sentiment_body = json.loads(fallback.read_text(encoding="utf-8"))
    (raw_dir / "sentiment_1B0016.json").write_text(json.dumps(sentiment_body, ensure_ascii=False, indent=2), encoding="utf-8")
    insert_h5_sentiment(conn, parse_h5_sentiment(sentiment_body))
    conn.commit(); conn.close()
    market_score, regime, reasons = score(latest)
    report = out_dir / f"report_{stamp}.md"
    lines = [f"# 次日资金流情绪研究报告\n", f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}", "", f"## 市场状态\n\n- 状态：**{regime}**\n- 综合分：**{market_score} / 100**\n- 仓位建议：**{'标准仓' if market_score>=60 else '轻仓' if market_score>=42 else '空仓'}**", "", "## 核心指标"]
    for key, name in METRICS.items():
        if latest.get(key): lines.append(f"- {name}（{key}）：`{json.dumps(latest[key], ensure_ascii=False)}`")
    lines += ["", "## 判断依据"] + [f"- {x}" for x in reasons]
    lines += ["", "## 次日执行框架", "- 只观察市场状态匹配的强势题材和个股。", "- 竞价明显低开、板块转弱或资金净流入反转时，信号失效。", "- 本报告不自动下单，需结合实时行情和个人风控确认。"]
    if stocks_path and stocks_path.exists():
        stocks = json.loads(stocks_path.read_text(encoding="utf-8"))
        enriched_stocks, sector_scores = enrich(stocks, config.get('strong_sector_score_min', 70))
        plans = select(enriched_stocks, market_score, session, config=config)
        sector_lines = ["", "## 板块排名"]
        sector_lines += [f"- {name}：{value:.2f}" for name, value in sorted(sector_scores.items(), key=lambda x: x[1], reverse=True)]
        lines += sector_lines + ["", render(plans, market_score, session)]
        for plan in plans:
            conn = sqlite3.connect(db_path)
            conn.execute("""INSERT INTO stock_plan VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                datetime.now().isoformat(timespec="seconds"), session, plan.symbol, plan.name,
                plan.action, plan.score, plan.buy_price, plan.target_price, plan.space_pct,
                plan.stop_price, plan.stop_pct, json.dumps(plan.reasons, ensure_ascii=False),
                json.dumps(plan.invalid_conditions, ensure_ascii=False)))
            conn.commit(); conn.close()
        plan_payload={'generated_at': datetime.now().isoformat(timespec='seconds'),'session':session,'market_score':market_score,'market_regime':regime,'position':('标准仓' if market_score>=60 else '轻仓' if market_score>=42 else '空仓'),'max_exposure':(1.0 if market_score>=60 else .3 if market_score>=42 else 0.0),'plans':[plan.__dict__ for plan in plans]}
        (out_dir / f"stock_report_{session}_{stamp}.json").write_text(json.dumps(plan_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="daily_output")
    ap.add_argument("--db", default="ths_daily.sqlite3")
    ap.add_argument("--proxy", default=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"))
    ap.add_argument("--stocks", type=Path, help="normalized stock snapshot JSON")
    ap.add_argument("--session", choices=["midday", "close"], default="close")
    ap.add_argument("--offline-dir", type=Path)
    ap.add_argument("--config", type=Path)
    args = ap.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config and args.config.exists() else {}
    print(run(Path(args.out), Path(args.db), args.proxy, args.stocks, args.session, args.offline_dir, config))
