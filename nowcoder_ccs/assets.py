"""图片资源:统一放在 CDP 目录 <assets-dir> 中(与 pta-ccs-api 同布局),由本程序对外提供下载。

    contest/logo.<ext>                  比赛图标(手动放置)
    organizations/<orgId>/logo.<ext>    学校图标
    teams/<uid>/photo.<ext>             队伍照片(手动放置)

学校图标默认从 avatar.hydrooj.com 的学校 logo 注册表按学校名/别名匹配,下载后直接保存到上述目录。
手动放入的图片优先,已下载过或确认无图片的学校不会重复请求;记录在 <data>/<比赛ID>/logo_index.json 中,
删除该文件可重新检查。
"""

import json
import os
import re
import threading
from html import unescape
from io import BytesIO
from urllib.parse import quote

from .utils import log

try:
    from PIL import Image
except ImportError:  # 未安装 Pillow 时无法转换格式,见 requirements.txt
    Image = None

AVATAR_BASE = 'https://avatar.hydrooj.com'

IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp')

MIME_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.webp': 'image/webp',
    '.bmp': 'image/bmp',
}

CONTENT_TYPE_EXT = {
    'image/png': '.png',
    'image/jpeg': '.jpg',
    'image/gif': '.gif',
    'image/svg+xml': '.svg',
    'image/webp': '.webp',
    'image/bmp': '.bmp',
}

ASSETS_README = """CDP 图片资源目录,由 nowcoder-css-api 对外提供下载:

  contest/logo.<ext>                  比赛图标
  organizations/<orgId>/logo.<ext>    学校图标(orgId 为学校 ID,或学校名称的 MD5;默认自动从 avatar 注册表下载)
  teams/<uid>/photo.<ext>             队伍照片(uid 为选手 uid)

手动放入或替换图片后重启本程序即可生效。
"""


# ---------------------------------------------------------------- CDP 目录

def find_image(assets_dir, segments, name):
    """在 assets_dir/segments/ 下查找 name.<ext>,返回 (文件名, 绝对路径) 或 None。"""
    folder = os.path.join(assets_dir, *segments)
    if not os.path.isdir(folder):
        return None
    for ext in IMAGE_EXTS:
        filename = name + ext
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            return filename, path
    return None


def file_reference(href_prefix, found):
    """查到的图片 -> ICPC Contest API 文件引用(href 相对于 /api/)。"""
    filename, _ = found
    ext = os.path.splitext(filename)[1].lower()
    return {
        'href': '%s/%s' % (href_prefix, quote(filename)),
        'mime': MIME_TYPES[ext],
        'filename': filename,
    }


def ensure_assets_dir(assets_dir):
    """首次运行时创建目录骨架与说明文件。"""
    if os.path.isdir(assets_dir):
        return
    for sub in ('contest', 'organizations', 'teams'):
        os.makedirs(os.path.join(assets_dir, sub), exist_ok=True)
    with open(os.path.join(assets_dir, 'README.txt'), 'w', encoding='utf-8') as fp:
        fp.write(ASSETS_README)


# ------------------------------------------------------------ 注册表解析

def to_png(body):
    """CDS 读图使用 Java ImageIO,不支持 WebP,因此把 WebP 转为 PNG(保留透明通道)。"""
    if Image is None:
        raise RuntimeError('转换 WebP 需要 Pillow,请执行: pip install -r requirements.txt')
    out = BytesIO()
    Image.open(BytesIO(body)).save(out, format='PNG')
    return out.getvalue()


def sniff_ext(body, content_type):
    """由 Content-Type 或文件头判断扩展名;无法判断返回 None。"""
    ext = CONTENT_TYPE_EXT.get((content_type or '').split(';')[0].strip().lower())
    if ext:
        return ext
    if body.startswith(b'\x89PNG'):
        return '.png'
    if body.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if body.startswith(b'GIF8'):
        return '.gif'
    if body[:4] == b'RIFF' and body[8:12] == b'WEBP':
        return '.webp'
    if body.lstrip()[:5] in (b'<?xml', b'<svg '):
        return '.svg'
    return None


def _unwrap_astro(value):
    """Astro 序列化的 props:[0, v] 为标量/对象,[1, [...]] 为数组。"""
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], int):
        if value[0] == 1:
            return [_unwrap_astro(item) for item in value[1]]
        return _unwrap_astro(value[1])
    if isinstance(value, dict):
        return {key: _unwrap_astro(item) for key, item in value.items()}
    return value


def parse_logo_registry(page):
    """从 avatar 注册表页面中解析学校 logo 列表:[{name, aliases, src, file, ...}]。"""
    for raw in re.findall(r'<astro-island[^>]*props="([^"]*)"', page):
        props = json.loads(unescape(raw))
        if 'logos' in props:
            return _unwrap_astro(props['logos'])
    raise ValueError('注册表页面中未找到学校 logo 列表')


class AssetStore(object):
    def __init__(self, client, assets_dir, state_dir):
        self.client = client
        self.assets_dir = assets_dir
        self.index_path = os.path.join(state_dir, 'logo_index.json')
        self.registry_path = os.path.join(state_dir, 'logo_registry.json')
        self.lock = threading.RLock()
        self.index = self._load_json(self.index_path, {})
        self.registry = None

    @staticmethod
    def _key(segments, name):
        return '/'.join(segments + (name,))

    def find(self, segments, name):
        """在 CDP 目录中查找图片,返回 (文件名, 路径) 或 None。"""
        return find_image(self.assets_dir, segments, name)

    def ref(self, segments, name, href_prefix):
        found = self.find(segments, name)
        return file_reference(href_prefix, found) if found else None

    def is_settled(self, segments, name):
        """已有图片,或已确认没有可用图片时返回 True。"""
        if self.find(segments, name):
            return True
        with self.lock:
            return self._key(segments, name) in self.index

    def prefetch_school_logo(self, org_id, school, school_id=None):
        """学校图标:从 avatar 注册表按 学校 ID / 学校名 / 别名 匹配,下载到 CDP 目录。"""
        segments, name = ('organizations', org_id), 'logo'
        if self.is_settled(segments, name):
            return
        entry = self._registry_match(school, school_id)
        if entry is None:
            self._record(segments, name, None)
            return
        url = AVATAR_BASE + entry['src']
        body, content_type = self.client.download(url)
        self._save(segments, name, url, body, content_type)

    def _registry_match(self, school, school_id):
        registry = self._load_registry()
        keys = {school}
        if school_id:
            keys.add(str(school_id))
        for entry in registry:
            names = {entry.get('name')} | set(entry.get('aliases') or [])
            if names & keys:
                return entry
        return None

    def _load_registry(self):
        if self.registry is not None:
            return self.registry
        if os.path.isfile(self.registry_path):
            self.registry = self._load_json(self.registry_path, [])
            return self.registry
        log('正在从 avatar 注册表获取学校 logo 列表 ...')
        body, _ = self.client.download(AVATAR_BASE + '/')
        self.registry = parse_logo_registry(body.decode('utf-8'))
        self._write_json(self.registry_path, self.registry)
        log('学校 logo 注册表: %d 条', len(self.registry))
        return self.registry

    def _save(self, segments, name, url, body, content_type):
        ext = sniff_ext(body, content_type)
        if ext is None:
            self._record(segments, name, None)
            log('无法识别图片格式,已跳过: %s', url)
            return
        if ext == '.webp':
            body, ext = to_png(body), '.png'
        folder = os.path.join(self.assets_dir, *segments)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name + ext)
        tmp_path = path + '.tmp'
        with open(tmp_path, 'wb') as fp:
            fp.write(body)
        os.replace(tmp_path, path)
        self._record(segments, name, url)

    def _record(self, segments, name, url):
        with self.lock:
            self.index[self._key(segments, name)] = {'url': url}
            self._write_json(self.index_path, self.index)

    # ------------------------------------------------------------ 文件读写

    @staticmethod
    def _load_json(path, default):
        if not os.path.isfile(path):
            return default
        with open(path, 'r', encoding='utf-8') as fp:
            return json.load(fp)

    @staticmethod
    def _write_json(path, obj):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8', newline='\n') as fp:
            json.dump(obj, fp, ensure_ascii=False)
        os.replace(tmp_path, path)
