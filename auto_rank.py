#!/usr/bin/env python3
"""全市场评分排名 —— 给所有股票按七维加权评分排序，输出可读榜单。

单独运行：
    python3 auto_rank.py                 # 读现有快照，约 3 秒
    python3 auto_rank.py --top 50
    python3 auto_rank.py --snapshot other.json

也会被 auto_pipeline 调用（build_ranking），随每次午盘/收盘运行自动更新，
并写一份 latest_ranking.json 供前端看板直接读取（只含头部，避免传输全量）。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics as st
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from selection_time import annotate  # noqa: E402

DIM_ORDER = ("趋势", "资金", "板块", "结构", "情绪", "流动", "大盘")


def _hhmmss(iso: str | None) -> str:
    """入选时间在榜单里只显示时分秒，完整日期放 JSON/前端 title。"""
    return iso[11:19] if iso and len(iso) >= 19 else "-"


def parse_dims(reasons) -> dict:
    """从「评分构成 趋势18.3/25 资金20.1/23 ...」这条理由里还原各维度得分。"""
    for text in reasons:
        if text.startswith("评分构成 "):
            out = {}
            for part in text[len("评分构成 "):].split():
                m = re.match(r"(.+?)([\d.]+)/([\d.]+)$", part)
                if m:
                    out[m.group(1)] = [float(m.group(2)), float(m.group(3))]
            return out
    return {}


def _resolve_market_score(explicit, base_dir: Path) -> float:
    """市场分是大盘维度的全局输入，优先用调用方传入的，否则取最新报告。"""
    if explicit is not None:
        return float(explicit)
    cands = sorted(base_dir.glob("*/[0-9]*/stock_report_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for path in cands[:1]:
        try:
            return float(json.loads(path.read_text(encoding="utf-8"))["market_score"])
        except Exception:  # noqa: BLE001
            pass
    return 58.35


def build_ranking(snapshot: Path | None = None, config: Path | None = None,
                  market_score: float | None = None, top: int = 30,
                  write: bool = True, tag: str | None = None,
                  quotes: dict | None = None) -> dict:
    """对全市场打分并排名，返回 {stats, ranking, files, market_score}。

    quotes 传入实时行情字典时（见 live_quotes.fetch_quotes），用盘中最新价覆盖
    快照里的价格类字段后重新打分——评分会随行情变动，而不是冻结在采集那一刻。
    """
    from sector_scoring import enrich
    import stock_selector as ss

    snapshot = Path(snapshot) if snapshot else ROOT / "normalized_snapshots.json"
    config = Path(config) if config else ROOT / "strategy_config.json"
    snap_rows = json.loads(snapshot.read_text(encoding="utf-8"))
    cfg = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    meta_path = ROOT / "normalized_snapshots_meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    market = _resolve_market_score(market_score, ROOT / "auto_output")

    live_merge = None
    if quotes:
        from live_quotes import apply_quotes
        snap_rows, live_merge = apply_quotes(snap_rows, quotes)

    enriched, sector_scores = enrich(snap_rows, cfg.get("strong_sector_score_min", 70))
    raw = {r["symbol"]: r for r in enriched}
    plans = [ss.score_stock(r, market, "close", cfg) for r in enriched]
    ranked = sorted(plans, key=lambda p: p.score, reverse=True)

    scores = [p.score for p in plans]
    n = len(scores) or 1
    stats = {
        "count": len(plans),
        "max": round(max(scores), 2) if scores else 0.0,
        "min": round(min(scores), 2) if scores else 0.0,
        "median": round(st.median(scores), 2) if scores else 0.0,
        "mean": round(sum(scores) / n, 2),
        "stdev": round(st.pstdev(scores), 2) if scores else 0.0,
        "ge72": sum(1 for s in scores if s >= cfg.get("buy_score_min", 72)),
        "ge60": sum(1 for s in scores if s >= 60),
        "actions": dict(Counter(p.action for p in plans)),
        "sectors": len(sector_scores),
    }

    rows = [{
        "rank": i,
        "symbol": p.symbol,
        "name": p.name,
        "score": round(p.score, 2),
        "action": p.action,
        "setup_state": p.setup_state,
        "next_action": p.next_action,
        "buy_price": p.buy_price,
        "space_pct": p.space_pct,
        "stop_pct": p.stop_pct,
        "change_pct": raw[p.symbol].get("change_pct"),
        "money_flow_pct": raw[p.symbol].get("money_flow_pct"),
        "amount_ratio": raw[p.symbol].get("amount_ratio"),
        "turnover_pct": raw[p.symbol].get("turnover_pct"),
        "sector": raw[p.symbol].get("sector"),
        "sector_score": raw[p.symbol].get("sector_score"),
        "risk_reward": p.journal.get("risk_reward"),
        "dims": parse_dims(p.reasons),
    } for i, p in enumerate(ranked, 1)]

    # 给榜单头部补「入选时间」：首次进榜的秒级时刻，连续在榜保持不变。
    # 写文件和不写文件（盘中实时重算）都要走，保证前端两个模式口径一致。
    annotate(rows, scope="rank", keep=100)

    stamp = tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stamp": stamp,
        "snapshot_at": meta.get("generated_at", ""),
        "snapshot_count": meta.get("count", len(snap_rows)),
        "market_score": market,
        "live": bool(quotes),
        "live_merge": live_merge,
        "stats": stats,
        "ranking": rows,
        "files": {},
    }
    if not write:
        return result

    out_dir = ROOT / "auto_output" / "ranking"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- Markdown 榜单 ----
    lines = [f"# 全市场评分排名（Top {top}）\n"]
    lines.append(f"- 数据快照：{result['snapshot_at'] or '未知'}（{result['snapshot_count']} 只）")
    lines.append(f"- 市场分：{market}（用于大盘维度加权）")
    lines.append(f"- 打分样本：{stats['count']} 只，最高 {stats['max']:.2f}，"
                 f"中位 {stats['median']:.2f}，标准差 {stats['stdev']:.2f}")
    lines.append(f"- 动作分布：{stats['actions']}\n")
    lines.append("| # | 代码 | 名称 | 入选时间 | 评分 | 动作 | 涨跌% | 资金流% | 量比 | 换手% | 板块 | 空间% | 止损% | RR | 阶段 |")
    lines.append("|---:|---|---|---|---:|---|---:|---:|---:|---:|---|---:|---:|---:|---|")
    for r in rows[:top]:
        rr = r["risk_reward"]
        lines.append(
            f"| {r['rank']} | {r['symbol']} | {r['name']} | {_hhmmss(r.get('selected_at'))} | "
            f"{r['score']:.2f} | {r['action']} | "
            f"{(r['change_pct'] or 0):.2f} | {(r['money_flow_pct'] or 0):.2f} | "
            f"{(r['amount_ratio'] or 0):.2f} | {(r['turnover_pct'] or 0):.2f} | "
            f"{r['sector'] or ''} | {r['space_pct']:.1f} | {r['stop_pct']:.2f} | "
            f"{rr if rr is not None else '-'} | {r['setup_state']} |")

    lines.append("\n## 维度得分率 Top 10（实际得分 / 该维度权重）\n")
    lines.append("| # | 名称 | 评分 | " + " | ".join(DIM_ORDER) + " |")
    lines.append("|---:|---|---:|" + "---:|" * len(DIM_ORDER))
    for r in rows[:10]:
        cells = []
        for key in DIM_ORDER:
            v = r["dims"].get(key)
            cells.append(f"{v[0] / v[1] * 100:.0f}%" if v and v[1] else "—")
        lines.append(f"| {r['rank']} | {r['name']} | {r['score']:.2f} | " + " | ".join(cells) + " |")

    lines.append(f"\n## Top {min(top, len(rows))} 的板块分布\n")
    for k, v in Counter(r["sector"] or "" for r in rows[:top]).most_common(12):
        lines.append(f"- {k}：{v} 只")
    lines.append("")

    md_path = out_dir / f"ranking_{stamp}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    # ---- CSV 全量 ----
    csv_path = out_dir / f"ranking_{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["排名", "代码", "名称", "入选时间", "评分", "动作", "涨跌幅", "资金流%", "量比", "换手率",
                    "板块", "板块分", "空间%", "止损%", "风险收益比", "阶段", "下一动作"])
        for r in rows:
            w.writerow([r["rank"], r["symbol"], r["name"], r.get("selected_at") or "", r["score"], r["action"],
                        r["change_pct"], r["money_flow_pct"], r["amount_ratio"],
                        r["turnover_pct"], r["sector"], r["sector_score"],
                        r["space_pct"], r["stop_pct"], r["risk_reward"],
                        r["setup_state"], r["next_action"]])

    # ---- JSON 全量 ----
    json_path = out_dir / f"ranking_{stamp}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    result["files"] = {
        "md": str(md_path.relative_to(ROOT)),
        "csv": str(csv_path.relative_to(ROOT)),
        "json": str(json_path.relative_to(ROOT)),
    }

    # ---- 前端用的轻量快照：只保留头部，别让浏览器拉 3MB ----
    head = min(100, len(rows))
    latest = dict(result)
    latest["ranking"] = rows[:head]
    latest["head_size"] = head
    (ROOT / "auto_output" / "latest_ranking.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="全市场评分排名")
    ap.add_argument("--snapshot", type=Path, help="快照文件，默认 normalized_snapshots.json")
    ap.add_argument("--config", type=Path, help="策略配置，默认 strategy_config.json")
    ap.add_argument("--market-score", type=float, help="市场分，默认取最新报告")
    ap.add_argument("--top", type=int, default=30, help="榜单输出条数")
    ap.add_argument("--quiet", action="store_true", help="只打印统计，不打印表格")
    args = ap.parse_args()

    res = build_ranking(snapshot=args.snapshot, config=args.config,
                        market_score=args.market_score, top=args.top)
    s = res["stats"]
    print(f"市场分 {res['market_score']} | 样本 {s['count']}")
    print(f"最高 {s['max']:.2f} 中位 {s['median']:.2f} 标准差 {s['stdev']:.2f} | "
          f"≥72 分 {s['ge72']} 只 | ≥60 分 {s['ge60']} 只")
    print(f"动作分布 {s['actions']}")
    if not args.quiet:
        print()
        print(f"{'#':>3} {'代码':<11}{'名称':<8}{'评分':>7} {'动作':<6}{'涨跌%':>7}{'资金%':>7}{'空间%':>7}  板块")
        for r in res["ranking"][:args.top]:
            print(f"{r['rank']:>3} {r['symbol']:<11}{r['name'][:6]:<8}{r['score']:7.2f} {r['action']:<6}"
                  f"{(r['change_pct'] or 0):7.2f}{(r['money_flow_pct'] or 0):7.2f}"
                  f"{r['space_pct']:7.1f}  {r['sector'] or ''}")
    print("\n落盘:")
    for k, v in res["files"].items():
        print(f"  {k}: {v}")
    print("  前端快照: auto_output/latest_ranking.json")


if __name__ == "__main__":
    main()
