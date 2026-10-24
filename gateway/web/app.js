// 数字人通话 demo 前端：选/建形象 → 建 OpenTalking 会话 → WebRTC 收音视频 → 麦克风分段推流对话
const $ = (id) => document.getElementById(id);
const OT = "/ot";
const S = {
  cfg: null, avatars: [], avatarId: null, provider: null,
  sid: null, pc: null, es: null, speaking: false,
  mic: null, // {ctx, stream, node, src}
  seg: null, // 当前语音分段 {ws, ready, queue[]}
};

function toast(msg, ms = 2600) {
  const t = $("toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
async function api(path, opt = {}) {
  const r = await fetch(path, opt);
  const body = await r.text();
  let data; try { data = JSON.parse(body); } catch { data = body; }
  if (!r.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${r.status}`);
  return data;
}
const jpost = (path, obj) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(obj || {}) });
const pref = { get(k, d) { try { return localStorage.getItem("talk." + k) ?? d; } catch { return d; } },
               set(k, v) { try { localStorage.setItem("talk." + k, v); } catch {} } };

// ---------------- 首页 ----------------
async function init() {
  try {
    S.cfg = await api("/api/config");
  } catch (e) { $("status").textContent = "网关不可用：" + e.message; return; }
  S.provider = S.cfg.provider;
  renderProviders();
  renderVoices();
  initDialect();
  $("voice").onchange = () => { pref.set("voice", $("voice").value); syncVoiceDel(); syncDialect(true); };
  $("voicePlay").onclick = previewVoice;
  $("voiceDel").onclick = deleteVoice;
  initClone();
  initPersonas();
  initStructuredPersonas();
  initPersonaBuilder();
  $("photo").onchange = onPhoto;
  $("animateRow").hidden = !S.cfg.animate_ready;
  $("animate").checked = S.cfg.animate_ready && pref.get("animate", "1") === "1";
  $("animate").onchange = () => pref.set("animate", $("animate").checked ? "1" : "0");
  $("clipsRow").hidden = !S.cfg.animate_ready;
  // 渲染模型切换（评估用）：后端 RENDER_MODELS 多于一个才显示
  const rms = S.cfg.render_models || [S.cfg.avatar_model];
  const rmName = { quicktalk: "口型（QuickTalk）", fasterliveportrait: "整脸（FasterLivePortrait）", flashhead: "整头（FlashHead）" };
  $("renderModel").innerHTML = rms.map((m) => `<option value="${m}">${rmName[m] || m}</option>`).join("");
  S.renderModel = rms.includes(pref.get("render", "")) ? pref.get("render", "") : rms[0];
  $("renderModel").value = S.renderModel;
  $("renderModel").onchange = () => { S.renderModel = $("renderModel").value; pref.set("render", S.renderModel); };
  $("renderRow").hidden = rms.length < 2;
  $("clips").value = pref.get("clips", "3");
  $("clips").onchange = () => pref.set("clips", $("clips").value);
  $("callBtn").onclick = startCall;
  await loadAvatars();
}

// ---------------- 结构化人设（后端 /v1/personas 建的，素材抽取 + build 出来的）：选中 → 填编译好的提示词 + 推荐音色 ----------------
async function initStructuredPersonas() {
  let list = [];
  try { list = await api("/api/personas"); } catch { return; }
  if (!list.length) return;
  const sel = $("psSelect");
  for (const p of list) {
    const o = document.createElement("option");
    o.value = p.id;
    o.textContent = `${p.name}（v${p.version}${p.status === "approved" ? "" : "·草稿"}${p.knowledge_mode === "external" ? "·知识需外部检索" : ""}）`;
    sel.appendChild(o);
  }
  $("psCell").hidden = false;
  const apply = (p, quiet) => {
    $("prompt").value = p.prompt; pref.set("persona", "custom"); pref.set("prompt", p.prompt);
    document.querySelectorAll("#personas .persona").forEach((b) => b.classList.remove("on"));
    $("personaHint").textContent = "结构化人设";
    $("psNote").textContent = p.knowledge_mode === "inline" ? " 知识已内联" : " 知识需后端检索";
    const opt = p.voice && [...$("voice").options].find((o) => o.value === p.voice);
    if (opt && $("voice").value !== p.voice) {
      $("voice").value = p.voice; pref.set("voice", p.voice); syncVoiceDel(); syncDialect(true);
      if (!quiet) toast(`人设「${p.name}」，音色换成「${opt.textContent}」`);
    } else if (!quiet) toast(`人设「${p.name}」`);
  };
  sel.onchange = () => {
    pref.set("ps", sel.value);
    const p = list.find((x) => x.id === sel.value);
    if (p) apply(p, false); else { $("psNote").textContent = ""; $("personaHint").textContent = "自定义"; }
  };
  const saved = pref.get("ps", "");
  if (saved && list.some((x) => x.id === saved)) { sel.value = saved; apply(list.find((x) => x.id === saved), true); }
}

// ---------------- 用素材创建人设：文件 + 网址 → /api/personas 一键流水线 → 轮询进度 → 建好后选中 ----------------
function initPersonaBuilder() {
  const sheet = $("psSheet"), files = $("psFiles"), list = $("psFileList"), submit = $("psSubmit"), prog = $("psProgress");
  let picked = [];
  const sync = () => {
    list.innerHTML = picked.map((f) => `<li><span>${esc(f.name)}</span><span>${(f.size / 1048576).toFixed(1)} MB</span></li>`).join("");
    submit.disabled = !$("psName").value.trim() || !(picked.length || $("psUrls").value.trim() || $("psIntro").value.trim());
  };
  files.onchange = () => { picked = picked.concat([...files.files]); files.value = ""; sync(); };
  $("psName").oninput = sync; $("psUrls").oninput = sync; $("psIntro").oninput = sync;
  $("psOpen").onclick = () => { picked = []; prog.hidden = true; submit.textContent = "开始构建"; sync(); sheet.showModal(); };
  const render = (j) => {
    const mats = (j.materials || []).map((m) => `<li class="${m.status}"><span>${esc(m.name)}</span><span>${m.status === "running" ? esc(m.step) : m.status === "done" ? "✓" : "失败"}</span></li>`).join("");
    prog.className = "progress" + (j.state === "done" ? " done" : "");
    prog.innerHTML = `<b>${esc(j.state === "error" ? "失败：" + (j.error || "") : j.step)}</b><ul class="file-list">${mats}</ul>` +
      (j.state === "running" ? `<div class="pbar"><i></i></div><small>已用 ${j.elapsed}s</small>` :
       j.state === "done" ? `<small>v${j.version} · ${j.compiled.knowledge_mode === "inline" ? "知识已内联" : "知识量大，需后端检索"} · ${j.compiled.prompt_tokens} tokens</small>` : "");
    prog.hidden = false;
  };
  submit.onclick = async () => {
    submit.disabled = true; submit.textContent = "上传中…";
    const fd = new FormData();
    fd.append("name", $("psName").value.trim()); fd.append("urls", $("psUrls").value); fd.append("hint", $("psHint").value); fd.append("intro", $("psIntro").value);
    for (const f of picked) fd.append("files", f, f.name);
    let job;
    try { job = (await api("/api/personas", { method: "POST", body: fd })).job; }
    catch (e) { toast(e.message); submit.disabled = false; submit.textContent = "开始构建"; return; }
    submit.textContent = "构建中…";
    let j;
    do {
      await new Promise((r) => setTimeout(r, 2000));
      try { j = await api(`/api/personas/jobs/${job}`); render(j); } catch (e) { toast(e.message); break; }
    } while (j && j.state === "running");
    submit.disabled = false; submit.textContent = "再建一个";
    if (j && j.state === "done") {
      toast(`人设「${j.name}」建好了`);
      await reloadStructuredPersonas(j.persona_id);
    }
  };
}

async function reloadStructuredPersonas(selectId) {
  const sel = $("psSelect");
  [...sel.options].slice(1).forEach((o) => o.remove());
  sel.onchange = null;
  await initStructuredPersonas();
  if (selectId && [...sel.options].some((o) => o.value === selectId)) { sel.value = selectId; sel.onchange(); }
}

// ---------------- 人设模板：选中 → 填提示词 + 换推荐音色；手改提示词 → 自定义 ----------------
function initPersonas() {
  const list = S.cfg.personas || [];
  let cur = pref.get("persona", S.cfg.default_persona);
  const saved = pref.get("prompt", "");
  const byId = (id) => list.find((p) => p.id === id);
  if (cur !== "custom" && !byId(cur)) cur = S.cfg.default_persona;
  $("prompt").value = cur === "custom" ? (saved || S.cfg.system_prompt) : byId(cur).prompt;
  const render = () => {
    $("personas").innerHTML = "";
    for (const p of list) {
      const b = document.createElement("button");
      b.className = "persona" + (p.id === cur ? " on" : "");
      b.innerHTML = `<b>${esc(p.name)}</b><small>${esc(p.desc)}</small>`;
      b.onclick = () => pick(p);
      $("personas").appendChild(b);
    }
    $("personaHint").textContent = cur === "custom" ? "自定义" : "";
  };
  // 形象绑定了人设时（后端 avatar_personas）选形象自动切人设；用户手改过提示词（custom）就不动
  S.pickPersonaFor = (avatar) => {
    const m = S.cfg.avatar_personas || {};
    const key = Object.keys(m).find((k) => avatar.id === k || (avatar.name || "").startsWith(k) || avatar.id.startsWith(k));
    const p = key && byId(m[key]);
    if (p && cur !== "custom" && cur !== p.id) pick(p);
  };
  const pick = (p) => {
    cur = p.id; pref.set("persona", cur); pref.set("prompt", "");
    if ($("psSelect").value) { $("psSelect").value = ""; pref.set("ps", ""); $("psNote").textContent = ""; }
    $("prompt").value = p.prompt;
    const opt = [...$("voice").options].find((o) => o.value === p.voice);
    if (opt && $("voice").value !== p.voice) {
      $("voice").value = p.voice; pref.set("voice", p.voice); syncVoiceDel(); syncDialect(true);
      toast(`人设「${p.name}」，音色换成「${opt.textContent}」`);
    } else toast(`人设「${p.name}」`);
    render();
  };
  $("prompt").oninput = () => {
    const p = byId(cur);
    if (p && $("prompt").value === p.prompt) return;
    cur = "custom"; pref.set("persona", "custom"); pref.set("prompt", $("prompt").value);
    render();
  };
  render();
  const row = $("personas");
  const on = row.querySelector(".on");
  if (on) row.scrollLeft = on.offsetLeft - (row.clientWidth - on.offsetWidth) / 2;
  dragScroll(row);
}

// 横向滚动条在电脑上用鼠标拖不动（只有触屏能滑）：补上按住拖动、滚轮横滚、两侧箭头
function dragScroll(row) {
  const arrows = row.parentElement.querySelectorAll(".row-arrow");
  const sync = () => {
    const max = row.scrollWidth - row.clientWidth - 2;
    arrows[0].hidden = row.scrollLeft <= 2;
    arrows[1].hidden = row.scrollLeft >= max;
  };
  arrows.forEach((a) => (a.onclick = () => row.scrollBy({ left: +a.dataset.dir * row.clientWidth * 0.8, behavior: "smooth" })));
  row.addEventListener("scroll", sync, { passive: true });
  window.addEventListener("resize", sync);
  row.addEventListener("wheel", (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;      // 触控板横滑交给浏览器
    const max = row.scrollWidth - row.clientWidth;
    if ((e.deltaY < 0 && row.scrollLeft <= 0) || (e.deltaY > 0 && row.scrollLeft >= max)) return;  // 到头了让页面滚
    e.preventDefault();
    row.scrollLeft += e.deltaY;
  }, { passive: false });
  let start = null;
  row.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    start = { x: e.clientX, left: row.scrollLeft, moved: false, id: e.pointerId };
  });
  row.addEventListener("pointermove", (e) => {
    if (!start || e.pointerId !== start.id) return;
    const dx = e.clientX - start.x;
    if (!start.moved && Math.abs(dx) > 5) {
      start.moved = true;
      row.setPointerCapture(e.pointerId);
      row.classList.add("dragging");
    }
    if (start.moved) row.scrollLeft = start.left - dx;
  });
  const end = (e) => {
    if (!start || e.pointerId !== start.id) return;
    const moved = start.moved;
    start = null;
    row.classList.remove("dragging");
    if (moved) {   // 拖完松手不算点选；浏览器不一定会发这次 click，所以下一轮事件循环就撤掉拦截
      const block = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
      row.addEventListener("click", block, { capture: true });
      setTimeout(() => row.removeEventListener("click", block, { capture: true }), 0);
    }
  };
  row.addEventListener("pointerup", end);
  row.addEventListener("pointercancel", end);
  sync();
}

// ---------------- 音色：按年龄分组 / 试听 / 克隆 ----------------
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
function renderVoices(select) {
  const vs = $("voice");
  vs.innerHTML = S.cfg.voice_groups.map((g) => `<optgroup label="${esc(g.label)}">` +
    g.voices.map((v) => `<option value="${esc(v.id)}">${esc(v.label)}</option>`).join("") + "</optgroup>").join("");
  const all = S.cfg.voice_groups.flatMap((g) => g.voices.map((v) => v.id));
  const want = select || pref.get("voice", S.cfg.default_voice);
  vs.value = all.includes(want) ? want : (all.includes(S.cfg.default_voice) ? S.cfg.default_voice : all[0]);
  pref.set("voice", vs.value);
  $("cloneOpen").hidden = !S.cfg.clone_ready;
  syncVoiceDel();
  syncDialect(false);
}
const isCloned = (id) => (S.cfg.voice_groups.find((g) => g.label === "我的克隆音色")?.voices || []).some((v) => v.id === id);
function syncVoiceDel() { $("voiceDel").hidden = !isCloned($("voice").value); }

// ---------------- 方言：本地音色（预设 / 克隆）可以说方言；方言预设自带方言 ----------------
const isLocalVoice = (id) => /^(clone_|vox_)/.test(id);
const voiceInfo = (id) => S.cfg.voice_groups.flatMap((g) => g.voices).find((v) => v.id === id) || {};
function initDialect() {
  const ds = S.cfg.dialects || [];
  $("dialectCell").hidden = !ds.length;
  $("dialect").innerHTML = `<option value="">普通话</option>` + ds.map((d) => `<option value="${esc(d)}">${esc(d)}</option>`).join("");
  $("dialect").value = ds.includes(pref.get("dialect", "")) ? pref.get("dialect", "") : "";
  $("dialect").onchange = () => pref.set("dialect", $("dialect").value);
  syncDialect(false);
}
// 选中方言预设 → 方言跟着切过去；MiniMax / edge 音色不支持方言，置灰
function syncDialect(fromVoiceChange) {
  if (!(S.cfg.dialects || []).length) return;
  const v = $("voice").value, own = voiceInfo(v).dialect;
  if (own && fromVoiceChange) { $("dialect").value = own; pref.set("dialect", own); }
  $("dialect").disabled = !isLocalVoice(v);
}
// 实际交给合成的音色 ID：本地音色 + 方言 → `音色@方言`
function voiceId() {
  const v = $("voice").value, d = $("dialect").value;
  return d && isLocalVoice(v) && !$("dialect").disabled ? `${v}@${d}` : v;
}
// 选了方言时让大模型也用方言的说法回复，合成出来才地道
function dialectPrompt() {
  const d = $("dialect").disabled ? "" : $("dialect").value;
  if (!d) return "";
  const hint = d === "粤语" ? "用粤语口语书写（如「咗、嘅、唔、啲」）" : `多用${d}特有的词汇和语气词`;
  return `\n请用${d}和对方聊天：${hint}，但要让人听得懂，不要标注拼音或解释。`;
}

const player = new Audio();
async function previewVoice() {
  const b = $("voicePlay");
  if (!player.paused) { player.pause(); b.textContent = "▶"; return; }
  b.textContent = "…";
  player.src = `/api/voices/preview?voice=${encodeURIComponent(voiceId())}`;
  player.onended = player.onerror = () => (b.textContent = "▶");
  try { await player.play(); b.textContent = "■"; } catch (e) { b.textContent = "▶"; toast("试听失败"); }
}

async function deleteVoice() {
  const id = $("voice").value, opt = $("voice").selectedOptions[0];
  if (!isCloned(id) || !confirm(`删除克隆音色「${opt ? opt.textContent : id}」？`)) return;
  try { await api(`/api/voices/${encodeURIComponent(id)}`, { method: "DELETE" }); await reloadVoices(); toast("已删除"); }
  catch (e) { toast(e.message); }
}
async function reloadVoices(select) { S.cfg = await api("/api/config"); renderVoices(select); }

function initClone() {
  const sheet = $("cloneSheet"), rec = $("recBtn"), audio = $("recAudio"), submit = $("cloneSubmit");
  let blob = null, mr = null, timer = null;
  const setBlob = (b, name) => {
    blob = b; submit.disabled = !b;
    if (audio.src) URL.revokeObjectURL(audio.src);
    audio.hidden = !b; if (b) audio.src = URL.createObjectURL(b);
    if (name && !$("cloneName").value) $("cloneName").value = name;
  };
  const stopRec = () => { if (mr && mr.state !== "inactive") mr.stop(); };
  $("cloneOpen").onclick = () => { setBlob(null); $("cloneName").value = ""; sheet.showModal(); };
  sheet.addEventListener("close", stopRec);
  rec.onclick = async () => {
    if (mr && mr.state === "recording") { stopRec(); return; }
    if (!navigator.mediaDevices?.getUserMedia) { toast("当前页面拿不到麦克风（需要 HTTPS），可以上传录音"); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch { toast("麦克风权限被拒绝，可以上传录音"); return; }
    const chunks = [], t0 = Date.now();
    mr = new MediaRecorder(stream);
    mr.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    mr.onstop = () => {
      clearInterval(timer); stream.getTracks().forEach((t) => t.stop());
      rec.classList.remove("rec"); rec.textContent = "● 重新录音";
      const sec = (Date.now() - t0) / 1000;
      if (sec < 6) { toast(`只录了 ${sec.toFixed(0)} 秒，至少要 5 秒`); setBlob(null); return; }
      setBlob(new Blob(chunks, { type: mr.mimeType || "audio/webm" }));
    };
    mr.start(250);
    rec.classList.add("rec");
    timer = setInterval(() => (rec.textContent = `■ 停止录音（${Math.round((Date.now() - t0) / 1000)} 秒）`), 250);
    rec.textContent = "■ 停止录音（0 秒）";
  };
  $("cloneFile").onchange = (ev) => {
    const f = ev.target.files[0]; ev.target.value = "";
    if (f) setBlob(f, f.name.replace(/\.[^.]+$/, "").slice(0, 16));
  };
  submit.onclick = async () => {
    if (!blob) return;
    submit.disabled = true; submit.textContent = "正在生成音色…";
    try {
      const fd = new FormData();
      fd.append("name", $("cloneName").value.trim() || "我的声音");
      fd.append("audio", blob, blob.name || "voice.webm");
      const v = await api("/api/voices/clone", { method: "POST", body: fd });
      await reloadVoices(v.id);
      sheet.close();
      toast(`音色「${v.name}」已生成，已选中`);
    } catch (e) { toast("克隆失败：" + e.message, 5000); }
    submit.disabled = !blob; submit.textContent = "生成音色";
  };
}

function renderProviders() {
  $("providers").innerHTML = "";
  for (const p of S.cfg.providers) {
    const b = document.createElement("button");
    b.textContent = p.label; b.disabled = !p.ready; b.className = p.key === S.provider ? "on" : "";
    b.title = p.model;
    b.onclick = async () => {
      try { await jpost("/api/config", { provider: p.key }); S.provider = p.key; renderProviders(); toast(`已切换到 ${p.label}`); }
      catch (e) { toast(e.message); }
    };
    $("providers").appendChild(b);
  }
}

async function loadAvatars(selectId) {
  let list = [];
  try { list = await api(`${OT}/avatars`); } catch (e) { $("status").textContent = "数字人服务未就绪：" + e.message; }
  // 只展示能用当前驱动模型跑的单人形象：自己上传的 + 内置 quicktalk 形象
  S.avatars = list.filter((a) => a.person_mode !== "double" && (a.is_custom || a.model_type === S.cfg.avatar_model));
  S.avatars.sort((a, b) => Number(b.is_custom) - Number(a.is_custom));
  const keep = selectId || pref.get("avatar", "");
  S.avatarId = S.avatars.some((a) => a.id === keep) ? keep : (S.avatars[0] || {}).id || null;
  renderAvatars();
  prewarm(S.avatarId).catch(() => {});
}

// 预热：加载口型模型 + 生成该形象缓存（新形象约 10s）。不预热的话首句要多等 6~8s
const warming = new Map();
function prewarm(id) {
  if (!id) return Promise.resolve();
  if (!warming.has(id)) {
    warming.set(id, jpost(`${OT}/avatars/${encodeURIComponent(id)}/prewarm`, { model: S.cfg.avatar_model })
      .catch((e) => { warming.delete(id); throw e; }));
  }
  return warming.get(id);
}

function renderAvatars() {
  const g = $("avatars"); g.innerHTML = "";
  if (!S.avatars.length) g.innerHTML = `<p class="hint">还没有形象，先上传一张照片吧。</p>`;
  for (const a of S.avatars) {
    const b = document.createElement("button");
    b.className = "avatar" + (a.id === S.avatarId ? " on" : "");
    const base = `${OT}/avatars/${encodeURIComponent(a.id)}`;
    b.innerHTML = a.has_preview_video   // 视频底板的形象（会眨眼/动手）直接循环播预览
      ? `<video src="${base}/preview-video" poster="${base}/preview" autoplay muted loop playsinline></video><i class="badge">动态</i><span></span>`
      : `<img loading="lazy" src="${base}/preview" alt=""><span></span>`;
    b.querySelector("span").textContent = a.name || a.id;
    b.onclick = () => { S.avatarId = a.id; pref.set("avatar", a.id); renderAvatars(); prewarm(a.id).catch(() => {}); S.pickPersonaFor?.(a); };
    if (a.is_custom) {
      const d = document.createElement("button");
      d.className = "del"; d.textContent = "✕"; d.title = "删除";
      d.onclick = async (ev) => {
        ev.stopPropagation();
        if (!confirm(`删除形象「${a.name || a.id}」？`)) return;
        try { await api(`${OT}/avatars/${encodeURIComponent(a.id)}`, { method: "DELETE" }); await loadAvatars(); }
        catch (e) { toast(e.message); }
      };
      b.appendChild(d);
    }
    g.appendChild(b);
  }
  $("callBtn").disabled = !S.avatarId;
}

// 浏览器端先缩到长边 1280 并转 JPEG，手机原图动辄十几 MB
function shrink(file, max = 1280) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const k = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      c.toBlob((b) => (b ? resolve(b) : reject(new Error("图片处理失败"))), "image/jpeg", 0.92);
      URL.revokeObjectURL(img.src);
    };
    img.onerror = () => reject(new Error("无法读取这张图片"));
    img.src = URL.createObjectURL(file);
  });
}

async function onPhoto(ev) {
  const file = ev.target.files[0]; ev.target.value = "";
  if (!file) return;
  const name = (prompt("给这个形象起个名字", file.name.replace(/\.[^.]+$/, "").slice(0, 16) || "我的形象") || "").trim();
  if (!name) return;
  const lab = $("uploadLabel"), btn = lab.parentElement;
  lab.textContent = "正在上传…"; btn.classList.add("busy"); $("photo").disabled = true;
  try {
    // 网关后台任务：(可选)生成眨眼/手势待机视频 → 建形象 → 预热
    const fd = new FormData();
    fd.append("name", name);
    fd.append("animate", $("animate").checked ? "true" : "false");
    fd.append("clips", $("clips").value);
    fd.append("image", await shrink(file), "photo.jpg");
    const { job } = await api("/api/avatars", { method: "POST", body: fd });
    let j;
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      j = await api(`/api/jobs/${job}`);
      if (j.state !== "running") break;
      lab.textContent = `${j.step}… ${j.elapsed}s`;
    }
    if (j.state === "error") throw new Error(j.error);
    pref.set("avatar", j.avatar_id);
    warming.set(j.avatar_id, Promise.resolve());
    await loadAvatars(j.avatar_id);
    toast(j.warn || (j.animated ? "动态形象已就绪" : "形象已就绪"), j.warn ? 6000 : 2600);
  } catch (e) { toast("创建失败：" + e.message, 5000); }
  lab.textContent = "＋ 上传照片创建形象"; btn.classList.remove("busy"); $("photo").disabled = false;
}

// ---------------- 通话 ----------------
function setState(text, cls = "") { const s = $("callState"); s.textContent = text; s.className = "pill " + cls; }
function bubble(text, cls = "") {
  const d = document.createElement("div"); d.className = "bubble " + cls; d.textContent = text;
  $("subs").appendChild(d);
  while ($("subs").children.length > 6) $("subs").firstChild.remove();
  return d;
}

async function startCall() {
  const a = S.avatars.find((x) => x.id === S.avatarId);
  if (!a) return;
  $("callBtn").disabled = true; $("status").textContent = "";
  $("home").hidden = true; $("call").hidden = false; $("call").classList.remove("live");
  $("callName").textContent = a.name || a.id;
  $("poster").src = `${OT}/avatars/${encodeURIComponent(a.id)}/preview`;
  $("subs").innerHTML = ""; setState("准备形象…");
  try {
    await prewarm(a.id).catch(() => {});
    setState("连接中…");
    const created = await jpost(`${OT}/sessions`, {
      avatar_id: a.id, model: S.renderModel || S.cfg.avatar_model,
      tts_provider: "openai_compatible", tts_voice: voiceId(), stt_provider: "sensevoice",
      llm_system_prompt: (($("prompt").value.trim() + dialectPrompt()).trim() + memMarker(a.id)) || undefined,
      agent_enabled: false, memory_enabled: false, knowledge_enabled: false,
    });
    S.sid = created.session_id;
    listenEvents();
    setState("加载模型…");
    await connectRtc();
    await jpost(`${OT}/sessions/${S.sid}/start`);
    setState("已接通");
    await startMic();
  } catch (e) {
    bubble("连接失败：" + e.message, "err"); setState("失败");
  }
}

function listenEvents() {
  const es = new EventSource(`${OT}/sessions/${S.sid}/events`);
  S.es = es;
  let cur = null;
  es.addEventListener("speech.started", () => { S.speaking = true; setState("说话中", "speaking"); });
  es.addEventListener("subtitle.chunk", (e) => {
    const d = JSON.parse(e.data || "{}");
    if (!d.text) return;
    if (d.is_final) { if (!cur) bubble(d.text); cur = null; return; }
    if (!cur) cur = bubble(""); cur.textContent += d.text;
  });
  es.addEventListener("speech.ended", () => { S.speaking = false; cur = null; setState(S.mic ? "在听" : "已接通", S.mic ? "listening" : ""); });
  es.addEventListener("session.queued", (e) => setState("排队中…"));
  es.addEventListener("session.state_changed", (e) => {
    const d = JSON.parse(e.data || "{}");
    if (d.state === "error") bubble("数字人出错了：" + (d.error || ""), "err");
  });
  es.onerror = () => {};
}

async function connectRtc() {
  let ice = {};
  try { ice = await api(`${OT}/sessions/webrtc/ice-config`); } catch {}
  const pc = new RTCPeerConnection({ iceServers: ice.iceServers || [], iceTransportPolicy: ice.iceTransportPolicy || "all" });
  S.pc = pc;
  const ms = new MediaStream();
  const v = $("video"); v.srcObject = ms;
  pc.ontrack = (ev) => {
    ms.addTrack(ev.track);
    v.play().catch(() => { v.muted = true; v.play(); toast("点一下画面开启声音"); v.onclick = () => (v.muted = false); });
  };
  v.onplaying = () => $("call").classList.add("live");
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((res) => {
    if (pc.iceGatheringState === "complete") return res();
    const t = setTimeout(res, 3000);
    pc.addEventListener("icegatheringstatechange", () => { if (pc.iceGatheringState === "complete") { clearTimeout(t); res(); } });
  });
  const ans = await jpost(`${OT}/sessions/${S.sid}/webrtc/offer`, { sdp: pc.localDescription.sdp, type: "offer" });
  await pc.setRemoteDescription(ans);
}

async function hangup() {
  stopMic();
  if (S.es) S.es.close();
  if (S.pc) S.pc.close();
  if (S.sid) api(`${OT}/sessions/${S.sid}`, { method: "DELETE" }).catch(() => {});
  Object.assign(S, { sid: null, pc: null, es: null, speaking: false });
  $("video").srcObject = null;
  $("call").hidden = true; $("home").hidden = false; $("callBtn").disabled = !S.avatarId;
}

async function sendText(text) {
  if (!S.sid || !text) return;
  bubble(text, "me");
  if (S.speaking) await jpost(`${OT}/sessions/${S.sid}/interrupt`).catch(() => {});
  try { await jpost(`${OT}/sessions/${S.sid}/speak`, { text, voice: voiceId(), tts_provider: "openai_compatible" }); }
  catch (e) { bubble("发送失败：" + e.message, "err"); }
}

// ---------------- 麦克风：16k PCM + 能量 VAD，分段走 speak_audio_stream ----------------
const WORKLET = `
class Pcm16 extends AudioWorkletProcessor {
  constructor() { super(); this.ratio = sampleRate / 16000; this.acc = 0; this.buf = []; }
  process(inputs) {
    const ch = inputs[0][0];
    if (ch) {
      for (let i = 0; i < ch.length; i++) {
        this.acc += 1;
        if (this.acc >= this.ratio) { this.acc -= this.ratio; this.buf.push(ch[i]); }
      }
      if (this.buf.length >= 320) {           // 20ms 一帧
        const f = this.buf.splice(0, 320); let e = 0;
        const out = new Int16Array(320);
        for (let i = 0; i < 320; i++) { const s = Math.max(-1, Math.min(1, f[i])); out[i] = s * 32767; e += s * s; }
        this.port.postMessage({ pcm: out.buffer, rms: Math.sqrt(e / 320) }, [out.buffer]);
      }
    }
    return true;
  }
}
registerProcessor("pcm16", Pcm16);`;

const VAD = { on: 0.02, onSpeaking: 0.06, startFrames: 4, silenceMs: 550, maxMs: 15000, prerollFrames: 15 };   // 静音 550ms 算说完（700→550，回应快 0.15 秒）

async function startMic() {
  if (S.mic) return;
  if (!navigator.mediaDevices?.getUserMedia) { toast("当前页面拿不到麦克风（需要 HTTPS）"); return; }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) { toast("麦克风权限被拒绝，可以先打字聊"); return; }
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: "application/javascript" })));
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "pcm16");
  src.connect(node);
  const pre = []; let loud = 0, lastVoice = 0, segStart = 0;
  node.port.onmessage = ({ data }) => {
    const { pcm, rms } = data; const now = performance.now();
    $("meter").firstChild.style.width = Math.min(100, rms * 600) + "%";
    const thr = S.speaking ? VAD.onSpeaking : VAD.on;   // 数字人说话时提高门限，减少回声误触发
    if (!S.seg) {
      pre.push(pcm); if (pre.length > VAD.prerollFrames) pre.shift();
      loud = rms > thr ? loud + 1 : 0;
      if (loud >= VAD.startFrames) { segStart = lastVoice = now; openSegment(pre.splice(0)); }
      return;
    }
    sendPcm(pcm);
    if (rms > VAD.on) lastVoice = now;
    if (now - lastVoice > VAD.silenceMs || now - segStart > VAD.maxMs) { closeSegment(); loud = 0; }
  };
  S.mic = { ctx, stream, node, src };
  $("micBtn").classList.add("on"); setState(S.speaking ? "说话中" : "在听", S.speaking ? "speaking" : "listening");
}

function stopMic() {
  if (!S.mic) return;
  if (S.seg) closeSegment(true);
  S.mic.stream.getTracks().forEach((t) => t.stop());
  S.mic.ctx.close();
  S.mic = null;
  $("micBtn").classList.remove("on"); $("meter").firstChild.style.width = "0";
  if (S.sid) setState(S.speaking ? "说话中" : "已接通", S.speaking ? "speaking" : "");
}

function openSegment(preroll) {
  if (!S.sid) return;
  if (S.speaking) jpost(`${OT}/sessions/${S.sid}/interrupt`).catch(() => {});   // 用户开口即打断
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/asr/${S.sid}`);   // 网关识别后转 /speak
  ws.binaryType = "arraybuffer";
  const seg = { ws, ready: false, queue: preroll.slice(), closed: false, bubble: bubble("…", "me") };
  S.seg = seg;
  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "meta", voice: voiceId(), tts_provider: "openai_compatible", tts_model: "", stt_provider: "sensevoice" }));
    seg.queue.forEach((b) => ws.send(b)); seg.queue = [];
    seg.ready = true;
    if (seg.closed) ws.send(JSON.stringify({ type: "end" }));
  };
  ws.onmessage = (e) => {
    let d = {}; try { d = JSON.parse(e.data); } catch {}
    if (d.text) seg.bubble.textContent = d.text;
    else if (d.error) { seg.bubble.remove(); if (!/未能识别/.test(d.error)) bubble(d.error, "err"); }
  };
  ws.onclose = () => { if (seg.bubble.textContent === "…") seg.bubble.remove(); };
  ws.onerror = () => { seg.bubble.remove(); };
}
function sendPcm(buf) {
  const seg = S.seg; if (!seg) return;
  if (seg.ready) seg.ws.send(buf); else seg.queue.push(buf);
}
function closeSegment(abort = false) {
  const seg = S.seg; S.seg = null; if (!seg) return;
  seg.closed = true;
  if (abort) { seg.ws.close(); seg.bubble.remove(); return; }
  if (seg.ready) seg.ws.send(JSON.stringify({ type: "end" }));
}

$("micBtn").onclick = () => (S.mic ? stopMic() : startMic());
$("hangBtn").onclick = hangup;
// ---------------- 长期记忆：浏览器固定编号 + 形象 → 网关按标记拼记忆、每轮后提炼 ----------------
function userId() {
  let u = pref.get("uid", "");
  if (!u) { u = "u" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36); pref.set("uid", u); }
  return u;
}
function memMarker(aid) { return $("memOn").checked ? `\u200b[MEM ${userId()} ${aid}]` : ""; }
async function loadMemory() {
  if (!S.avatarId) return;
  try {
    const m = await api(`/api/memory?uid=${encodeURIComponent(userId())}&avatar=${encodeURIComponent(S.avatarId)}`);
    $("memList").innerHTML = (m.facts || []).map((f) => `<li>${esc(f)}</li>`).join("");
    $("memCount").textContent = m.facts && m.facts.length ? `${m.facts.length} 条` : "";
  } catch {}
}
$("memOn").checked = pref.get("mem", "1") === "1";
$("memOn").onchange = () => pref.set("mem", $("memOn").checked ? "1" : "0");
$("memCell").addEventListener("toggle", () => { if ($("memCell").open) loadMemory(); });
$("memClear").onclick = async () => {
  if (!S.avatarId) return;
  try {
    await api(`/api/memory?uid=${encodeURIComponent(userId())}&avatar=${encodeURIComponent(S.avatarId)}`, { method: "DELETE" });
    loadMemory(); toast("已清除");
  } catch (e) { toast(e.message); }
};
// 字幕开关：通话页右上角，记住用户的选择
function setSubs(on) {
  $("call").classList.toggle("nosubs", !on);
  $("subsBtn").setAttribute("aria-pressed", on ? "true" : "false");
  $("subsBtn").textContent = on ? "字幕 开" : "字幕 关";
  pref.set("subs", on ? "1" : "0");
}
setSubs(pref.get("subs", "1") === "1");
$("subsBtn").onclick = () => setSubs($("call").classList.contains("nosubs"));
$("textForm").onsubmit = (e) => { e.preventDefault(); const t = $("textInput").value.trim(); $("textInput").value = ""; sendText(t); };
init();
