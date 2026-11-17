"""题目气球配色:与 pta-ccs-api 相同的 colors.csv 格式(label,颜色名,RGB),A~M 默认配色。

首次运行时自动生成默认 colors.csv,之后可自行修改;修改后重启即可生效(只会重新写入有变化的题目事件)。
"""

import os
import re

from .utils import log

# 默认题目配色(A~M 各不同颜色,类似气球色)
DEFAULT_PALETTE = [
    ('A', 'Red', '#E53935'),
    ('B', 'Orange', '#FB8C00'),
    ('C', 'Yellow', '#FDD835'),
    ('D', 'Lime', '#7CB342'),
    ('E', 'Green', '#43A047'),
    ('F', 'Teal', '#00897B'),
    ('G', 'Cyan', '#00ACC1'),
    ('H', 'Blue', '#1E88E5'),
    ('I', 'Indigo', '#3949AB'),
    ('J', 'Purple', '#8E24AA'),
    ('K', 'Magenta', '#D81B60'),
    ('L', 'Brown', '#8D6E63'),
    ('M', 'Gray', '#546E7A'),
]

COLORS_CSV_HEADER = (
    '# 题目配色文件:每行 label,颜色名,RGB(如 #1E88E5);# 开头为注释\n'
    '# 修改后重启本程序生效\n'
    'label,color,rgb\n'
)

_RGB_RE = re.compile(r'^#[0-9A-F]{6}$')


def load_colors_csv(path):
    """读取配色 CSV,返回 {label: {'color', 'rgb'}};格式非法的行跳过并告警。"""
    palette = {}
    with open(path, 'r', encoding='utf-8-sig') as fp:
        for line_no, raw in enumerate(fp, 1):
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            parts = [p.strip() for p in line.split(',')]
            if parts[0].lower() == 'label':
                continue  # 表头
            if len(parts) == 3:
                label, color, rgb = parts
            elif len(parts) == 2:
                label, color, rgb = parts[0], parts[1], parts[1]
            else:
                log('colors.csv 第 %d 行格式非法,已跳过: %s', line_no, line)
                continue
            rgb = rgb.upper()
            if not rgb.startswith('#'):
                rgb = '#' + rgb
            if not _RGB_RE.match(rgb):
                log('colors.csv 第 %d 行 RGB 值非法,已跳过: %s', line_no, line)
                continue
            palette[label.upper()] = {'color': color, 'rgb': rgb}
    return palette


def ensure_colors_csv(path):
    """配色文件不存在时写入默认配色;返回解析后的 palette。"""
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8', newline='\n') as fp:
            fp.write(COLORS_CSV_HEADER)
            for label, color, rgb in DEFAULT_PALETTE:
                fp.write('%s,%s,%s\n' % (label, color, rgb))
        log('已生成默认题目配色文件 %s(A~M 各不同颜色,可自行修改后重启)', path)
    palette = load_colors_csv(path)
    if not palette:
        log('警告: 配色文件 %s 为空,题目将使用默认白色', path)
    return palette
