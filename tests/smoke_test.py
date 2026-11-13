#!/usr/bin/env python3
"""离线冒烟自测：不联网、不读行情快照，可在 GitHub Actions 里直接跑。

覆盖三块核心逻辑：
  1. 七维加权饱和评分（分数有上界、有区分度、理由里能还原维度得分）
  2. 选股排序（条件买入置顶、数量受 limit 约束）
  3. 入选时间（首次记录、连续在榜保持不变、新标的单独计时、跨日重置）
  4. 核心指标人话化（中文标签、无代码名/JSON、历史与预告标记）
  5. 静态看板生成（入选时间列 + 核心指标卡片）
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


def test_metric_display() -> None:
    import metric_display as md
    raw = {
        "sjdp_market_capital": {"date": "20261111", "net_inflow": "204.79", "szzz": "3680.64"},
        "sjdp_north_capital": {"date": "20240816", "net_purchase": "-67.75",
                               "net_inflow": "-53.22", "szzz": "2879.43"},
        "sjdp_temperature_hs": {"date": "20261110", "temperature": "76"},
        "sjdp_rzrq": {"date": "20261110", "lrye": "22945.61", "szzz": "3663.72", "jlr": "-13.69"},
        "sjdp_new_investors": {"date": "202610", "szzz": "3599.02", "invest": "150.06"},
        "sjdp_market_financing": {"date": "20261111", "ipo": None, "add": None,
                                  "szzz": "3663.70", "bond": None},
        "sjdp_lift_the_ban": {"date": "20261230", "szzz": None, "lift_the_ban": "332.69"},
        "sjdp_increase_or_decrease": {"date": "20261110", "szzz": "3663.72", "zjc": "-0.30"},
        "sjdp_new_fund_size": {"date": "202611", "szzz": "3655.66", "new_fund_size": "2.80"},
        "sjdp_reverse_repurchase": {"date": "20210830", "szzz": "3534.22",
                                    "jtf": "400.0", "jhg": "500.0"},
        "sjdp_dollar_rmb": {"date": "20261110", "dollar_rmb": "6.68", "szzz": "3663.72"},
    }
    ms = md.metrics_from_raw(raw)
    check(len(ms) == len(md.SPEC), f"11 项核心指标全部结构化：{len(ms)}")
    check(all("sjdp_" not in json.dumps(m, ensure_ascii=False) for m in ms),
          "卡片数据不含 sjdp_ 代码名")
    check(ms[0]["label"] == "大盘资金" and ms[0]["main_value"] == "+204.79",
          "主数值保留精度与正负号")
    check(ms[5]["no_data"] and ms[5]["main_value"] == "暂无数据",
          "全空指标不拿上证指数冒充结果")
    check(ms[1]["stale"] and ms[6]["future"], "历史数据与未来预告被单独标记")
    check(not ms[4]["stale"], "月度指标不会误判为过期")

    lines = [f"- 指标（{k}）：`{json.dumps(v, ensure_ascii=False)}`" for k, v in raw.items()]
    text = "## 核心指标\n" + "\n".join(lines) + \
           "\n\n## 交易明细\n- 交易日记：`{\"principle_pass_rate\": 100.0, \"risk_reward\": 1.52}`\n"
    r = md.normalize_report({"markdown": text, "metrics": raw})
    check(len(md.parse_report_metrics(text)) == 11, "兼容历史报告里的 JSON 指标段")
    check("sjdp_" not in r["markdown"] and '{"' not in r["markdown"],
          "报告正文无代码名、无 JSON 串")
    check("交易审计" in r["markdown"] and "核心指标" not in r["markdown"],
          "交易日记转中文结果、指标段由卡片区接管")
    check(md.normalize_report(r) == r, "normalize_report 幂等")


def test_static_page() -> None:
    import build_static
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "index.html"
        build_static.build(source=ROOT / "sample" / "latest_ranking.json", output=out)
        html = out.read_text(encoding="utf-8")
        check("入选时间" in html and "评分" in html, "静态看板含「入选时间」列")
        check("市场核心指标" in html, "静态看板含核心指标胶囊条")
        # 原意是「别把 sjdp_* 代码名和 JSON 串当内容显示」。静态层的 JS 源码里
        # 当然要出现这些接口键（浏览器直连指标接口要用），所以只断言数据载荷：
        # 内嵌的 FLOWMOOD_BOOT 里不能再夹带原始代码名/原始字典。
        boot = html.split("window.FLOWMOOD_BOOT", 1)[-1].split("</script>", 1)[0]
        check("sjdp_" not in boot, "静态页数据载荷不含指标代码名")
        check("大盘资金" in html and "融资融券" in html, "静态页指标以中文名展示")
        check("FlowMood" in html, "静态看板标题正确")
        for key, label in (("kpi-grid", "KPI 卡区"), ("core-strip", "指标胶囊条"),
                           ("main-panel", "标签页主面板"), ("right-sidebar", "右侧实时流侧栏"),
                           ("openStock", "个股详情弹窗"), ("stkchip", "可点击个股标的"),
                           ("splitReport", "报告逐股明细折叠"), ("showTab", "主面板标签页"),
                           ("extra_quotes", "报告标的额外行情"),
                           ("data-theme", "浅色 / 深色主题")):
            check(key in html, f"静态看板含{label}")
        check(Path(td, "summary.md").exists(), "生成了 Actions 运行摘要")


def main() -> None:
    for fn in (test_scoring, test_select, test_selection_time,
               test_metric_display, test_static_page):
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
