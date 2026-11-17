"""时间工具与 牛客 -> CCS(ICPC Contest API)格式映射。

牛客接口的时间戳均为毫秒,且为北京时间(+08:00)。
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone

CONTEST_TZ = timezone(timedelta(hours=8))
UNKNOWN_SCHOOL = '未知学校'

# 牛客判题文案 -> CCS judgement-type id(文案取自 status-list 的 statusMessage)
VERDICT_MAP = {
    '答案正确': 'ac',
    '答案错误': 'wa',
    '运行超时': 'tle',
    '内存超限': 'mle',
    '段错误': 'rte',
    '运行错误': 'rte',
    '执行出错': 'rte',
    '编译错误': 'ce',
    '格式错误': 'pe',
}

# 牛客 language 字段 -> (CCS language id, 展示名)
LANGUAGES = {
    'C++': ('cpp', 'C++'),
    'G++': ('gpp', 'C++ (g++)'),
    'C': ('c', 'C'),
    'GCC': ('gcc', 'C (gcc)'),
    'Java': ('java', 'Java'),
    'Pypy3': ('pypy3', 'PyPy 3'),
    'Python 3': ('python3', 'Python 3'),
    'Python': ('python2', 'Python 2'),
    'Php': ('php', 'PHP'),
}

GROUPS = [
    {'id': 'participants', 'name': '正式队伍'},
    {'id': 'observers', 'name': '打星队伍'},
]

# 固定 judgement-types 表: (id, name, penalty, solved)
JUDGEMENT_TYPES = [
    ('ac', 'Accepted', False, True),
    ('wa', 'Wrong Answer', True, False),
    ('tle', 'Time Limit Exceeded', True, False),
    ('mle', 'Memory Limit Exceeded', True, False),
    ('rte', 'Runtime Error', True, False),
    ('ce', 'Compile Error', False, False),
    ('pe', 'Presentation Error', True, False),
]

_TIME_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{3})\+08:00$')


# --------------------------------------------------------------------- 时间

def format_time(ms):
    """毫秒时间戳 -> 北京时间 ISO8601,形如 2024-12-19T14:30:00.000+08:00。None 原样返回。"""
    if ms is None:
        return None
    seconds, millis = divmod(int(ms), 1000)
    dt = datetime.fromtimestamp(seconds, CONTEST_TZ)
    return '%s.%03d+08:00' % (dt.strftime('%Y-%m-%dT%H:%M:%S'), millis)


def parse_time_ms(s):
    """format_time 生成的字符串 -> 毫秒时间戳。"""
    m = _TIME_RE.match(s or '')
    if not m:
        return None
    dt = datetime(*[int(g) for g in m.groups()[:6]], tzinfo=CONTEST_TZ)
    return int(dt.timestamp()) * 1000 + int(m.group(7))


def format_duration_ms(ms):
    """毫秒 -> H:MM:SS.mmm(ICPC Contest API 时长格式)。"""
    ms = max(0, int(ms))
    hours = ms // 3600000
    minutes = ms % 3600000 // 60000
    seconds = ms % 60000 // 1000
    milli = ms % 1000
    return '%d:%02d:%02d.%03d' % (hours, minutes, seconds, milli)


def contest_time_str(begin_ms, at_ms):
    """比赛相对时间(开始时刻起算,负值截断为 0:00:00.000)。"""
    return format_duration_ms(at_ms - begin_ms)


# ----------------------------------------------------------------- 封榜

def freeze_from_setting(setting):
    """比赛设置 settingInfoJson -> 封榜参数。

    frozen_ms:     封榜时长(毫秒),结束前多久开始封榜;不封榜为 None
    end_frozen_ms: 比赛结束后仍保持封榜的时长(毫秒),解冻时刻 = 结束 + end_frozen_ms
    """
    if not setting.get('needRankFrozen'):
        return {'frozen_ms': None, 'end_frozen_ms': 0}
    return {
        'frozen_ms': setting.get('rankFrozenMillisecond') or None,
        'end_frozen_ms': setting.get('rankEndFrozenMillisecond') or 0,
    }


# --------------------------------------------------------------- CCS 适配

def to_contest(contest_id, name, begin, end, freeze):
    frozen_ms = freeze['frozen_ms']
    return {
        'id': str(contest_id),
        'name': name,
        'formal_name': name,
        'start_time': format_time(begin),
        'duration': format_duration_ms(end - begin),
        'scoreboard_type': 'pass-fail',
        'scoreboard_freeze_duration': format_duration_ms(frozen_ms) if frozen_ms else None,
        'penalty_time': 20,
    }


def to_state(begin, end, now, freeze):
    """按当前时间生成 state。各时刻都是固定值(不随 now 漂移),避免重复推送。"""
    frozen_ms = freeze['frozen_ms']
    frozen_at = end - frozen_ms if frozen_ms else None
    thaw_at = end + freeze['end_frozen_ms']
    return {
        'started': format_time(begin) if now >= begin else None,
        'frozen': format_time(frozen_at) if frozen_at and now >= frozen_at else None,
        'ended': format_time(end) if now >= end else None,
        'thawed': format_time(thaw_at) if now >= thaw_at else None,
        'finalized': format_time(end + 120 * 1000) if now >= end else None,
        'end_of_updates': format_time(end + 150 * 1000) if now >= end else None,
    }


def to_problem(problem, index, palette=None):
    """比赛 problemList 条目 -> CCS problem。virtualId 为题号字母,title 为题目名。

    palette: label -> {color, rgb}(见 colors.py),缺省白色。
    牛客不提供时限与测试点数量,这两项留空。
    """
    label = problem['virtualId']
    entry = (palette or {}).get(label.upper()) or {}
    return {
        'id': str(problem['problemId']),
        'label': label,
        'name': problem['title'],
        'ordinal': index,
        'color': entry.get('color') or 'white',
        'rgb': entry.get('rgb') or '#ffffff',
        'time_limit': None,
        'test_data_count': None,
    }


def to_language(language, language_name=None):
    """牛客 language 字段 -> CCS language。未知语言直接用原始字段作为 id。"""
    if language in LANGUAGES:
        lang_id, name = LANGUAGES[language]
        return {'id': lang_id, 'name': name}
    lang_id = re.sub(r'\W+', '_', language or 'unknown').strip('_').lower() or 'unknown'
    return {'id': lang_id, 'name': language_name or language or lang_id}


def to_group(group):
    return {'id': group['id'], 'name': group['name']}


def organization_id(school, school_id=None):
    """组织 id:有学校 ID 用 ID;没有则用学校名称的 MD5。

    学校名称多为中文,不符合 CCS 对 ID 字符的限制([a-zA-Z0-9_.-],最长 36),MD5 十六进制串满足该限制。
    """
    if school_id:
        return str(school_id)
    return hashlib.md5(school.encode('utf-8')).hexdigest()


def to_organization(school, school_id=None):
    return {
        'id': organization_id(school, school_id),
        'name': school,
        'formal_name': school,
    }


def to_team(uid, name, school, school_id, official):
    """队伍:正式选手归入 participants,未报名但赛时有提交的归入 observers(打星)。"""
    return {
        'id': str(uid),
        'label': str(uid),
        'name': name,
        'display_name': name if official else '⭐' + name,
        'organization_id': organization_id(school, school_id),
        'group_ids': ['participants' if official else 'observers'],
    }


def to_submission(row, begin):
    submit = row['submitTime']
    return {
        'id': str(row['submissionId']),
        'language_id': to_language(row.get('language'))['id'],
        'problem_id': str(row['problemId']),
        'team_id': str(row['userId']),
        'time': format_time(submit),
        'contest_time': contest_time_str(begin, submit),
    }


def judgement_type_id(message):
    """判题文案 -> judgement-type id;未知文案返回 None(视为待判)。"""
    return VERDICT_MAP.get(message)


def to_judgement(row, begin, judgement_type, observed_ms):
    """判题结果。牛客不提供判题完成时间,end_time 取我们首次观察到结果的时刻。

    待判(judgement_type 为 None)时 end_time 同样为 None。
    """
    submit = row['submitTime']
    done = judgement_type is not None
    return {
        'id': 'j-%s' % row['submissionId'],
        'submission_id': str(row['submissionId']),
        'judgement_type_id': judgement_type,
        'start_time': format_time(submit),
        'start_contest_time': contest_time_str(begin, submit),
        'end_time': format_time(observed_ms) if done else None,
        'end_contest_time': contest_time_str(begin, observed_ms) if done else None,
    }
