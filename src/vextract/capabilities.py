"""能力矩阵：CLI / HTTP / README 共用一份，避免三处漂移。

"verified" 字段记录本机实测日期与结果，不采信上游 README 的自述。
"""

from __future__ import annotations

CAPABILITIES: list[dict[str, str]] = [
    {
        "platform": "bilibili",
        "resolver": "first-party（WBI 签名 + playurl）",
        "auth": "可选；SESSDATA 解锁 1080P+",
        "merge": "必须（DASH 音视频分离）",
        "verified": "✅ 2026-10-25 实测拉流成功（15 码流）",
    },
    {
        "platform": "weibo",
        "resolver": "first-party（游客墙 + statuses/show）",
        "auth": "无需（游客 cookie 自动获取）",
        "merge": "不需要（单 mp4）",
        "verified": "✅ 2026-10-25 实测成功（2 码流）",
    },
    {
        "platform": "kuaishou",
        "resolver": "first-party（移动页 window.INIT_STATE）",
        "auth": "公开作品无需；部分需 cookie",
        "merge": "不需要",
        "verified": "✅ 2026-10-25 实测 4 条真实作品：3 成功（720p avc/hevc + origin），1 正确报「作品不可见」",
    },
    {
        "platform": "xhs",
        "resolver": "first-party（__INITIAL_STATE__）",
        "auth": "需要分享文本里的 xsec_token；高码率需 cookie",
        "merge": "不需要",
        "verified": "⚠️ 解析实现 + fixture 已覆盖；线上正例需一条未失效的分享链接（现有公开样本已 404）",
    },
    {
        "platform": "douyin",
        "resolver": "first-party（本机算 md5 签名 + 游客 ttwid）",
        "auth": "需要浏览器身份 UIFID（一次即可，之后自动复用）",
        "merge": "不需要（抖音给音视频一体的 mp4）",
        "verified": "✅ 2026-10-25 实测 1080p/720p/540p 四档解析 + 下载（h264 1080x1920 + aac，ffprobe 校验）",
    },
    {
        "platform": "youtube / tiktok",
        "resolver": "yt-dlp",
        "auth": "可选（年龄限制内容需 cookie）",
        "merge": "YouTube 必须（DASH）",
        "verified": "✅ 2026-10-25 YouTube 实测成功",
    },
    {
        "platform": "视频号（weixin channels）",
        "resolver": "本期未实现",
        "auth": "需要 MITM 证书 + 系统代理",
        "merge": "取决于源",
        "verified": "❌ yt-dlp 零覆盖；与常驻 TUN 代理冲突，留待 P2 独立服务",
    },
    {
        "platform": "图文作品（微博/抖音/小红书）",
        "resolver": "provider 的 assets 通道（不占码流表）",
        "auth": "与视频同门槛",
        "merge": "不需要（每张图独立落盘）",
        "verified": (
            "✅ 2026-10-25 微博实测 18 张图（pic_infos 7 档，按面积取 largest）；"
            "抖音/小红书解析实现 + fixture 覆盖，线上正例待一条未失效链接"
        ),
    },
    {
        "platform": "本地转录（音频 → 文字稿）",
        "resolver": "自研 asr.py（mlx-whisper / faster-whisper / whisper 级联）",
        "auth": "不涉及平台登录态",
        "merge": "需要 ffmpeg 先出音频（可选依赖另装）",
        "verified": "✅ 2026-10-25 抖音 19.7s 样本端到端（音频 313KB + 文字稿 .md）；默认 large-v3-turbo 未下载实测",
    },
    {
        "platform": "其他 1700+ 站点",
        "resolver": "yt-dlp 兜底",
        "auth": "视站点",
        "merge": "DASH 站点需要",
        "verified": "✅ 引擎已内嵌（yt-dlp）",
    },
]