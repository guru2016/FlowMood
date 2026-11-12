"""Validate and normalize externally exported stock snapshots."""
from __future__ import annotations
import argparse,csv,json,math
from pathlib import Path

ALIASES={'代码':'symbol','证券代码':'symbol','股票代码':'symbol','名称':'name','股票名称':'name','最新价':'price','现价':'price','昨收':'prev_close','昨收价':'prev_close','最高':'high','最高价':'high','最低':'low','最低价':'low','成交额':'amount','20日均成交额':'avg_amount20','换手率':'turnover_pct','涨跌幅':'change_pct','资金流强度':'money_flow_pct','板块':'sector'}

def apply_aliases(row):
 return {ALIASES.get(str(k).strip(),str(k).strip()):v for k,v in row.items()}

REQUIRED=['symbol','name','price','prev_close','high','low']
NUMERIC=['price','prev_close','high','low','amount','avg_amount20','money_flow_pct','sector_score','sentiment_score','turnover_pct','atr14','change_pct']

def num(v):
 try:
  s=str(v).strip().replace(',','').replace('%','')
  mult=1
  if s.endswith('亿'): mult=100000000; s=s[:-1]
  elif s.endswith('万'): mult=10000; s=s[:-1]
  return float(s)*mult
 except (TypeError,ValueError): return None

def load(path):
 if path.suffix.lower()=='.csv':
  with path.open(newline='',encoding='utf-8-sig') as f: return list(csv.DictReader(f))
 return json.loads(path.read_text(encoding='utf-8'))

def normalize(rows):
 out=[]; seen=set(); errors=[]
 for i,row in enumerate(rows):
  row=apply_aliases(row)
  missing=[k for k in REQUIRED if not str(row.get(k,'')).strip()]
  if missing: errors.append({'row':i,'missing':missing}); continue
  symbol=str(row['symbol']).strip().upper()
  if symbol in seen: continue
  x=dict(row); x['symbol']=symbol; seen.add(symbol)
  for k in NUMERIC:
   if k in x and str(x[k]).strip()!='':
    x[k]=num(x[k])
    if x[k] is None: errors.append({'row':i,'field':k,'error':'not_numeric'})
  if not x.get('price') or x['price']<=0 or x.get('high',0)<x.get('low',0):
   errors.append({'row':i,'error':'invalid_price_range'}); continue
  if x.get('prev_close') and (x['high'] < x['prev_close']*0.5 or x['low'] > x['prev_close']*1.5):
   errors.append({'row':i,'error':'extreme_price_vs_prev_close'}); continue
  if x.get('change_pct') is not None:
   implied=(x['price']/x['prev_close']-1)*100 if x.get('prev_close') else 0
   if abs(implied-x['change_pct'])>3:
    errors.append({'row':i,'error':'change_pct_mismatch','implied':round(implied,2),'reported':x['change_pct']}); continue
  if not x.get('amount_ratio'):
   amount=x.get('amount'); avg=x.get('avg_amount20')
   x['amount_ratio']=amount/avg if amount and avg else 1.0
  out.append(x)
 return out,errors

if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--input',type=Path,required=True); ap.add_argument('--output',type=Path,required=True); ap.add_argument('--errors',type=Path,default=Path('snapshot_errors.json')); a=ap.parse_args()
 out,errors=normalize(load(a.input)); a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)); a.errors.write_text(json.dumps(errors,ensure_ascii=False,indent=2)); print(json.dumps({'accepted':len(out),'errors':len(errors)},ensure_ascii=False))
