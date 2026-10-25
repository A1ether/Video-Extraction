/* vextract 前端：无构建步骤、无外部依赖。
   数据流：解析(/extract) → 建任务(/tasks) → SSE(/events) 推进度 → 播放/打开目录(/tasks/{id}/file)。 */

const $ = (sel) => document.querySelector(sel);

/* ---------- 小工具 ---------- */
function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;          // 文本一律走 textContent，避免标题里的 HTML 生效
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const fmtBytes = (n) => {
  if (!n && n !== 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
};
const fmtDuration = (s) => {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
};
const fmtAge = (ts) => {
  if (!ts) return "";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
};

const PLATFORM_LABEL = { bilibili: "B站", weibo: "微博", kuaishou: "快手", xhs: "小红书", douyin: "抖音", youtube: "YouTube", tiktok: "TikTok", generic: "其他" };
const STATE_LABEL = { queued: "排队中", resolving: "解析中", downloading: "下载中", merging: "合并中", processing: "处理中", done: "已完成", error: "失败", cancelled: "已取消" };
const STATE_CLASS = { queued: "pill-muted", resolving: "pill-accent", downloading: "pill-accent", merging: "pill-accent", processing: "pill-accent", done: "pill-ok", error: "pill-err", cancelled: "pill-warn" };
const IMAGE_EXT = /\.(jpe?g|png|webp|gif|heic|avif)$/i;
const TEXT_EXT = /\.(md|txt|srt)$/i;

let toastSeq = 0;
function toast(message, kind = "info", timeout = 4200) {
  const node = el("div", { class: `toast ${kind === "error" ? "err" : kind === "ok" ? "ok" : ""}`, text: message });
  $("#toasts").append(node);
  const id = ++toastSeq;
  setTimeout(() => node.remove(), timeout + id * 0);
}

class ApiError extends Error {
  constructor(payload, status) {
    super(payload.message || `HTTP ${status}`);
    this.code = payload.code || payload.detail || `HTTP_${status}`;
    this.hint = payload.hint;
    this.status = status;
  }
}

async function api(path, { method = "GET", body } = {}) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["content-type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(path, init);
  const text = await resp.text();
  let payload = {};
  try { payload = text ? JSON.parse(text) : {}; } catch { payload = { message: text.slice(0, 200) }; }
  if (!resp.ok) throw new ApiError(payload, resp.status);
  return payload;
}

/* ---------- 错误展示 ---------- */
function showError(container, err) {
  container.replaceChildren(
    el("div", { class: "code", text: err.code || "ERROR" }),
    el("div", { text: err.message || String(err) }),
    err.hint ? el("div", { class: "hint-line", text: err.hint }) : null,
  );
  container.classList.remove("hidden");
}
const clearError = (container) => { container.replaceChildren(); container.classList.add("hidden"); };

/* ---------- 标签页（带 hash 路由，刷新后停在原页） ---------- */
function activateTab(name) {
  const tab = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!tab) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t === tab));
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("is-active"));
  $(`#panel-${name}`)?.classList.add("is-active");
  if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
}
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => activateTab(tab.dataset.tab));
});
const switchTab = activateTab;

/* ---------- 提取 ---------- */
let media = null;
let selectedIndex = null;

const SHARE_RE = /https?:\/\/[^\s"'<>]+/;
const PLATFORM_PATTERNS = [
  ["bilibili", /bilibili\.com|b23\.tv|bili2233\.cn/],
  ["douyin", /douyin\.com/],
  ["kuaishou", /kuaishou\.com|gifshow\.com|chenzhongtech\.com/],
  ["xhs", /xiaohongshu\.com|xhslink\.com|xhs\.link/],
  ["weibo", /weibo\.com|weibo\.cn/],
  ["youtube", /youtube\.com|youtu\.be/],
  ["tiktok", /tiktok\.com/],
];

function detectPlatform(text) {
  const match = text.match(SHARE_RE);
  if (!match) return null;
  try {
    const host = new URL(match[0]).hostname;
    for (const [name, pattern] of PLATFORM_PATTERNS) if (pattern.test(host)) return name;
  } catch { return null; }
  return null;
}

function refreshDetected() {
  const value = $("#share-text").value.trim();
  const chip = $("#detected");
  const platform = detectPlatform(value);
  if (!platform) { chip.classList.add("hidden"); return; }
  chip.textContent = `${PLATFORM_LABEL[platform] || platform} 链接已识别`;
  chip.classList.remove("hidden");
}
$("#share-text").addEventListener("input", refreshDetected);

$("#btn-paste").addEventListener("click", async () => {
  try {
    const text = await navigator.clipboard.readText();
    if (!text) return toast("剪贴板是空的", "error");
    $("#share-text").value = text;
    refreshDetected();
    toast("已粘贴", "ok");
  } catch {
    toast("浏览器拒绝了剪贴板读取，请手动粘贴（⌘V）", "error");
  }
});

document.querySelectorAll(".sample").forEach((btn) => {
  btn.addEventListener("click", () => {
    $("#share-text").value = btn.dataset.url;
    refreshDetected();
  });
});

async function extract() {
  const text = $("#share-text").value.trim();
  if (!text) return toast("先粘贴分享文本或链接", "error");
  const btn = $("#btn-extract");
  btn.disabled = true;
  btn.textContent = "解析中…";
  clearError($("#extract-error"));
  $("#extract-result").classList.add("hidden");
  try {
    media = await api("/extract", { method: "POST", body: { text } });
    selectedIndex = null;                       // 默认"最高清晰度"
    renderMedia(media);
    toast(`解析成功：${media.title.slice(0, 24)}`, "ok");
  } catch (err) {
    media = null;
    showError($("#extract-error"), err);
    toast(`${err.code || "解析失败"}：${err.message || ""}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "解析";
  }
}
$("#btn-extract").addEventListener("click", extract);
$("#share-text").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") extract();
});

function streamRows(data) {
  return [...data.streams].sort((a, b) => Number(a.audio_only) - Number(b.audio_only) || b.rank - a.rank);
}

function coverPlaceholder(data) {
  return el("div", { class: "cover placeholder", title: "该平台没有返回封面" }, [
    el("span", { text: PLATFORM_LABEL[data.platform] || data.platform || "无封面" }),
  ]);
}

function renderMedia(data) {
  const rows = streamRows(data);
  const assets = data.assets || [];
  const isImagePost = rows.length === 0 && assets.length > 0;
  const best = rows.findIndex((s) => !s.audio_only);
  const cover = data.cover_url
    ? el("img", { class: "cover", src: `/proxy/image?url=${encodeURIComponent(data.cover_url)}`, alt: "封面", loading: "lazy", onerror: (e) => e.target.replaceWith(coverPlaceholder(data)) })
    : coverPlaceholder(data);

  const stats = [];
  for (const [key, label] of [["view", "播放"], ["like", "点赞"], ["comment", "评论"], ["favorite", "收藏"], ["repost", "转发"]]) {
    if (data.stats && data.stats[key] !== undefined) stats.push(el("span", { class: "muted", text: `${label} ${data.stats[key]}` }));
  }

  const table = el("table", {}, [
    el("thead", {}, el("tr", {}, [el("th", { text: "" }), el("th", { text: "质量" }), el("th", { text: "类型" }), el("th", { text: "编码" }), el("th", { text: "码率" }), el("th", { text: "大小" })])),
  ]);
  const tbody = el("tbody");
  rows.forEach((stream, i) => {
    const pickable = !stream.audio_only;
    const tr = el("tr", { class: `${pickable ? "pickable" : ""} ${selectedIndex === null ? (i === best ? "is-selected" : "") : (selectedIndex === i ? "is-selected" : "")}` }, [
      el("td", {}, el("input", { type: "radio", name: "stream", disabled: pickable ? null : "disabled", checked: pickable && (selectedIndex === null ? i === best : selectedIndex === i) ? "checked" : null })),
      el("td", { text: stream.quality }),
      el("td", { text: stream.audio_only ? "音频" : "视频" }),
      el("td", { text: stream.codec || "—" }),
      el("td", { class: "num", text: stream.bitrate ? `${Math.round(stream.bitrate / 1000)}k` : "—" }),
      el("td", { class: "num", text: fmtBytes(stream.filesize) }),
    ]);
    if (pickable) {
      tr.addEventListener("click", () => {
        selectedIndex = i;
        tbody.querySelectorAll("tr").forEach((row) => row.classList.remove("is-selected"));
        tr.classList.add("is-selected");
        tr.querySelector("input").checked = true;
      });
    }
    tbody.append(tr);
  });
  table.append(tbody);
  const tableWrap = el("div", { class: "table-wrap" }, table);

  const filename = el("input", { type: "text", placeholder: "文件名（留空自动生成：作者_标题_ID）" });
  const outDir = el("input", { type: "text", placeholder: "输出目录", value: settings.out_dir || "" });

  /* ---------- 产物模式：视频 / 仅音频 / 图集 ---------- */
  const modeSelect = el("select", { id: "dl-mode", title: "要下载什么" }, [
    el("option", { value: "video", text: "视频" }),
    el("option", { value: "audio", text: "仅音频" }),
    el("option", { value: "images", text: "图集（图文作品）" }),
  ]);
  modeSelect.value = isImagePost ? "images" : "video";
  if (!rows.length) modeSelect.querySelector('option[value="video"]').disabled = "disabled";
  if (!assets.length) modeSelect.querySelector('option[value="images"]').disabled = "disabled";

  const audioFormat = el("select", {}, (settings.audio_formats || ["m4a", "mp3", "flac"]).map((name) => el("option", { value: name, text: name })));
  audioFormat.value = settings.audio_format || "m4a";
  const transcriptFormat = el("select", {}, (settings.transcript_formats || ["md", "txt", "srt"]).map((name) => el("option", { value: name, text: name })));
  transcriptFormat.value = settings.transcript_format || "md";
  const transcribeBox = el("input", { type: "checkbox", style: "width:auto" });
  if (!asrReady()) {
    transcribeBox.disabled = "disabled";
    transcribeBox.title = "没装本地转录依赖：uv sync --extra transcribe";
  }
  const audioTools = el("div", { class: "row wrap gap8 hidden" }, [
    el("span", { class: "muted", text: "格式" }), audioFormat,
    el("label", { class: "row gap6", style: "cursor:pointer", title: "纯音乐/环境音会让 Whisper 陷入重复循环；那种素材换 faster_whisper 后端（设置页可选）" }, [
      transcribeBox, el("span", { text: "本地转录出文字稿" }),
    ]),
    el("span", { class: "muted", text: "文字稿" }), transcriptFormat,
  ]);

  const grid = el("div", { class: "image-grid hidden" });
  assets.forEach((asset) => {
    grid.append(el("figure", { class: "shot" }, [
      el("img", { src: `/proxy/image?url=${encodeURIComponent(asset.url)}`, alt: `第 ${asset.index} 张`, loading: "lazy", onerror: (e) => e.target.closest("figure").classList.add("is-broken") }),
      el("figcaption", { class: "muted", text: asset.width && asset.height ? `${asset.index}. ${asset.width}×${asset.height}` : `${asset.index}.${asset.ext}` }),
    ]));
  });

  const downloadBtn = el("button", { class: "btn primary", type: "button", text: "下载视频" }, null);
  const syncMode = () => {
    const mode = modeSelect.value;
    tableWrap.classList.toggle("hidden", mode !== "video");
    audioTools.classList.toggle("hidden", mode !== "audio");
    grid.classList.toggle("hidden", mode !== "images");
    downloadBtn.textContent = mode === "audio" ? "下载音频" : (mode === "images" ? "下载图集" : "下载视频");
  };
  modeSelect.addEventListener("change", syncMode);

  downloadBtn.addEventListener("click", async () => {
    const mode = modeSelect.value;
    const label = downloadBtn.textContent;
    downloadBtn.disabled = true;
    downloadBtn.textContent = "创建任务…";
    try {
      const body = { text: $("#share-text").value.trim(), mode };
      // 行号只在视频模式传：音频模式选的是"最优音轨"，图集模式没有码流概念
      if (mode === "video" && selectedIndex !== null) body.stream_index = selectedIndex + 1;   // 后端行号是 1-based
      if (mode === "audio") {
        body.audio_format = audioFormat.value;
        body.transcribe = transcribeBox.checked;
        body.transcript_format = transcriptFormat.value;
      }
      if (filename.value.trim()) body.filename = filename.value.trim();
      if (outDir.value.trim()) body.out_dir = outDir.value.trim();
      const task = await api("/tasks", { method: "POST", body });
      upsertTask(task);
      switchTab("tasks");
      toast("任务已创建", "ok");
    } catch (err) {
      showError($("#extract-error"), err);
      toast(`${err.code || "创建任务失败"}：${err.message || ""}`, "error");
    } finally {
      downloadBtn.disabled = false;
      downloadBtn.textContent = label;
    }
  });

  const card = el("div", { class: "card" }, [
    el("div", { class: "media-head" }, [
      cover,
      el("div", { class: "meta" }, [
        el("h3", { text: data.title }),
        el("div", { class: "row wrap gap8" }, [
          el("span", { class: "pill pill-accent", text: PLATFORM_LABEL[data.platform] || data.platform }),
          el("span", { class: "pill pill-muted", text: isImagePost ? "图文作品" : "视频作品" }),
          data.author ? el("span", { class: "muted", text: `@${data.author}` }) : null,
          el("span", { class: "muted", text: fmtDuration(data.duration) }),
          el("span", { class: "muted", text: isImagePost ? `${assets.length} 张图` : `${rows.length} 条码流` }),
        ]),
        el("div", { class: "media-stats" }, stats),
        data.quality_note ? el("p", { class: "hint", text: data.quality_note }) : null,
      ]),
    ]),
    el("div", { class: "row wrap gap8 mode-row" }, [
      el("span", { class: "muted", text: "产物" }), modeSelect,
      el("span", { class: "spacer" }),
      el("span", { class: "muted", text: `共 ${rows.length + assets.length} 项` }),
    ]),
    tableWrap,
    audioTools,
    grid,
    el("div", { class: "row wrap gap8", style: "margin-top:12px" }, [filename, outDir, downloadBtn]),
    data.needs_merge ? el("p", { class: "hint", text: "该平台音视频分离，下载时会自动调用 ffmpeg 无损合并（-c copy）。" }) : null,
    isImagePost ? el("p", { class: "hint", text: "图文作品会下载成同名文件夹（01.jpg / 02.jpg …），每张取最高分辨率档；元数据不参与命名。" }) : null,
  ]);

  const result = $("#extract-result");
  result.replaceChildren(card);
  result.classList.remove("hidden");
  syncMode();
}

/* ---------- 任务列表（原地更新，避免重渲染打断播放） ---------- */
const rows = new Map();          // taskId -> { root, refs }
const openPlayers = new Set();
let tasks = new Map();
let sseConnected = false;

function taskRow() {
  const bar = el("i", { style: "width:0%" });
  const progress = el("div", { class: "progress" }, bar);
  const stage = el("span", { class: "muted", text: "排队中" });
  const speed = el("span", { class: "muted", text: "" });
  const size = el("span", { class: "muted", text: "" });
  const state = el("span", { class: "pill pill-muted", text: "排队中" });
  const title = el("div", { class: "task-title" });
  const platform = el("span", { class: "pill pill-muted" });
  const created = el("span", { class: "muted" });
  const errorBox = el("div", { class: "task-error hidden" });
  const actions = el("div", { class: "task-actions" });
  const player = el("div", { class: "hidden" });
  const artifacts = el("div", { class: "artifacts hidden" });

  const root = el("div", { class: "task" }, [
    el("div", { class: "task-head" }, [platform, title, el("span", { class: "spacer" }), created, state]),
    progress,
    el("div", { class: "task-meta" }, [stage, speed, size]),
    errorBox,
    player,
    artifacts,
    actions,
  ]);

  return { root, refs: { bar, progress, stage, speed, size, state, title, platform, created, errorBox, actions, player, artifacts } };
}

function upsertTask(task) {
  tasks.set(task.id, task);
  let row = rows.get(task.id);
  if (!row) {
    row = taskRow();
    rows.set(task.id, row);
    $("#tasks-list").prepend(row.root);
  }
  paintTask(task, row);
  paintSummary();
}

function paintTask(task, row) {
  const { refs } = row;
  const media = task.media;
  const platform = media?.platform || detectPlatform(task.text || "") || "";
  refs.platform.textContent = platform ? (PLATFORM_LABEL[platform] || platform) : (task.path ? "历史" : "链接");
  refs.platform.className = "pill pill-muted";
  refs.title.textContent = media
    ? media.title
    : (task.path ? task.path.split("/").pop().replace(/\.[^.]+$/, "") : (task.text || "").slice(0, 60));
  refs.created.textContent = fmtAge(task.created_at);

  refs.state.textContent = STATE_LABEL[task.state] || task.state;
  refs.state.className = `pill ${STATE_CLASS[task.state] || "pill-muted"}`;

  const progress = task.progress || {};
  const overall = Math.max(0, Math.min(1, progress.overall ?? 0));
  refs.bar.style.width = `${(task.state === "done" ? 1 : overall) * 100}%`;
  refs.progress.className = `progress ${task.state === "done" ? "is-done" : task.state === "error" ? "is-error" : ""}`;
  refs.stage.textContent = (progress.stage || "排队中") + (task.state === "done" ? "" : ` · ${Math.round(overall * 100)}%`);
  refs.speed.textContent = progress.total ? `${fmtBytes(progress.done)} / ${fmtBytes(progress.total)}` : (progress.done ? fmtBytes(progress.done) : "");
  refs.size.textContent = task.size ? `文件 ${fmtBytes(task.size)}` : "";

  if (task.error) {
    refs.errorBox.replaceChildren(
      el("div", { text: `${task.error.code || "ERROR"} ${task.error.message || ""}` }),
      task.error.hint ? el("div", { text: task.error.hint }) : null,
    );
    refs.errorBox.classList.remove("hidden");
  } else {
    refs.errorBox.classList.add("hidden");
  }

  const artifacts = artifactNodes(task);
  refs.artifacts.replaceChildren(...artifacts);
  refs.artifacts.classList.toggle("hidden", artifacts.length === 0);

  refs.actions.replaceChildren(...taskActions(task));
}

function artifactNodes(task) {
  /* 主产物之外的成果：图集的每张图（缩略图点开大图）、转录出的文字稿（直接保存）。 */
  const items = task.artifacts || [];
  if (!items.length) return [];
  const nodes = [];
  const shots = items.map((path, index) => ({ path, index })).filter((item) => IMAGE_EXT.test(item.path));
  if (shots.length) {
    const grid = el("div", { class: "image-grid" });
    for (const { path, index } of shots) {
      const name = path.split("/").pop();
      grid.append(el("a", {
        class: "shot", href: `/tasks/${task.id}/artifact?index=${index}`, target: "_blank", rel: "noreferrer", title: name,
      }, el("img", { src: `/tasks/${task.id}/artifact?index=${index}`, alt: name, loading: "lazy" })));
    }
    nodes.push(
      grid,
      el("p", { class: "hint", text: `${shots.length} 张图已存进文件夹：${(task.path || "").split("/").pop()}（点缩略图看原图）` }),
    );
  }
  const docs = items.map((path, index) => ({ path, index })).filter((item) => TEXT_EXT.test(item.path));
  if (docs.length) {
    const row = el("div", { class: "row wrap gap8" });
    for (const { path, index } of docs) {
      row.append(el("a", {
        class: "btn sm", href: `/tasks/${task.id}/artifact?index=${index}&attachment=1`,
        text: `保存文字稿 · ${path.split(".").pop()}`,
      }));
    }
    nodes.push(row);
  }
  return nodes;
}

function taskActions(task) {
  const buttons = [];
  const isImageTask = task.mode === "images" || (task.artifacts || []).some((path) => IMAGE_EXT.test(path));

  if (task.state === "done" && task.path && !isImageTask) {
    const isAudio = /\.[a-z0-9]+$/i.test(task.path) && /(\.m4a|\.mp3|\.flac|\.wav|\.aac|\.ogg)$/i.test(task.path);
    buttons.push(el("button", {
      class: "btn sm", type: "button", text: openPlayers.has(task.id) ? "收起播放" : (isAudio ? "试听" : "播放"),
      onclick: () => {
        const row = rows.get(task.id);
        const { player } = row.refs;
        if (openPlayers.has(task.id)) {
          openPlayers.delete(task.id);
          player.classList.add("hidden");
          player.replaceChildren();
        } else {
          openPlayers.add(task.id);
          player.replaceChildren(el(isAudio ? "audio" : "video", { controls: "", preload: "metadata", src: `/tasks/${task.id}/file` }));
          player.classList.remove("hidden");
        }
        paintTask(task, row);
      },
    }));
    buttons.push(el("a", { class: "btn sm", href: `/tasks/${task.id}/file?attachment=1`, text: "保存文件" }));
  }

  if (task.state === "done" && task.path && isImageTask) {
    const first = (task.artifacts || []).findIndex((path) => IMAGE_EXT.test(path));
    if (first >= 0) buttons.push(el("a", { class: "btn sm", href: `/tasks/${task.id}/artifact?index=${first}`, target: "_blank", rel: "noreferrer", text: "看第一张" }));
  }

  if (task.state === "done" && task.path) {
    buttons.push(el("button", {
      class: "btn sm", type: "button", text: isImageTask ? "打开文件夹" : "在访达中显示",
      onclick: async () => {
        try {
          const res = await api(`/tasks/${task.id}/reveal`, { method: "POST" });
          toast(res.message || "已在访达中定位", res.ok === false ? "error" : "ok");
        } catch (err) { toast(err.message, "error"); }
      },
    }));
  }

  if (["queued", "resolving", "downloading", "merging"].includes(task.state)) {
    buttons.push(el("button", {
      class: "btn sm danger", type: "button", text: "取消",
      onclick: async () => {
        try { upsertTask(await api(`/tasks/${task.id}/cancel`, { method: "POST" })); toast("已取消", "ok"); }
        catch (err) { toast(err.message, "error"); }
      },
    }));
  }

  if (task.state === "error" || task.state === "cancelled") {
    buttons.push(el("button", {
      class: "btn sm", type: "button", text: "重试",
      onclick: async () => {
        try {
          const next = await api("/tasks", {
            method: "POST",
            body: {
              text: task.text, quality: task.quality, stream_index: task.stream_index,
              filename: task.filename, out_dir: task.out_dir,
              mode: task.mode, audio_format: task.audio_format,
              transcribe: task.transcribe, transcript_format: task.transcript_format,
            },
          });
          upsertTask(next);
          toast("已重试", "ok");
        } catch (err) { toast(err.message, "error"); }
      },
    }));
  }

  buttons.push(el("button", {
    class: "btn sm ghost", type: "button", text: "移除",
    onclick: async () => {
      try {
        await api(`/tasks/${task.id}`, { method: "DELETE" });
        rows.get(task.id)?.root.remove();
        rows.delete(task.id);
        tasks.delete(task.id);
        paintSummary();
      } catch (err) { toast(err.message, "error"); }
    },
  }));
  return buttons;
}

function paintSummary() {
  const list = [...tasks.values()];
  const active = list.filter((t) => !["done", "error", "cancelled"].includes(t.state)).length;
  $("#tasks-summary").textContent = list.length ? `共 ${list.length} 个任务${active ? `，${active} 个进行中` : ""}` : "暂无任务";
  const badge = $("#tab-badge");
  badge.textContent = String(active);
  badge.classList.toggle("hidden", active === 0);
}

async function loadTasks() {
  try {
    const data = await api("/tasks");
    for (const task of data.tasks) upsertTask(task);
    const known = new Set(data.tasks.map((t) => t.id));
    for (const [id, row] of [...rows.entries()]) {
      if (!known.has(id)) { row.root.remove(); rows.delete(id); tasks.delete(id); }
    }
    paintSummary();
  } catch (err) {
    toast(`读取任务失败：${err.message}`, "error");
  }
}

/* ---------- SSE / 轮询 ---------- */
let pollTimer = null;

function startPolling(reason) {
  if (pollTimer) return;
  pollTimer = setInterval(loadTasks, 3000);
  const pill = $("#sse-pill");
  pill.textContent = reason || "轮询模式";
  pill.className = "pill pill-warn";
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

function connectSse() {
  stopPolling();
  const source = new EventSource("/events");
  source.onopen = () => {
    sseConnected = true;
    const pill = $("#sse-pill");
    pill.textContent = "实时推送已连接";
    pill.className = "pill pill-ok";
  };
  source.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    if (payload.event === "hello") {
      for (const task of payload.tasks || []) upsertTask(task);
      paintSummary();
      return;
    }
    if (payload.task) {
      const previous = tasks.get(payload.task.id);
      upsertTask(payload.task);
      if (payload.task.state === "done" && previous?.state !== "done") toast(`下载完成：${(payload.task.media?.title || "").slice(0, 24)}`, "ok");
      if (payload.task.state === "error" && previous?.state !== "error") toast(`任务失败：${payload.task.error?.code || ""}`, "error");
    }
  };
  source.onerror = () => {
    if (sseConnected) toast("实时推送断开，已切换为轮询", "error");
    sseConnected = false;
    source.close();
    startPolling("轮询模式");
    setTimeout(connectSse, 5000);
  };
}

function useRealtimeMode() {
  const override = new URLSearchParams(location.search).get("live");
  if (override !== null) return override !== "0";     // ?live=0 强制轮询
  return settings.realtime !== false;
}

function startProgressChannel() {
  if (useRealtimeMode()) connectSse();
  else startPolling("轮询模式（已关闭实时推送）");
}

/* ---------- 平台 / 自检 / 设置 ---------- */
let settings = {};
let doctor = {};                     // /doctor 的原始数据（含 ASR 可用性）
const asrReady = () => Boolean(doctor.asr && (doctor.asr.available || []).length);

async function loadPlatforms() {
  try {
    const data = await api("/platforms");
    const table = el("table", {}, [
      el("thead", {}, el("tr", {}, ["平台", "解析方式", "鉴权", "合并", "实测"].map((h) => el("th", { text: h })))),
    ]);
    const tbody = el("tbody");
    for (const row of data.platforms) {
      tbody.append(el("tr", {}, [
        el("td", { text: row.platform }),
        el("td", { text: row.resolver }),
        el("td", { text: row.auth }),
        el("td", { text: row.merge }),
        el("td", { text: row.verified }),
      ]));
    }
    table.append(tbody);
    $("#platforms-table").replaceChildren(table);
  } catch (err) { showError($("#platforms-table"), err); }
}

async function loadDoctor() {
  try {
    const data = await api("/doctor");
    doctor = data;
    const grid = el("div", { class: "grid" });
    const kv = (k, v) => el("div", { class: "kv" }, [el("span", { text: k }), el("span", { text: v ?? "—" })]);
    const asr = data.asr || {};
    grid.append(
      kv("yt-dlp", data.yt_dlp),
      kv("ffmpeg", data.ffmpeg || "未找到（B站/YouTube 无法合并）"),
      kv("ffprobe", data.ffprobe || "未找到（仅音频模式会退回「总是转码」，仍能出文件）"),
      kv("本地转录", (asr.available || []).length ? `${asr.available.join("/")} · 默认模型 ${asr.model}` : "未安装（uv sync --extra transcribe）"),
      kv("Python", data.python),
      kv("输出目录", data.out_dir),
      kv("provider 顺序", (data.providers || []).join(" → ")),
    );
    $("#doctor-grid").replaceChildren(grid);
    const status = $("#asr-status");
    if (status) {
      status.textContent = asrReady()
        ? `本地转录就绪：${asr.available.join("/")}（模型 ${asr.model}）`
        : "本地转录未安装：uv sync --extra transcribe 之后刷新本页即可勾选。";
      status.className = asrReady() ? "hint" : "hint warn";
    }
    const pill = $("#engine-pill");
    pill.textContent = data.ffmpeg ? `引擎 ${data.yt_dlp} · ffmpeg 就绪` : "ffmpeg 缺失";
    pill.className = `pill ${data.ffmpeg ? "pill-ok" : "pill-err"}`;

    const list = el("div", { class: "grid" });
    for (const row of data.cookies || []) {
      const ok = row.cookie.startsWith("已加载");
      list.append(el("div", { class: "kv" }, [
        el("span", { text: `${PLATFORM_LABEL[row.platform] || row.platform} cookie` }),
        el("span", { text: ok ? "已就绪" : "未提供", title: row.path }),
      ]));
    }
    $("#cookie-list").replaceChildren(list);
  } catch (err) {
    $("#engine-pill").textContent = "引擎状态未知";
    $("#engine-pill").className = "pill pill-err";
    toast(`自检失败：${err.message}`, "error");
  }
}

async function loadSettings() {
  settings = await api("/settings");
  $("#set-out-dir").value = settings.out_dir || "";
  $("#set-home").textContent = settings.home || "";
  $("#set-realtime").checked = settings.realtime !== false;
  $("#set-embed-tags").checked = settings.embed_tags !== false;
  const backendSelect = $("#set-asr-backend");
  const installed = (doctor.asr && doctor.asr.available) || [];
  backendSelect.replaceChildren(...(settings.asr_backend_choices || [""]).map((name) =>
    el("option", {
      value: name,
      text: name ? `${name}${installed.includes(name) ? "" : "（未安装）"}` : "自动（按偏好）",
      selected: (settings.asr_backend || "") === name ? "selected" : null,
    })));
  $("#set-asr-model").value = settings.asr_model || "";
  $("#set-asr-language").value = settings.asr_language || "";
  const audioSelect = $("#set-audio-format");
  audioSelect.replaceChildren(...(settings.audio_formats || ["m4a"]).map((name) => el("option", { value: name, text: name, selected: settings.audio_format === name ? "selected" : null })));
  const transcriptSelect = $("#set-transcript-format");
  transcriptSelect.replaceChildren(...(settings.transcript_formats || ["md"]).map((name) => el("option", { value: name, text: name, selected: settings.transcript_format === name ? "selected" : null })));
  const select = $("#set-browser");
  select.replaceChildren(...(settings.browser_choices || [""]).map((name) =>
    el("option", { value: name, text: name || "不读取浏览器 cookie", selected: (settings.cookies_from_browser || "") === name ? "selected" : null })));
}

$("#btn-save-settings").addEventListener("click", async () => {
  const wantRealtime = $("#set-realtime").checked;
  try {
    settings = await api("/settings", {
      method: "PUT",
      body: {
        out_dir: $("#set-out-dir").value.trim(),
        cookies_from_browser: $("#set-browser").value,
        realtime: wantRealtime,
        audio_format: $("#set-audio-format").value,
        transcript_format: $("#set-transcript-format").value,
        embed_tags: $("#set-embed-tags").checked,
        asr_backend: $("#set-asr-backend").value,
        asr_model: $("#set-asr-model").value.trim(),
        asr_language: $("#set-asr-language").value.trim(),
      },
    });
    toast("设置已保存", "ok");
    await loadDoctor();
    if (media) renderMedia(media);
    // 推送方式变了就立刻生效，不用刷新页面
    if (wantRealtime !== sseConnected && wantRealtime !== (pollTimer !== null)) startProgressChannel();
  } catch (err) {
    toast(`保存失败：${err.message}`, "error");
  }
});

$("#btn-refresh").addEventListener("click", async () => { await loadTasks(); toast("已刷新", "ok"); });
$("#btn-clear-finished").addEventListener("click", async () => {
  try {
    const res = await api("/tasks/clear-finished", { method: "POST" });
    await loadTasks();
    toast(`已清理 ${res.removed} 条`, "ok");
  } catch (err) { toast(err.message, "error"); }
});

/* ---------- 启动 ---------- */
(async function boot() {
  await Promise.all([loadSettings(), loadPlatforms(), loadDoctor(), loadTasks()]);
  refreshDetected();
  const hash = location.hash.replace("#", "");
  if (hash) activateTab(hash);
  startProgressChannel();

  // 深链：/?text=<分享文本> 直接解析；/?text=…&download=1 顺手建下载任务。
  // 用途：书签、shell 别名、把链接发给别人一键打开。
  const params = new URLSearchParams(location.search);
  const preset = params.get("text");
  if (preset) {
    $("#share-text").value = preset;
    refreshDetected();
    await extract();
    if (params.get("download") === "1" && media) {
      document.querySelector("#extract-result .btn.primary")?.click();
    }
  }
})();