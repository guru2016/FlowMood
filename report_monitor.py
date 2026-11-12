"""Compare stock plan JSON reports and emit operational alerts."""
from __future__ import annotations
import argparse,json
from pathlib import Path

def inspect(current: dict, previous: dict|None=None) -> dict:
    plans=current.get('plans',[]); alerts=[]; market=float(current.get('market_score',50) or 50)
    if current.get('accepted',1)==0: alerts.append('快照没有有效股票')
    if current.get('errors',0)>0: alerts.append(f"快照存在 {current.get('errors')} 条异常记录")
    active=[p for p in plans if p.get('action')=='条件买入']
    if not plans: alerts.append('没有生成任何候选计划')
    if market<42: alerts.append(f'市场分过低：{market:.2f}')
    if active and max(float(p.get('space_pct',0) or 0) for p in active)<4: alerts.append('所有条件买入标的预期空间低于4%')
    if any(abs(float(p.get('stop_pct',0) or 0))>8 for p in active): alerts.append('存在止损幅度超过8%的标的')
    if previous:
        old_active=sum(p.get('action')=='条件买入' for p in previous.get('plans',[]))
        if old_active and len(active)<old_active/2: alerts.append(f'条件买入数量骤减：{old_active}->{len(active)}')
    return {'alerts':alerts,'active_count':len(active),'plan_count':len(plans),'market_score':market}

if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--current',type=Path,required=True); ap.add_argument('--previous',type=Path); ap.add_argument('--output',type=Path,default=Path('alerts.json')); a=ap.parse_args()
 out=inspect(json.loads(a.current.read_text()),json.loads(a.previous.read_text()) if a.previous and a.previous.exists() else None); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False,indent=2))
