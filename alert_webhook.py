"""Send research alerts to a configured generic webhook."""
from __future__ import annotations
import json,urllib.request

def send(webhook: str, title: str, alerts: list[str], timeout: int = 10) -> int:
    payload=json.dumps({'title':title,'alerts':alerts},ensure_ascii=False).encode()
    req=urllib.request.Request(webhook,data=payload,headers={'Content-Type':'application/json','User-Agent':'ths-research/1.0'},method='POST')
    with urllib.request.urlopen(req,timeout=timeout) as r: return int(r.status)

if __name__=='__main__':
 import argparse
 ap=argparse.ArgumentParser(); ap.add_argument('--webhook',required=True); ap.add_argument('--alerts',required=True); a=ap.parse_args()
 data=json.load(open(a.alerts)); print(send(a.webhook,'同花顺研究告警',data.get('alerts',[])))
