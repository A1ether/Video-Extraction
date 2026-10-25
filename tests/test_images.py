"""图文作品（图集）：微博（真实 payload 裁出的 fixture）、抖音、小红书的解析 + 落盘。

诚实标注（本项目口径：不采信上游自述，只认实测）：
- **微博**：`_assets` 的用例跑的是从真实图文微博裁下来的 fixture（18 张图那条，见 docs/03），
  各档尺寸都是实测值——`largest` 2048x1152 最大，代码必须按面积挑而不是按字段名猜。
- **抖音 / 小红书**：目前没拿到未失效的图文作品链接，用例里的 payload 是**按字段约定构造的
  合成样本**；线上正例补齐前，能力矩阵里它们标"解析实现 + fixture 覆盖"。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vextract import download
from vextract.errors import DownloadFailed
from vextract.models import Asset, MediaInfo, Stream
from vextract.parsers import ext_of_url
from vextract.providers.douyin import Douyin
from vextract.providers.weibo import Weibo
from vextract.providers.xhs import Xiaohongshu


# ---------- 微博（fixture 来自真实图文微博）----------
def test_weibo_picks_largest_variant_by_area(fixture):
    data = json.loads(fixture("weibo_status_images.json"))
    assets = Weibo._assets(data)

    assert len(assets) == 2
    assert [a.index for a in assets] == [1, 2], "pic_ids 的顺序就是作者排的顺序"
    first = assets[0]
    assert first.width == 2048 and first.height == 1152, "必须挑面积最大的档（largest），不是按字段名猜"
    # 实测各档：wap180(180x101) wap360(360x202) orj960(1706x960) orj1080(1920x1080)
    #          large(2048x1152) mw2000(2000x1125) → largest 面积最大，落盘 URL 必须是 /large/ 那一档
    assert first.url == "https://wx4.sinaimg.cn/large/STUBPID01.jpg"
    assert first.url.startswith("https://") and first.ext == "jpg"
    assert "weibo.com" in first.headers.get("Referer", "")


def test_weibo_image_post_resolves_as_image_post_without_raising(fixture, monkeypatch):
    """图文微博不该再报 NO_MEDIA——这是本次要修的行为。"""
    payload = json.loads(fixture("weibo_status_images.json"))

    class FakeResponse:
        text = json.dumps(payload)
        url = SimpleNamespace(path="/ajax/statuses/show")

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    async def no_visitor(_self, _cli):
        return None

    monkeypatch.setattr("vextract.providers.weibo.http.client", lambda *a, **k: FakeClient())
    monkeypatch.setattr(Weibo, "_visitor", no_visitor)

    import asyncio

    media = asyncio.run(Weibo().resolve("https://m.weibo.cn/status/5357907014320571"))
    assert media.is_image_post, "没有视频流但有图 → 图文作品"
    assert len(media.assets) == 2
    assert media.work_kind == "图文作品"
    assert "图集" in (media.quality_note or "")


# ---------- 抖音（合成样本）----------
def douyin_image_detail() -> dict:
    """按抖音 images[] 的 url_list 约定构造的合成图文详情（非线上抓包）。"""
    return {
        "aweme_id": "7300000000000000000",
        "aweme_type": 68,
        "desc": "图文作品（合成样本）",
        "author": {"nickname": "某作者"},
        "statistics": {"digg_count": 10, "comment_count": 2, "share_count": 1, "collect_count": 3},
        "images": [
            {
                "uri": "img-1",
                "width": 1080,
                "height": 1440,
                "url_list": [
                    "https://p3-sign.douyinpic.com/tos-cn-i-0813/first.jpg",
                    "https://p6-sign.douyinpic.com/tos-cn-i-0813/first.jpg",
                ],
            },
            {"uri": "img-2", "width": 720, "height": 960, "url_list": ["https://p3-sign.douyinpic.com/tos-cn-i-0813/second.jpg"]},
        ],
    }


def test_douyin_image_post_becomes_assets_not_error():
    media = Douyin()._to_media(douyin_image_detail(), "7300000000000000000")
    assert media.is_image_post
    assert [a.index for a in media.assets] == [1, 2]
    assert media.assets[0].width == 1080 and media.assets[0].height == 1440
    assert media.assets[0].url.endswith("first.jpg"), "url_list 里取第一个直链"
    assert "douyin.com" in media.assets[0].headers.get("Referer", ""), "抖音图片 CDN 也要 Referer"


def test_douyin_still_reports_no_media_when_nothing_available():
    from vextract.errors import NoMedia

    with pytest.raises(NoMedia):
        Douyin()._to_media({"aweme_id": "1", "desc": "空的", "video": {}}, "1")


def test_douyin_video_post_has_no_assets(monkeypatch):
    """视频作品不能凭空长出 assets（否则会被误判成图文）。"""
    media = Douyin()._to_media(
        {
            "aweme_id": "1",
            "desc": "视频",
            "video": {
                "bit_rate": [{"gear_name": "normal_1080_0", "bit_rate": 2_000_000, "play_addr": {"url_list": ["https://v/x.mp4"], "width": 1080, "height": 1920}}],
                "duration": 1000,
            },
            "images": [],
        },
        "1",
    )
    assert media.assets == [] and not media.is_image_post


# ---------- 小红书（合成样本）----------
def xhs_note_page(image_list: list) -> str:
    state = {"note": {"noteDetailMap": {"n1": {"note": {"noteId": "n1", "title": "图文笔记", "imageList": image_list, "user": {"nickname": "某作者"}, "interactInfo": {"likedCount": "3"}}}}}}
    return f"<html><script>window.__INITIAL_STATE__={json.dumps(state, ensure_ascii=False)}</script></html>"


def test_xhs_image_assets_prefer_clean_default_and_strip_crop_params():
    note = {
        "imageList": [
            {
                "urlDefault": "https://sns-img-bd.xhscdn.com/aaa?imageView2/2/w/1080/format/webp",
                "urlPre": "https://sns-webpic-qc.xhscdn.com/aaa!=/format/webp",
                "width": 1080,
                "height": 1440,
            },
            {"urlDefault": "https://sns-img-bd.xhscdn.com/bbb.png", "width": 800, "height": 600},
        ]
    }
    assets = Xiaohongshu._assets(note)
    assert [a.index for a in assets] == [1, 2]
    assert assets[0].url == "https://sns-img-bd.xhscdn.com/aaa", "裁切参数要剥掉（换原图），水印预览图 urlPre 绝不使用"
    assert assets[0].width == 1080
    assert assets[1].ext == "png"
    assert "xiaohongshu.com" in assets[0].headers.get("Referer", "")


def test_xhs_image_note_resolves_from_page_state(monkeypatch):
    html = xhs_note_page(
        [{"urlDefault": "https://sns-img-bd.xhscdn.com/aaa", "width": 1080, "height": 1440}]
    )

    class FakeResponse:
        text = html
        url = SimpleNamespace(path="/explore/n1")

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("vextract.providers.xhs.http.client", lambda *a, **k: FakeClient())

    import asyncio

    media = asyncio.run(Xiaohongshu().resolve("https://www.xiaohongshu.com/explore/n1"))
    assert media.is_image_post and len(media.assets) == 1


def test_xhs_assets_survive_missing_image_list():
    assert Xiaohongshu._assets({}) == []


# ---------- 扩展名 ----------
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.com/x.jpg", "jpg"),
        ("https://a.com/x.JPEG", "jpeg"),
        ("https://a.com/x.png?a=1", "png"),
        ("https://a.com/x?imageView2/2/w/1080", "jpg"),   # 猜不出来 → 默认
        ("https://a.com/x.tiff", "jpg"),                  # 不支持的格式 → 默认
        ("https://a.com/weibo/007fzS16gy1iiej1j1f37j.jpg", "jpg"),
    ],
)
def test_ext_of_url(url, expected):
    assert ext_of_url(url) == expected


# ---------- 图集落盘 ----------
def stub_fetch(monkeypatch):
    calls: list[str] = []

    async def fake_fetch(stream, dest: Path, label, progress, cancel=None, mode="video", message=""):
        calls.append(dest.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"img")
        if progress:
            progress(download.Progress(stage=label, done=1, total=2, overall=0.5, message=message))
        return 3

    monkeypatch.setattr(download, "_fetch", fake_fetch)
    return calls


def image_media() -> MediaInfo:
    return MediaInfo(
        platform="weibo",
        media_id="5357907014320571",
        source_url="https://m.weibo.cn/status/5357907014320571",
        title="图文微博",
        author="某用户",
        assets=[
            Asset(url="https://wx1.sinaimg.cn/largest/a.jpg", index=1),
            Asset(url="https://wx1.sinaimg.cn/largest/b.jpg", index=2),
            Asset(url="https://wx1.sinaimg.cn/largest/c.jpg", index=3),
        ],
    )


@pytest.mark.asyncio
async def test_download_images_writes_numbered_folder(monkeypatch, tmp_path: Path):
    calls = stub_fetch(monkeypatch)
    seen: list[download.Progress] = []
    artifacts: list[Path] = []

    folder = await download.download(
        image_media(), out_dir=tmp_path, mode="images", progress=seen.append, on_artifact=artifacts.append
    )

    assert folder.is_dir() and folder.parent == tmp_path
    assert sorted(item.name for item in folder.iterdir()) == ["01.jpg", "02.jpg", "03.jpg"]
    assert calls == ["01.jpg", "02.jpg", "03.jpg"]
    assert len(artifacts) == 3 and artifacts[0].name == "01.jpg"

    overalls = [p.overall for p in seen]
    assert overalls == sorted(overalls), "整组进度必须单调递增，不能每张图都从 0 跳回"
    assert seen[-1].overall == 1.0 and seen[-1].stage == "完成"
    assert [p.message for p in seen if p.stage == "下载图片"] == ["第 1/3 张", "第 2/3 张", "第 3/3 张"]


@pytest.mark.asyncio
async def test_download_images_avoids_overwriting_existing_folder(monkeypatch, tmp_path: Path):
    stub_fetch(monkeypatch)
    existing = tmp_path / download._stem(image_media(), None)
    existing.mkdir()

    folder = await download.download(image_media(), out_dir=tmp_path, mode="images")
    assert folder != existing and folder.name == f"{existing.name}_2", "冲突时加序号即可"
    assert folder.is_dir()


@pytest.mark.asyncio
async def test_repeat_download_does_not_duplicate_media_id(monkeypatch, tmp_path: Path):
    """回归：早先冲突时拼 `stem_{media_id}`，而自动 stem 已以 media_id 结尾 →
    文件名里同一个 ID 出现两遍（实测目录名 …_5357907014320571_5357907014320571）。"""
    stub_fetch(monkeypatch)
    media = image_media()

    first = await download.download(media, out_dir=tmp_path, mode="images")
    second = await download.download(media, out_dir=tmp_path, mode="images")
    third = await download.download(media, out_dir=tmp_path, mode="images")

    assert [p.name for p in (first, second, third)] == [
        download._stem(media, None),
        f"{download._stem(media, None)}_2",
        f"{download._stem(media, None)}_3",
    ]
    assert str(media.media_id) not in second.name.replace(download._stem(media, None), ""), "序号后缀里不该再出现 ID"


@pytest.mark.asyncio
async def test_download_images_requires_assets(tmp_path: Path):
    media = MediaInfo(platform="weibo", media_id="1", source_url="u", title="t")
    with pytest.raises(DownloadFailed) as err:
        await download.download(media, out_dir=tmp_path, mode="images")
    assert "图片" in err.value.message


def test_image_mode_is_inferred_for_image_posts():
    assert download.resolve_mode(image_media(), None) == "images"


def test_video_streams_still_win_when_present():
    """视频作品（哪怕也带图，如微博视频带封面）绝不能被推断成图集。"""
    media = MediaInfo(
        platform="weibo", media_id="1", source_url="u", title="t",
        streams=[Stream(url="https://cdn/v.mp4", quality="hd")],
        assets=[Asset(url="https://cdn/cover.jpg", index=1)],
    )
    assert download.resolve_mode(media, None) == "video"
    assert not media.is_image_post