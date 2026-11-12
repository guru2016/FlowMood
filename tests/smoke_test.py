#!/usr/bin/env python3
"""离线冒烟自测：不联网、不读行情快照，可在 GitHub Actions 里直接跑。

覆盖三块核心逻辑：
  1. 七维加权饱和评分（分数有上界、有区分度、理由里能还原维度得分）
  2. 选股排序（条件买入置顶、数量受 limit 约束）
  3. 入选时间（首次记录、连续在榜保持不变、新标的单独计时、跨日重置）
  4. 静态看板生成（表格含「入选时间」列）
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import selection_time  # noqa: E402
import stock_selector as ss  # noqa: E402
from auto_rank import parse_dims  # noqa: E402

PASS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    PASS.append(msg)


def sample_rows() -> list[dict]:
    def row(sym, name, price, prev, high, low, flow, ratio, sector_score):
        return {
            "symbol": sym, "name": name, "price": price, "prev_close": prev,
            "high": high, "low": low, "open": prev, "amount": 3.2e8,
            "avg_amount20": 2.0e8, "amount_ratio": ratio, "turnover_pct": 3.1,
            "money_flow_pct": flow, "sector_score": sector_score, "sector": "通信设备",
            "atr14": price * 0.035, "ma20": prev * 0.98, "high20": high * 1.01,
            "trend_consistency": 0.85, "relative_strength": 3.2,
            "strong_sector_count": 2, "independent_confirmation": True,
        }
    return [
        row("600001.SH", "强势样本", 12.6, 11.8, 12.7, 11.7, 18.5, 2.3, 86.0),
        row("600002.SH", "中性样本", 20.0, 20.0, 20.4, 19.5, 2.0, 1.0, 60.0),
        row("600003.SH", "弱势样本", 8.1, 8.5, 8.6, 8.0, -9.0, 0.7, 38.0),
    ]


def test_scoring() -> None:
    cfg = json.loads((ROOT / "strategy_config.json").read_text(encoding="utf-8"))
    plans = [ss.score_stock(r, 62.0, "close", cfg) for r in sample_rows()]
    scores = [p.score for p in plans]
    check(all(0 <= s <= 100 for s in scores), "所有评分落在 0~100，无饱和触顶溢出")
    check(scores[0] > scores[1] > scores[2], f"强弱样本分数单调：{scores}")
    check(len(set(round(s, 2) for s in scores)) == len(scores), "评分未被压成同一个值")
    dims = parse_dims(plans[0].reasons)
    check(len(dims) >= 4, f"评分构成可解析出维度得分：{sorted(dims)}")
    total_weight = sum(v[1] for v in dims.values())
    check(abs(total_weight - 100) < 1.5, f"维度权重合计归一化到 100（实际 {total_weight}）")


def test_select() -> None:
    cfg = json.loads((ROOT / "strategy_config.json").read_text(encoding="utf-8"))
    out = ss.select(sample_rows(), 62.0, "close", limit=2, config=cfg)
    check(isinstance(out, list) and len(out) == 2, "select 遵守 limit")
    if any(p.action == "条件买入" for p in out):
        check(out[0].action == "条件买入", "条件买入标的被置顶")


def test_selection_time() -> None:
    with tempfile.TemporaryDirectory() as td:
        old = selection_time.STATE_PATH
        selection_time.STATE_PATH = Path(td) / "state.json"
        try:
            t0 = datetime(2026, 11, 12, 9, 35, 12)
            r1 = [{"symbol": "A", "rank": 1}, {"symbol": "B", "rank": 2}]
            selection_time.annotate(r1, scope="rank", now=t0)
            check(r1[0]["selected_at"] == "2026-11-12T09:35:12", "首次入选记录到秒")

            t1 = t0 + timedelta(minutes=4)
            r2 = [{"symbol": "A", "rank": 1}, {"symbol": "C", "rank": 2}]
            selection_time.annotate(r2, scope="rank", now=t1)
            check(r2[0]["selected_at"] == "2026-11-12T09:35:12", "连续在榜保持原入选时间")
            check(r2[1]["selected_at"] == "2026-11-12T09:39:12", "新进榜标的单独计时")
            check(r2[1]["selected_new"] is True, "新进榜标的被标记为「新」")

            t2 = t0 + timedelta(days=1)
            r3 = [{"symbol": "A", "rank": 1}]
            selection_time.annotate(r3, scope="rank", now=t2)
            check(r3[0]["selected_at"].startswith("2026-11-13"), "跨交易日自动重置")
        finally:
            selection_time.STATE_PATH = old


def test_static_page() -> None:
    import build_static
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "index.html"
        build_static.build(source=ROOT / "sample" / "latest_ranking.json", output=out)
        html = out.read_text(encoding="utf-8")
        check("入选时间" in html and "评分" in html, "静态看板含「入选时间」列")
        check("FlowMood" in html, "静态看板标题正确")
        check(Path(td, "summary.md").exists(), "生成了 Actions 运行摘要")


def main() -> None:
    for fn in (test_scoring, test_select, test_selection_time, test_static_page):
        fn()
    print(f"FlowMood 冒烟自测通过：{len(PASS)} 项")
    for msg in PASS:
        print(f"  [OK] {msg}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"自测失败：{exc}", file=sys.stderr)
        sys.exit(1)
