"""比赛数据存储:实体表 + 事件日志(events),支持落盘与事件回放。

事件格式与 ICPC Contest API event-feed 一致:
    {"type": "submissions", "id": "...", "data": {...}, "token": "...", "time": "..."}

文件布局(<data_dir>/<contest_id>/):
    events.jsonl  事件日志(逐行 JSON)。重启后回放即可恢复全部对外数据与 token,无需重新抓取。
"""

import json
import os
import threading
from datetime import datetime

from .adapter import format_time
from .utils import log


class ContestStore(object):
    def __init__(self, contest_id, data_dir):
        self.contest_id = str(contest_id)
        self.base_dir = os.path.join(data_dir, self.contest_id)
        self.events_path = os.path.join(self.base_dir, 'events.jsonl')

        self.lock = threading.RLock()
        self.events = []            # 全量事件列表(下标即 token 位置)
        self._token_index = {}      # token -> 下标
        self._events_fp = None

        # 对外服务的实体表(CCS 格式)
        self.contest = None
        self.state = None
        self.languages = {}
        self.problems = {}
        self.groups = {}
        self.organizations = {}
        self.teams = {}
        self.judgement_types = {}
        self.submissions = {}
        self.judgements = {}
        self.runs = {}

    # ------------------------------------------------------------ 持久化

    def reset(self):
        with self.lock:
            self._close()
            if os.path.exists(self.events_path):
                os.remove(self.events_path)
            self.events = []
            self._token_index = {}
            self.contest = None
            self.state = None
            for table in self._tables().values():
                table.clear()

    def load(self):
        """回放本地事件日志;返回已回放的事件数(0 表示无缓存,需全量同步)。"""
        with self.lock:
            count = 0
            if os.path.exists(self.events_path):
                with open(self.events_path, 'r', encoding='utf-8') as fp:
                    for line in fp:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except ValueError:
                            log('事件日志存在损坏行,已跳过')
                            continue
                        self._append(event)
                        count += 1
            self._open_for_append()
            return count

    def _open_for_append(self):
        os.makedirs(self.base_dir, exist_ok=True)
        self._events_fp = open(self.events_path, 'a', encoding='utf-8', newline='\n')

    def _close(self):
        if self._events_fp:
            try:
                self._events_fp.close()
            except OSError:
                pass
            self._events_fp = None

    # ------------------------------------------------------------ 事件

    def add_event(self, event_type, data):
        """追加一条事件并写入日志文件。"""
        with self.lock:
            token = str(len(self.events) + 1)
            # state 事件的信封 id 为 null,其余取数据自身的 id
            event_id = None if event_type == 'state' else data.get('id')
            event = {
                'type': event_type,
                'id': event_id,
                'data': data,
                'token': token,
                'time': format_time(int(datetime.now().timestamp() * 1000)),
            }
            self._append(event)
            if self._events_fp:
                self._events_fp.write(json.dumps(event, ensure_ascii=False) + '\n')
                self._events_fp.flush()
            return event

    def upsert(self, event_type, data):
        """数据与当前实体不同时才追加事件(幂等)。返回是否产生了新事件。"""
        with self.lock:
            if self._current(event_type, data) == data:
                return False
            self.add_event(event_type, data)
            return True

    def events_since(self, token):
        """返回 token 之后的事件列表;token 不存在时返回 None。"""
        with self.lock:
            if token is None:
                return list(self.events)
            index = self._token_index.get(token)
            if index is None:
                return None
            return list(self.events[index + 1:])

    def index_of(self, token):
        with self.lock:
            return self._token_index.get(token)

    def _append(self, event):
        self.events.append(event)
        self._token_index[event['token']] = len(self.events) - 1
        self._apply(event)

    def _apply(self, event):
        event_type, data = event['type'], event['data']
        if event_type == 'contest':
            self.contest = data
        elif event_type == 'state':
            self.state = data
        else:
            self._tables()[event_type][data['id']] = data

    def _current(self, event_type, data):
        if event_type == 'contest':
            return self.contest if self.contest and self.contest.get('id') == data['id'] else None
        if event_type == 'state':
            return self.state
        return self._tables()[event_type].get(data['id'])

    def _tables(self):
        return {
            'languages': self.languages,
            'problems': self.problems,
            'groups': self.groups,
            'organizations': self.organizations,
            'teams': self.teams,
            'judgement-types': self.judgement_types,
            'submissions': self.submissions,
            'judgements': self.judgements,
            'runs': self.runs,
        }
