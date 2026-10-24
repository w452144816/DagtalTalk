# talk — 实时数字人视频对话 demo

基于开源编排框架 [OpenTalking](https://github.com/datascale-ai/opentalking)：
上传一张正脸照片 → 生成数字人 → 浏览器麦克风说话 → SenseVoice 识别 → 豆包 / MiniMax 回复 → MiniMax / 本地克隆 TTS → QuickTalk 口型驱动 → WebRTC 实时回放。

## 部署位置
- 服务器 `wwang@<GPU1_LAN_IP>`（2×3090，用 **GPU1**；GPU0 是别人的 comfyui）
- 所有数据只在 `/mnt/hdd-16t/wwang/talk/`：`models/`（QuickTalk、SenseVoice 权重）、`avatars/`（形象，首启从内置示例拷入）、`cache/`、`certs/`（自签证书）、`logs/`、`state/`、`build/`（构建上下文 + `.env`）
- 访问：**https://<GPU1_LAN_IP>:8443**（自签证书，首次点"继续访问"；必须 HTTPS 浏览器才给麦克风）

## 架构（多机，2026-10-18 起；完整方案见 `docs/AI服务多机方案.md`）
```
浏览器 ──HTTPS/WSS──> talk-control :8443（205，调度 + 素材分发，无 GPU）
   │                      │ 内网 HTTP + X-Node-Token，按会话路由到 /internal/ot/{idx}/…
   │                      ├─> 205 talk-node :8301 ── OpenTalking 127.0.0.1:8000（GPU1）── TTS/LLM 回调 127.0.0.1:8301 ── talk-tts :8302（GPU0）
   │                      └─> 206 talk-node :8301 ── OpenTalking 8000~8004（5 卡）──────────────────────────────── talk-tts :8302（2 卡）
   └──── WebRTC 音视频 UDP 直连会话所在机器 ─────────────────────────────────────────┘
```
- 同一个镜像按 `ROLE` 跑两种角色（`entrypoint.sh`、`gateway/cluster.py`）：
  - **control**：页面、`/api/*`、`/ot/*` 反代；节点注册表来自节点心跳（5 秒一次，15 秒没心跳算掉线）；建会话按 `(在线数/cap, 未预热该形象)` 全局选实例（`gateway/ot_pool.py`）
  - **node**：本机每张渲染卡一个 OpenTalking 实例 + 节点网关：`/internal/ot/{idx}/*` 反代、`/internal/asr/{sid}` 识别、形象目录打包/导入、给本机 OT 的 TTS/LLM 回调
- 素材每台本地全量：新形象建在一个节点后立刻推给其他节点；心跳对账补缺（形象整目录 tar 互拉，克隆音色的 wav 从控制面拉再注册到本机 talk-tts）
- 会话真源在节点（心跳里带在线会话列表），控制面只是缓存；LLM 引擎选择由心跳应答下发给节点
- 配置：`scripts/hosts/cluster.env`（CONTROL_URL、NODE_TOKEN，所有机器共用）+ `scripts/hosts/<机器>.env`（`DEPLOY_SERVICES` 决定这台机起哪些容器、`NODE_ID`）
- `gateway/app.py`：以上两种角色的路由都在这一个文件里，`_need("control"/"node")` 区分
- **对外接口**（`gateway/public_api.py`，只在 control 挂载；设计见 `docs/AI服务多机方案.md` 第七节）：
  - `/v1/*` 给业务后端，`Authorization: Bearer $AI_SERVICE_KEY`（`scripts/hosts/cluster.env`）：会话（建/查/PATCH 音色方言/speak chat|say/interrupt/重签 ticket/删除拿用量）、形象、音色、任务、容量
  - `/rt/*` 给前端，凭建会话返回的 ticket（`X-Session-Ticket` 或 `?ticket=`，HMAC 无状态、默认 2 小时）：ice-config、webrtc/offer、start、events(SSE)、asr(WS)、interrupt、text
  - 回调到 `callback_url`（`X-Signature` = HMAC-SHA256(body, AI_SERVICE_KEY)，失败 1/5/30/120s 重试）：`session.started`、`conversation.turn{user_text,bot_text}`、`session.ended{reason,usage}`、`avatar.ready/failed`
  - 控制面给每个 /v1 会话起一个任务跟 SSE 统计说话时长/拼对话轮次；sweeper 处理 `idle_timeout_s`（默认 300）/ `max_duration_s`（默认 3600）
  - `speak(mode=say)` 的实现：OpenTalking 的 /speak 必走 LLM，所以文本前加零宽标记 `SAY_MARK`，节点的 LLM 路由看到就原样回显（`llm_router.echo`）；`speech.started` 事件里的文本会带这个标记，`speech.ended`/字幕不带
  - **会话真源在节点**（`gateway/node_sessions.py`）：建会话时控制面把元数据（biz_ref、callback_url、音色、超时）用 `X-Session-Meta` 头交给节点，节点落盘 `state/sessions/`、自己跟 SSE 算用量/轮次、做 idle/max 超时、事件发控制面 `/internal/events`（发不出去落 `state/events_spool/` 重试）。控制面的 `SESS` 只是心跳重建的缓存——实测通话中重启 talk-control 会话不断、回调续上
  - 建形象任务落盘 `state/jobs/`（含原图），控制面重启续跑（`animate.resume`）
  - **主备**（`scripts/hosts/cluster.env` 的 `CONTROL_URLS`，第一个主、第二个备）：节点同时向两个控制面心跳，备机 `/health` 探测主机连续 10 秒不可达才受理（否则一律 503 + `X-Primary`），主机回来就让回去；后端配同样两个地址。实测停 205 控制面 11 秒后 206 接管，老会话照常 speak、回调续上，205 回来后会话又能从 205 操作。要对后端透明得加 VIP（keepalived，需要 root，见 `scripts/keepalived/README.md`）
  - **合成池**（`tts.py` 的 `LOCAL_TTS_URLS` / `LOCAL_TTS_MAX`，各机 env 里配）：本机 talk-tts 优先，并发到上限或连不上（15 秒内不再选）就用别的机器的；talk-tts 因此改听 0.0.0.0（内网无鉴权）。实测停掉 206 的 talk-tts，206 上的会话由 205 合成继续说话
  - **节点上线预热**（`cluster.warmup`）：每个 OpenTalking 实例先空跑一次会话（create → start 等 runner 就绪 → delete）把 QuickTalk worker 加载好，心跳里 `ready=false` 的实例控制面不分会话。3090 约 20 秒、TITAN V 约 30 秒。之前没有这步时 206 冷实例的第一路 `webrtc/offer` 等 60 秒超时 500
  - **看板** `https://<GPU1_LAN_IP>:8443/dashboard`（`gateway/web/dashboard.html` + `/api/dashboard`，2 秒轮询，无鉴权——内网用）：控制面主备/受理状态、容量、每节点 GPU 算力/显存（节点心跳里带 nvidia-smi）、每实例在线/cap/就绪/预热、合成池 inflight、/v1 会话表（演示页/压测直建的另列）、建形象任务、最近事件（建会话、结束+用量、对话轮次、节点上下线、接管）。备控制面上打开会提示未受理
  - **多模态素材 → 结构化人设**（`gateway/persona_builder.py`，`/v1/personas/*`，设计见方案文档 10.2）：
    建人设 → 传素材（`kind=doc|image|audio|video|url`，文档用 pypdf/python-docx/bs4 抽正文，图片和视频关键帧走方舟多模态模型描述+OCR，音视频 ffmpeg 转 16k 分 50 秒段送节点 SenseVoice）→ 每份素材 LLM 按 schema 抽要点（identity/background/style/knowledge/boundaries/recommend）→ `build` 把所有素材 + 人工字段合并成一版，编译 system_prompt（≤3000 token 内联知识 `inline`，否则 `external` 要后端做 RAG，`/export` 给全量知识）→ `chat` 文本试聊 → `approve` → 建会话 `persona: {persona_id, version?, extra_prompt?}`（未审核要 `allow_draft`；没传音色就用人设推荐的）。
    落盘 `state/personas/<pid>/`（persona.json 带版本、materials/ 原文件 + 抽取 JSON + 正文）。回调 `material.extracted/failed`、`persona.built`。冒烟：`scripts/personatest.py`（文本 + 图 + 音频三种素材全链路约 1 分钟）。
    演示页「人设」区多了「结构化人设」下拉（`GET /api/personas`，无鉴权，草稿也列出来方便测）：选中 → 提示词换成编译结果、音色换成推荐音色，再点开始通话即可；
    「＋ 用素材创建人设」弹层（`POST /api/personas` 多文件 + 网址 + 一句话介绍 → `persona_builder.start_pipeline` 一键：建人设 → 并行抽取 → build；`GET /api/personas/jobs/{id}` 轮询进度），建好自动出现在下拉里并选中。文件按 MIME/后缀自动分 doc/image/audio/video
  - 还没做：`llm.mode=external`、通话中换 system_prompt（OT 不支持，返回 501）、视频素材只在冒烟脚本外试过
  - 冒烟：`docker cp scripts/v1test.py talk-control:/tmp/ && docker exec -e AI_SERVICE_KEY=… talk-control python /tmp/v1test.py`
- `gateway/llm_router.py`：方舟直转（关思考）；MiniMax 走 Anthropic 接口转 OpenAI 流
- `gateway/web/`：手机优先 iOS 风格单页；麦克风 AudioWorklet 16k PCM + 能量 VAD，分段走 `speak_audio_stream`，开口即 `interrupt`

## 新机器上线（本地工程不含模型和 OpenTalking 源码）
1. 照 `scripts/hosts/205.env` 建 `scripts/hosts/<机器>.env`（SSH、TALK_HOME、TTS_HOME、PUBLIC_HOST、显卡分配、`DEPLOY_SERVICES`、`NODE_ID`、合成池地址）
2. `TARGET=<机器> bash scripts/bootstrap.sh --from <GPU1_LAN_IP>`：从已有节点 rsync 源码 + 权重 + 预设（约 12GB；目标机要能 ssh 到源机）。
   没有内网源就不带 `--from`：GitHub 拉 OpenTalking（锁 commit）、HF 镜像拉 QuickTalk、ModelScope 拉 SenseVoice / VoxCPM2，下载跑在一次性 `python:3.11-slim` 容器里不依赖宿主机环境；flash_attn wheel 要手动放。`--check` 只看缺什么
3. `TARGET=<机器> bash scripts/deploy.sh` 构建、启动。HuBERT 的 `.bin → .safetensors` 转换首次启动自动做；形象目录不用拷，节点上线后从其他节点对账拉
4. 要当控制面（主或备）就把地址加进 `scripts/hosts/cluster.env` 的 `CONTROL_URLS`，所有机器重新 `deploy.sh --no-build`
5. 新卡型先 `scripts/loadtest.py` 实测，再定该机 env 里的 `OT_MAX_SESSIONS_PER_INSTANCE`、`LOCAL_TTS_MAX`

## 常用操作（本地 Git Bash）
```bash
TARGET=205 bash scripts/deploy.sh             # 205：control + node + tts（同步代码、写 .env、构建、重启）
TARGET=206 bash scripts/deploy.sh --no-build  # 206：node + tts；--no-build 只同步 .env / 重建容器
ssh wwang@<GPU1_LAN_IP> 'docker logs -f --tail 100 talk-node'      # talk-control / talk-node / talk-tts
ssh wwang@<GPU1_LAN_IP> 'docker exec talk-control python /app/scripts/smoke.py --wav /w/cache/hello.mp3'
curl -sk https://<GPU1_LAN_IP>:8443/health      # 各节点实例、在线数、cap
```

## 实测（2026-10-15，3090）
- 新照片建形象 ~3s + 预热 ~10s（容器重启后第一次 ~30s，含加载模型）；预热过的形象建会话 <1s
- 说完话 → 数字人开口：MiniMax 音色 ≈ 1.4s；克隆音色 ≈ 3s（克隆模型和 QuickTalk 抢 GPU1，首帧渲染从 0.3s 拖到 1.3s）；edge 音色 ≈ 3s
  （识别 ~70ms，豆包 mini 首字 ~0.4s，MiniMax TTS 首包 ~0.15s，克隆 TTS 首包 ~0.19s，edge ~1.2s）
- 浏览器 WebRTC：540×900 @ ~23fps
- 不想点证书警告时可走隧道：`ssh -N -L 18080:127.0.0.1:8301 wwang@<GPU1_LAN_IP>`，打开 http://localhost:18080（localhost 也算安全上下文，能用麦克风）

## 注意
- OpenTalking 文档里写的 `transcribe?trigger_chat` 实际代码里没有；`/sessions/{id}/speak` 在会话 runner 里会先走 LLM 再播报
- 基础镜像锁 torch 2.5.1+cu121（服务器驱动 535 只到 CUDA 12.2），transformers 锁 4.57.3；
  因此 HuBERT 权重已转成 `chinese-hubert-large/model.safetensors`（transformers 不让 torch<2.6 读 .bin），重新下载权重后要再转一次
- 服务器网络对 HTTP/1.1 单连接限速 ~0.4MB/s（HTTP/2 不限），所以镜像里用 uv 并行装依赖、构建用 host 网络
- 语音识别在网关进程里做（`gateway/asr.py`），不用 OpenTalking 的 speak_audio_stream：后者和渲染/WebRTC 抢 GIL，3 秒语音要 1~2 秒才出字
- 方舟 plan key 能用的：doubao-seed-2-0-mini（默认，最快）、doubao-seed-2-1-pro/lite；flash/1.6 系列不支持
- 单张照片建形象走官方 `POST /avatars/custom`（base 形象 `office-woman` + `model=quicktalk`，生成静态模板视频）
- **动态形象**（`gateway/animate.py`，页面"动态形象"开关默认开）：QuickTalk 只改嘴，其余画面取自底板视频，照片底板是静帧所以只有嘴动。
  现在先用 MiniMax-H3-Max 图生视频（首帧=尾帧=照片，6s，便于无缝循环）生成眨眼/点头/手势的待机视频，再以 `video=` 建形象。
  全程约 40s（生成 ~20s + 建形象 + 逐帧人脸缓存预热 ~17s）；生成失败自动退回静态照片。手要动，照片里得拍到手。
  消耗 MiniMax 视频额度，每张照片一次。模型可用 `MINIMAX_VIDEO_MODEL` 换。
- 打字发送时 `/speak` 必须带 `voice`，否则 OpenTalking 用默认音色（会话创建时的 `tts_voice` 不会当默认）

## 音色（`gateway/tts.py`、`gateway/clone_tts.py`）
- OpenTalking 的 TTS 全部指向网关 `127.0.0.1:8301/v1/audio/speech`（openai_compatible、pcm 流）。网关按音色 ID 分流：
  `clone_*` → 本地克隆；`zh-CN-*Neural` → edge；其余 → MiniMax T2A（speech-2.8-turbo）。出错退回 edge 晓晓。
  不能只在单次 /speak 里切 provider：OpenTalking 的开场短句缓存用的是默认 provider，会和正文音色不一致。
- 预设音色按 儿童/少年/青年女/青年男/中年/老年 分组，都是 MiniMax 系统音色（`VOICE_GROUPS`）。
- **克隆**：MiniMax 这个 key 是 token 套餐，`voice_clone` 报不支持，所以本地做：Qwen3-TTS-12Hz-0.6B-Base
  + faster-qwen3-tts 0.3.2（CUDA graph，首包 ~0.19s、~2.7 倍实时；官方 qwen-tts 实现比实时还慢）。
  录音/上传 → 去开头静音、截 20 秒存 `state/voices/<id>.wav`，SenseVoice 识别出参考文本，登记在 `state/voices.json`。
  模型只在 8301 进程（GATEWAY_TTS=1）里加载，8443 的试听也转调 8301。权重从 ModelScope 下（HF 的 xet 在这台机器上卡死）。
  qwen-tts 用 `--no-deps` 装：它会拖进 CPU 版 onnxruntime 和 gradio；faster-qwen3-tts 0.4 起要 transformers 5。
- 打字发送时 `/speak` 必须带 `voice`，否则 OpenTalking 用默认音色（会话创建时的 `tts_voice` 不会当默认）

## 本地语音合成（`tts/`，容器 talk-tts，GPU0）
- VoxCPM2（2B，Apache-2.0）+ nano-vllm-voxcpm（MIT，连续批处理），网关经 `LOCAL_TTS_URL=127.0.0.1:8302` 调；
  `vox_*` 预设音色和 `clone_*` 克隆音色都走它，MiniMax/edge 音色照旧。不配 `LOCAL_TTS_URL` 时克隆退回旧的 Qwen3-TTS（单路串行）
- 预设音色：`tts/make_presets.py` 用 voice design（文字描述）各生成一段样音固定成参考（15 个，按年龄分组），
  不满意的用 `--only id` 重新生成；人设推荐音色自动映射到本地预设（`tts.LOCAL_MAP`）
- 构建上下文 `/mnt/hdd-16t/wwang/tts/build` 里的 `nanovllm-voxcpm/`（git clone）和 flash_attn 2.7.4 wheel 是一次性手放的
  （服务器下 GitHub release 会断）；权重从 ModelScope `OpenBMB/VoxCPM2` 下到 `/mnt/hdd-16t/wwang/tts/models/VoxCPM2`
- 双卡分工：GPU1 只做渲染，GPU0 只做语音（合成 16G + 识别 1.3G）。渲染和合成同卡会把渲染拖垮（实测渲染 1.1s/块 → 4~5s）
- 实测（3090，2026-10-16）：合成服务单独压 8 路首包 0.3s、RTF 0.23 不断音，16 路首包 0.6s；端到端 4 路同时说话
  合成首包 0.17s（MiniMax 0.42s），断音不比 MiniMax 多；6 路卡在 GPU1 渲染
- 压测：`scripts/ttsbench.py`（只测合成）、`scripts/bench.sh` + `loadtest.py`（端到端，含视频帧率和音频断音）

## 待机预编码与单卡在线上限（2026-10-17，205 实测）
- 待机会话原来每路约 0.7 核（72% 花在 VP8/Opus 编码），还拖慢同进程渲染。`patches/opentalking_patch.py` 补丁 4：
  每个形象的底板循环在后台只编一次 VP8（每 25 帧一个关键帧），待机按顺序取帧时直接发预编码数据，开口时强制关键帧；
  补丁 4b：连续 200ms 静音后 Opus 直接发预编好的静音包（时间戳自己数，静音帧照样喂重采样器）。`OPENTALKING_IDLE_PREENCODE=0` 关闭
- 结果（3090）：5 待机 + 3 说话 渲染超实时 18→0、卡顿 10→0；4 待机 CPU 362%→31%；客户端解码错误 0
- 仍有：4 待机 + 4 说话（说话槽全满）会卡——剩下的是 8 路收发包和渲染抢 Python 主线程（GIL），要彻底解决得把渲染和推流拆进程
- 所以单卡在线上限 = 2 ×「有待机时的说话路数」：网关 `OT_MAX_SESSIONS_PER_INSTANCE`（3090 设 6，TITAN V 设 3，4090 估 9），
  满了新用户收到 503「当前通话人数已满」，不拖累已在通话的人
- 压测：`scripts/mixtest.sh <标签> <待机路数> <说话路数> ...`

## 底板循环与不跳帧（`patches/opentalking_patch.py`，构建时打进镜像）
- 原版说话按 正放→倒放 往返取底板帧、待机按 0..N 循环播原视频，两边各自计数，开口/说完时画面会跳；往返时手势像"倒带"。
- 补丁：待机改为和说话共用 `session_state.frame_index`（准备回复期间停在当前帧，回复从下一帧接着往下）；
  `OPENTALKING_QUICKTALK_TEMPLATE_LOOP=wrap` 时一直正向循环。
- 动态底板生成时首尾帧都指定为照片；`animate.seamless_loop` 量首尾帧差，接不上（> 相邻帧差 90 分位）才做 1 秒交叉淡化。
- 浏览器实测（requestVideoFrameCallback 逐帧比对）：开口/说完处帧差 ~1.3，与正常播放一致；待机 30 秒无接缝突变，
  偶发的大帧差都对应 WebRTC 丢帧（82ms 间隔）。

## 人设模板（`gateway/personas.py`）
- 15 个风格：温柔知心（默认）、高冷御姐、元气少女、毒舌损友、霸道总裁、阳光学长、慈祥奶奶、幽默大爷、萌娃、职场导师、
  心理倾听师、新闻主播、深夜电台、古风侠客、英语陪练。每个 = 风格设定 + 共用口语规则 `RULES` + 推荐音色。
- 页面选模板 → 填进提示词框并换成推荐音色（之后仍可单独改音色）；手改提示词 → 标"自定义"，存在浏览器里。
- 提示词在建会话时以 `llm_system_prompt` 传给 OpenTalking；网关日志 `chat via ... system=...` 可确认生效。
- 豆包 mini 对风格要写具体（例词、必须做什么），太泛会退化成普通口吻（幽默大爷、英语陪练都调过）。

## 口型同步（`patches/opentalking_patch.py` 第 3 处）
- 现象：口型时准时不准。根因不在渲染（生成只占语音时长 ~30%，服务端音视频队列对齐到 20ms 内），在 WebRTC 时间戳：
  OpenTalking 的音频 RTP 时间戳只按已发样本数走，待机不发音频时停住，视频却一直走。Chrome 由此算出的音画差值
  是几十到上百秒（getStats 的 estimatedPlayoutTimestamp 差），超过 10 秒就放弃 A/V 同步，两路各播各的，
  音频缓冲一被网络抖动拉长，嘴就比声音快。
- 修法：视频时间戳取真实发送时刻；音频没语音时按真实时间补静音一直发（不能让音频 pts 跳：Opus 前的
  PyAV 重采样要求 pts 连续，跳了整条音频发送会卡死，队列只进不出）。
- 实测（Chrome getStats）：修前 Δplayout 34~184 秒乱跳、待机时音频全靠浏览器补帧；修后 Δplayout 0~2ms，
  跨待机、第二句、说话中途打断都稳定，Chrome 自动把视频缓冲目标从 21ms 调到 66ms 去对齐音频。
- 浏览器端自测：通话中在控制台跑 `S.pc.getStats()`，比较 audio / video inbound-rtp 的 estimatedPlayoutTimestamp。
## 全身实时通话（`gateway/vidu_live.py`，页面 `/rt`）

第二种通话方式：整个人由第三方实时数字人（Vidu S 实时版）生成，头、表情、身体一起动。语音识别、对话模型、合成、渲染和 RTC 都在对方那边，我们只提供照片、人设、音色和控制通道；按通话时长计费（1.5 点/秒）。页面上不出现供应商名字。

- **流程**：页面选形象/照片 → `POST /api/rt/profile` 自动配人设和音色（形象有固定人设就用它，否则多模态模型看照片生成，并从音色库挑最贴合的；所选音色首次自动克隆）→ 选了背景/服装就 `POST /api/rt/compose` 先生成一张换好装的照片（参考生图，脸相似度实测 0.82~0.97）→ `POST /api/rt/live` 建会话（返回阿里云 RTC 入会凭证）→ 页面用 AliRTC Web SDK 入会收发音视频，控制信号走 `WS /api/rt/ws/{live_id}`（服务端代理，浏览器 WebSocket 不能带鉴权头）。
- **稳定性**：背景/服装在通话前换进照片，通话中不换（实测通话中换参考图会让脸变年轻、衣服像平铺图贴上去）；所有形象只用细微待机动作，关闭按对方话语匹配动作；人设规则要求只说话、不拿东西；默认模型 `RT_MODEL=vidu-s1`（通话中脸的稳定性比 s2 好）；页面不提供打字输入（文字是对方"控制数字人行为"的通道）；同一个用户同时只保留一个会话；浏览器断开自动挂断，没人说话 5 分钟自动断。
- **已知限制**：对方的视频模型会照用户的话直接做动作（如"戴眼镜、拿杯子"会真的戴上/拿出来），现有接口参数挡不住；根治需要换"组件版"（我们自己的识别/对话/合成，对方只渲染推流，需要自己的阿里云 RTC 应用）。
- **部署准备**：
  - 密钥：写到控制面 `/w/state/vidu.key`（或环境变量 `VIDU_API_KEY`），不进代码库。
  - 音色样音：把合成服务的预设音色 `presets/<id>.wav` 和 `presets.json` 拷到 `/w/data/vidu_voices/`（`voices.json` 为音色库，`<id>.txt` 为样音文本）。
  - 预设背景/服装：`assets/rt_presets/` 拷到 `/w/data/rt_presets/`，或在控制面容器里跑 `scripts/rt_presets.py` 重新生成（MiniMax 生图）。
- **测试工具**：`scripts/rt_bargein_test.py`（打断后再提问，开录像）、`scripts/rt_mouth_motion.py`（录像里每秒嘴部动作量和脸部相似度）；建会话时传内部参数 `record: true` 可在结束后用 `GET /api/rt/lives/{id}/recording` 下载整场录像。
