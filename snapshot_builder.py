"""Build stock_selector snapshots from quote + daily bar JSON.

Quote input: list of {symbol,name,price,prev_close,high,low,amount,turnover_pct,...}
Bars input: {"600000.SH": [{"high":..,"low":..,"close":..}, ...]}
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
from typing import Any


def f(v: Any, default=0.0) -> float:
    try: return float(v)
    except (TypeError, ValueError): return default


def atr14(bars: list[dict[str, Any]]) -> float:
    if not bars: return 0.0
    values=[]
    prev=None
    for b in bars[-14:]:
        h,l,c=f(b.get('high')),f(b.get('low')),f(b.get('close'))
        values.append(max(h-l, abs(h-prev) if prev else 0, abs(l-prev) if prev else 0))
        prev=c
    return sum(values)/len(values) if values else 0.0


def build(quotes: list[dict[str, Any]], bars: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    out=[]
    for q in quotes:
        symbol=str(q.get('symbol',''))
        price=f(q.get('price'))
        avg_amount=f(q.get('avg_amount20'))
        amount=f(q.get('amount'))
        row=dict(q)
        row['amount_ratio']=amount/avg_amount if avg_amount else f(q.get('amount_ratio'),1.0)
        row['atr14']=atr14(bars.get(symbol, [])) or price*0.03
        if not row.get('prev_close') and bars.get(symbol): row['prev_close']=bars[symbol][-1].get('close',price)
        out.append(row)
    return out

if __name__ == '__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--quotes',required=True); ap.add_argument('--bars',required=True); ap.add_argument('--output',required=True); a=ap.parse_args()
    quotes=json.loads(Path(a.quotes).read_text()); bars=json.loads(Path(a.bars).read_text())
    result=build(quotes,bars); Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)); print(f'built {len(result)} snapshots')
