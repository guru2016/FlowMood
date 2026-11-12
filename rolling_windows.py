"""Rolling 5/20-session performance windows."""
from __future__ import annotations
import argparse,json,sqlite3
from pathlib import Path

def rows(db):
 c=sqlite3.connect(db); return c.execute("select substr(generated_at,1,10),realized_return_pct from plan_performance where action='条件买入' and realized_return_pct is not null order by generated_at").fetchall()
def calc(xs):
 vals=[x[1] for x in xs]; equity=peak=1.; dd=0
 for v in vals: equity*=1+v/100; peak=max(peak,equity); dd=max(dd,(peak-equity)/peak)
 return {'count':len(vals),'win_rate_pct':round(sum(v>0 for v in vals)/len(vals)*100,2) if vals else 0,'avg_return_pct':round(sum(vals)/len(vals),3) if vals else 0,'cumulative_return_pct':round((equity-1)*100,3),'max_drawdown_pct':round(dd*100,3)}
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--db',required=True); ap.add_argument('--output',default='rolling_windows.json'); a=ap.parse_args(); allrows=rows(a.db); dates=sorted({x[0] for x in allrows}); out={}
 for n in [5,20]: out[f'{n}d']=calc([x for x in allrows if x[0] in dates[-n:]])
 Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False,indent=2))
