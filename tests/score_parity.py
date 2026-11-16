#!/usr/bin/env python3
"""JS 评分引擎与 Python 评分引擎的逐只对账。

Pages 上那套浏览器端重排之所以可信，全靠一件事：它和本地服务端算出来的是
同一个分。一旦公式飘了，静态页会开始给出一份「看起来专业、实际口径不同」的
榜单——那比不实时更糟。所以这里拿同一份快照、同一套配置，两边各算一遍，
逐只比对，误差超过 0.01 就判失败。

用法：
    python3 tests/score_parity.py                 # 全市场对账
    python3 tests/score_parity.py --market 47.13  # 指定市场分
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TOL = 0.01          # 分数允许误差（两边 round 策略不同）
ACTION_MISMATCH_MAX = 0     # 动作判定必须完全一致，一处不同都不行
LIVE_ELAPSED = 0.6  # 模拟「上午 10:54」这种已交易 60% 的时点


def make_quotes(rows: list[dict], elapsed: float = LIVE_ELAPSED) -> dict:
    """构造一份确定性的「盘中行情」，用来端到端比对两边的覆盖+评分链路。

    用代码字符做扰动种子，纯静态评估里没有任何真实行情也能验证：
    行情覆盖 → 量比重算 → 板块聚合 → 七维评分 这一整条链两边是否同解。
    """
    quotes = {}
    for r in rows:
        code = str(r.get("code") or "")
        px = float(r.get("price") or 0)
        if not code or px <= 0:
            continue
        seed = sum(ord(c) * (i + 1) for i, c in enumerate(code))
        prev = float(r.get("prev_close") or px) or px
        price = round(px * (1 + (((seed % 61) - 30) / 10.0) / 100), 2)
        if price <= 0:
            continue
        avg20 = float(r.get("avg_amount20") or 0)
        ratio = 0.5 + (seed % 45) / 10.0            # 0.5~4.9 倍量能
        amount = avg20 * elapsed * ratio
        quotes[code] = {
            "f2": price,
            "f3": round((price / prev - 1) * 100, 2),
            "f6": round(amount, 2),
            "f8": round(float(r.get("turnover_pct") or 1) * (1 + (price / prev - 1)), 2),
            "f15": round(max(price, px) * 1.005, 2),
            "f16": round(min(price, px) * 0.995, 2),
            "f17": prev,
            "f18": prev,
            "f100": r.get("sector"),
            "f184": round(((seed % 400) - 150) / 10.0, 2),   # -15% ~ +25% 资金流
        }
    return quotes


def python_scores(snapshot: Path, market: float, cfg: dict, session: str = "close",
                  quotes: dict | None = None) -> dict:
    """用本地生产代码给全市场打分，返回 {symbol: (score, action)}。

    传 quotes 时走与 dashboard_server.live_ranking 相同的路径：
    先 apply_quotes 覆盖价格类字段，再 enrich、再评分。
    """
    import stock_selector as ss
    from sector_scoring import enrich

    rows = json.loads(snapshot.read_text(encoding="utf-8"))
    if quotes:
        from live_quotes import apply_quotes
        rows, _ = apply_quotes(rows, quotes, elapsed=LIVE_ELAPSED)
    enriched, _ = enrich(rows, cfg.get("strong_sector_score_min", 70.0))
    out = {}
    for row in enriched:
        plan = ss.score_stock(row, market, session, cfg)
        out[str(row.get("symbol"))] = {"score": round(plan.score, 2), "action": plan.action}
    return out


NODE_SCRIPT = r"""
const path = require('path');
const fs = require('fs');
const engine = require(path.join(process.argv[2], 'web', 'score-engine.js'));
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
let rows = payload.rows;
if (payload.quotes) {
  rows = engine.applyQuotes(rows, payload.quotes, payload.elapsed).rows;
}
const out = engine.rank(rows, {
  market_score: payload.market, session: payload.session,
  config: payload.config, limit: payload.limit
});
fs.writeFileSync(payload.out, JSON.stringify({ ranking: out.ranking, stats: out.stats }));
console.log(JSON.stringify({ ranked: out.ranking.length, scored: out.stats.count,
                             max: out.stats.max, ge72: out.stats.ge72 }));
"""


def real_quotes() -> dict | None:
    """拉一份真实盘中行情（腾讯通道），用来验证两边吃同一份真数据是否同解。

    比起模拟行情，这一步额外覆盖了「涨停价判封板」「换手率取源值」
    「资金流回填定稿」这些只在真行情里才出现的情形。
    """
    try:
        from live_quotes import fetch_quotes_tencent
    except Exception:  # noqa: BLE001
        return None
    quotes, meta = fetch_quotes_tencent()
    if not quotes or meta.get("coverage", 0) < 0.5:
        print(f"腾讯行情不可达（覆盖 {meta.get('coverage')}），跳过真实行情对账")
        return None
    print(f"真实行情：{len(quotes)} 只，覆盖率 {meta['coverage']}")
    return quotes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=str(ROOT / "normalized_snapshots.json"))
    ap.add_argument("--market", type=float, default=None)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--live", action="store_true",
                    help="叠加一份确定性模拟行情，验证「行情覆盖→板块聚合→评分」整链")
    ap.add_argument("--tencent", action="store_true",
                    help="拉真实腾讯行情做对账（覆盖涨停价判封板等真行情分支）")
    args = ap.parse_args()

    snapshot = Path(args.snapshot)
    if not snapshot.exists():
        print(f"跳过：{snapshot.name} 不存在（云端采集失败或未同步）")
        return 0

    cfg = json.loads((ROOT / "strategy_config.json").read_text(encoding="utf-8"))
    market = args.market
    if market is None:
        market = 58.35
        for p in sorted((ROOT / "auto_output").glob("*/[0-9]*/stock_report_*.json"),
                        key=lambda x: x.stat().st_mtime, reverse=True)[:1]:
            try:
                market = float(json.loads(p.read_text(encoding="utf-8"))["market_score"])
            except Exception:  # noqa: BLE001
                pass

    rows = json.loads(snapshot.read_text(encoding="utf-8"))
    if args.tencent:
        quotes = real_quotes()
        if quotes is None:
            return 0
        mode = "真实腾讯行情覆盖"
    else:
        quotes = make_quotes(rows) if args.live else None
        mode = "盘中行情覆盖" if quotes else "静态快照"
    print(f"对账起点：{snapshot.name} · 市场分 {market} · {mode}")
    py = python_scores(snapshot, market, cfg, quotes=quotes)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        payload = tmp / "in.json"
        result = tmp / "out.json"
        runner = tmp / "run.js"
        runner.write_text(NODE_SCRIPT, encoding="utf-8")
        payload.write_text(json.dumps({
            "rows": rows, "market": market, "session": "close",
            "config": cfg, "limit": args.limit, "out": str(result),
            "quotes": quotes, "elapsed": LIVE_ELAPSED,
        }, ensure_ascii=False), encoding="utf-8")

        proc = subprocess.run(
            ["node", str(runner), str(ROOT), str(payload)],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            print("Node 执行失败：")
            print(proc.stderr[-3000:])
            return 1
        print("JS 侧：" + proc.stdout.strip())
        js = json.loads(result.read_text(encoding="utf-8"))

    js_rank = js["ranking"]
    print(f"JS 返回榜单 {len(js_rank)} 只，Python 打分 {len(py)} 只")

    # 1) 前 limit 名逐只比分数与动作
    bad_score, bad_action = [], []
    for row in js_rank[:args.limit]:
        sym = row["symbol"]
        ref = py.get(sym)
        if not ref:
            bad_score.append((sym, row["name"], row["score"], None, "Python 无此标的"))
            continue
        if abs(ref["score"] - row["score"]) > TOL:
            bad_score.append((sym, row["name"], row["score"], ref["score"], "分数"))
        if ref["action"] != row["action"]:
            bad_action.append((sym, row["name"], row["action"], ref["action"]))

    # 2) 分数分布核对：最高分/中位/≥72 家数
    py_scores_all = sorted((v["score"] for v in py.values()), reverse=True)
    js_stats = js["stats"]
    dist_ok = True
    if py_scores_all:
        py_max = round(max(py_scores_all), 2)
        if abs(py_max - js_stats["max"]) > TOL:
            dist_ok = False
            print(f"  最高分不一致：Python {py_max} vs JS {js_stats['max']}")
        py_ge72 = sum(1 for s in py_scores_all if s >= cfg.get("buy_score_min", 72))
        if py_ge72 != js_stats["ge72"]:
            dist_ok = False
            print(f"  ≥72 分家数不一致：Python {py_ge72} vs JS {js_stats['ge72']}")

    print()
    if bad_score:
        print(f"分数不一致 {len(bad_score)} 只（前 10）：")
        for sym, name, a, b, why in bad_score[:10]:
            print(f"  {sym} {name:<6} JS={a} Python={b} {why}")
    if bad_action:
        print(f"动作不一致 {len(bad_action)} 只（前 10）：")
        for sym, name, a, b in bad_action[:10]:
            print(f"  {sym} {name:<6} JS={a} Python={b}")

    ok = not bad_score and len(bad_action) <= ACTION_MISMATCH_MAX and dist_ok
    if ok:
        print(f"✅ 对账通过：前 {min(len(js_rank), args.limit)} 名分数误差 ≤ {TOL}，动作完全一致，分布一致")
        top = js_rank[0] if js_rank else None
        if top:
            print(f"   Top1 {top['name']} {top['score']} 分 {top['action']}")
    else:
        print("❌ 对账失败：浏览器端与本地口径不一致")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
