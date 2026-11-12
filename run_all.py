"""One-command stock research run."""
from __future__ import annotations
import argparse,json,subprocess,sys
from pathlib import Path
from market_score import compute,latest

def main():
 ap=argparse.ArgumentParser(); ap.add_argument('--input',type=Path,required=True); ap.add_argument('--session',choices=['midday','close'],required=True); ap.add_argument('--market-score',type=float); ap.add_argument('--auto-market-score',action='store_true'); ap.add_argument('--out',type=Path,default=Path('run_output')); ap.add_argument('--db',type=Path,default=Path('run.sqlite3')); ap.add_argument('--proxy',default='http://127.0.0.1:7897'); ap.add_argument('--offline-dir',type=Path,default=Path('dynamic/h5_api')); ap.add_argument('--config',type=Path,default=Path('strategy_config.json')); a=ap.parse_args(); a.out.mkdir(parents=True,exist_ok=True)
 normalized=a.out/'normalized.json'; errors=a.out/'errors.json'
 if a.auto_market_score:
  a.market_score,_,_,_,_=compute(latest(a.offline_dir/'sjdp_market_capital.json','net_inflow'),latest(a.offline_dir/'sjdp_temperature_hs.json','temperature'))
 if a.market_score is None: raise SystemExit('--market-score or --auto-market-score is required')
 subprocess.run([sys.executable,'ingest_snapshots.py','--input',str(a.input),'--output',str(normalized),'--errors',str(errors)],check=True)
 subprocess.run([sys.executable,'daily_runner.py','--out',str(a.out),'--db',str(a.db),'--proxy',a.proxy,'--stocks',str(normalized),'--session',a.session,'--offline-dir',str(a.offline_dir),'--config',str(a.config)],check=True)
 reports=sorted(a.out.glob('stock_report_*.json'))
 if reports: subprocess.run([sys.executable,'performance_tracker.py','--db',str(a.db),'--plan-json',str(reports[-1])],check=True)
 subprocess.run([sys.executable,'performance_daily.py','--db',str(a.db),'--output',str(a.out/'performance_daily.csv')],check=True)
 subprocess.run([sys.executable,'rolling_windows.py','--db',str(a.db),'--output',str(a.out/'rolling_windows.json')],check=True)
 print(a.out)
if __name__=='__main__': main()
