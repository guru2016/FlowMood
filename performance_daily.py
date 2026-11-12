"""Build daily performance summary from plan_performance SQLite table."""
from __future__ import annotations
import argparse,csv,sqlite3
from pathlib import Path

def build(db):
 c=sqlite3.connect(db)
 rows=c.execute("select substr(generated_at,1,10) day,session,count(*),sum(action='条件买入'),avg(score),avg(realized_return_pct),sum(realized_return_pct>0) from plan_performance group by day,session order by day,session").fetchall()
 return [{'date':r[0],'session':r[1],'plans':r[2],'active_plans':r[3],'avg_score':round(r[4] or 0,2),'avg_return_pct':round(r[5],3) if r[5] is not None else None,'wins':r[6]} for r in rows]

def write(rows,path):
 with path.open('w',newline='',encoding='utf-8') as f:
  w=csv.DictWriter(f,fieldnames=['date','session','plans','active_plans','avg_score','avg_return_pct','wins']); w.writeheader(); w.writerows(rows)
 md=path.with_suffix('.md'); lines=['# 个股表现日报','']
 for r in rows: lines.append(f"- {r['date']} {r['session']}：候选{r['plans']}，条件买入{r['active_plans']}，平均收益{r['avg_return_pct'] if r['avg_return_pct'] is not None else '待回填'}%，胜笔数{r['wins']}")
 md.write_text('\n'.join(lines)+'\n',encoding='utf-8')
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--db',required=True); ap.add_argument('--output',default='performance_daily.csv'); a=ap.parse_args(); rows=build(a.db); write(rows,Path(a.output)); print(rows)
