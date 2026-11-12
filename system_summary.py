"""Build a compact system status summary."""
from __future__ import annotations
import argparse,json,sqlite3
from pathlib import Path
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--db',type=Path,required=True); ap.add_argument('--state',type=Path); ap.add_argument('--output',type=Path,default=Path('system_summary.md')); ap.add_argument('--report',type=Path); a=ap.parse_args(); c=sqlite3.connect(a.db)
 tables={}
 for t in ['ths_h5_chart','ths_h5_sentiment','stock_plan','plan_performance']:
  try: tables[t]=c.execute(f'select count(*) from {t}').fetchone()[0]
  except sqlite3.Error: tables[t]=0
 state=json.loads(a.state.read_text()) if a.state and a.state.exists() else {}; last=(state.get('runs') or [{}])[-1]; report={}
 if a.report and a.report.exists():
  try:
   loaded=json.loads(a.report.read_text()); report=loaded if isinstance(loaded,dict) else {'plans':loaded}
  except Exception: pass
 plans=report.get('plans',[]); alerts=report.get('alerts',[])
 text='# 系统运行摘要\n\n'+f"- 监控状态：{last.get('status','unknown')}\n- 最后运行：{last.get('time','')}\n- 处理耗时：{last.get('duration_sec','')} 秒\n- 市场分：{report.get('market_score','')}\n- 市场状态：{report.get('market_regime','')}\n- 仓位建议：{report.get('position','')}\n- 候选数量：{len(plans)}\n- 条件买入：{sum(p.get('action')=='条件买入' for p in plans)}\n- 告警数量：{len(alerts)}\n\n## 数据量\n\n"+'\n'.join(f'- {k}：{v}' for k,v in tables.items())+'\n'
 a.output.write_text(text,encoding='utf-8'); print(text)
