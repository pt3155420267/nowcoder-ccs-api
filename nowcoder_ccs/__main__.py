"""nowcoder-css-api 命令行入口。

将牛客 (ac.nowcoder.com) ACM/ICPC 赛制比赛适配为 ICPC Contest API 2023-06 服务,
供 ICPC Tools CDS 等工具连接。
"""

import argparse
import os
import sys

from . import __version__
from .assets import AssetStore, ensure_assets_dir
from .colors import ensure_colors_csv
from .nowcoder import NowcoderAuthError, NowcoderClient, NowcoderError
from .server import make_server
from .store import ContestStore
from .sync import Syncer
from .utils import log


def build_parser():
    parser = argparse.ArgumentParser(
        prog='nowcoder-css',
        description='将牛客 ACM/ICPC 赛制比赛适配为 CCS (ICPC Contest API 2023-06) 服务',
    )
    parser.add_argument('contest_id', nargs='?', default=None,
                        help='牛客比赛 ID(如 144285,即 contestId= 后的数字)')
    parser.add_argument('--contest', dest='contest_opt', default=None,
                        help='同上(可选写法)')
    parser.add_argument('--cookie', default=None, metavar='COOKIE',
                        help='牛客 Cookie 请求头原文,如 "t=xxxx";也可用环境变量 NOWCODER_COOKIE 提供')
    parser.add_argument('--host', default='0.0.0.0', help='HTTP 监听地址(默认 0.0.0.0)')
    parser.add_argument('--port', type=int, default=8891, help='HTTP 监听端口(默认 8891)')
    parser.add_argument('--user', default='nowcoder', help='Basic 认证用户名(默认 nowcoder)')
    parser.add_argument('--password', default='nowcoder-pass', help='Basic 认证密码(默认 nowcoder-pass)')
    parser.add_argument('--rate', type=float, default=1.0,
                        help='访问外部网站的最小请求间隔秒数(默认 1.0,请勿设置过小)')
    parser.add_argument('--poll-interval', type=float, default=15,
                        help='比赛进行中的增量轮询间隔秒数(默认 15)')
    parser.add_argument('--poll-interval-ended', type=float, default=60,
                        help='比赛结束后的轮询间隔秒数(默认 60)')
    parser.add_argument('--static-interval', type=float, default=300,
                        help='比赛信息/名单/排名的刷新间隔秒数(默认 300)')
    parser.add_argument('--assets-dir', default='assets', metavar='DIR',
                        help='CDP 图片目录(学校 logo 自动下载到此,也可手动放置),默认 ./assets')
    parser.add_argument('--colors', default='colors.csv', metavar='FILE',
                        help='题目气球配色 CSV(label,颜色名,RGB),不存在时自动生成 A~M 默认配色,默认 ./colors.csv')
    parser.add_argument('--data-dir', default='data', help='数据目录(默认 ./data)')
    parser.add_argument('--proxy', default=None, metavar='URL',
                        help='访问牛客使用的代理(默认直连,忽略系统代理设置)')
    parser.add_argument('--no-poll', action='store_true', help='不请求牛客,仅用本地数据对外服务(无需 Cookie)')
    parser.add_argument('--no-serve', action='store_true', help='仅同步数据后退出,不启动 HTTP 服务')
    parser.add_argument('--reset', action='store_true', help='删除本地事件日志与学校 logo 下载记录并重新同步')
    parser.add_argument('--verbose', action='store_true', help='输出更详细的日志')
    parser.add_argument('--version', action='version', version='nowcoder-css-api %s' % __version__)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    contest_id = args.contest_opt or args.contest_id
    if not contest_id:
        parser.error('必须提供比赛 ID,例如: nowcoder-css 144285')
    if not contest_id.isdigit():
        parser.error('比赛 ID 应为数字: %s' % contest_id)

    cookie = args.cookie or os.environ.get('NOWCODER_COOKIE') or ''
    store = ContestStore(contest_id, args.data_dir)
    ensure_assets_dir(args.assets_dir)
    palette = ensure_colors_csv(args.colors)
    server = None

    try:
        if args.reset:
            store.reset()
            logo_index = os.path.join(store.base_dir, 'logo_index.json')
            if os.path.exists(logo_index):
                os.remove(logo_index)
            log('已清除本地事件日志与学校 logo 下载记录,开始重新同步')
        replayed = store.load()
        if replayed:
            log('已从事件日志恢复 %s 的数据(%d 条事件)', contest_id, replayed)

        # --no-poll 且已有本地数据时只对外服务,不需要 cookie
        sync_needed = not (args.no_poll and replayed)
        if sync_needed and not cookie:
            parser.error('必须通过 --cookie 或环境变量 NOWCODER_COOKIE 提供牛客 Cookie '
                         '(登录 ac.nowcoder.com 后从浏览器 DevTools 复制请求头中的 Cookie)')
        client = NowcoderClient(cookie, min_interval=args.rate, proxy=args.proxy)
        assets = AssetStore(client, args.assets_dir, store.base_dir)
        syncer = Syncer(client, store, args, assets, palette=palette)

        if sync_needed:
            try:
                syncer.sync_once(static=True)
            except (NowcoderError, NowcoderAuthError) as e:
                log('同步失败: %s', e)
                if not store.events:
                    sys.exit(1)
                log('将继续使用本地已有数据对外服务,轮询会自动重试')
        else:
            log('已跳过牛客同步(--no-poll),仅使用本地数据对外服务')

        if args.no_serve:
            # 不启动后台线程(进程随即退出),直接同步一次学校 logo
            syncer.sync_assets()
            log('同步完成(--no-serve,不启动 HTTP 服务)')
            return
        syncer.start_threads()

        server = make_server(args.host, args.port, store, assets, args.user, args.password, args.verbose)
        log('CCS API 服务已启动: http://%s:%d/api/contests/%s', args.host, args.port, contest_id)
        log('ICPC Tools CDS 连接地址: http://<本机IP>:%d/api/contests/%s (用户 %s)',
            args.port, contest_id, args.user)
        server.serve_forever()
    except KeyboardInterrupt:
        log('收到中断,退出')
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        store._close()


if __name__ == '__main__':
    main()
