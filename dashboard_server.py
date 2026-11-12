#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selection_time import annotate

# A 股所有交易时段判断都基于系统本地时间。VPS 常为 UTC，这里强制对齐北京时区，
# 否则「早盘/午盘/已收盘」判断和量比的时间折算会整体错位。
os.environ["TZ"] = os.environ.get("THS_TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    time.tzset()

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'frontend_output'
if not OUT.exists():          # 某些沙箱会把 exist_ok=True 也判成冲突，先探一次
    OUT.mkdir(parents=True, exist_ok=True)


def latest_json(pattern):
    """只在产出目录里找，别递归整个项目——apktool/ 下有 7 万个文件，全量 glob 会让每次刷新卡住。"""
    bases = [ROOT / 'auto_output', ROOT / 'frontend_output']
    hits = []
    for base in bases:
        if base.exists():
            hits.extend(base.rglob(pattern))
    for path in sorted(hits, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            return json.loads(path.read_text(encoding='utf-8')), str(path.relative_to(ROOT))
        except Exception:
            pass
    return {}, ''


def status():
    report, report_path = latest_json('**/stock_report_*.json')
    if not report:
        report, report_path = latest_json('**/report_*.json')
    summary, summary_path = latest_json('**/system_summary.json')
    alerts, alerts_path = latest_json('**/*_alerts.json')
    # 自动流水线最近一次运行摘要（晨盘/收盘各一份）
    auto = {}
    for session in ('midday', 'close'):
        path = ROOT / 'auto_output' / f'latest_{session}.json'
        if path.exists():
            try:
                auto[session] = json.loads(path.read_text(encoding='utf-8'))
            except Exception:
                pass
    plans = report.get('plans')
    if isinstance(plans, list):
        annotate(plans, scope='plans', keep=len(plans))
    return {'report': report, 'report_path': report_path, 'summary': summary,
            'summary_path': summary_path, 'alerts': alerts, 'alerts_path': alerts_path,
            'auto': auto}


def reports():
    """午盘 / 收盘各取最新一份研究报告，供看板报告区切换展示。

    与 /api/status 只给「最近一份」不同，这里固定返回两个时段，
    这样前端和 GitHub Pages 静态页能拿到完全一样的数据结构。
    """
    out = {}
    for session in ('midday', 'close'):
        base = ROOT / 'auto_output' / session
        if not base.exists():
            continue
        cands = sorted(base.glob('*/stock_report_*.json'),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        if not cands:
            continue
        path = cands[0]
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            continue
        md = ''
        md_files = sorted(path.parent.glob('report_*.md'),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        if md_files:
            try:
                md = md_files[0].read_text(encoding='utf-8')
            except Exception:  # noqa: BLE001
                pass
        meta = {}
        latest = ROOT / 'auto_output' / f'latest_{session}.json'
        if latest.exists():
            try:
                m = json.loads(latest.read_text(encoding='utf-8'))
                meta = {'finished_at': m.get('finished_at'), 'elapsed_sec': m.get('elapsed_sec'),
                        'all_ok': m.get('all_ok'),
                        'steps': [{'step': s.get('step'), 'ok': s.get('ok')}
                                  for s in (m.get('steps') or [])]}
            except Exception:  # noqa: BLE001
                pass
        out[session] = {
            'session': session,
            'generated_at': report.get('generated_at'),
            'market_score': report.get('market_score'),
            'market_regime': report.get('market_regime'),
            'position': report.get('position'),
            'max_exposure': report.get('max_exposure'),
            'plans': report.get('plans') or [],
            'markdown': md,
            'path': str(path.relative_to(ROOT)),
            'meta': meta,
        }
    return {'reports': out}


def ranking(limit=30):
    """读流水线写下的排名快照。文件本身只含头部，这里再按请求截断。"""
    path = ROOT / 'auto_output' / 'latest_ranking.json'
    if not path.exists():
        return {'error': '尚无排名数据，先跑一次流水线', 'ranking': [], 'stats': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        return {'error': f'排名文件读取失败：{exc}', 'ranking': [], 'stats': {}}
    rows = data.get('ranking') or []
    data['ranking'] = annotate(rows[:limit], scope='rank', keep=100)
    data['returned'] = len(data['ranking'])
    data['available'] = len(rows)
    return data


_live_lock = threading.Lock()
_live_cache = {"at": 0.0, "data": None}
LIVE_TTL = 10.0          # 秒；实时重算约 1.3s，挡住并发重复计算


def live_ranking(limit=30):
    """盘中实时排名：拉最新行情覆盖快照后重算评分，而不是只换数字不换结论。

    重算约 1.3 秒（含拉全市场行情 1.0s），因此加 10 秒缓存 + 串行锁，
    避免多个标签页同时轮询把上游打爆。
    """
    import live_quotes as lq
    from auto_rank import build_ranking
    fetch_quotes_cached, is_trading_now = lq.fetch_quotes_cached, lq.is_trading_now
    QUOTE_MAX_AGE = lq.QUOTE_MAX_AGE

    trading, why = is_trading_now()
    if not trading:
        data = ranking(limit)
        data['live'] = False
        data['live_reason'] = why
        return data

    # 必须先验证行情本身是不是新鲜、完整。东财间歇性限流时 fetch_quotes 会返回
    # 空/半份数据，旧实现只要「在交易时段」就无条件标 live=True，结果一半股票
    # 用昨天的价格却显示「● 实时」——这是误导，比不刷新更糟。
    quotes, quote_at = fetch_quotes_cached()
    age = (time.time() - quote_at) if quote_at else None
    if not quotes or age is None or age > QUOTE_MAX_AGE:
        data = ranking(limit)
        data['live'] = False
        data['live_reason'] = ('行情源暂不可用，已回退快照' if not quotes
                               else f'行情已滞后 {int(age)} 秒，已回退快照')
        data['quote_age_sec'] = int(age) if age is not None else None
        return data

    with _live_lock:
        if _live_cache['data'] and (time.time() - _live_cache['at']) < LIVE_TTL:
            data = dict(_live_cache['data'])
        else:
            base = ranking(1)
            res = build_ranking(quotes=quotes, market_score=base.get('market_score'),
                                top=100, write=False)
            merge = res.get('live_merge') or {}
            hit = merge.get('hit', 0)
            total = merge.get('total', 0) or 1
            coverage = hit / total
            res['live'] = True
            res['live_reason'] = why
            res['quote_at'] = datetime.fromtimestamp(quote_at).isoformat(timespec='seconds')
            res['quote_age_sec'] = int(age)
            res['coverage'] = round(coverage, 3)
            if lq.LAST_SOURCE == 'ths_fallback':
                warning = '东财列表不可用，价格来自同花顺（分钟级），资金流仍为快照值'
            elif coverage < 0.8:
                warning = f'行情覆盖仅 {coverage:.0%}，其余仍为快照价格'
            else:
                warning = f'行情滞后 {int(age)} 秒' if age > 30 else ''
            res['live_warning'] = warning
            res['quote_source'] = lq.LAST_SOURCE
            _live_cache['data'] = res
            _live_cache['at'] = time.time()
            data = dict(res)

    rows = data.get('ranking') or []
    data['ranking'] = annotate(rows[:limit], scope='rank', keep=100)
    data['returned'] = len(data['ranking'])
    data['available'] = len(rows)
    return data


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/status': self.send_json(status()); return
        if path == '/api/reports': self.send_json(reports()); return
        if path == '/api/ranking':
            qs = parse_qs(parsed.query)
            try:
                limit = max(1, min(int(qs.get('limit',['30'])[0]), 100))
            except ValueError:
                limit = 30
            live = qs.get('live',['0'])[0] in ('1','true','yes')
            self.send_json(live_ranking(limit) if live else ranking(limit)); return
        if path == '/':
            body = (ROOT / 'web' / 'dashboard.html').read_bytes()
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith('/files/'):
            target = (ROOT / path.removeprefix('/files/')).resolve()
            if ROOT not in target.parents or not target.is_file(): self.send_json({'error':'file not found'},404); return
            self.send_response(200); self.send_header('Content-Type','text/plain; charset=utf-8'); data=target.read_bytes(); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data); return
        self.send_json({'error':'not found'},404)

    def do_POST(self):
        if urlparse(self.path).path != '/api/run': self.send_json({'error':'not found'},404); return
        length = int(self.headers.get('Content-Length','0')); data=parse_qs(self.rfile.read(length).decode())
        session = data.get('session',['close'])[0]
        if session not in ('midday','close'): self.send_json({'error':'invalid session'},400); return
        # 走 auto_pipeline：自动全市场采集 + 实时市场分。
        # 原来的 run_all.py 读的是 static 离线样本，市场分会失真，这里不再使用。
        cmd=[sys.executable,'auto_pipeline.py','--session',session,'--workers','32']
        try:
            p=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True,timeout=600)
            self.send_json({'ok':p.returncode==0,'session':session,'stdout':p.stdout[-4000:],'stderr':p.stderr[-4000:],'status':status()})
        except Exception as exc: self.send_json({'ok':False,'error':str(exc)},500)

    def log_message(self, *_): pass


if __name__ == '__main__':
    host = os.environ.get('THS_HOST', '127.0.0.1')
    port = int(os.environ.get('THS_PORT', '8787'))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f'Dashboard: http://{host}:{port}', flush=True)
    server.serve_forever()
