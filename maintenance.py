"""Maintenance utility for raw market artifacts."""
from __future__ import annotations
import argparse,time,sqlite3
from pathlib import Path

def cleanup_raw(root: Path, days: int, dry: bool=False):
 cutoff=time.time()-days*86400; removed=[]
 for p in root.rglob('*.json'):
  if 'raw' in p.parts and p.stat().st_mtime < cutoff:
   removed.append(str(p));
   if not dry: p.unlink()
 return removed

def vacuum(db: Path):
 c=sqlite3.connect(db); c.execute('vacuum'); c.close()
if __name__=='__main__':
 ap=argparse.ArgumentParser(); ap.add_argument('--root',type=Path,required=True); ap.add_argument('--days',type=int,default=30); ap.add_argument('--db',type=Path); ap.add_argument('--dry-run',action='store_true'); a=ap.parse_args(); r=cleanup_raw(a.root,a.days,a.dry_run); 
 if a.db and a.db.exists() and not a.dry_run: vacuum(a.db)
 print({'removed':len(r),'dry_run':a.dry_run})
