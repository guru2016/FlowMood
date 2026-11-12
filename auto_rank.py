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


def snapshot_quality(rows: list[dict]) -> dict:
    """判定评分输入是否完整，防止残缺行情把定稿榜单整体写坏。

    七维评分里「资金 20 分 + 板块 18 分」占 38%，东财 clist 被限流走同花顺
    兜底时这两列会整列归零/未分类，此时评分仍是合法数字但结论完全失真
    （2026-11-12 午盘：最高分 85.68→65.63、条件买入 7→0、板块 128→1）。
    宁可保留上一份完整榜单，也不能让这种结果覆盖 latest_ranking.json。
    """
    n = len(rows) or 1

    def nonzero(v) -> bool:
        try:
            return abs(float(v or 0)) > 1e-6
        except (TypeError, ValueError):
            return False

    flow = sum(1 for r in rows if nonzero(r.get("money_flow_pct"))) / n
    sector = sum(
        1 for r in rows
        if str(r.get("sector") or "").strip() not in ("", "未分类")
    ) / n
    ok = flow >= 0.2 and sector >= 0.2
    return {
        "ok": ok,
        "flow_ratio": round(flow, 3),
        "sector_ratio": round(sector, 3),
        "reason": "" if ok else (
            f"主力资金覆盖 {flow:.0%}、行业覆盖 {sector:.0%}（均需 ≥20%），"
            "判定为残缺快照"),
    }


# ---- 定稿字段基准 ----------------------------------------------------------
# 行业是静态的；主力净占比只有东财 clist 给，而它对线上出口 IP 会按域名+频次
# 断连，批量翻 56 页经常整轮失败。A 股作战系统 emotion_flow 的处理是「主力净
# 占比与行业沿用收盘定稿快照，盘中只用实时报价覆盖价格后重排」。这里照此办理：
# 每次成功定稿就记一份 code -> (资金流, 行业)，上游降级时按代码补回，只补这两
# 列，价格/涨跌/量比仍用实时值，绝不拿旧价格冒充现价。
BASE_FLOW_PATH = ROOT / "auto_output" / "base_flow_sector.json"


def _load_base() -> dict[str, dict]:
    for path in (BASE_FLOW_PATH, ROOT / "sample" / "base_flow_sector.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (OSError, ValueError, TypeError):
            continue
    # 没预生成时，从最近一份完整的历史排名归档现场恢复一次并缓存下来，
    # 避免每次重排都去读几百 MB 的归档。
    try:
        cands = sorted((ROOT / "auto_output" / "ranking").glob("ranking_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:6]
    except OSError:
        cands = []
    for path in cands:
        try:
            rows = json.loads(path.read_text(encoding="utf-8")).get("ranking") or []
            if len(rows) < 1000 or not snapshot_quality(rows)["ok"]:
                continue
            base = _base_from_rows(rows)
            _save_base(base)
            return base
        except (OSError, ValueError, TypeError):
            continue
    return {}


def _base_from_rows(rows: list[dict]) -> dict[str, dict]:
    return {
        str(r.get("code") or str(r.get("symbol") or "").split(".")[0]): {
            "money_flow_pct": r.get("money_flow_pct"),
            "sector": r.get("sector"),
        }
        for r in rows
        if r.get("symbol") or r.get("code")
    }


def _save_base(base: dict[str, dict]) -> None:
    try:
        BASE_FLOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASE_FLOW_PATH.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def restore_from_base(rows: list[dict]) -> tuple[list[dict], dict]:
    """整列缺资金流/行业时，按代码沿用最近一份完整定稿值。"""
    before = snapshot_quality(rows)
    if before["ok"]:
        _save_base(_base_from_rows(rows))   # 数据完整，顺手刷新定稿基准
        return rows, {"used": False, **before}
    base = _load_base()
    info = {"used": bool(base), "available": len(base), "matched": 0,
            "before": before, "after": snapshot_quality(rows)}
    if not base:
        return rows, info
    out: list[dict] = []
    for raw in rows:
        key = str(raw.get("code") or str(raw.get("symbol") or "").split(".")[0])
        ref = base.get(key)
        if not ref:
            out.append(raw)
            continue
        row = dict(raw)
        try:
            cur = abs(float(row.get("money_flow_pct") or 0))
        except (TypeError, ValueError):
            cur = 0.0
        if cur <= 1e-6 and ref.get("money_flow_pct") is not None:
            row["money_flow_pct"] = ref["money_flow_pct"]
        if (str(row.get("sector") or "未分类") in ("", "未分类")
                and ref.get("sector")):
            row["sector"] = ref["sector"]
        row["base_fields"] = True
        info["matched"] += 1
        out.append(row)
    info["after"] = snapshot_quality(out)
    return out, info


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

    # 上游降级时整列缺资金流/行业：沿用最近一次完整定稿值，只补这两列。
    snap_rows, base_restore = restore_from_base(snap_rows)

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
        "base_restore": base_restore,
        # 前端据此标注「资金流沿用定稿」而不是让人误以为是盘中实时资金
        "flow_source": ("live" if live_merge and any(
            ("f184" in q or "f62" in q) for q in list(quotes.values())[:100]
        ) else "defined" if base_restore.get("used") else "snapshot"),
        "stats": stats,
        "ranking": rows,
        "files": {},
    }

    quality = snapshot_quality(snap_rows)
    result["snapshot_quality"] = quality
    # 「定稿」和「盘中重排」必须分开：作战系统只在收盘用完整定稿快照出榜单，
    # 盘中拿实时报价覆盖价格后重排，结果只在内存、绝不覆盖定稿。混在一起
    # 就会出现 11-12 那种「午盘把 85 分的定稿榜覆盖成 65 分」的口径错乱。
    result["ranking_basis"] = (
        "intraday_recalc" if quotes else
        "defined_with_base" if base_restore.get("used") else
        "defined_snapshot")
    if not write:
        return result

    # 残缺快照不落盘：md/csv/json 是历史归档可以留，但前端读的
    # latest_ranking.json 必须保住上一份完整榜单，否则页面会静默显示一份
    # 「全市场只有 7 只重点观察」的假结论，比不更新危险得多。
    if not quality["ok"]:
        result["written"] = False
        result["latest_written"] = False
        result["write_blocked"] = quality["reason"]
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

    result["written"] = True
    # ---- 前端用的轻量快照：只让「原生完整定稿」更新 ----
    # 盘中重排 / 资金流沿用定稿值的结果只归档，不覆盖前端定稿榜单；
    # 看板要看盘中变化，走 /api/ranking?live=1 实时重算。
    if result["ranking_basis"] != "defined_snapshot":
        result["latest_written"] = False
        result["latest_note"] = "盘中重排结果仅归档，收盘定稿榜单保持不变"
        return result
    head = min(100, len(rows))
    latest = dict(result)
    latest["ranking"] = rows[:head]
    latest["head_size"] = head
    (ROOT / "auto_output" / "latest_ranking.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    result["latest_written"] = True

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
    if res.get("write_blocked"):
        print(f"\n⚠ 未更新前端快照：{res['write_blocked']}")
        print("  latest_ranking.json 保留上一份完整榜单（本次评分仅作诊断，不作定稿）")
        return
    print("\n落盘:")
    for k, v in res["files"].items():
        print(f"  {k}: {v}")
    if res.get("latest_written"):
        print("  前端快照: auto_output/latest_ranking.json（已更新定稿）")
    else:
        print(f"  前端快照: 保持不变（{res.get('latest_note', '盘中重排')}）")


if __name__ == "__main__":
    main()
