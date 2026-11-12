"""Index reports plus sector/stock performance summaries."""
from __future__ import annotations
import argparse,csv,json,sqlite3
from pathlib import Path

def index(root):
 rows=[]
 for p in sorted(root.rglob('*.json')):
  if p.name.endswith(('_alerts.json','errors.json','normalized.json','rolling_windows.json')): continue
  try: x=json.loads(p.read_text())
  except Exception: continue
  if not isinstance(x,dict) or 'plans' not in x: continue
  rows.append({'file':str(p),'generated_at':x.get('generated_at',''),'session':x.get('session',''),'market_score':x.get('market_score',''),'plans':len(x.get('plans',[])),'active_plans':sum(i.get('action')=='条件买入' for i in x.get('plans',[])),'alerts':len(x.get('alerts',[]))})
 return rows

def db_summary(db):
 c=sqlite3.connect(db)
 try:
  sectors=c.execute("select sector,count(*),avg(realized_return_pct),sum(realized_return_pct>0) from plan_performance where action='条件买入' and realized_return_pct is not null group by sector order by avg(realized_return_pct) desc").fetchall()
 except sqlite3.OperationalError:
  sectors=[]
 stocks=c.execute("select symbol,count(*),avg(realized_return_pct),sum(realized_return_pct>0) from plan_performance where action='条件买入' and realized_return_pct is not null group by symbol order by avg(realized_return_pct) desc").fetchall()
 return sectors,stocks

if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--root',type=Path,required=True); ap.add_argument('--output',type=Path,default=Path('report_index.csv')); ap.add_argument('--db',type=Path); a=ap.parse_args(); rows=index(a.root)
 with a.output.open('w',newline='',encoding='utf-8') as f:
  w=csv.DictWriter(f,fieldnames=['file','generated_at','session','market_score','plans','active_plans','alerts']); w.writeheader(); w.writerows(rows)
 sectors,stocks=db_summary(a.db) if a.db and a.db.exists() else ([],[])
 lines=['# 报告索引','']+[f"- {r['generated_at']} {r['session']}：市场分 {r['market_score']}，候选 {r['plans']}，条件买入 {r['active_plans']}，告警 {r['alerts']}" for r in rows]
 lines += ['', '# 板块表现']+[f'- {s or "未分类"}：样本{n}，平均收益{avg:.3f}%，盈利{wins}' for s,n,avg,wins in sectors]
 lines += ['', '# 个股表现']+[f'- {s}：样本{n}，平均收益{avg:.3f}%，盈利{wins}' for s,n,avg,wins in stocks]
 a.output.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8'); print(f'indexed {len(rows)} reports')
