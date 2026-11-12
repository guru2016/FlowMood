"""Compute market score and regime from H5 chart JSON samples."""
from __future__ import annotations
import argparse,json
from pathlib import Path

def latest(path,key):
 x=json.loads(Path(path).read_text()); d=x.get('data') or {}; keys=d.get('point_key_list') or []; pts=d.get('point_list') or []; return dict(zip(keys,pts[-1])) if pts else {}
def compute(capital,temp):
 score=50.; reasons=[]; temp_value=50.
 try:
  n=float(capital.get('net_inflow')); score+=max(-20,min(20,n/100)); reasons.append(f'大盘资金净流入{n:.2f}')
 except (TypeError,ValueError): pass
 try:
  temp_value=float(temp.get('temperature')); score+=(temp_value-50)*.25; reasons.append(f'市场温度{temp_value:.0f}')
 except (TypeError,ValueError): pass
 score=max(0,min(100,score)); state='偏强' if score>=60 else '震荡' if score>=42 else '偏弱'; position='标准仓' if score>=60 else '轻仓' if score>=42 else '空仓'; max_exposure=1.0 if position=='标准仓' else .3 if position=='轻仓' else 0.0
 return round(score,2),reasons,state,position,max_exposure
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--capital',type=Path,required=True); ap.add_argument('--temperature',type=Path,required=True); ap.add_argument('--output',default='market_score.json'); a=ap.parse_args(); s,r,state,position,max_exposure=compute(latest(a.capital,'net_inflow'),latest(a.temperature,'temperature')); out={'market_score':s,'state':state,'position':position,'max_exposure':max_exposure,'reasons':r}; Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False))
