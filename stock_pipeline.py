"""One-command sector + stock midday/close pipeline."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from stock_selector import select,render
from sector_scoring import enrich


def run(input_path, output_path, market_score, session, limit, config=None):
    rows=json.loads(Path(input_path).read_text(encoding='utf-8'))
    enriched,sectors=enrich(rows)
    plans=select(enriched,market_score,session,limit,config)
    report=render(plans,market_score,session)
    lines=['# 板块排名','']
    for name,score in sorted(sectors.items(),key=lambda x:x[1],reverse=True): lines.append(f'- {name}: **{score:.2f}**')
    Path(output_path).write_text('\n'.join(lines)+'\n\n'+report+'\n',encoding='utf-8')
    plan_rows=[]
    for p in plans:
        item=dict(p.__dict__); item['sector']=next((str(r.get('sector','未分类')) for r in enriched if str(r.get('symbol'))==p.symbol),'未分类'); plan_rows.append(item)
    return {'generated_at': __import__('datetime').datetime.now().isoformat(timespec='seconds'), 'market_score': market_score, 'session': session, 'sectors': sectors,'plans':plan_rows}

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--output',default='stock_pipeline.md'); ap.add_argument('--market-score',type=float,required=True); ap.add_argument('--session',choices=['midday','close'],default='close'); ap.add_argument('--limit',type=int,default=10); ap.add_argument('--config',type=Path); a=ap.parse_args()
    config=json.loads(a.config.read_text()) if a.config else {}
    print(json.dumps(run(a.input,a.output,a.market_score,a.session,a.limit,config),ensure_ascii=False,indent=2))
