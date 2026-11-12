#!/usr/bin/env python3
"""把本地看板原样打包成 GitHub Pages 静态页。

关键原则：**不另写一套页面**。`web/dashboard.html` 是本地服务与 Pages 共用的
唯一模板，这里只在主脚本之前注入两样东西：
  1. `FLOWMOOD_BOOT` —— 午盘/收盘报告、状态、排名快照（与 /api/* 同形）
  2. `web/static-feed.js` —— 实现同形 API 的静态数据层 + 浏览器端实时行情源

所以 Pages 的布局、表格、实时分析流与本地/VPS 看板完全一致，只是数据层不同。
用法：
    python3 build_static.py                         # 输出 docs/index.html
    python3 build_static.py --output /tmp/x.html
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from metric_display import normalize_report

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
SAMPLE = ROOT / "sample"
PLACEHOLDER = "<!--FLOWMOOD_BOOT-->"


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def _latest(pattern: str) -> Path | None:
    hits = sorted((ROOT / "auto_output").glob(pattern),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def _live_reports() -> dict:
    """从本地 auto_output 读取午盘/收盘报告；字段刻意精简，不带入绝对路径。"""
    out: dict = {}
    for session in ("midday", "close"):
        p = _latest(f"{session}/*/stock_report_*.json")
        if not p:
            continue
        r = _json(p)
        if not isinstance(r, dict):
            continue
        md = ""
        mds = sorted(p.parent.glob("report_*.md"), key=lambda x: x.stat().st_mtime, reverse=True)
        if mds:
            try:
                md = mds[0].read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        latest = _json(ROOT / "auto_output" / f"latest_{session}.json", {}) or {}
        out[session] = {
            "session": session,
            "generated_at": r.get("generated_at"),
            "market_score": r.get("market_score"), "market_regime": r.get("market_regime"),
            "position": r.get("position"), "max_exposure": r.get("max_exposure"),
            "plans": r.get("plans") or [], "metrics": r.get("metrics") or {}, "markdown": md,
            "path": str(p.relative_to(ROOT)),
            "meta": {
                "finished_at": latest.get("finished_at"), "elapsed_sec": latest.get("elapsed_sec"),
                "all_ok": latest.get("all_ok"),
                "steps": [{"step": s.get("step"), "ok": s.get("ok")}
                          for s in (latest.get("steps") or [])],
            },
        }
    return out


def _live_status() -> dict:
    """构造成 /api/status 的形状，避免把运行机绝对路径写进公开页面。"""
    p = _latest("*/[0-9]*/stock_report_*.json")
    report = _json(p, {}) if p else {}
    if not isinstance(report, dict):
        report = {}
    auto = {}
    for session in ("midday", "close"):
        m = _json(ROOT / "auto_output" / f"latest_{session}.json")
        if not isinstance(m, dict):
            continue
        # steps[].detail 会带运行机绝对路径，前端也不需要，只留步名与结果
        auto[session] = {
            "session": m.get("session"), "finished_at": m.get("finished_at"),
            "elapsed_sec": m.get("elapsed_sec"), "all_ok": m.get("all_ok"),
            "skipped": m.get("skipped"),
            "steps": [{"step": s.get("step"), "ok": s.get("ok")}
                      for s in (m.get("steps") or [])],
        }
    return {"report": report, "report_path": str(p.relative_to(ROOT)) if p else "",
            "summary": {}, "alerts": {}, "auto": auto}


def collect_payload(source: Path | None = None) -> tuple[dict, str]:
    """优先用本机真实产出；没有（例如 GitHub Actions 云端采集失败）就用仓库示例。"""
    source = Path(source) if source else ROOT / "auto_output" / "latest_ranking.json"
    used_sample = not source.exists()
    if used_sample:
        source = SAMPLE / "latest_ranking.json"
    ranking = _json(source, {}) or {}
    label = "示例快照" if (used_sample or "sample" in source.parts) else "Actions 采集"

    # 报告按「时段」合并：本机/Actions 新产出的覆盖示例，缺的时段仍用示例补齐，
    # 保证午盘、收盘两个 tab 永远都有内容。
    sample_reports = (_json(SAMPLE / "reports.json", {}) or {}).get("reports", {}) or {}
    reports = dict(sample_reports)
    reports.update(_live_reports())
    status = _live_status()
    if not status.get("report"):
        status = _json(SAMPLE / "status.json", {}) or status

    boot = {
        "status": status,
        "reports": reports,
        "ranking": ranking,
        "source_label": label,
        "generated_at": ranking.get("generated_at", ""),
    }
    return boot, label


def render_html(boot: dict) -> str:
    # 无论报告来自本机产出、Actions 新跑的还是仓库示例，都在注入前统一人话化：
    # 中文指标卡片 + 无代码名/无 JSON 串的报告正文。
    boot = dict(boot)
    boot["reports"] = {k: normalize_report(v) for k, v in (boot.get("reports") or {}).items()}
    tpl = (WEB / "dashboard.html").read_text(encoding="utf-8")
    feed = (WEB / "static-feed.js").read_text(encoding="utf-8")
    if PLACEHOLDER not in tpl:
        raise RuntimeError("web/dashboard.html 缺少 FLOWMOOD_BOOT 注入点")
    blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    inject = ("<!-- 静态模式：内嵌快照 + 浏览器端实时行情源；页面模板与本地看板同源 -->\n"
              f"<script>window.FLOWMOOD_BOOT={blob};</script>\n"
              f"<script>\n{feed}\n</script>\n")
    return tpl.replace(PLACEHOLDER, inject)


def _num(v, d=2) -> str:
    return "--" if v is None else f"{float(v):.{d}f}"


def write_summary(boot: dict, label: str, out: Path) -> Path:
    rk = boot.get("ranking") or {}
    rows = rk.get("ranking") or []
    stats = rk.get("stats") or {}
    reps = boot.get("reports") or {}
    lines = ["## FlowMood · 研究报告 + 实时分析流", "",
             f"- 数据来源：**{label}** ｜榜单生成 `{rk.get('generated_at','')}`"
             f" ｜行情快照 `{rk.get('snapshot_at','')}`",
             f"- 排名样本 {stats.get('count', len(rows))} 只｜最高 {_num(stats.get('max'))}"
             f"｜中位 {_num(stats.get('median'))}｜≥72 分 {stats.get('ge72','--')} 只", ""]
    for sess, cn in (("midday", "午盘"), ("close", "收盘")):
        r = reps.get(sess)
        if not r:
            lines.append(f"- **{cn}报告**：暂无")
            continue
        m = r.get("meta") or {}
        lines.append(f"- **{cn}报告**：{r.get('generated_at','')} · 市场分 {_num(r.get('market_score'))}"
                     f" · {r.get('market_regime')} · 仓位 {r.get('position')}"
                     f" · 候选 {len(r.get('plans') or [])} 只"
                     f" · 流水线 {'全部成功' if m.get('all_ok') else '有环节失败'}")
    lines += ["", "### 排名 Top 10", "",
              "| # | 代码 | 名称 | 入选时间 | 评分 | 结论 | 涨跌 | 板块 |",
              "|---:|---|---|---|---:|---|---:|---|"]
    for r in rows[:10]:
        t = str(r.get("selected_at") or "")[11:19] or "—"
        ch = r.get("change_pct")
        lines.append(f"| {r.get('rank')} | {r.get('symbol')} | {r.get('name')} | {t} | "
                     f"{_num(r.get('score'))} | {r.get('action')} | "
                     f"{'+' if ch and ch > 0 else ''}{_num(ch)}% | {r.get('sector') or ''} |")
    lines += ["", "> 页面下方「实时分析流」由浏览器直连行情源每 20 秒刷新价格 / 涨跌 / 资金流，"
              "并与上一帧对比生成事件；评分沿用快照值，不伪装实时重算。"]
    p = out.parent / "summary.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def build(source: Path | None = None, output: Path | None = None) -> dict:
    output = Path(output) if output else ROOT / "docs" / "index.html"
    boot, label = collect_payload(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_html(boot), encoding="utf-8")
    summary = write_summary(boot, label, output)
    return {"boot": boot, "source_label": label, "output": output, "summary_path": summary}


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 FlowMood 静态看板（与本地看板同源）")
    ap.add_argument("--source", type=Path, help="排名 JSON，默认 auto_output/latest_ranking.json")
    ap.add_argument("--output", type=Path, default=ROOT / "docs" / "index.html")
    args = ap.parse_args()
    res = build(args.source, args.output)
    boot = res["boot"]
    print(f"静态看板: {res['output']}  [{res['source_label']}]")
    print(f"报告时段: {', '.join((boot.get('reports') or {}).keys()) or '无'}"
          f" | 排名 {len((boot.get('ranking') or {}).get('ranking') or [])} 条"
          f" | 体积 {res['output'].stat().st_size} bytes")
    print(f"运行摘要: {res['summary_path']}")


if __name__ == "__main__":
    main()
