// 全身实时通话页：选形象/照片 → 自动配人设和音色（/api/rt/profile）→ 建会话（/api/rt/live）→ 控制通道走服务端代理 WS → 媒体走阿里云 RTC
const $ = (id) => document.getElementById(id);
const pref = { get(k, d) { try { return localStorage.getItem("talk." + k) ?? d; } catch { return d; } },
               set(k, v) { try { localStorage.setItem("talk." + k, v); } catch {} } };
const S = { avatars: [], avatarId: pref.get("rt.avatar", ""), photo: "", live: null, ws: null, rtc: null, t0: 0, timer: 0,
            muted: false, ended: false, profSeq: 0, bg: pref.get("rt.bg", ""), outfit: pref.get("rt.outfit", ""),
            compose: "", composeSeq: 0, composeUrl: "" };

function userId() {                      // 和原来的通话页共用同一个用户编号（长期记忆按它区分）
  let u = pref.get("uid", "");
  if (!u) { u = "u" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36); pref.set("uid", u); }
  return u;
}
async function api(path, opt = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opt });
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch { d = { detail: t }; }
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}
const status = (t) => { $("status").textContent = t; };

// ---------------- 首页 ----------------
async function loadAvatars() {
  try {
    const list = await api("/ot/avatars");
    S.avatars = list.filter((a) => a.is_custom || a.person_mode !== "double");
  } catch (e) { status("形象列表加载失败：" + e.message); }
  if (!S.avatars.some((a) => a.id === S.avatarId)) S.avatarId = (S.avatars.find((a) => a.is_custom) || S.avatars[0] || {}).id || "";
  renderAvatars();
}
function renderAvatars() {
  const g = $("avatars"); g.innerHTML = "";
  for (const a of S.avatars) {
    const b = document.createElement("button");
    b.className = "avatar" + (a.id === S.avatarId && !S.photo ? " on" : "");
    b.innerHTML = `<img loading="lazy" src="/ot/avatars/${encodeURIComponent(a.id)}/preview" alt=""><span></span>`;
    b.querySelector("span").textContent = a.name || a.id;
    b.onclick = () => { S.avatarId = a.id; S.photo = ""; pref.set("rt.avatar", a.id); $("photoRow").hidden = true; renderAvatars(); loadProfile(); compose(); };
    g.appendChild(b);
  }
}
$("photo").onchange = () => {
  const f = $("photo").files[0];
  if (!f) return;
  if (f.size > 15e6) { status("照片太大（请小于 15MB）"); return; }
  const rd = new FileReader();
  shrink(f, 1600).then((uri) => { S.photo = uri; $("photoPreview").src = S.photo; $("photoRow").hidden = false; renderAvatars(); loadProfile(); compose(); });
};
$("photoClear").onclick = () => { S.photo = ""; $("photoRow").hidden = true; $("photo").value = ""; renderAvatars(); loadProfile(); compose(); };

// 图片压到长边 max 像素的 JPEG（手机原图十几 MB，传起来慢）
function shrink(file, max) {
  return new Promise((ok, bad) => {
    const img = new Image(); const url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, max / Math.max(img.naturalWidth, img.naturalHeight));
      const c = document.createElement("canvas");
      c.width = Math.round(img.naturalWidth * k); c.height = Math.round(img.naturalHeight * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url); ok(c.toDataURL("image/jpeg", 0.9));
    };
    img.onerror = () => { URL.revokeObjectURL(url); bad(new Error("图片读不出来")); };
    img.src = url;
  });
}

// ---------------- 预设背景 / 服装（接通前套上，通话中不再变） ----------------
async function loadPresets() {
  let p = { bg: [], outfit: [] };
  try { p = await api("/api/rt/presets"); } catch {}
  for (const [kind, box, key] of [["bg", "bgs", "bg"], ["outfit", "outfits", "outfit"]]) {
    const items = [{ id: "", name: "照片原样" }, ...(p[kind] || [])];
    if (!items.some((x) => x.id === S[key])) S[key] = "";
    const g = $(box); g.innerHTML = "";
    for (const it of items) {
      const b = document.createElement("button"); b.type = "button";
      b.className = it.id === S[key] ? "on" : "";
      b.innerHTML = it.id ? `<img loading="lazy" src="/api/rt/presets/${kind}/${it.id}.jpg" alt=""><span></span>`
                          : `<div class="none">不换</div><span></span>`;
      b.querySelector("span").textContent = it.name;
      b.onclick = () => { S[key] = it.id; pref.set("rt." + key, it.id); g.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); compose(); };
      g.appendChild(b);
    }
  }
}

// 选了背景/服装：先生成一张换好装的照片给你看（通话就用这张）
async function compose(again = false) {
  const seq = ++S.composeSeq;
  S.compose = ""; S.composeUrl = "";
  if (!(S.bg || S.outfit) || (!S.avatarId && !S.photo)) { $("composeRow").hidden = true; S.composing = false; refreshStart(); return; }
  $("composeRow").hidden = false; $("composePreview").src = S.photo || `/ot/avatars/${encodeURIComponent(S.avatarId)}/preview`;
  $("composeText").textContent = "正在换装，约半分钟…"; S.composing = true; refreshStart(); $("composeAgain").disabled = true;
  try {
    const r = await api("/api/rt/compose", { method: "POST", body: JSON.stringify({
      ...(S.photo ? { image: S.photo } : { avatar_id: S.avatarId }), bg: S.bg, outfit: S.outfit, again }) });
    if (seq !== S.composeSeq) return;
    S.compose = r.key; S.composeUrl = r.url; $("composePreview").src = r.url;
    $("composeText").textContent = "换好了，通话就用这张。不满意可以点右边换一张";
  } catch (e) {
    if (seq === S.composeSeq) $("composeText").textContent = "换装失败：" + e.message;
  } finally {
    if (seq === S.composeSeq) { S.composing = false; refreshStart(); $("composeAgain").disabled = false; }
  }
}
$("composeAgain").onclick = () => compose(true);

// ---------------- 人设和音色：形象有固定人设就用它，否则看照片自动配 ----------------
async function loadVoices(select) {
  try {
    const vs = await api("/api/rt/voices");
    const cur = select || $("voice").value;
    const groups = [["mine", "我克隆的声音"], ["our", "推荐音色"], ["builtin", "平台音色"]];
    const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    $("voice").innerHTML = groups.map(([g, label]) => {
      const items = vs.filter((v) => (v.group || "our") === g);
      return items.length ? `<optgroup label="${label}">${items.map((v) => `<option value="${esc(v.id)}">${esc(v.name || v.id)}</option>`).join("")}</optgroup>` : "";
    }).join("");
    if (cur && $("voice").querySelector(`option[value="${CSS.escape(cur)}"]`)) $("voice").value = cur;
  } catch {}
  voiceDelVisible();
}
function voiceDelVisible() { $("voiceDel").hidden = !String($("voice").value).startsWith("c:"); }
$("voice").onchange = voiceDelVisible;
$("voiceDel").onclick = async () => {
  const v = $("voice").value;
  if (!v.startsWith("c:")) return;
  $("voiceDel").disabled = true;
  try { await api(`/api/rt/voices/${encodeURIComponent(v)}`, { method: "DELETE" }); status("已删除这个克隆音色"); await loadVoices(); }
  catch (e) { status("删除失败：" + e.message); }
  finally { $("voiceDel").disabled = false; }
};

// ---------------- 克隆声音：录音或上传 → 服务端转码后克隆 ----------------
const C = { blob: null, rec: null, chunks: [], t0: 0, timer: 0 };
function cloneReady() { $("cloneSubmit").disabled = !C.blob; }
$("recBtn").onclick = async () => {
  if (C.rec && C.rec.state === "recording") { C.rec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    C.chunks = []; C.rec = new MediaRecorder(stream);
    C.rec.ondataavailable = (e) => { if (e.data.size) C.chunks.push(e.data); };
    C.rec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop()); clearInterval(C.timer);
      C.blob = new Blob(C.chunks, { type: C.rec.mimeType || "audio/webm" });
      const sec = Math.round((Date.now() - C.t0) / 1000);
      $("recInfo").textContent = sec < 8 ? `只录了 ${sec} 秒，建议 10~20 秒` : `录好了，${sec} 秒`;
      $("recBtn").textContent = "● 重新录音"; cloneReady();
    };
    C.rec.start(); C.t0 = Date.now(); $("recBtn").textContent = "■ 停止录音";
    C.timer = setInterval(() => {
      const sec = Math.round((Date.now() - C.t0) / 1000);
      $("recInfo").textContent = `正在录音… ${sec} 秒`;
      if (sec >= 30) C.rec.stop();
    }, 500);
  } catch (e) { $("recInfo").textContent = "打不开麦克风：" + e.message; }
};
$("cloneFile").onchange = () => {
  const f = $("cloneFile").files[0];
  if (!f) return;
  C.blob = f; $("recInfo").textContent = `已选文件：${f.name}`; cloneReady();
};
$("cloneSubmit").onclick = async () => {
  if (!C.blob) return;
  $("cloneSubmit").disabled = true; $("recInfo").textContent = "正在生成音色，约十几秒…";
  try {
    const fd = new FormData();
    fd.append("audio", C.blob, C.blob.name || "voice.webm");
    fd.append("name", $("cloneName").value.trim());
    fd.append("text", $("cloneText").textContent.trim());
    const r = await fetch("/api/rt/voices/clone", { method: "POST", body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || r.statusText);
    await loadVoices(d.id);
    $("recInfo").textContent = `已生成「${d.name}」，选上了`; $("cloneCell").open = false; C.blob = null;
  } catch (e) { $("recInfo").textContent = "生成失败：" + e.message; }
  finally { cloneReady(); }
};
async function loadProfile() {
  const seq = ++S.profSeq;
  if (!S.avatarId && !S.photo) return;
  $("profLine").textContent = "正在根据照片配人设和音色…"; $("profSrc").textContent = "";
  $("persona").value = ""; S.profLoading = true; refreshStart();
  try {
    const p = await api("/api/rt/profile", { method: "POST", body: JSON.stringify(S.photo ? { image: S.photo } : { avatar_id: S.avatarId }) });
    if (seq !== S.profSeq) return;
    $("persona").value = p.persona || "";
    if (p.voice && !$("voice").querySelector(`option[value="${p.voice}"]`)) {
      $("voice").insertAdjacentHTML("afterbegin", `<option value="${p.voice}">${p.voice_name || p.voice}</option>`);
    }
    if (p.voice && !/^[vc]:/.test($("voice").value)) $("voice").value = p.voice;
    voiceDelVisible();
    const who = p.source === "preset" ? (p.name || "固定人设") : [p.name, p.gender, p.age ? `约 ${p.age} 岁` : "", p.look].filter(Boolean).join(" · ");
    $("profLine").innerHTML = `<span><b></b>　音色：<b></b></span>`;
    $("profLine").querySelectorAll("b")[0].textContent = who || "自动配置";
    $("profLine").querySelectorAll("b")[1].textContent = p.voice_name || p.voice || "默认";
    $("profSrc").textContent = p.source === "preset" ? "（形象自带）" : "（看照片自动配的，可以改）";
  } catch (e) {
    if (seq === S.profSeq) $("profLine").textContent = "自动配置失败：" + e.message + "（可以直接开始，会用默认人设）";
  } finally {
    if (seq === S.profSeq) { S.profLoading = false; refreshStart(); }
  }
}

// ---------------- 通话 ----------------
function sub(text, me) {
  if (!text) return;
  const box = $("subs"); const p = document.createElement("p");
  p.className = me ? "me" : ""; p.textContent = text; box.appendChild(p);
  while (box.children.length > 3) box.firstChild.remove();
  clearTimeout(p._t); p._t = setTimeout(() => p.remove(), 9000);
}
function textOf(o) {                      // 字幕事件的 payload 结构没写死：取第一个像文本的字段
  if (!o || typeof o !== "object") return "";
  for (const k of ["text", "content", "transcript", "message"]) if (typeof o[k] === "string" && o[k].trim()) return o[k].trim();
  for (const v of Object.values(o)) { const t = textOf(v); if (t) return t; }
  return "";
}
function setState(t) { $("callState").textContent = t; }

// "开始通话"按钮：正在开始/通话中、人设还在配、换装照片还在生成时都不能点（之前后台加载完会把按钮重新点亮，
// 通话启动过程中能再点一次，建出两个会话同时计费，还会出现听到一个会话的声音、看到另一个会话的画面）
function refreshStart() { $("start").disabled = !!(S.busy || S.profLoading || S.composing); }

async function start() {
  if (S.busy) return;
  if (!S.avatarId && !S.photo) { status("先选一个形象或上传照片"); return; }
  const Engine = window.AliRtcEngine && (window.AliRtcEngine.default || window.AliRtcEngine);
  if (!Engine) { status("阿里云 RTC 组件没加载出来（网络拦截了 g.alicdn.com？）"); return; }
  S.busy = true; refreshStart(); status("正在创建通话…（约 5~15 秒）");
  S.ended = false;
  try {
    S.live = await api("/api/rt/live", { method: "POST", body: JSON.stringify({
      avatar_id: S.photo ? "" : S.avatarId, image: S.photo || undefined, uid: userId(),
      persona: $("persona").value.trim() || undefined, voice: $("voice").value || undefined,
      compose: (S.bg || S.outfit) ? S.compose || undefined : undefined }) });
  } catch (e) { status("创建失败：" + e.message); S.busy = false; refreshStart(); return; }
  $("home").hidden = true; $("call").hidden = false;
  $("waitImg").src = (S.bg || S.outfit) && S.composeUrl ? S.composeUrl : S.photo || `/ot/avatars/${encodeURIComponent(S.avatarId)}/preview`;
  $("waitText").textContent = "正在连接…"; $("wait").hidden = false; $("subs").innerHTML = "";
  openWs();
  try { await joinRtc(Engine); }
  catch (e) { console.error(e); $("waitText").textContent = "音视频连接失败：" + (e.message || e); setState("失败"); }
}

function openWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/rt/ws/${S.live.live_id}`);
  S.ws = ws;
  ws.onmessage = (ev) => {
    let d; try { d = JSON.parse(ev.data); } catch { return; }
    if (d.type === "error") { $("waitText").textContent = d.msg; return; }
    const p = d.payload || {};
    if (d.type === 2) {
      const ack = p.conn_init_ack || {};
      setState(ack.success ? "已连接" : "等待数字人就绪…");
    } else if (d.type === 6) {
      const r = (p.hangup || {}).hangup_reason || "";
      end({ timeout: "长时间没说话，已自动挂断", credit_insufficient: "通话额度不足", replaced: "已在别处开始了新的通话", audit_violation: "内容触发风控，已挂断" }[r]
          || "通话已结束" + (r ? `（${r}）` : ""));
    } else if (d.type === 9) {
      sub(textOf(p), true);
    } else if (d.type === 10) {
      sub(textOf(p), false);
    }
  };
  ws.onclose = () => { if (!S.ended) setState("控制通道已断开"); };
}
const send = (o) => { if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify(o)); };

async function joinRtc(Engine) {
  const e = Engine.getInstance();
  S.rtc = e;
  e.on("videoSubscribeStateChanged", (userId, oldState, newState) => {
    if (newState === 3) {                // 订阅到数字人的画面
      e.setRemoteViewConfig($("remote"), userId, 1);
      onVideo();
    } else if (newState === 1) {
      e.setRemoteViewConfig(null, userId, 1);
    }
  });
  e.on("remoteUserOnLineNotify", () => { $("waitText").textContent = "数字人已进入，正在出画面…"; });
  e.on("remoteUserOffLineNotify", () => { if (!S.ended) end("数字人已离开"); });
  e.on("bye", () => { if (!S.ended) end("通话已结束"); });
  // 不推自己的摄像头（只要麦克风）：数字人照样能听、能说
  try { await e.publishLocalVideoStream?.(false); } catch {}
  try { await e.enableCamera?.(false); } catch {}
  await e.joinChannel(S.live.rtc.token, S.live.rtc.user_id);
  try { await e.publishLocalVideoStream?.(false); } catch {}
  $("waitText").textContent = "已进入房间，等待数字人…";
}

// 画面真正开始播放就收起"连接中"的头像遮罩（只靠订阅事件不可靠：有的手机上事件参数不同，画面出来了遮罩还盖着）
function onVideo() {
  $("wait").hidden = true; setState("通话中");
  if (!S.t0) { S.t0 = Date.now(); S.timer = setInterval(tick, 1000); }
}
document.addEventListener("playing", (ev) => {
  if (ev.target && ev.target.tagName === "VIDEO" && $("call").contains(ev.target) && !S.ended) onVideo();
}, true);

function tick() {
  const s = Math.floor((Date.now() - S.t0) / 1000);
  $("timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")} · 约 ${Math.round(s * 1.5)} 点`;
}

async function end(msg) {
  if (S.ended) return;
  S.ended = true;
  send({ type: 5 });
  try { S.ws && S.ws.close(); } catch {}
  try { await S.rtc?.leaveChannel(); } catch {}
  try { S.rtc?.destroy(); } catch {}
  S.rtc = null; S.ws = null;
  clearInterval(S.timer); S.timer = 0; S.t0 = 0;
  $("call").hidden = true; $("home").hidden = false; S.busy = false; refreshStart();
  status(msg || "通话已结束");
}

$("start").onclick = start;
$("hang").onclick = () => end("已挂断");
$("interrupt").onclick = () => send({ type: 7 });
$("mute").onclick = async () => {
  S.muted = !S.muted;
  try { await S.rtc?.muteLocalMic?.(S.muted); } catch {}
  $("mute").classList.toggle("off", S.muted);
  $("mute").innerHTML = S.muted ? "<b>🔇</b>已静音" : "<b>🎤</b>静音";
};
window.addEventListener("pagehide", () => { if (!S.ended) send({ type: 5 }); });
loadPresets().then(() => loadVoices()).then(loadAvatars).then(() => { loadProfile(); compose(); });
