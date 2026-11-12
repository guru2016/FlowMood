"""Persistent state for snapshot watcher."""
from __future__ import annotations
import json
from pathlib import Path

def load(path):
 try:
  data=json.loads(path.read_text())
  return data if isinstance(data,dict) else {'hashes':{},'runs':[]}
 except Exception: return {'hashes':{},'runs':[],'state_recovered':True}
def save(path,state): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(state,ensure_ascii=False,indent=2))
