"""Watch a directory for new stock snapshot CSV/JSON files."""
from __future__ import annotations
import argparse,json,time,hashlib
from datetime import datetime
from pathlib import Path
from ingest_snapshots import load,normalize
from stock_pipeline import run as run_pipeline
from report_monitor import inspect
from alert_webhook import send


def file_hash(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def stable(path: Path, delay: float = 1.0) -> bool:
    first=(path.stat().st_size,path.stat().st_mtime_ns); time.sleep(delay)
    second=(path.stat().st_size,path.stat().st_mtime_ns); return first==second


def process(path: Path, output: Path, market_score: float, session: str, webhook: str | None = None):
    rows, errors=normalize(load(path)); output.mkdir(parents=True,exist_ok=True)
    normalized=output/(path.stem+'_normalized.json'); normalized.write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    (output/(path.stem+'_errors.json')).write_text(json.dumps(errors,ensure_ascii=False,indent=2))
    report=output/f"{session}_{datetime.now():%Y%m%d_%H%M%S}.md"
    result=run_pipeline(normalized,report,market_score,session,10)
    result['accepted']=len(rows); result['errors']=len(errors); result['source']=str(path)
    (output/(report.stem+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    reports=sorted(output.glob(f'{session}_*.json'))
    previous=None
    if len(reports)>1:
        try: previous=json.loads(reports[-2].read_text(encoding='utf-8'))
        except Exception: previous=None
    alerts=inspect(result,previous)
    result['alerts']=alerts['alerts']; result['status']='alert' if alerts['alerts'] else 'ok'
    (output/(report.stem+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    (output/(report.stem+'_alerts.json')).write_text(json.dumps(alerts,ensure_ascii=False,indent=2))
    if webhook and alerts.get('alerts'):
        try: send(webhook, f'同花顺研究告警 {session}', alerts['alerts'])
        except Exception as exc: (output/(report.stem+'_webhook_error.txt')).write_text(str(exc),encoding='utf-8')
    return report,result

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--watch',type=Path,required=True); ap.add_argument('--output',type=Path,default=Path('watch_output')); ap.add_argument('--market-score',type=float,required=True); ap.add_argument('--session',choices=['midday','close'],default='close'); ap.add_argument('--interval',type=int,default=30); ap.add_argument('--webhook'); ap.add_argument('--once',action='store_true'); a=ap.parse_args()
    seen=set(); seen_hashes=set()
    while True:
        files=sorted([*a.watch.glob('*.csv'),*a.watch.glob('*.json')],key=lambda p:p.stat().st_mtime)
        for p in files:
            if p in seen: continue
            if p.name.endswith('_normalized.json') or p.name.endswith('_errors.json'): seen.add(p); continue
            if not stable(p): continue
            digest=file_hash(p)
            if digest in seen_hashes: seen.add(p); continue
            report,result=process(p,a.output,a.market_score,a.session,a.webhook); print(report); seen.add(p); seen_hashes.add(digest)
        if a.once: break
        time.sleep(a.interval)
