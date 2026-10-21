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

    $("v-enabled").checked = !!s.speech.enabled;
    $("v-backend").value = s.speech.backend;
    savedVoice = s.speech.voice || "";
    $("v-rate").value = s.speech.rate || "";
    $("v-pitch").value = s.speech.pitch || "+0Hz";
    $("v-max").value = s.speech.max_chars;

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
  return {
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
  };
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
  $("btn-save").addEventListener("click", saveSettings);
  $("btn-preview").addEventListener("click", previewVoice);
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
  refreshLogs();
  refreshReplies();

  statusTimer = setInterval(refreshStatus, 3000);
  setInterval(refreshLogs, 2000);
  setInterval(refreshReplies, 8000);
})();
