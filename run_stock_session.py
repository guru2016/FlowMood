"""Run one scheduled midday or close research session."""
from __future__ import annotations
import argparse
from pathlib import Path
from daily_runner import run

if __name__ == '__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--session',choices=['midday','close'],required=True); ap.add_argument('--stocks',type=Path,required=True); ap.add_argument('--proxy',default='http://127.0.0.1:7897'); ap.add_argument('--offline-dir',type=Path,default=Path('dynamic/h5_api')); ap.add_argument('--out',type=Path,default=Path('scheduled_output')); ap.add_argument('--db',type=Path,default=Path('scheduled.sqlite3')); a=ap.parse_args()
    print(run(a.out,a.db,a.proxy,a.stocks,a.session,a.offline_dir))
