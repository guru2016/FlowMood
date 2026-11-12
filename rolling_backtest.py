"""Walk-forward backtest for midday/close stock plans."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from statistics import mean
from stock_selector import select


def run(dataset, session='close', commission_bps=5.0, slippage_bps=5.0):
    cost=(commission_bps*2+slippage_bps*2)/100
    days=sorted({str(x.get('trade_date','')) for x in dataset if x.get('trade_date')})
    equity=peak=1.0; max_dd=0.0; daily=[]; trades=[]
    for day in days:
        rows=[x for x in dataset if str(x.get('trade_date'))==day]
        market=float(rows[0].get('market_score',50) or 50)
        plans=select(rows,market,session,limit=len(rows)); by={str(x.get('symbol')):x for x in rows}
        returns=[]
        for plan in plans:
            if plan.action!='条件买入': continue
            row=by.get(plan.symbol,{})
            if row.get('untradable') or row.get('limit_up') or row.get('limit_down'): continue
            net=float(row.get('next_return_pct',0) or 0)-cost
            returns.append(net); trades.append({'trade_date':day,'symbol':plan.symbol,'score':plan.score,'net_return_pct':net})
        day_ret=mean(returns) if returns else 0.0
        equity*=1+day_ret/100; peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak)
        daily.append({'trade_date':day,'market_score':market,'trade_count':len(returns),'return_pct':round(day_ret,4),'equity':round(equity,6)})
    rets=[x['net_return_pct'] for x in trades]
    wins=[x for x in rets if x>0]; losses=[x for x in rets if x<0]
    return {'session':session,'days':len(days),'trades':len(trades),'win_rate_pct':round(len(wins)/len(rets)*100,2) if rets else 0,'avg_trade_return_pct':round(mean(rets),4) if rets else 0,'profit_factor':round(sum(wins)/abs(sum(losses)),3) if losses else None,'cumulative_return_pct':round((equity-1)*100,3),'max_drawdown_pct':round(max_dd*100,3),'daily':daily,'trade_log':trades}

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--session',choices=['midday','close'],default='close'); ap.add_argument('--commission-bps',type=float,default=5); ap.add_argument('--slippage-bps',type=float,default=5); ap.add_argument('--output',default='rolling_backtest.json'); a=ap.parse_args()
    out=run(json.loads(Path(a.input).read_text()),a.session,a.commission_bps,a.slippage_bps); Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)); print(json.dumps(out,ensure_ascii=False,indent=2))
