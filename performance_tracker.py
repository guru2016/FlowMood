"""Track stock plans and realized next-day returns."""
from __future__ import annotations
import argparse,csv,json,sqlite3

def schema(c):
 c.execute('''CREATE TABLE IF NOT EXISTS plan_performance (generated_at TEXT,session TEXT,symbol TEXT,sector TEXT,action TEXT,score REAL,buy_price TEXT,target_price TEXT,stop_price REAL,realized_return_pct REAL,updated_at TEXT)''')
 cols={r[1] for r in c.execute('pragma table_info(plan_performance)')}
 if 'sector' not in cols: c.execute('alter table plan_performance add column sector TEXT')
def import_plans(db,path):
 c=sqlite3.connect(db); schema(c); x=json.load(open(path)); plans=x if isinstance(x,list) else x.get('plans',[]); session=x.get('session','') if isinstance(x,dict) else ''
 for p in plans: c.execute('insert into plan_performance(generated_at,session,symbol,sector,action,score,buy_price,target_price,stop_price) values(?,?,?,?,?,?,?,?,?)',((x.get('generated_at','') if isinstance(x,dict) else ''),session,p.get('symbol'),p.get('sector','未分类'),p.get('action'),p.get('score'),p.get('buy_price'),p.get('target_price'),p.get('stop_price')))
 c.commit(); c.close()
def update_returns(db,path):
 c=sqlite3.connect(db); schema(c)
 with open(path,newline='',encoding='utf-8-sig') as f:
  for r in csv.DictReader(f): c.execute("update plan_performance set realized_return_pct=?,updated_at=datetime('now') where symbol=? and realized_return_pct is null",(float(r['realized_return_pct']),r['symbol']))
 c.commit(); c.close()
def summary(db):
 c=sqlite3.connect(db); schema(c); vals=[r[0] for r in c.execute("select realized_return_pct from plan_performance where action='条件买入' and realized_return_pct is not null")]; return {'count':len(vals),'avg_return_pct':round(sum(vals)/len(vals),3) if vals else 0,'win_rate_pct':round(sum(v>0 for v in vals)/len(vals)*100,2) if vals else 0}
def grouped_summary(db):
 c=sqlite3.connect(db); schema(c); rows=c.execute("select session,sector,score,realized_return_pct from plan_performance where action='条件买入' and realized_return_pct is not null").fetchall(); groups={}
 for session,sector,score,ret in rows: groups.setdefault((session,sector,'high' if score>=80 else 'mid' if score>=70 else 'low'),[]).append(ret)
 out=[]
 for (session,sector,bucket),vals in sorted(groups.items()):
  wins=[x for x in vals if x>0]; losses=[x for x in vals if x<0]; out.append({'session':session,'sector':sector,'score_bucket':bucket,'count':len(vals),'avg_return_pct':round(sum(vals)/len(vals),3),'win_rate_pct':round(len(wins)/len(vals)*100,2),'profit_factor':round(sum(wins)/abs(sum(losses)),3) if losses else None})
 return out
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--db',required=True); ap.add_argument('--plan-json'); ap.add_argument('--returns-csv'); a=ap.parse_args()
 if a.plan_json: import_plans(a.db,a.plan_json)
 if a.returns_csv: update_returns(a.db,a.returns_csv)
 print(json.dumps({'overall':summary(a.db),'groups':grouped_summary(a.db)},ensure_ascii=False))
