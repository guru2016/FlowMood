"""Watch snapshots and run the complete research pipeline once per content hash."""
from __future__ import annotations
import argparse,hashlib,json,time,subprocess,sys
from pathlib import Path
from market_score import compute,latest
from watcher_state import load,save

def digest(p):
 h=hashlib.sha256(); h.update(p.read_bytes()); return h.hexdigest()
def stable(p,delay=1):
 a=(p.stat().st_size,p.stat().st_mtime_ns); time.sleep(delay); return a==(p.stat().st_size,p.stat().st_mtime_ns)
def run_one(p,args):
 score=args.market_score
 if args.auto_market_score: score,_,_,_,_=compute(latest(args.offline_dir/'sjdp_market_capital.json','net_inflow'),latest(args.offline_dir/'sjdp_temperature_hs.json','temperature'))
 stamp=p.stem+'_'+time.strftime('%Y%m%d_%H%M%S'); out=args.output/stamp; db=args.output/'research.sqlite3'; out.mkdir(parents=True,exist_ok=True)
 cmd=[sys.executable,'run_all.py','--input',str(p),'--session',args.session,'--market-score',str(score),'--out',str(out),'--db',str(db),'--proxy',args.proxy,'--offline-dir',str(args.offline_dir)]
 for attempt in range(2):
  try: subprocess.run(cmd,check=True); return out
  except Exception:
   if attempt==1: raise
   time.sleep(2)
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--watch',type=Path,required=True); ap.add_argument('--output',type=Path,default=Path('watch_all_output')); ap.add_argument('--market-score',type=float); ap.add_argument('--auto-market-score',action='store_true'); ap.add_argument('--session',choices=['midday','close'],default='close'); ap.add_argument('--proxy',default='http://127.0.0.1:7897'); ap.add_argument('--offline-dir',type=Path,default=Path('dynamic/h5_api')); ap.add_argument('--interval',type=int,default=30); ap.add_argument('--once',action='store_true'); a=ap.parse_args()
 if not a.auto_market_score and a.market_score is None: ap.error('--market-score or --auto-market-score is required')
 state_path=a.output/'watcher_state.json'; state=load(state_path); seen=set(state.get('hashes',{}))
 while True:
  for p in sorted([*a.watch.glob('*.csv'),*a.watch.glob('*.json')],key=lambda x:x.stat().st_mtime):
   if p.name.endswith(('_normalized.json','_errors.json')) or not stable(p): continue
   d=digest(p)
   if d in seen: continue
   started=time.time()
   try:
    out=run_one(p,a)
    state.setdefault('hashes',{})[d]=str(out); state.setdefault('runs',[]).append({'hash':d,'source':str(p),'output':str(out),'time':time.strftime('%Y-%m-%dT%H:%M:%S'),'duration_sec':round(time.time()-started,2),'status':'ok'}); save(state_path,state)
    latest_report=sorted(out.glob('stock_report_*.json'))[-1] if list(out.glob('stock_report_*.json')) else None
    summary_cmd=[sys.executable,'system_summary.py','--db',str(a.output/'research.sqlite3'),'--state',str(state_path),'--output',str(out/'system_summary.md')]
    if latest_report: summary_cmd += ['--report',str(latest_report)]
    subprocess.run(summary_cmd,check=True)
    print(out); seen.add(d)
   except Exception as exc:
    state.setdefault('runs',[]).append({'hash':d,'source':str(p),'time':time.strftime('%Y-%m-%dT%H:%M:%S'),'duration_sec':round(time.time()-started,2),'status':'failed','error':str(exc)}); save(state_path,state); print(f'failed: {p}: {exc}',file=sys.stderr)
  if a.once: break
  time.sleep(a.interval)
