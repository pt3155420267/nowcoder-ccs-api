"""牛客 (ac.nowcoder.com) 比赛数据客户端:带全局限速、指数退避重试。"""

import random
import threading
import time

import requests

from .utils import log

BASE_URL = 'https://ac.nowcoder.com'

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')

# 接口允许的最大分页大小(超过会返回 code=1 "pageSize is too big")
PARTICIPANT_PAGE_SIZE = 100
STATUS_PAGE_SIZE = 50

RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 6


class NowcoderError(Exception):
    """牛客请求失败(非认证类)。"""


class NowcoderAuthError(Exception):
    """牛客认证失败(cookie 过期、无效或无权限)。"""


class RateLimiter(object):
    """全局最小请求间隔限速器(带随机抖动,线程安全)。"""

    def __init__(self, min_interval, jitter=0.2):
        self.min_interval = max(0.0, min_interval)
        self.jitter = jitter
        self._next_time = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self._next_time - now
            interval = self.min_interval * (1.0 + random.uniform(-self.jitter, self.jitter))
            self._next_time = max(now, self._next_time) + interval
        if delay > 0:
            time.sleep(delay)


def _new_session(headers, proxy):
    session = requests.Session()
    # 默认直连,忽略系统/环境代理设置(Windows 注册表代理常导致不可用连接);可用 --proxy 指定
    session.trust_env = False
    if proxy:
        session.proxies = {'http': proxy, 'https': proxy}
    session.headers.update(headers)
    return session


class NowcoderClient(object):
    """对牛客比赛后台接口的只读访问。所有请求(含外站图片)经过同一个限速器。"""

    def __init__(self, cookie, min_interval=1.0, timeout=30, proxy=None):
        base_headers = {'User-Agent': USER_AGENT}
        # 牛客会话:带 Cookie
        self.session = _new_session(dict(base_headers, **{
            'Accept': 'application/json, text/plain, */*',
            'Referer': BASE_URL + '/',
            'Cookie': cookie.strip(),
        }), proxy)
        # 外站(头像服务、牛客图片 CDN)不带 Cookie,避免把登录凭证发给第三方
        self.plain = _new_session(base_headers, proxy)
        self.limiter = RateLimiter(min_interval)
        self.timeout = timeout

    # ------------------------------------------------------------------ core

    def _send(self, session, url, params=None, allow_redirects=False):
        attempt = 0
        while True:
            self.limiter.wait()
            try:
                resp = session.get(url, params=params, timeout=self.timeout, allow_redirects=allow_redirects)
            except requests.RequestException as e:
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise NowcoderError('网络请求失败: %s' % e)
                delay = min(30, 2 ** attempt)
                log('请求网络异常(%s),第 %d 次重试,%.0fs 后重试', e, attempt, delay)
                time.sleep(delay)
                continue
            if resp.status_code in RETRYABLE_STATUSES:
                attempt += 1
                if attempt >= MAX_ATTEMPTS:
                    raise NowcoderError('持续返回 HTTP %d: %s' % (resp.status_code, url))
                retry_after = resp.headers.get('Retry-After')
                if retry_after and retry_after.strip().isdigit():
                    delay = min(120, float(retry_after))
                else:
                    delay = min(60, 2 ** attempt * 2)
                log('返回 HTTP %d,第 %d 次重试,%.0fs 后重试', resp.status_code, attempt, delay)
                time.sleep(delay)
                continue
            return resp

    def get_json(self, path, params):
        """牛客 JSON 接口,返回 data 字段。"""
        resp = self._send(self.session, BASE_URL + path, params)
        if resp.status_code in (301, 302, 303, 307, 308):
            if 'login' in (resp.headers.get('Location') or ''):
                raise NowcoderAuthError('Cookie 无效或已过期(被重定向到登录页),请重新从浏览器复制 Cookie')
            raise NowcoderError('牛客返回意外重定向 HTTP %d: %s' % (resp.status_code, path))
        if resp.status_code in (401, 403):
            raise NowcoderAuthError('牛客认证失败(HTTP %d),请检查 Cookie 与管理员权限' % resp.status_code)
        if resp.status_code != 200:
            raise NowcoderError('牛客请求失败 HTTP %d: %s' % (resp.status_code, path))
        try:
            body = resp.json()
        except ValueError:
            raise NowcoderAuthError('牛客返回非 JSON 响应,Cookie 可能无效(请检查是否已登录且有管理权限)')
        if body.get('code') != 0:
            raise NowcoderError('牛客接口错误 %s: code=%s msg=%s' % (path, body.get('code'), body.get('msg')))
        return body['data']

    def download(self, url):
        """下载外站文件(不带 Cookie)。返回 (字节内容, Content-Type)。"""
        resp = self._send(self.plain, url, allow_redirects=True)
        if resp.status_code != 200:
            raise NowcoderError('下载失败 HTTP %d: %s' % (resp.status_code, url))
        return resp.content, resp.headers.get('Content-Type', '')

    # -------------------------------------------------------------- endpoints

    def get_contest_info(self, contest_id):
        """比赛基本信息与题目列表(含封榜配置、时间)。返回 data。"""
        return self.get_json('/acm/contest/admin/contest/contest-info-with-problem', {
            'token': '',
            'contestId': contest_id,
        })

    def get_participants_page(self, contest_id, page):
        """参赛名单分页(scope=ALL 为全部报名人员)。"""
        return self.get_json('/acm/contest/admin/participants/list', {
            'token': '',
            'contestId': contest_id,
            'scope': 'ALL',
            'search': '',
            'page': page,
            'pageSize': PARTICIPANT_PAGE_SIZE,
            'refresh': 'false',
        })

    def get_all_participants(self, contest_id):
        """全部参赛名单。rows 中 subjectId 即选手 uid。"""
        rows = []
        page = 1
        while True:
            data = self.get_participants_page(contest_id, page)
            rows.extend(data.get('rows') or [])
            if page >= (data.get('pageCount') or 0) or not data.get('rows'):
                break
            page += 1
        return rows

    def get_rank_rows(self, contest_id):
        """实时排名(全部页,含学校)。limit=0 时按 page 翻页。"""
        rows = []
        page = 1
        while True:
            data = self.get_json('/acm-heavy/acm/contest/real-time-rank-data', {
                'token': '',
                'id': contest_id,
                'rankScope': 'ALL',
                'limit': 0,
                'page': page,
            })
            basic = data.get('basicInfo') or {}
            rows.extend(data.get('rankData') or [])
            if page >= (basic.get('pageCount') or 0) or not data.get('rankData'):
                break
            page += 1
        return rows

    def get_status_page(self, contest_id, page):
        """赛时提交列表的一页(按提交时间倒序,page=1 为最新)。返回 (basicInfo, rows)。"""
        data = self.get_json('/acm-heavy/acm/contest/status-list', {
            'token': '',
            'id': contest_id,
            'page': page,
            'pageSize': STATUS_PAGE_SIZE,
            'searchUserName': '',
        })
        return data.get('basicInfo') or {}, data.get('data') or []
