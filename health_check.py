"""Health check for run_watch_all state."""
from __future__ import annotations
import argparse,json,time
from pathlib import Path
from alert_webhook import send

if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--state',type=Path,required=True); ap.add_argument('--webhook'); ap.add_argument('--max-silence-min',type=int,default=0); a=ap.parse_args()
 try:
  s=json.loads(a.state.read_text()); runs=s.get('runs',[]); last=runs[-1] if runs else None
  out={'state_exists':True,'state_recovered':bool(s.get('state_recovered',False)),'run_count':len(runs),'processed_hashes':len(s.get('hashes',{})),'last_run':last,'healthy':bool(last and last.get('status')=='ok')}
 except Exception as e: out={'state_exists':a.state.exists(),'healthy':False,'error':str(e)}
 if a.max_silence_min and out.get('last_run'):
  age=(time.time()-time.mktime(time.strptime(out['last_run']['time'],'%Y-%m-%dT%H:%M:%S')))/60; out['silence_minutes']=round(age,1)
  if age>a.max_silence_min: out['healthy']=False; out['silence_alert']=f'超过 {a.max_silence_min} 分钟无新快照'
 print(json.dumps(out,ensure_ascii=False,indent=2))
 if a.webhook and not out.get('healthy'):
  try: send(a.webhook,'研究监控健康告警',[out.get('error') or out.get('silence_alert') or '最近一次监控运行失败'])
  except Exception: pass
