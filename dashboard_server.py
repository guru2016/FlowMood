#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys, threading, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from selection_time import annotate
from metric_display import normalize_report

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
        # _adhoc 是定稿窗口外的补跑归档（盘后/部署验证），数据残缺，不能当最新产出
        if '_adhoc' in path.parts:
            continue
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
        out[session] = normalize_report({
            'session': session,
            'generated_at': report.get('generated_at'),
            'market_score': report.get('market_score'),
            'market_regime': report.get('market_regime'),
            'position': report.get('position'),
            'max_exposure': report.get('max_exposure'),
            'plans': report.get('plans') or [],
            'metrics': report.get('metrics') or {},
            'markdown': md,
            'path': str(path.relative_to(ROOT)),
            'meta': meta,
        })
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
    # 定稿榜单里的资金流本来就是采集那一刻的值，不是盘中实时资金流。
    # 旧文件没有这些标注字段，读的时候补上，前端才不会把定稿说成实时。
    data.setdefault('flow_source', 'defined')
    data.setdefault('ranking_at', data.get('generated_at', ''))
    data.setdefault('ranking_basis', 'defined_snapshot')
    data['ranking_live'] = False
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
            res['ranking_live'] = True   # 真·七维评分重排，区别于静态页只换价格
            res['live_reason'] = why
            res['quote_at'] = datetime.fromtimestamp(quote_at).isoformat(timespec='seconds')
            res['quote_age_sec'] = int(age)
            res['coverage'] = round(coverage, 3)
            # 不能看 LAST_SOURCE 全局态：它是上一次 fetch 留下的，会拿上一轮的
            # 「同花顺兜底」给这一轮东财完整行情挂上「资金流仍为快照值」的假警告。
            # 只认本次 quotes 里到底有没有资金流字段。
            probe = list(quotes.values())[:200]
            has_flow = any(('f184' in q or 'f62' in q) for q in probe)
            flow_defined = any(q.get('_flow_from') == 'defined' for q in probe)
            if flow_defined:
                warning = '价格/涨跌/成交额实时，主力资金流与行业沿用定稿值'
            elif not has_flow:
                warning = '行情源不带主力资金流，资金流/行业沿用定稿快照值'
            elif coverage < 0.8:
                warning = f'行情覆盖仅 {coverage:.0%}，其余仍为快照价格'
            else:
                warning = f'行情滞后 {int(age)} 秒' if age > 30 else ''
            res['live_warning'] = warning
            res['quote_source'] = lq.LAST_SOURCE or ('eastmoney_clist' if has_flow else 'unknown')
            _live_cache['data'] = res
            _live_cache['at'] = time.time()
            data = dict(res)

    rows = data.get('ranking') or []
    data['ranking'] = annotate(rows[:limit], scope='rank', keep=100)
    data['returned'] = len(data['ranking'])
    data['available'] = len(rows)
    return data


_METRIC_CACHE: dict = {'at': 0.0, 'data': None}
METRIC_TTL = 120.0


def metrics_live(ttl: float = METRIC_TTL) -> dict:
    """现拉核心指标，而不是读报告里那份生成时就冻住的值。

    这些指标（大盘资金、两融余额、市场温度…）是**日频**数据，同花顺在收盘后
    才补当天的点。旧做法是把指标跟着报告一起序列化，报告 15:10 生成后页面就一直
    停在那一刻，第二天看还是前一天、甚至更早的日期。这里每次按 TTL 去上游取最新，
    页面照实显示数据的真实日期，不假装它是盘中实时值。
    """
    now = time.time()
    cached = _METRIC_CACHE.get('data')
    if cached and now - float(_METRIC_CACHE.get('at') or 0) < ttl:
        out = dict(cached)
        out['cached'] = True
        return out

    try:
        from daily_runner import BASE, METRICS, fetch_json, latest_chart
        from metric_display import metrics_from_raw
    except Exception as exc:  # noqa: BLE001
        return {'metrics': [], 'error': f'指标模块加载失败：{exc}'}

    raw: dict[str, dict] = {}
    errors: list[str] = []
    for key in METRICS:
        try:
            raw[key] = latest_chart(fetch_json(f'{BASE}{key}.json'))
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{key}: {exc}')
    if not raw:
        return {'metrics': [], 'error': '指标接口全部不可达', 'errors': errors[:3]}

    data = {
        'metrics': metrics_from_raw(raw),
        'fetched_at': datetime.now().isoformat(timespec='seconds'),
        'source': '同花顺 dapan_v3',
        'count': len(raw),
        'errors': errors[:3],
        'cached': False,
    }
    # 有新数据才覆盖缓存，避免一次网络抖动把好的那份冲掉
    _METRIC_CACHE['at'] = now
    _METRIC_CACHE['data'] = data
    return data


def extra_quotes(raw: str) -> dict:
    """报告里的标的不一定在 Top100 榜单里，个股详情弹窗仍要能显示实时价。

    只从已缓存的全市场行情里查，不额外发起上游请求；查不到就留空由前端标注。
    """
    syms = [s.strip() for s in (raw or '').split(',') if s.strip()][:60]
    if not syms:
        return {}
    try:
        import live_quotes as lq
        quotes, _ = lq.fetch_quotes_cached(ttl=30.0)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for sym in syms:
        q = quotes.get(sym.split('.')[0])
        if not q:
            continue
        out[sym] = {'price': q.get('f2'), 'change_pct': q.get('f3'),
                    'money_flow_pct': q.get('f184'), 'turnover_pct': q.get('f8'),
                    'sector': q.get('f100')}
    return out


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, code=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/api/status': self.send_json(status()); return
        if path == '/api/reports': self.send_json(reports()); return
        if path == '/api/metrics':
            qs = parse_qs(parsed.query)
            try:
                ttl = max(0.0, min(float(qs.get('ttl', [str(METRIC_TTL)])[0]), 1800.0))
            except (TypeError, ValueError):
                ttl = METRIC_TTL
            self.send_json(metrics_live(ttl)); return
        if path == '/api/ranking':
            qs = parse_qs(parsed.query)
            try:
                limit = max(1, min(int(qs.get('limit',['30'])[0]), 100))
            except ValueError:
                limit = 30
            live = qs.get('live',['0'])[0] in ('1','true','yes')
            data = live_ranking(limit) if live else ranking(limit)
            data['extra_quotes'] = extra_quotes(qs.get('extra',[''])[0])
            self.send_json(data); return
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
