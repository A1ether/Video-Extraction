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
        "resolver": "yt-dlp DouyinIE + cookie 阶梯",
        "auth": "必须 cookie（匿名已实测被拒）",
        "merge": "不需要（yt-dlp 给合成流）",
        "verified": "⚠️ 无 cookie 报 Fresh cookies，需 --cookies-from-browser",
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
        "platform": "其他 1700+ 站点",
        "resolver": "yt-dlp 兜底",
        "auth": "视站点",
        "merge": "DASH 站点需要",
        "verified": "✅ 引擎已内嵌（yt-dlp）",
    },
]