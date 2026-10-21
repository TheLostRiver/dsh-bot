/* dsh-bot 管理后台前端 —— 无构建步骤，改完刷新即可 */

const $ = (id) => document.getElementById(id);

let logSeq = 0;
let loginTimer = null;
let statusTimer = null;
let settingsLoaded = false;
let botRunning = false;
let savedVoice = "";

/* ---------------------------------------------------------------- 工具 */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try { body = await res.json(); } catch { /* 空响应 */ }
  if (!res.ok) {
    const detail = body && (body.detail || body.message);
    throw new Error(typeof detail === "string" ? detail : `HTTP ${res.status}`);
  }
  return body;
}

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function fmtUptime(sec) {
  if (!sec) return "—";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h${m}m` : `${m}m`;
}

/* ---------------------------------------------------------------- 状态 */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const acc = s.account || {};
    const room = s.room || {};
    const bot = s.bot || {};

    const dot = $("conn-dot");
    dot.className = "dot " + (bot.running ? "on" : (acc.configured ? "mid" : ""));

    $("pill-account").textContent = acc.name
      ? `账号：${acc.name} Lv${acc.level ?? "?"}`
      : "账号：未登录";
    $("pill-room").textContent = room.id
      ? `直播间：${room.id}${room.live ? " · 直播中" : " · 未开播"}`
      : "直播间：未配置";

    botRunning = !!bot.running;
    const btn = $("btn-toggle");
    btn.textContent = botRunning ? "停止" : "启动";
    btn.classList.toggle("primary", !botRunning);
    btn.classList.toggle("danger", botRunning);

    $("st-replied").textContent = bot.replied ?? 0;
    $("st-ignored").textContent = bot.ignored ?? 0;
    $("st-considered").textContent = bot.considered ?? 0;
    $("st-queued").textContent = bot.queued ?? 0;
    $("st-sent").textContent = (s.usage && s.usage.danmaku_sent) || 0;
    $("st-uptime").textContent = fmtUptime(bot.uptime || 0);

    // 概览页：今天的量
    const br = s.browse || {};
    const t = br.today || {};
    $("ov-like").textContent = t.like || 0;
    $("ov-fav").textContent = t.favorite || 0;
    $("ov-comment").textContent = t.comment || 0;
    $("ov-seen").textContent = br.seen || 0;
    $("ov-actions").textContent = br.actions_ok || 0;
    $("ov-creply").textContent = (s.comments && s.comments.replied) || 0;
    $("ov-live-state").textContent = bot.running
      ? `运行中 · 已回 ${bot.replied ?? 0}`
      : "未启动";

    // 顶部横幅：把风险说清楚
    const banner = $("banner");
    const msgs = [];
    let bad = false;
    if (acc.warnings && acc.warnings.length) {
      msgs.push(...acc.warnings);
      bad = acc.configured && acc.valid === false;
    }
    if (bot.error) { msgs.push(`机器人异常退出：${bot.error}`); bad = true; }
    if (!s.llm.ok) { msgs.push("DeepSeek API Key 未配置，AI 无法思考"); bad = true; }
    if (msgs.length) {
      banner.textContent = msgs.join("　·　");
      banner.className = "banner" + (bad ? " bad" : "");
    } else {
      banner.className = "banner hidden";
    }

    const st = $("login-state");
    if (acc.configured && acc.valid) {
      st.className = "login-state ok";
      st.textContent = `已登录：${acc.name}（uid=${acc.uid}）`;
    } else if (acc.configured) {
      st.className = "login-state bad";
      st.textContent = "凭据已失效，请重新扫码";
    }
  } catch (e) {
    $("conn-dot").className = "dot";
    console.warn("状态刷新失败", e);
  }
}

/* ---------------------------------------------------------------- 登录 */
async function startLogin() {
  const btn = $("btn-login");
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const r = await api("/api/login/start", { method: "POST" });
    $("qr-img").src = r.qr;
    $("qr-img").classList.remove("hidden");
    $("qr-placeholder").classList.add("hidden");
    $("login-state").className = "login-state";
    $("login-state").textContent = "等待扫码…";
    btn.textContent = "刷新二维码";
    pollLogin(r.key);
  } catch (e) {
    toast("生成二维码失败：" + e.message);
    btn.textContent = "生成二维码";
  } finally {
    btn.disabled = false;
  }
}

function pollLogin(key) {
  clearInterval(loginTimer);
  loginTimer = setInterval(async () => {
    try {
      const r = await api(`/api/login/poll?key=${encodeURIComponent(key)}`);
      const st = $("login-state");
      if (r.state === "ok") {
        clearInterval(loginTimer);
        st.className = "login-state ok";
        st.textContent = r.account && r.account.name
          ? `已登录：${r.account.name}`
          : "登录成功";
        toast("登录成功");
        refreshStatus();
      } else if (r.state === "expired" || r.state === "error") {
        clearInterval(loginTimer);
        st.className = "login-state bad";
        st.textContent = r.message || "二维码已过期，请刷新";
      } else if (r.state === "scanned") {
        st.className = "login-state";
        st.textContent = "已扫码，请在手机上确认";
      }
    } catch (e) {
      clearInterval(loginTimer);
      $("login-state").className = "login-state bad";
      $("login-state").textContent = "轮询失败：" + e.message;
    }
  }, 2000);
}

/* ---------------------------------------------------------------- 设置 */
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("s-room").value = s.room_id ?? 0;
    $("s-dryrun").checked = !!s.dry_run;

    $("p-name").value = s.persona.name || "";
    $("p-bio").value = s.persona.bio || "";
    $("p-style").value = s.persona.style || "";
    $("p-catch").value = s.persona.catchphrase || "";
    $("p-aliases").value = (s.persona.aliases || []).join(",");

    $("t-prob").value = s.trigger.reply_probability;
    $("t-prob-out").textContent = Number(s.trigger.reply_probability).toFixed(2);
    $("t-question").checked = !!s.trigger.reply_on_question;
    $("t-ignore").value = (s.trigger.ignore_patterns || []).join("\n");

    $("g-interval").value = s.guard.min_interval;
    $("g-minute").value = s.guard.per_minute;
    $("g-hour").value = s.guard.per_hour;
    $("g-day").value = s.guard.per_day;
    $("g-user").value = s.guard.user_cooldown;
    $("g-len").value = s.guard.max_danmaku_len;

    $("v-enabled").checked = !!s.speech.enabled;    $("v-backend").value = s.speech.backend;
    savedVoice = s.speech.voice || "";
    $("v-rate").value = s.speech.rate || "";
    $("v-pitch").value = s.speech.pitch || "+0Hz";
    $("v-max").value = s.speech.max_chars;

    if (s.comments) {      $("c-enabled").checked = !!s.comments.enabled;
      $("c-upmid").value = s.comments.up_mid;
      $("c-videos").value = s.comments.videos;
      $("c-interval").value = s.comments.poll_interval;
      $("c-lookback").value = s.comments.lookback_hours;
      $("c-day").value = s.comments.max_per_day;
      $("c-gap").value = s.comments.min_interval;
      $("c-len").value = s.comments.max_len;
      $("c-prob").value = s.comments.reply_probability;
    }

    if (s.browse) {
      $("b-keywords").value = (s.browse.keywords || []).join(",");
      selectedZones = new Set(s.browse.zones || []);
      buildZoneChips();
      $("b-batch").value = s.browse.batch;
      $("b-llm").value = s.browse.max_llm;
      $("b-order").value = s.browse.order || "pubdate";
      $("b-days").value = s.browse.days;
      $("b-interval").value = s.browse.interval;
      $("b-folder").value = s.browse.favorite_folder || "";
      for (const [key, prefix] of ACTIONS) {
        const spec = s.browse[key] || {};
        $(`${prefix}-enabled`).checked = !!spec.enabled;
        $(`${prefix}-day`).value = spec.per_day ?? 0;
        $(`${prefix}-prob`).value = spec.probability ?? 0;
      }
    }

    settingsLoaded = true;
  } catch (e) {
    toast("读取设置失败：" + e.message);
  }
}

function collectSettings() {
  const num = (id, d = 0) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : d;
  };
  const out = {
    room_id: Math.max(0, Math.trunc(num("s-room"))),
    dry_run: $("s-dryrun").checked,
    persona: {
      name: $("p-name").value.trim(),
      bio: $("p-bio").value.trim(),
      style: $("p-style").value.trim(),
      catchphrase: $("p-catch").value.trim(),
      aliases: $("p-aliases").value.split(",").map((x) => x.trim()).filter(Boolean),
    },
    trigger: {
      reply_probability: num("t-prob", 0.12),
      reply_on_question: $("t-question").checked,
      ignore_patterns: $("t-ignore").value
        .split("\n").map((x) => x.trim()).filter(Boolean),
    },
    guard: {
      min_interval: num("g-interval", 12),
      per_minute: Math.trunc(num("g-minute", 3)),
      per_hour: Math.trunc(num("g-hour", 25)),
      per_day: Math.trunc(num("g-day", 180)),
      user_cooldown: num("g-user", 240),
      max_danmaku_len: Math.trunc(num("g-len", 20)),
    },
    speech: {
      enabled: $("v-enabled").checked,
      backend: $("v-backend").value,
      voice: currentVoice(),
      rate: $("v-rate").value.trim() || "+8%",
      pitch: $("v-pitch").value.trim() || "+0Hz",
      max_chars: Math.trunc(num("v-max", 80)),
    },
    comments: {
      enabled: $("c-enabled").checked,
      up_mid: Math.max(0, Math.trunc(num("c-upmid", 0))),
      videos: Math.trunc(num("c-videos", 5)),
      poll_interval: num("c-interval", 300),
      lookback_hours: num("c-lookback", 72),
      max_per_day: Math.trunc(num("c-day", 20)),
      min_interval: num("c-gap", 90),
      max_len: Math.trunc(num("c-len", 60)),
      reply_probability: num("c-prob", 0.5),
    },
    browse: {
      interval: num("b-interval", 1800),
      batch: Math.trunc(num("b-batch", 6)),
      max_llm: Math.trunc(num("b-llm", 3)),
      zones: Array.from(selectedZones),
      keywords: $("b-keywords").value.split(",").map((x) => x.trim()).filter(Boolean),
      days: Math.trunc(num("b-days", 7)),
      order: $("b-order").value,
      max_subtitle_chars: 6000,
      favorite_folder: $("b-folder").value.trim() || "AI 精选",
    },
  };

  for (const [key, prefix] of ACTIONS) {
    out.browse[key] = {
      enabled: $(`${prefix}-enabled`).checked,
      per_day: Math.trunc(num(`${prefix}-day`, 0)),
      probability: num(`${prefix}-prob`, 0),
    };
  }
  return out;
}

async function saveSettings() {
  const btn = $("btn-save");
  btn.disabled = true;
  try {
    const r = await api("/api/settings", {
      method: "POST",
      body: JSON.stringify(collectSettings()),
    });
    toast(r.restart_required ? "已保存 —— 重启机器人后生效" : "已保存");
    $("save-hint").textContent = "已保存 " + new Date().toLocaleTimeString();
    refreshStatus();
  } catch (e) {
    toast("保存失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 日志 */
async function refreshLogs() {
  try {
    const r = await api(`/api/logs?after=${logSeq}`);
    if (!r.items.length) return;
    const box = $("logbox");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    for (const it of r.items) {
      const div = document.createElement("div");
      div.className = "logline";
      div.innerHTML =
        `<span class="t">${it.ts}</span>` +
        `<span class="lv lv-${it.level}">${it.level.slice(0, 4)}</span>` +
        `<span class="m"></span>`;
      div.querySelector(".m").textContent = `[${it.logger}] ${it.msg}`;
      box.appendChild(div);
    }
    while (box.childElementCount > 400) box.removeChild(box.firstChild);
    logSeq = r.last;
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch { /* 日志拉取失败不打扰用户 */ }
}

/* ---------------------------------------------------------------- 回复 */
async function refreshReplies() {
  try {
    const r = await api("/api/replies?limit=20");
    const box = $("replybox");
    if (!r.items || !r.items.length) return;
    box.innerHTML = "";
    for (const it of r.items) {
      const div = document.createElement("div");
      div.className = "reply " + (it.ok ? "ok" : "fail");
      const who = document.createElement("div");
      who.className = "who";
      const t = new Date(it.ts * 1000).toLocaleTimeString();
      who.textContent = `${t} · ${it.uname || "观众"}：${it.incoming || ""}`;
      const out = document.createElement("div");
      out.className = "out";
      out.textContent = "→ " + (it.danmaku || "");
      div.append(who, out);
      if (!it.ok && it.error) {
        const err = document.createElement("div");
        err.className = "err";
        err.textContent = "发送失败：" + it.error;
        div.appendChild(err);
      }
      box.appendChild(div);
    }
  } catch { /* 同上 */ }
}

/* ---------------------------------------------------------------- 语音 */
async function loadVoices() {
  const sel = $("v-voice");
  try {
    const r = await api("/api/voices");
    const items = r.items || [];
    sel.innerHTML = "";

    const zh = items.filter((v) => v.zh);
    const other = items.filter((v) => !v.zh);
    const addGroup = (label, list) => {
      if (!list.length) return;
      const g = document.createElement("optgroup");
      g.label = label;
      for (const v of list) {
        const o = document.createElement("option");
        o.value = v.id;
        o.textContent = `${v.name} · ${v.gender}`;
        g.appendChild(o);
      }
      sel.appendChild(g);
    };
    addGroup("中文", zh);
    addGroup("其他语言", other);

    $("voice-count").textContent = `共 ${items.length} 个音色 · 中文 ${zh.length}`;
    applyVoice(savedVoice);
  } catch (e) {
    $("voice-count").textContent = "音色列表加载失败";
    toast("音色列表加载失败：" + e.message);
  }
}

/** 把已保存的音色还原到控件上：列表里有就选中，没有就填进自定义框 */
function applyVoice(saved) {
  const sel = $("v-voice");
  const custom = $("v-voice-custom");
  const ids = Array.from(sel.options).map((o) => o.value);

  if (saved && ids.includes(saved)) {
    sel.value = saved;
    custom.value = "";
  } else if (saved) {
    custom.value = saved;
    if (ids.length) sel.value = ids[0];
  } else if (ids.length) {
    sel.value = ids[0];
  }
}

function currentVoice() {
  const custom = $("v-voice-custom").value.trim();
  return custom || $("v-voice").value || savedVoice;
}

async function previewVoice() {
  const voice = currentVoice();
  const hint = $("preview-hint");
  const btn = $("btn-preview");
  if (!voice) { toast("先选一个音色"); return; }

  btn.disabled = true;
  hint.textContent = "合成中…";
  try {
    const url =
      `/api/tts/preview?voice=${encodeURIComponent(voice)}` +
      `&rate=${encodeURIComponent($("v-rate").value.trim() || "+8%")}` +
      `&pitch=${encodeURIComponent($("v-pitch").value.trim() || "+0Hz")}`;
    const res = await fetch(url);
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail; } catch { /* 非 JSON */ }
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const audio = new Audio(URL.createObjectURL(await res.blob()));
    audio.onended = () => { hint.textContent = "试听结束"; btn.disabled = false; };
    await audio.play();
    hint.textContent = `正在播放：${voice}`;
  } catch (e) {
    hint.textContent = "试听失败：" + e.message;
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 人设预览 */
function openPersonaModal() {
  $("persona-modal").classList.remove("hidden");
  const t = $("pv-text");
  if (!t.value.trim()) t.value = "主播今天播到几点啊？";
  t.focus();
  t.select();
}

function closePersonaModal() {
  $("persona-modal").classList.add("hidden");
}

function addPreviewCard(r) {
  const box = $("pv-results");
  const card = document.createElement("div");
  card.className = "pv-item" + (r.should_reply ? "" : " skip");

  const q = document.createElement("div");
  q.className = "pv-q";
  q.textContent = `观众：${r.input}`;

  const a = document.createElement("div");
  a.className = "pv-a";
  a.textContent = r.should_reply ? `弹幕 → ${r.danmaku}` : "（选择沉默，不回这条）";
  card.append(q, a);

  if (r.should_reply && r.speech) {
    const s = document.createElement("div");
    s.className = "pv-s";
    s.textContent = `语音 → ${r.speech}`;
    card.appendChild(s);
  }

  const meta = document.createElement("div");
  meta.className = "pv-meta";
  meta.textContent = `${r.elapsed_ms}ms · ${r.model} · ${r.mood}`;
  card.appendChild(meta);

  box.prepend(card);
  while (box.childElementCount > 6) box.removeChild(box.lastChild);
}

async function runPersonaPreview() {
  const btn = $("btn-pv-run");
  const text = $("pv-text").value.trim();
  btn.disabled = true;
  btn.textContent = "生成中…";
  try {
    const settings = collectSettings();
    const r = await api("/api/persona/preview", {
      method: "POST",
      body: JSON.stringify({ text, persona: settings.persona }),
    });
    addPreviewCard(r);
  } catch (e) {
    const box = $("pv-results");
    const div = document.createElement("div");
    div.className = "pv-item bad";
    div.textContent = "试说失败：" + e.message;
    box.prepend(div);
  } finally {
    btn.disabled = false;
    btn.textContent = "试说（Ctrl+Enter）";
  }
}

/* ---------------------------------------------------------------- 评论区 */
let commentsRunning = false;

async function refreshComments() {
  try {
    const c = await api("/api/comments/status");
    commentsRunning = !!c.running;
    const btn = $("btn-comment-toggle");
    btn.textContent = commentsRunning ? "停止" : "启动";
    btn.classList.toggle("primary", !commentsRunning);
    btn.classList.toggle("danger", commentsRunning);

    const parts = [];
    parts.push(commentsRunning ? "值班中" : "未启动");
    if (c.up_name) parts.push(`主播 ${c.up_name}`);
    if (c.scans) parts.push(`已扫 ${c.scans} 轮`);
    if (c.seen) parts.push(`见过 ${c.seen} 条`);
    if (c.replied) parts.push(`已回 ${c.replied} 条`);
    if (c.last_scan_at) parts.push(new Date(c.last_scan_at * 1000).toLocaleTimeString() + " 扫过");
    $("c-stats").textContent = parts.join(" · ");
  } catch { /* 忽略 */ }
}

async function toggleComments() {
  const btn = $("btn-comment-toggle");
  btn.disabled = true;
  try {
    if (commentsRunning) {
      await api("/api/comments/stop", { method: "POST" });
      toast("评论区值班已停止");
    } else {
      await api("/api/comments/start", { method: "POST" });
      toast("评论区值班已启动");
    }
    await refreshComments();
  } catch (e) {
    toast("操作失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function scanComments() {
  const btn = $("btn-comment-scan");
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "扫描中…";
  try {
    const r = await api("/api/comments/scan", { method: "POST" });
    const x = r.result || {};
    if (x.error) {
      toast("扫描出错：" + x.error);
    } else {
      toast(`扫了 ${x.videos} 个视频 / ${x.comments} 条评论，新候选 ${x.candidates}，回复 ${x.replied}`);
    }
    await refreshComments();
    await refreshCommentReplies();
  } catch (e) {
    toast("扫描失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function refreshCommentReplies() {
  try {
    const r = await api("/api/comments/replies?limit=10");
    const box = $("comment-replies");
    if (!r.items || !r.items.length) return;
    box.innerHTML = "";
    for (const it of r.items) {
      const div = document.createElement("div");
      div.className = "reply " + (it.ok ? "ok" : "fail");
      const who = document.createElement("div");
      who.className = "who";
      who.textContent = new Date(it.ts * 1000).toLocaleString() + " · " + (it.incoming || "");
      const out = document.createElement("div");
      out.className = "out";
      out.textContent = "→ " + (it.reply || "");
      div.append(who, out);
      if (!it.ok && it.error) {
        const err = document.createElement("div");
        err.className = "err";
        err.textContent = "发送失败：" + it.error;
        div.appendChild(err);
      }
      box.appendChild(div);
    }
  } catch { /* 忽略 */ }
}

/* ---------------------------------------------------------------- 路由 */
const ROUTES = {
  overview: "概览",
  live: "直播互动",
  comments: "评论区",
  browse: "自动逛 B 站",
  persona: "人设",
  settings: "设置",
  logs: "日志",
};

function currentRoute() {
  const h = (location.hash || "").replace(/^#\/?/, "");
  return ROUTES[h] ? h : "overview";
}

function applyRoute() {
  const r = currentRoute();
  document.querySelectorAll(".nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === r)
  );
  document.querySelectorAll(".page").forEach((p) =>
    p.classList.toggle("active", p.dataset.page === r)
  );
  $("page-title").textContent = ROUTES[r];
  // 概览和日志没有可编辑项，藏掉保存栏
  $("savebar").classList.toggle("hidden", r === "logs" || r === "overview");

  if (r === "browse") {
    refreshBrowse();
    refreshBrowseActions();
    refreshBrowseReport();
  }
  if (r === "logs") refreshLogs();
}

/* ---------------------------------------------------------------- 自动逛 B 站 */
const ZONE_LIST = [
  ["Game", "游戏"], ["Knowledge", "知识"], ["Technology", "科技"],
  ["Douga", "动画"], ["Kichiku", "鬼畜"], ["Music", "音乐"],
  ["Dance", "舞蹈"], ["Life", "生活"], ["Food", "美食"],
  ["Ent", "娱乐"], ["Cinephile", "影视"], ["Car", "汽车"],
  ["Sports", "运动"], ["Animal", "动物"], ["Documentary", "纪录片"],
];

const ACTIONS = [
  ["like", "b-like"],
  ["favorite", "b-fav"],
  ["coin", "b-coin"],
  ["follow", "b-follow"],
  ["comment", "b-comment"],
];

let selectedZones = new Set();
let browseRunning = false;

function buildZoneChips() {
  const box = $("b-zones");
  box.innerHTML = "";
  for (const [name, label] of ZONE_LIST) {
    const el = document.createElement("span");
    el.className = "chip" + (selectedZones.has(name) ? " on" : "");
    el.textContent = label;
    el.addEventListener("click", () => {
      if (selectedZones.has(name)) selectedZones.delete(name);
      else selectedZones.add(name);
      el.classList.toggle("on");
    });
    box.appendChild(el);
  }
}

async function refreshBrowse() {
  try {
    const b = await api("/api/browse/status");
    browseRunning = !!b.running;
    const btn = $("btn-browse-toggle");
    btn.textContent = browseRunning ? "停止" : "启动";
    btn.classList.toggle("primary", !browseRunning);
    btn.classList.toggle("danger", browseRunning);

    const t = b.today || {};
    const bits = [browseRunning ? "逛站中" : "未启动"];
    if (b.rounds) bits.push(`已逛 ${b.rounds} 轮`);
    if (b.seen) bits.push(`看过 ${b.seen} 条`);
    const acts = Object.entries(t).map(([k, v]) => `${k} ${v}`).join(" · ");
    if (acts) bits.push(acts);
    $("b-stats").textContent = bits.join(" · ");

    $("ov-like").textContent = t.like || 0;
    $("ov-fav").textContent = t.favorite || 0;
    $("ov-comment").textContent = t.comment || 0;
    $("ov-seen").textContent = b.seen || 0;
    $("ov-actions").textContent = b.actions_ok || 0;
  } catch { /* 忽略 */ }
}

async function toggleBrowse() {
  const btn = $("btn-browse-toggle");
  btn.disabled = true;
  try {
    if (browseRunning) {
      await api("/api/browse/stop", { method: "POST" });
      toast("已停止逛站");
    } else {
      await api("/api/browse/start", { method: "POST" });
      toast("已开始逛站");
    }
    await refreshBrowse();
  } catch (e) {
    toast("操作失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function runBrowseRound() {
  const btn = $("btn-browse-round");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "逛站中…";
  try {
    const r = await api("/api/browse/round", { method: "POST" });
    const x = r.result || {};
    if (x.error) toast("出错：" + x.error);
    else toast(`找到 ${x.found} 条 / 新 ${x.fresh} / 判断 ${x.judged} / 值得 ${x.worth} / 动作 ${(x.actions || []).length}`);
    await refreshBrowse();
    await refreshBrowseActions();
    await refreshBrowseReport();
  } catch (e) {
    toast("失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

async function refreshBrowseActions() {
  try {
    const r = await api("/api/browse/actions?limit=20");
    const box = $("browse-actions");
    if (!r.items || !r.items.length) return;
    box.innerHTML = "";
    const NAMES = { like: "点赞", favorite: "收藏", coin: "投币", follow: "关注", comment: "评论" };
    for (const it of r.items) {
      const div = document.createElement("div");
      div.className = "reply " + (it.ok ? "ok" : "fail");
      const who = document.createElement("div");
      who.className = "who";
      who.textContent =
        `${new Date(it.ts * 1000).toLocaleTimeString()} · ${NAMES[it.action] || it.action}` +
        (it.title ? ` · ${String(it.title).slice(0, 26)}` : "");
      div.appendChild(who);
      const out = document.createElement("div");
      out.className = "out";
      out.textContent = it.ok ? "✅ 已执行" : `❌ ${it.error || "未执行"}`;
      div.appendChild(out);
      box.appendChild(div);
    }
  } catch { /* 忽略 */ }
}

async function refreshBrowseReport() {
  try {
    const r = await api("/api/browse/report");
    $("browse-report").textContent = r.exists ? r.content : "今天还没有逛站记录。";
  } catch { /* 忽略 */ }
}

/* ---------------------------------------------------------------- 交互 */
async function toggleBot() {
  const btn = $("btn-toggle");
  btn.disabled = true;
  try {
    if (botRunning) {
      await api("/api/bot/stop", { method: "POST" });
      toast("已停止");
    } else {
      const dry = $("s-dryrun").checked;
      await api("/api/bot/start", {
        method: "POST",
        body: JSON.stringify({ dry_run: dry }),
      });
      toast(dry ? "已启动（演练模式，不会真发弹幕）" : "已启动");
    }
    await refreshStatus();
  } catch (e) {
    toast("操作失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 启动 */
function bind() {
  $("btn-login").addEventListener("click", startLogin);
  $("btn-logout").addEventListener("click", async () => {
    if (!confirm("退出登录会清除本机保存的凭据，确定？")) return;
    try {
      await api("/api/logout", { method: "POST" });
      $("qr-img").classList.add("hidden");
      $("qr-placeholder").classList.remove("hidden");
      $("login-state").textContent = "未登录";
      $("login-state").className = "login-state";
      toast("已退出登录");
      refreshStatus();
    } catch (e) { toast("退出失败：" + e.message); }
  });

  $("btn-toggle").addEventListener("click", toggleBot);
  $("btn-browse-toggle").addEventListener("click", toggleBrowse);
  $("btn-browse-round").addEventListener("click", runBrowseRound);
  $("btn-comment-toggle").addEventListener("click", toggleComments);
  $("btn-comment-scan").addEventListener("click", scanComments);
  $("btn-save").addEventListener("click", saveSettings);
  $("btn-preview").addEventListener("click", previewVoice);

  $("btn-preview-persona").addEventListener("click", openPersonaModal);  $("btn-pv-close").addEventListener("click", closePersonaModal);
  $("btn-pv-run").addEventListener("click", runPersonaPreview);
  $("btn-pv-clear").addEventListener("click", () => { $("pv-results").innerHTML = ""; });
  $("persona-modal").addEventListener("click", (e) => {
    if (e.target.id === "persona-modal") closePersonaModal();
  });
  $("pv-text").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runPersonaPreview();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("persona-modal").classList.contains("hidden")) {
      closePersonaModal();
    }
  });
  $("v-voice-custom").addEventListener("input", (e) => {
    // 一旦填了自定义 ID，就把上面的选择清掉，避免两个值打架
    if (e.target.value.trim()) $("v-voice").selectedIndex = -1;
  });
  $("btn-clear-log").addEventListener("click", () => { $("logbox").innerHTML = ""; });

  $("t-prob").addEventListener("input", (e) => {
    $("t-prob-out").textContent = Number(e.target.value).toFixed(2);
  });

  // Ctrl+S 保存
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") {
      e.preventDefault();
      saveSettings();
    }
  });
}

(async function init() {
  bind();
  await loadSettings();
  await loadVoices();
  await refreshStatus();
  applyRoute();
  refreshLogs();
  refreshReplies();
  refreshComments();
  refreshCommentReplies();

  window.addEventListener("hashchange", applyRoute);
  statusTimer = setInterval(refreshStatus, 3000);
  setInterval(refreshLogs, 2000);
  setInterval(refreshReplies, 8000);
  setInterval(refreshComments, 5000);
})();
