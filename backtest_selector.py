"""Evaluate stock_selector plans with costs and execution constraints."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from statistics import mean
from stock_selector import select


def evaluate(rows, market_score, session, commission_bps=5.0, slippage_bps=5.0):
    plans=select(rows,market_score,session,limit=len(rows)); by_symbol={str(r.get('symbol','')):r for r in rows}; result=[]
    roundtrip_cost=(commission_bps*2+slippage_bps*2)/100
    for p in plans:
        row=by_symbol.get(p.symbol,{})
        gross=float(row.get('next_return_pct',0) or 0)
        locked=bool(row.get('limit_up') or row.get('limit_down') or row.get('untradable'))
        net=None if locked else gross-roundtrip_cost
        outcome='untradable' if locked else ('hit' if net>=0 else 'loss')
        result.append({'symbol':p.symbol,'action':p.action,'score':p.score,'gross_return_pct':gross,'net_return_pct':net,'target_space_pct':p.space_pct,'stop_pct':p.stop_pct,'outcome':outcome})
    active=[x for x in result if x['action']=='条件买入' and x['net_return_pct'] is not None]
    returns=[x['net_return_pct'] for x in active]; wins=[x for x in returns if x>0]; losses=[x for x in returns if x<0]
    equity=peak=1.0; max_dd=0.0
    for r in returns:
        equity*=1+r/100; peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak)
    return {'session':session,'commission_bps':commission_bps,'slippage_bps':slippage_bps,'roundtrip_cost_pct':roundtrip_cost,'plans':result,'active_count':len(active),'win_rate_pct':round(len(wins)/len(active)*100,2) if active else 0,'avg_net_return_pct':round(mean(returns),2) if returns else 0,'profit_factor':round(sum(wins)/abs(sum(losses)),2) if losses else None,'max_drawdown_pct':round(max_dd*100,2)}

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--market-score',type=float,required=True); ap.add_argument('--session',choices=['midday','close'],default='close'); ap.add_argument('--commission-bps',type=float,default=5.0); ap.add_argument('--slippage-bps',type=float,default=5.0); ap.add_argument('--output',default='backtest_result.json'); a=ap.parse_args()
    rows=json.loads(Path(a.input).read_text()); out=evaluate(rows,a.market_score,a.session,a.commission_bps,a.slippage_bps); Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False,indent=2))
