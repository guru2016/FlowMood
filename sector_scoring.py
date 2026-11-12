"""Aggregate stock snapshots into sector strength and enrich each stock."""
from __future__ import annotations
import argparse,json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def enrich(rows, strong_sector_min=70.0):
    groups=defaultdict(list)
    for r in rows: groups[str(r.get('sector','未分类'))].append(r)
    sector_scores={}
    for sector,items in groups.items():
        breadth=sum(float(x.get('change_pct',0) or 0)>0 for x in items)/len(items)
        money=mean(float(x.get('money_flow_pct',0) or 0) for x in items)
        amount=mean(float(x.get('amount_ratio',1) or 1) for x in items)
        limitups=sum(bool(x.get('limit_up')) for x in items)
        score=max(0,min(100, breadth*45 + max(-10,min(10,money))*2 + min(2,amount-1)*15 + min(3,limitups)*5 + 20))
        sector_scores[sector]=round(score,2)
    # 强势板块计数：stock_selector 的 min_strong_sectors 判据依赖它。
    # 此前该字段从未被赋值（全市场 0/5024），使「条件买入」在数学上永远不可达。
    strong_count=sum(1 for v in sector_scores.values() if v>=strong_sector_min)
    out=[]
    for r in rows:
        x=dict(r)
        x['sector_score']=sector_scores[str(r.get('sector','未分类'))]
        x['strong_sector_count']=strong_count
        # 独立确认：信号若全部来自行情数据而非消息面，即满足利弗莫尔「不凭消息交易」。
        # 用 setdefault 保留调用方已有的值，不覆盖上游判定。
        x.setdefault('independent_confirmation', str(x.get('signal_source','market_data'))=='market_data')
        out.append(x)
    return out,sector_scores

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--input',required=True); ap.add_argument('--output',required=True); ap.add_argument('--sectors-output',default='sector_scores.json'); a=ap.parse_args()
    rows=json.loads(Path(a.input).read_text()); out,scores=enrich(rows); Path(a.output).write_text(json.dumps(out,ensure_ascii=False,indent=2)); Path(a.sectors_output).write_text(json.dumps(scores,ensure_ascii=False,indent=2)); print(json.dumps(scores,ensure_ascii=False,indent=2))
