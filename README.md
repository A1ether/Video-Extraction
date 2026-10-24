# vextract

自媒体视频提取工具：**贴一个分享链接/分享文本，拿到无水印视频 + 元数据**。

覆盖 抖音 / 快手 / 小红书 / B站 / 微博 / YouTube / TikTok，以及 yt-dlp 支持的 1700+ 站点。

> 技术选型与代码复用策略见 [`docs/01-技术调研与选型方案.md`](docs/01-技术调研与选型方案.md)，
> 实现与验收记录见 [`docs/02-实现与验收.md`](docs/02-实现与验收.md)。
> 文档里的每条"支持/不支持"都带本机实测证据——本项目**不采信上游 README 的自述**。

![提取页](docs/screenshots/extract.png)

## 快速开始

```bash
uv sync                # 安装依赖（Python 3.12，由 uv 管理）
uv run vx serve        # 启动服务并自动打开浏览器
```

浏览器里就做完了全部操作：**粘贴分享文本 → 解析 → 选清晰度 → 下载 → 看进度 → 页面内播放**。
默认地址 <http://127.0.0.1:8787/>，`vx serve --no-open --port 9000` 可改端口。

> 端口被占用时会**自动让位到下一个空闲端口**并在终端打印提示（含占用者进程名），
> 而且浏览器**只在服务真正监听、且探活确认是本程序之后**才打开——
> 早期版本因为"先开浏览器、后启动服务"，曾把用户送到占用 8000 端口的别的程序页面上。

### 界面里能做的事

| 页 | 功能 |
|---|---|
| **提取** | 粘贴/一键读剪贴板；识别平台；展示封面、标题、作者、时长、互动数；码流表（默认选最高清晰度，可改）；自定义文件名与输出目录；建下载任务 |
| **任务** | 实时进度（阶段 + 字节 + 百分比）、取消、重试、**页面内播放**（支持拖动进度）、保存文件、在访达中定位、移除、清空已完成；历史落盘，重启仍在 |
| **平台** | 能力矩阵（含本机实测状态）+ 环境自检（yt-dlp 版本、ffmpeg、cookie 就绪情况、provider 顺序） |
| **设置** | 输出目录、从浏览器读取 cookie（chrome/edge/firefox/safari…）、实时推送开关（关掉改轮询） |

### 深链（可脚本化）

```bash
# 打开就解析
open "http://127.0.0.1:8787/?text=https%3A%2F%2Fm.weibo.cn%2Fdetail%2F4189191225395228"
# 打开就解析并直接开始下载
open "http://127.0.0.1:8787/?text=<分享文本>&download=1"
# 强制轮询（脚本/无头环境里 SSE 会挂住页面加载）
open "http://127.0.0.1:8787/?live=0"
```

也可以做成 shell 别名：

```bash
alias vxg='open "http://127.0.0.1:8787/?text=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.stdin.read()))")"'
```

## 命令行 / HTTP

CLI 保留完整能力，适合批处理与调试：

```bash
uv run vx doctor        # 自检：引擎版本 / ffmpeg / cookie
uv run vx platforms     # 平台能力矩阵
uv run vx info "<分享文本>"            # 只解析
uv run vx get  "<分享文本>" -o ~/Downloads
```

HTTP（前端用的就是这套）：

```bash
curl -s localhost:8787/health
curl -s -X POST localhost:8787/extract -H 'content-type: application/json' \
  -d '{"text":"https://m.weibo.cn/detail/4189191225395228"}'
curl -s -X POST localhost:8787/tasks   -H 'content-type: application/json' \
  -d '{"text":"<分享文本>","stream_index":1}'
curl -N localhost:8787/events          # SSE：任务状态与进度
```

## 平台能力（截至 2026-10-25 实测）

| 平台 | 解析方式 | 鉴权 | 需要 ffmpeg 合并 |
|---|---|---|---|
| B站 | 自研（WBI 签名 + playurl） | 可选，SESSDATA 解锁 1080P+ | **是**（DASH 分离） |
| 微博 | 自研（游客墙 + statuses/show） | 免登录 | 否 |
| 快手 | 自研（移动页 `window.INIT_STATE`） | 公开作品免登录 | 否 |
| 小红书 | 自研（`__INITIAL_STATE__`） | 需分享文本里的 `xsec_token` | 否 |
| 抖音 | yt-dlp + cookie | **必须 cookie** | 否 |
| YouTube/TikTok | yt-dlp | 可选 | YouTube 是 |
| 视频号 | 未实现（见 P2） | 需 MITM 证书 + 系统代理 | — |

**B站匿名只能拿 480P**（实测：接口声明到 1080P+，`dash.video` 实际只有 360P/480P）。
要高清就导出浏览器 cookie（Netscape 格式）放到 `~/.config/vextract/cookies/bilibili.txt`，
或在「设置」里选择"从浏览器读取 cookie"。

抖音同理：匿名直连会被风控挡（`Fresh cookies are needed`），页面会直接告诉你两条出路：

```bash
vx get "<抖音分享文本>" --cookies-from-browser chrome   # 读本机 Chrome cookie（只读）
# 或插件导出 Netscape cookie → ~/.config/vextract/cookies/douyin.txt
```

## 设计要点

- **前端就是产品**：`vx serve` 一条命令，FastAPI 直接托管内置前端（无构建步骤、无 CDN 依赖，离线可用）。
- **一个平台一个文件**：`src/vextract/providers/*.py`，实现 `matches()` + `resolve()` 两个方法，
  注册表自动发现，新增平台不改任何路由、前端或 CLI 代码。
- **统一中间表示**：所有 provider 产出 `MediaInfo`，前端/CLI/下载器完全不感知平台差异。
- **错误带 code + hint**：`AUTH_REQUIRED` / `RATE_LIMITED` / `UPSTREAM_CHANGED` / `NO_MEDIA` …
  前端把 hint 原样呈现（"你下一步该做什么"），HTTP 返回机器可读 code。
- **cookie 阶梯不静默重试**：无 cookie → 报错并给出两条具体路径，绝不偷偷降级。
- **SSR JSON 容错集中在一处**：`parsers.py` 处理 `undefined` / `new Map([])` / `new Set([])` /
  C1 控制字符 / 尾逗号——平台改版时只改这一个文件。
- **任务队列 + SSE**：进度推送与取消都走同一份 `Progress` 回调（CLI 的进度条复用同一实现）；
  SSE 不可用时前端自动降级为轮询。

## 开发

```bash
uv run pytest              # 离线测试（含前端 HTML/JS/路由一致性检查，不碰网络）
uv run pytest -m live      # 真机测试（会访问真实平台）
uv run ruff check src tests scripts
uv run python scripts/capture_fixtures.py   # 平台改版时重抓 fixture
```

前端三个文件在 `src/vextract/web/`（`index.html` / `app.js` / `styles.css`），改完刷新即可，
`tests/test_web.py` 会检查"JS 调用的接口是否真的存在""JS 引用的 id 是否真的在 HTML 里"。

## 许可证与合规

本项目为 MIT。依赖清单只允许 Unlicense / MIT / Apache-2.0 / BSD：

- 引擎 `yt-dlp` = **Unlicense**（可自由内嵌、闭源商用、无 copyleft 义务）。
- 调研中排除的项目：`XHS-Downloader` / `Bili23-Downloader` / `TikTokDownloader`（GPL-3.0）、
  `bilibili-api`（GPL-3.0，已被律师函关停）、`bilibili-API-collect`（CC BY-NC，已清空）、
  `wx_channels_download`（MIT + Commons Clause，禁售卖）。

工具仅用于个人学习与研究，请遵守各平台服务条款与著作权法，不要用于批量搬运或商业分发。