# nowcoder-css-api

为牛客 (ac.nowcoder.com) 上采用 ACM/ICPC 赛制的比赛提供 [ICPC Contest API 2023-06](https://ccs-specs.icpc.io/2023-06/contest_api) 支持,
使 ICPC Tools CDS 等工具可以直接连接。以独立 Python 脚本方式运行,仅依赖 `requests`。

## 工作原理

```
ac.nowcoder.com ──(带 Cookie 限速拉取)──> nowcoder-css-api ──(Contest API)──> ICPC Tools CDS
                                          事件日志 data/<比赛ID>/events.jsonl
                                          图片目录 assets/(CDP 布局)
```

- 启动时全量同步:比赛信息(含封榜)、题目、报名名单、排名(学校)、赛时全部提交与判题结果
- 之后按轮询间隔增量同步新提交与待判提交的判题结果变化;比赛信息/名单/排名低频刷新(默认 5 分钟)
- 比赛状态(开始/封榜/结束/解冻)随时间推进,变化时补发 `state` 事件
- 所有数据写入事件日志 `events.jsonl`,重启后直接回放恢复,无需重新请求牛客
- 图片在独立线程中预取,每下载一张就重新发出对应的队伍/学校/比赛事件
- 对外部网站的请求经过全局限速器(默认最小间隔 1 秒 + 随机抖动),429/5xx 自动指数退避重试
- event-feed 流式连接空闲时每 3 秒检查一次新事件,有新事件立即下发;每 10 秒发送一次心跳空行

## 数据来源

| 接口 | 用途 |
|---|---|
| `/acm/contest/admin/contest/contest-info-with-problem?contestId=<ID>` | 比赛名称、开始/结束时间、封榜配置、题目列表 |
| `/acm/contest/admin/participants/list` | 报名名单(队伍来源,`subjectId` 为选手 uid,含未提交者) |
| `/acm-heavy/acm/contest/real-time-rank-data` | 排名,仅用于获取选手所在学校;`limit=0` 时按 `page` 翻页 |
| `/acm-heavy/acm/contest/status-list` | 赛时提交(按提交时间倒序,每页最多 50 条) |
| `avatar.hydrooj.com` | 学校 logo 注册表与图片(不带 Cookie) |

> [!NOTE]
> 不使用 `resolver-event-feed` 导出:该导出只在比赛结束后才可用,无法用于赛时数据。

## 已支持

- `/api`
- `/api/contests`
- `/api/contests/<contestId>`
- `/api/contests/<contestId>/state`
- `/api/contests/<contestId>/languages`
- `/api/contests/<contestId>/problems`
- `/api/contests/<contestId>/teams`
- `/api/contests/<contestId>/organizations`
- `/api/contests/<contestId>/groups`
- `/api/contests/<contestId>/judgement-types`
- `/api/contests/<contestId>/submissions`
- `/api/contests/<contestId>/judgements`
- `/api/contests/<contestId>/runs`(始终为空,见下文)
- `/api/contests/<contestId>/event-feed` (支持 `stream` 和 `since_token` 查询参数)
- 图片:`/api/contests/<contestId>/organizations/<orgId>/logo.*`、`/api/contests/<contestId>/logo.*`、`/api/contests/<contestId>/teams/<uid>/photo.*`(后两者仅本地 CDP 目录提供)

## How to use

Windows / Linux 通用,Python ≥ 3.8:

```bash
pip3 install -r requirements.txt   # 仅依赖 requests

# 登录 ac.nowcoder.com(账号需有该比赛的管理权限)后,从浏览器 DevTools (F12) 复制请求头中的 Cookie 整行
python3 -m nowcoder_ccs <比赛ID> --cookie "t=xxxxxxxx"
```

> [!TIP]
> 比赛 ID 即 `contestId=` 后面的数字,例如 `https://ac.nowcoder.com/acm/contest/admin/contest/contest-info-with-problem?token=&contestId=144285` 中的 `144285`。

- 使用 ICPC Tools CDS 连接,URL 格式为:
  `http(s)://<host>:<port>/api/contests/<比赛ID>`
- 认证方式为 HTTP Basic,用户名/密码见下方参数(默认 `nowcoder` / `nowcoder-pass`)

Linux 后台运行示例:

```bash
nohup python3 -m nowcoder_ccs <比赛ID> --cookie "t=xxxx" > nowcoder-css.log 2>&1 &
# cookie 过期后更换: kill 进程 → 重新执行上面的命令(本地事件日志不会丢失)
```

Cookie 也可通过环境变量 `NOWCODER_COOKIE` 提供,避免出现在命令行历史中。

## 中断恢复

进程意外中断后重启,直接回放 `events.jsonl`,对外服务立即可用,随后继续增量轮询与图片预取。
同步是幂等的:同一份数据重复同步不会产生重复事件。学校 logo 只检查一次,下载记录在 `data/<比赛ID>/logo_index.json` 中。

## 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `<比赛ID>` | (必填) | 牛客比赛 ID |
| `--cookie` | 环境变量 `NOWCODER_COOKIE` | 牛客 Cookie 请求头原文 |
| `--host` / `--port` | `0.0.0.0` / `8891` | HTTP 监听地址/端口 |
| `--user` / `--password` | `nowcoder` / `nowcoder-pass` | Basic 认证凭据 |
| `--rate` | `1.0` | 访问外部网站的最小请求间隔(秒),**请勿设置过小以免触发风控** |
| `--poll-interval` | `15` | 比赛进行中的增量轮询与图片预取间隔(秒) |
| `--poll-interval-ended` | `60` | 比赛结束后的轮询间隔(秒) |
| `--static-interval` | `300` | 比赛信息/名单/排名的刷新间隔(秒) |
| `--assets-dir` | `./assets` | CDP 图片目录,学校 logo 自动下载到此,也可手动放置 |
| `--colors` | `./colors.csv` | 题目气球配色 CSV,不存在时自动生成 A~M 默认配色 |
| `--data-dir` | `./data` | 数据目录(按比赛 ID 分目录) |
| `--proxy` | 直连 | 访问外部网站使用的代理(默认忽略系统代理设置) |
| `--no-poll` | 关 | 不请求牛客,仅用本地数据对外服务(无需 Cookie) |
| `--no-serve` | 关 | 仅同步数据后退出,不启动 HTTP 服务 |
| `--reset` | 关 | 删除本地事件日志与学校 logo 下载记录并重新同步 |
| `--verbose` | 关 | 输出 HTTP 请求等详细日志 |

## 图片资源

图片统一放在 CDP 目录 `--assets-dir`(默认 `./assets`,布局与 pta-ccs-api 相同):

```
assets/
├── contest/logo.<ext>                比赛图标(手动放置)
├── organizations/<orgId>/logo.<ext>  学校图标(默认自动下载)
└── teams/<uid>/photo.<ext>           队伍照片(手动放置)
```

- **学校 logo 默认自动下载**:从 `avatar.hydrooj.com` 的学校 logo 注册表中,按学校 ID 或学校名称(含别名)匹配,直接保存到 `organizations/<orgId>/` 下。
- 手动放入的图片优先,已有图片的学校不会重复下载。
- 已确认没有图片的学校记录在 `data/<比赛ID>/logo_index.json` 中,删除该文件可重新检查。

## 题目配色(气球)

与 pta-ccs-api 相同的 `colors.csv`(`--colors` 指定路径):

```csv
label,color,rgb
A,Red,#E53935
B,Orange,#FB8C00
...
```

首次运行自动生成 A~M 的默认配色,之后可自行修改并重启生效。未列出的题目为白色。

## 映射说明

- **contest id** = 牛客比赛 ID;名称、开始时间、时长取自比赛信息(`contestInfo`)
- **封榜**:由比赛设置决定。`needRankFrozen` 为真时,封榜时长 = `rankFrozenMillisecond`,即结束前多少时间开始封榜;`rankEndFrozenMillisecond` 为结束后仍保持封榜的时长,解冻时刻 = 结束时刻 + 该值
- **teams** 来自报名名单,队伍 id 为选手 uid,名称取报名名单中的 `name`(队伍照片仅可通过 `cdp` 本地目录提供);未报名却赛时提交的用户以 `⭐` 前缀归入 `observers`(打星队伍)
- **organizations**:选手所在学校(排名接口的 `school`)。组织 id 规则:**有学校 ID 用学校 ID;没有则用学校名称的 MD5**(中文名称不符合 CCS 的 ID 字符限制)。无学校信息的选手归入"未知学校"
- **problems** 来自比赛信息的 `problemList`:`virtualId` 为 label(A/B/C…),`title` 为题目名;颜色来自 `colors.csv`;牛客不提供时限与测试点数量,这两项留空
- **groups**:`participants`(正式队伍)、`observers`(打星队伍)
- **languages** 将牛客语言名映射为 CCS 语言 id(如 `C++`→`cpp`、`G++`→`gpp`、`Pypy3`→`pypy3`、`Python 3`→`python3`)
- **judgement-types** 将牛客判题文案映射为短名:`答案正确→ac`、`答案错误→wa`、`运行超时→tle`、`内存超限→mle`、`段错误/运行错误/执行出错→rte`、`编译错误→ce`、`格式错误→pe`(`ce` 不计罚时)
- **submissions** 仅包含比赛时间窗口内(开始 ≤ 提交时间 ≤ 结束)的提交,赛后提交忽略
- **judgements**:提交时先发出 `judgement_type_id=null` 的待判事件,出结果后更新

## 注意事项

- **组织 ID**:CCS 2023-06 规范要求 ID 只能包含 `[a-zA-Z0-9_.-]`,长度不超过 36 且不以 `-` 或 `.` 开头。牛客目前没有学校 ID,因此组织 ID 为学校名称的 MD5(32 位十六进制)。若以后能拿到学校 ID,会自动改用学校 ID
- 牛客接口不提供判题完成时间,因此 judgement 的 `end_time` 取**本程序首次观察到结果的时刻**。比赛进行中轮询时误差约为轮询间隔;对已结束的比赛做补录时,该时间为补录时刻,`end_contest_time` 会超出比赛时长,不影响排名。
- 牛客接口不提供逐测试点结果,因此 `runs` 始终为空。
- 未知的判题文案会记录告警并暂按待判处理;如遇到,请反馈以补充映射表。
- 比赛中参赛名单可能增加,会在每次静态刷新(`--static-interval`,默认 5 分钟)时补进来;赛时提交中出现的未报名选手会立即补建为打星队伍。
- Cookie 过期后轮询会持续失败并提示,重新登录后带新 cookie 重启即可,本地事件日志不会丢失。
- 多场比赛请为每个比赛 ID 分别启动一个实例(默认端口相同时需用 `--port` 区分)。
- 请合理设置 `--rate`,请求过快可能触发风控导致账号受限。

## PRs welcome ~
