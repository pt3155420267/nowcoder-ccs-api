"""数据同步:比赛信息/名单/排名定期刷新 + 赛时提交增量轮询 + 图片预取,把牛客数据转换为 CCS 事件写入 store。

同步依赖 store.upsert 的幂等性:每次都由当前数据重新构建实体,只有与已发出的内容不同才追加事件。
图片预取在独立线程中进行,完成一张图后只重新发出受影响的实体(队伍/学校/比赛)。
"""

import threading
import time
from urllib.parse import quote

from .adapter import (GROUPS, JUDGEMENT_TYPES, UNKNOWN_SCHOOL, freeze_from_setting,
                      judgement_type_id, organization_id, parse_time_ms, to_contest, to_group,
                      to_judgement, to_language, to_organization, to_problem, to_state,
                      to_submission, to_team)
from .nowcoder import NowcoderAuthError, NowcoderError
from .utils import log


def now_ms():
    return int(time.time() * 1000)


class Syncer(object):
    def __init__(self, client, store, opts, assets, palette=None):
        self.client = client
        self.store = store
        self.opts = opts
        self.assets = assets
        self.palette = palette or {}
        self.lock = threading.RLock()
        self.meta = None            # {'name', 'begin', 'end'},时间为毫秒
        self.freeze = {'frozen_ms': None, 'end_frozen_ms': 0}
        self.problems = []          # 按题号排序的 problemList
        self.teams = {}             # uid(str) -> {'name', 'school', 'school_id', 'official'}
        self.rank_school = {}       # uid(int) -> (学校名, 学校 ID)
        self.static_at = None       # 上次刷新静态数据的单调时钟
        self._warned_messages = set()

    # ------------------------------------------------------------- 静态数据

    def sync_static(self):
        """比赛信息(含封榜)、报名名单、排名(学校)。低频刷新。"""
        cid = self.store.contest_id
        info = self.client.get_contest_info(cid)
        contest = info['contestInfo']
        setting = contest.get('settingInfoJson') or {}
        begin, end = contest.get('startTime'), contest.get('endTime')
        if not begin or not end:
            raise NowcoderError('牛客未返回比赛时间,请确认比赛 ID 与 Cookie 权限')

        participants = self.client.get_all_participants(cid)
        rank_rows = self.client.get_rank_rows(cid)
        rank_school = {}
        for row in rank_rows:
            rank_school[row['uid']] = (row.get('school') or None, row.get('schoolId') or row.get('school_id'))

        with self.lock:
            self.meta = {
                'name': contest.get('name') or str(cid),
                'begin': begin,
                'end': end,
            }
            self.freeze = freeze_from_setting(setting)
            self.problems = sorted(info.get('problemList') or [], key=lambda p: p['virtualId'])
            self.rank_school = rank_school
            for row in participants:
                school, school_id = rank_school.get(row['subjectId'], (None, None))
                self.teams[str(row['subjectId'])] = {
                    'name': row['name'],
                    'school': school or UNKNOWN_SCHOOL,
                    'school_id': school_id,
                    'official': True,
                }
            self.static_at = time.monotonic()

        self.emit_static()
        log('比赛: %s,%d 道题,%d 名报名选手,封榜 %s',
            self.meta['name'], len(self.problems), len(participants),
            '%d 分钟' % (self.freeze['frozen_ms'] // 60000) if self.freeze['frozen_ms'] else '无')

    def emit_static(self):
        """比赛、状态、题目、分组、判题类型,以及全部队伍与学校。只使用本地数据,不访问网络。"""
        store = self.store
        self.emit_contest()
        store.upsert('state', to_state(self.meta['begin'], self.meta['end'], now_ms(), self.freeze))
        for index, problem in enumerate(self.problems):
            store.upsert('problems', to_problem(problem, index, self.palette))
        for group in GROUPS:
            store.upsert('groups', to_group(group))
        for jt_id, jt_name, penalty, solved in JUDGEMENT_TYPES:
            store.upsert('judgement-types', {
                'id': jt_id, 'name': jt_name, 'penalty': penalty, 'solved': solved,
            })
        with self.lock:
            uids = list(self.teams)
        for uid in uids:
            self.emit_team(uid)

    def emit_contest(self):
        cid = self.store.contest_id
        data = to_contest(cid, self.meta['name'], self.meta['begin'], self.meta['end'], self.freeze)
        ref = self.assets.ref(('contest',), 'logo', 'contests/%s' % cid)
        if ref:
            data['logo'] = [ref]
        self.store.upsert('contest', data)

    def emit_team(self, uid):
        cid = self.store.contest_id
        with self.lock:
            team = dict(self.teams[uid])
        data = to_team(uid, team['name'], team['school'], team['school_id'], team['official'])
        ref = self.assets.ref(('teams', uid), 'photo', 'contests/%s/teams/%s' % (cid, uid))
        if ref:
            data['photo'] = [ref]
        self.store.upsert('teams', data)
        self.emit_organization(team['school'], team['school_id'])

    def emit_organization(self, school, school_id=None):
        cid = self.store.contest_id
        org = to_organization(school, school_id)
        oid = org['id']
        ref = self.assets.ref(('organizations', oid), 'logo',
                              'contests/%s/organizations/%s' % (cid, quote(oid, safe='')))
        if ref:
            org['logo'] = [ref]
        self.store.upsert('organizations', org)

    # ------------------------------------------------------------- 赛时提交

    def _judged(self, sid):
        judgement = self.store.judgements.get('j-%s' % sid)
        return judgement is not None and judgement['judgement_type_id'] is not None

    def _pending_oldest(self):
        """所有待判提交中最早的提交时间(毫秒),没有待判时返回 None。"""
        times = [parse_time_ms(j['start_time']) for j in self.store.judgements.values()
                 if j['judgement_type_id'] is None]
        times = [t for t in times if t is not None]
        return min(times) if times else None

    def _collect_fresh(self):
        """从最新一页开始向前翻页,收集新提交与待判提交。

        停止条件:
          - 翻到比赛开始之前的提交;或
          - 当前页在比赛窗口内全部已判定,且所有待判提交都比本页更新(它们在更靠前的页上,已扫描过)。
        赛后提交(晚于比赛结束)不纳入,直接跳过。
        """
        begin, end = self.meta['begin'], self.meta['end']
        pending_oldest = self._pending_oldest()
        fresh = {}
        page = 1
        while True:
            basic, rows = self.client.get_status_page(self.store.contest_id, page)
            in_window = 0
            page_oldest = None
            settled = True
            before_begin = False
            for row in rows:
                submit = row['submitTime']
                if submit < begin:
                    before_begin = True
                    continue
                if submit > end:
                    continue
                in_window += 1
                page_oldest = submit if page_oldest is None else min(page_oldest, submit)
                sid = str(row['submissionId'])
                if not self._judged(sid):
                    settled = False
                    fresh[sid] = row
            if before_begin:
                break
            if in_window and settled and (pending_oldest is None or pending_oldest >= page_oldest):
                break
            if page >= (basic.get('pageCount') or 0) or not rows:
                break
            page += 1
        return fresh

    def sync_status(self):
        begin = self.meta['begin']
        store = self.store
        fresh = self._collect_fresh()
        if fresh:
            log('发现 %d 条新提交或待判提交', len(fresh))
        observed = now_ms()
        for row in sorted(fresh.values(), key=lambda r: (r['submitTime'], r['submissionId'])):
            uid = str(row['userId'])
            with self.lock:
                unknown = uid not in self.teams
                if unknown:
                    # 未报名却有提交:补建打星队伍
                    school, school_id = self.rank_school.get(row['userId'], (None, None))
                    self.teams[uid] = {
                        'name': row['userName'],
                        'school': school or UNKNOWN_SCHOOL,
                        'school_id': school_id,
                        'official': False,
                    }
            if unknown:
                self.emit_team(uid)
            language = to_language(row.get('language'), row.get('languageName'))
            store.upsert('languages', language)
            store.upsert('submissions', to_submission(row, begin))

            message = row.get('statusMessage')
            verdict = judgement_type_id(message)
            if verdict is None and message not in self._warned_messages:
                self._warned_messages.add(message)
                log('警告: 未知的牛客判题文案 %r(提交 %s),暂按待判处理', message, row['submissionId'])
            store.upsert('judgements', to_judgement(row, begin, verdict, observed))

    # ------------------------------------------------------------- 图片预取

    def sync_assets(self):
        """预取学校 logo 到 CDP 目录。每下载一张就重新发出对应学校。会访问网络,应在独立线程中运行。"""
        with self.lock:
            schools = {(t['school'], t['school_id']) for t in self.teams.values()}
        for school, school_id in sorted(schools, key=lambda s: s[0]):
            oid = organization_id(school, school_id)
            if self.assets.is_settled(('organizations', oid), 'logo'):
                continue
            self.assets.prefetch_school_logo(oid, school, school_id)
            self.emit_organization(school, school_id)
        log('学校 logo 预取检查完成(%d 所学校)', len(schools))

    # ------------------------------------------------------------- 驱动

    def sync_once(self, static=False):
        if static or self.meta is None or time.monotonic() - self.static_at >= self.opts.static_interval:
            self.sync_static()
        self.sync_status()
        # 状态随时间推进(开始/封榜/结束),只在变化时追加事件
        self.store.upsert('state', to_state(self.meta['begin'], self.meta['end'], now_ms(), self.freeze))

    def poll_forever(self):
        interval = self.opts.poll_interval
        while True:
            time.sleep(interval)
            try:
                self.sync_once()
                ended = (self.store.state or {}).get('ended')
                interval = self.opts.poll_interval_ended if ended else self.opts.poll_interval
            except NowcoderAuthError as e:
                log('轮询失败: %s(请更新 cookie 后重启)', e)
            except Exception as e:  # 轮询循环不允许中断
                log('轮询失败: %s', e)

    def asset_forever(self):
        while True:
            try:
                self.sync_assets()
            except NowcoderAuthError as e:
                log('图片预取失败: %s(请更新 cookie 后重启)', e)
            except Exception as e:  # 预取循环不允许中断
                log('图片预取失败: %s', e)
            time.sleep(self.opts.poll_interval)

    def start_threads(self):
        if not self.opts.no_poll:
            threading.Thread(target=self.poll_forever, name='nowcoder-poll', daemon=True).start()
            log('增量轮询已启动(比赛中 %.0fs,结束后 %.0fs)', self.opts.poll_interval, self.opts.poll_interval_ended)
        threading.Thread(target=self.asset_forever, name='nowcoder-assets', daemon=True).start()
        log('学校 logo 预取已启动(avatar 注册表 -> %s)', self.assets.assets_dir)
