#!/usr/bin/env python3
"""把排名 JSON 生成一个自包含的静态看板（GitHub Pages 用）。

浏览器打开不需要任何后端：榜单数据直接内嵌进 HTML。
用法：
    python3 build_static.py                        # 默认读最新排名，输出 docs/index.html
    python3 build_static.py --source sample/latest_ranking.json --output /tmp/index.html
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / "sample" / "latest_ranking.json"

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FlowMood · 资金流情绪交易台</title><style>
:root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:#172033;background:#f4f6fa}
body{margin:0}.top{background:#101828;color:#fff;padding:20px 28px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.top h1{margin:0;font-size:20px}.top small{color:#aab5c6}.wrap{max-width:1180px;margin:24px auto;padding:0 20px}
.panel{background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:16px;box-shadow:0 1px 2px #00000008;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}
.card{background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:14px}.label{font-size:12px;color:#667085}.value{font-size:22px;font-weight:700;margin-top:6px}
.meta{font-size:13px;color:#475467;line-height:1.8}.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;background:#ecfdf3;color:#067647}
.badge.sample{background:#f2f4f7;color:#667085}.warn{color:#b54708;font-weight:600}
.controls{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px;font-size:13px;color:#344054}
select,input[type=search]{border:1px solid #d0d5dd;border-radius:6px;padding:8px 12px;font-size:13px;background:#fff;color:#172033}
.wide{overflow:auto}.table{width:100%;border-collapse:collapse;font-size:13px;min-width:900px}
.table th,.table td{text-align:left;padding:10px;border-bottom:1px solid #eef0f3;white-space:nowrap}
.table th{color:#667085;font-weight:600}.buy{color:#d92d20;font-weight:700}.watch{color:#b54708;font-weight:700}.drop{color:#667085}
.up{color:#d92d20}.down{color:#067647}.muted{color:#98a2b3}
.seltime{font-variant-numeric:tabular-nums;color:#475467;font-size:12px}
.newtag{display:inline-block;margin-left:4px;padding:0 4px;border-radius:3px;background:#fef3f2;color:#d92d20;font-size:10px;font-weight:700}
footer{color:#98a2b3;font-size:12px;text-align:center;padding:8px 0 28px;line-height:1.8}
@media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}.wrap{margin:16px auto}}
</style></head><body>
<header class="top"><div><h1>FlowMood · 资金流情绪交易台</h1><small>资金流 × 情绪 × 七维加权评分 · 研究用途，不自动下单</small></div><small>由 GitHub Actions 自动生成</small></header>
<main class="wrap">
<div class="panel"><div class="meta">__META__</div></div>
<div class="grid">__CARDS__</div>
<section class="panel"><h2 style="font-size:16px;margin:0 0 12px">全市场评分排名</h2>
<div class="controls"><label>显示条数 <select id="limit"><option value="20">Top 20</option><option value="50">Top 50</option><option value="100" selected>Top 100</option></select></label>
<label>搜索 <input type="search" id="q" placeholder="代码 / 名称 / 板块"></label><span id="count" class="muted"></span></div>
<div class="wide"><table class="table"><thead><tr><th>#</th><th>股票</th><th>入选时间</th><th>评分</th><th>结论</th><th>涨跌</th><th>资金流</th><th>板块</th><th>空间</th><th>止损</th></tr></thead><tbody id="rows"></tbody></table></div></section>
<footer>数据说明：GitHub Actions 在交易时段定时采集全市场行情并重新评分；采集失败时展示仓库内示例快照。<br>涨用红色、跌用绿色（A 股习惯）。本页仅为量化研究记录，不构成任何投资建议。</footer>
</main><script id="rank-data" type="application/json">__DATA__</script><script>
const D=JSON.parse(document.getElementById('rank-data').textContent);
const $=id=>document.getElementById(id);
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(v,d)=>v==null?'--':Number(v).toFixed(d==null?2:d);
const pct=v=>{if(v==null)return '<span class="muted">--</span>';const c=v>0?'up':v<0?'down':'';return '<span class="'+c+'">'+(v>0?'+':'')+Number(v).toFixed(2)+'%</span>'};
const sel=o=>{const s=o&&o.selected_at;if(!s)return '<span class="muted">--</span>';const t=String(s).slice(11,19);const tag=o.selected_new?'<span class="newtag">新</span>':'';return '<span class="seltime" title="入选 '+esc(String(s).replace('T',' '))+'">'+esc(t)+'</span>'+tag};
function render(){const lim=Number($('limit').value),q=$('q').value.trim().toLowerCase();
 const rows=(D.ranking||[]).filter(r=>!q||[r.symbol,r.name,r.sector].some(v=>String(v||'').toLowerCase().includes(q))).slice(0,lim);
 $('count').textContent='显示 '+rows.length+' / '+(D.ranking||[]).length+' 条';
 $('rows').innerHTML=rows.length?rows.map(p=>'<tr><td>'+p.rank+'</td><td>'+esc(p.symbol)+' '+esc(p.name)+'</td><td>'+sel(p)+'</td><td><b>'+num(p.score)+'</b></td><td class="'+(p.action==='条件买入'?'buy':p.action==='重点观察'?'watch':'drop')+'">'+esc(p.action)+'</td><td>'+pct(p.change_pct)+'</td><td>'+pct(p.money_flow_pct)+'</td><td>'+esc(p.sector)+'</td><td>'+num(p.space_pct,1)+'%</td><td>'+num(p.stop_pct)+'%</td></tr>').join(''):'<tr><td colspan="10" class="muted">无匹配记录</td></tr>'}
$('limit').onchange=render;$('q').oninput=render;render();
</script></body></html>
"""


def build(source: Path | None = None, output: Path | None = None) -> dict:
    """返回 {data, source_label, output, summary_path}。"""
    source = Path(source) if source else ROOT / "auto_output" / "latest_ranking.json"
    output = Path(output) if output else ROOT / "docs" / "index.html"
    used_sample = not source.exists()
    if used_sample:
        source = SAMPLE
    data = json.loads(source.read_text(encoding="utf-8"))
    rows = data.get("ranking") or []
    stats = data.get("stats") or {}
    is_sample = used_sample or "sample" in source.parts

    snap_at = str(data.get("snapshot_at") or data.get("generated_at") or "").replace("T", " ")
    gen_at = str(data.get("generated_at") or "").replace("T", " ")
    label = "示例快照" if is_sample else "Actions 采集"
    badge = f'<span class="badge{" sample" if is_sample else ""}">{label}</span>'
    meta = (f'{badge} · 榜单生成 {esc(gen_at)} · 行情快照 {esc(snap_at)} · '
            f'样本 {stats.get("count", len(rows))} 只 · 市场分 {num(stats.get("market_score") or data.get("market_score"))}')
    if data.get("live_warning"):
        meta += f' <span class="warn">⚠ {esc(data["live_warning"])}</span>'

    cards = [("最高分", num(stats.get("max"))), ("中位分", num(stats.get("median"))),
             ("标准差", num(stats.get("stdev"))), ("≥72 分", str(stats.get("ge72", "--")) + " 只"),
             ("条件买入", str((stats.get("actions") or {}).get("条件买入", "--")) + " 只")]
    cards_html = "".join(f'<div class="card"><div class="label">{esc(k)}</div>'
                         f'<div class="value">{esc(v)}</div></div>' for k, v in cards)

    payload = {"ranking": rows, "stats": stats, "generated_at": data.get("generated_at"),
               "snapshot_at": data.get("snapshot_at")}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = (HTML.replace("__META__", meta).replace("__CARDS__", cards_html)
                .replace("__DATA__", blob))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")

    # 同时产出 Actions Step Summary，运行结果页直接可读
    lines = [f"## FlowMood 排名 · {label}", "",
             f"- 榜单生成：{gen_at or '—'}｜行情快照：{snap_at or '—'}",
             f"- 样本 {stats.get('count', len(rows))} 只｜最高 {num(stats.get('max'))}"
             f"｜中位 {num(stats.get('median'))}｜标准差 {num(stats.get('stdev'))}"
             f"｜≥72 分 {stats.get('ge72', '--')} 只", "",
             "| # | 代码 | 名称 | 入选时间 | 评分 | 结论 | 涨跌 | 板块 |",
             "|---:|---|---|---|---:|---|---:|---|"]
    for r in rows[:10]:
        t = str(r.get("selected_at") or "")[11:19] or "—"
        ch = r.get("change_pct")
        lines.append(f"| {r.get('rank')} | {r.get('symbol')} | {r.get('name')} | {t} | "
                     f"{num(r.get('score'))} | {r.get('action')} | "
                     f"{'+' if ch and ch > 0 else ''}{num(ch)}% | {r.get('sector') or ''} |")
    summary = output.parent / "summary.md"
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"data": data, "source_label": label, "output": output, "summary_path": summary}


def esc(x) -> str:
    return (str(x if x is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def num(v, d=2) -> str:
    return "--" if v is None else f"{float(v):.{d}f}"


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 FlowMood 静态看板")
    ap.add_argument("--source", type=Path, help="排名 JSON，默认 auto_output/latest_ranking.json")
    ap.add_argument("--output", type=Path, default=ROOT / "docs" / "index.html")
    args = ap.parse_args()
    res = build(args.source, args.output)
    print(f"静态看板: {res['output']}  [{res['source_label']}]")
    print(f"运行摘要: {res['summary_path']}")


if __name__ == "__main__":
    main()
