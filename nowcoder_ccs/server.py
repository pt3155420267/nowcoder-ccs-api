"""ICPC Contest API 2023-06 HTTP 服务。"""

import base64
import hmac
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__
from .assets import MIME_TYPES
from .utils import log

API_INFO = {
    'version': '2023-06',
    'version_url': 'https://ccs-specs.icpc.io/2023-06/contest_api',
    'name': 'Nowcoder Contest API',
    'provider': {
        'name': 'nowcoder-css-api',
        'version': __version__,
    },
}

# event-feed 流式模式的轮询间隔(秒);有新事件时立即排空,不受此间隔限制
EVENT_FEED_POLL_INTERVAL = 3.0
EVENT_FEED_HEARTBEAT_INTERVAL = 10.0

COLLECTIONS = ('languages', 'problems', 'teams', 'organizations', 'groups',
               'judgement-types', 'submissions', 'judgements', 'runs')

ROUTE_RE = re.compile(r'^/api/contests/([^/]+)(?:/([^/]+)(?:/([^/]+)(?:/([^/]+))?)?)?$')

NAMES = {
    'languages': 'Language', 'problems': 'Problem', 'teams': 'Team',
    'organizations': 'Organization', 'groups': 'Group',
    'judgement-types': 'Judgement type', 'submissions': 'Submission',
    'judgements': 'Judgement', 'runs': 'Run',
}


def _num(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def _list(store, collection):
    """各集合的输出顺序。"""
    if collection == 'problems':
        return sorted(store.problems.values(), key=lambda p: p['ordinal'])
    if collection == 'teams':
        return sorted(store.teams.values(), key=lambda t: _num(t['id']))
    if collection == 'submissions':
        return sorted(store.submissions.values(), key=lambda s: _num(s['id']))
    if collection == 'judgements':
        return sorted(store.judgements.values(), key=lambda j: _num(j['submission_id']))
    if collection == 'runs':
        return sorted(store.runs.values(), key=lambda r: (_num(r['judgement_id'][2:]), r['ordinal']))
    return list(getattr(store, collection.replace('-', '_')).values())


def make_server(host, port, store, assets, auth_user, auth_password, verbose=False):
    handler = CCSRequestHandler
    handler.store = store
    handler.assets = assets
    handler.auth_user = auth_user
    handler.auth_password = auth_password
    handler.verbose = verbose
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


class CCSRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'nowcoder-css-api/%s' % __version__
    store = None
    assets = None
    auth_user = ''
    auth_password = ''
    verbose = False

    # ------------------------------------------------------------- 基础设施

    def log_message(self, fmt, *args):
        if self.verbose:
            log('HTTP %s', fmt % args)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self, message):
        self._json({'message': message}, status=404)

    def _unauthorized(self):
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="nowcoder-css-api"')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        body = b'{"message":"Unauthorized"}'
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self, query):
        """支持 HTTP Basic 认证与 ?auth=<base64(user:pass)> 查询参数两种方式。"""
        expected = base64.b64encode(('%s:%s' % (self.auth_user, self.auth_password))
                                    .encode('utf-8')).decode('ascii')
        header = self.headers.get('Authorization') or ''
        if header.startswith('Basic'):
            supplied = header[len('Basic'):].strip()
            if hmac.compare_digest(supplied, expected):
                return True
        supplied_query = unquote(query.get('auth', [''])[0])
        if supplied_query and hmac.compare_digest(supplied_query, expected):
            return True
        return False

    # ------------------------------------------------------------- 路由分发

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _route(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = unquote(parsed.path).rstrip('/') or '/'

        if path == '/api':
            self._json(API_INFO)
            return

        contest_id, collection, item_id, extra = None, None, None, None
        if path != '/api/contests':
            match = ROUTE_RE.match(path)
            if not match:
                self._not_found('Not found')
                return
            contest_id, collection, item_id, extra = match.groups()

        if not self._check_auth(query):
            self._unauthorized()
            return

        store = self.store
        if contest_id is None:
            # /api/contests 比赛列表(单实例仅服务一个比赛)
            self._json([store.contest] if store.contest else [])
            return
        if contest_id != store.contest_id or store.contest is None:
            self._not_found('Contest not found')
            return

        if collection is None:
            self._json(store.contest)
        elif collection == 'state':
            self._json(store.state)
        elif collection == 'event-feed':
            self._event_feed(query)
        elif collection == 'organizations' and item_id and extra and extra.lower().startswith('logo'):
            self._serve_image(('organizations', item_id), 'logo')
        elif collection == 'teams' and item_id and extra and extra.lower().startswith('photo'):
            self._serve_image(('teams', item_id), 'photo')
        elif collection in COLLECTIONS:
            self._collection(collection, item_id)
        elif collection.lower().startswith('logo'):
            # 比赛图标: /api/contests/<id>/logo.png
            self._serve_image(('contest',), 'logo')
        else:
            self._not_found('Not found')

    def _serve_image(self, segments, name):
        """提供图片(本地 CDP 或远程预取),ICPC Tools 按 baseUrl + href 拼接下载。"""
        found = self.assets.find(segments, name)
        if found is None:
            self._not_found('Image not found')
            return
        filename, path = found
        try:
            with open(path, 'rb') as fp:
                body = fp.read()
        except OSError:
            self._not_found('Image not readable')
            return
        ext = filename.rsplit('.', 1)[1].lower()
        self.send_response(200)
        self.send_header('Content-Type', MIME_TYPES.get('.' + ext, 'application/octet-stream'))
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(body)

    def _collection(self, collection, item_id):
        store = self.store
        with store.lock:
            if item_id is None:
                items = _list(store, collection)
            else:
                items = getattr(store, collection.replace('-', '_')).get(item_id)
        if item_id is not None and items is None:
            self._not_found('%s not found' % NAMES[collection])
            return
        self._json(items)

    # ------------------------------------------------------------- 事件流

    def _event_feed(self, query):
        stream = query.get('stream', ['true'])[0].lower() not in ('false', '0', 'no')
        token = query.get('since_token', [None])[0]

        backlog = self.store.events_since(token)
        if backlog is None:
            # ICPC Tools 系工具把 400 视为"比赛已重置",会丢弃本地缓存后不带 token 全量重连;
            # 若返回 404,它们会带着同一个 token 无限重试
            self._json({'message': 'Unknown since_token'}, status=400)
            return

        if not stream:
            # 单次模式:输出全部待发事件(逐行 JSON)后关闭连接
            payload = ''.join(json.dumps(ev, ensure_ascii=False) + '\n' for ev in backlog)
            body = payload.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # 流式模式:NDJSON,每行一个事件(裸 JSON,不是 SSE),空行为心跳(客户端解析时跳过)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            start = self.store.index_of(token) + 1 if token else 0
            last_heartbeat = time.monotonic()
            while True:
                with self.store.lock:
                    chunk = self.store.events[start:]
                for event in chunk:
                    self.wfile.write((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8'))
                    start += 1
                self.wfile.flush()
                now = time.monotonic()
                if now - last_heartbeat >= EVENT_FEED_HEARTBEAT_INTERVAL:
                    self.wfile.write(b'\n')
                    self.wfile.flush()
                    last_heartbeat = now
                # 空转时按 EVENT_FEED_POLL_INTERVAL 轮询;刚发过数据则立即继续检查
                time.sleep(EVENT_FEED_POLL_INTERVAL if not chunk else 0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
            if self.verbose:
                log('event-feed 客户端断开连接')
