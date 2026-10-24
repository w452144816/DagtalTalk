#!/usr/bin/env bash
# 新机器准备：把代码仓库里不含的大文件（OpenTalking 源码、模型权重、合成服务的源码/权重/预设）放到位。
# 之后再 `TARGET=<机器> bash scripts/deploy.sh` 构建、启动。
#
# 用法（在本地 Git Bash 跑，目标机信息来自 scripts/hosts/<TARGET>.env）：
#   TARGET=207 bash scripts/bootstrap.sh --from <GPU1_LAN_IP>     # 内网：从已有节点 rsync（约 12GB，万兆几分钟）推荐
#   TARGET=207 bash scripts/bootstrap.sh                           # 公网：从 GitHub / HF 镜像 / ModelScope 下载
#   TARGET=207 bash scripts/bootstrap.sh --check                   # 只检查缺什么
#
# 需要准备的东西（都在服务器上，本地工程不含）：
#   $TALK_HOME/opentalking/                 OpenTalking 源码（datascale-ai/opentalking，锁定 commit，见 OT_COMMIT）
#   $TALK_HOME/models/quicktalk/checkpoints  QuickTalk 权重 + HuBERT + InsightFace buffalo_l（HF datascale-ai/quicktalk，约 2.9G）
#   $TALK_HOME/models/local-audio/iic__SenseVoiceSmall   识别模型（ModelScope iic/SenseVoiceSmall，约 0.9G）
#   $TTS_HOME/models/VoxCPM2                 合成模型（ModelScope OpenBMB/VoxCPM2，约 4.7G）
#   $TTS_HOME/build/nanovllm-voxcpm          合成服务依赖源码（GitHub a710128/nanovllm-voxcpm）
#   $TTS_HOME/build/flash_attn-*.whl         可选，Ampere 及以上用（Volta 不装，用 VOX_ATTN=sdpa）
#   $TTS_HOME/presets                        预设音色样音（从已有节点拷；没有就 tts/make_presets.py 重新生成）
# HuBERT 的 pytorch_model.bin → model.safetensors 转换由 entrypoint.sh 首次启动时自动做（torch<2.6 读不了 .bin）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
TARGET=${TARGET:?请指定 TARGET=<机器>（scripts/hosts/<机器>.env）}
HOSTENV="$HERE/scripts/hosts/$TARGET.env"
[ -f "$HOSTENV" ] || { echo "没有 $HOSTENV：先照 205.env 建一份（SSH、TALK_HOME、TTS_HOME、PUBLIC_HOST、显卡分配、DEPLOY_SERVICES）"; exit 1; }
# shellcheck disable=SC1090
source "$HOSTENV"
HOST=$SSH; T=$TALK_HOME; S=$TTS_HOME
OT_COMMIT=${OT_COMMIT:-8c739a5a6f114daf71aeace832a668c3ad60f536}   # 2026-09-04 的 opentalking，补丁按这个版本写的
FROM=""; CHECK=0
while [ $# -gt 0 ]; do case "$1" in --from) FROM=$2; shift 2 ;; --check) CHECK=1; shift ;; *) echo "未知参数 $1"; exit 1 ;; esac; done

remote() { ssh "$HOST" "$@"; }
have() { remote "test -e $1" 2>/dev/null; }

echo "== $TARGET ($HOST) 检查"
missing=()
for p in "$T/opentalking/pyproject.toml:OpenTalking 源码" "$T/models/quicktalk/checkpoints/quicktalk.pth:QuickTalk 权重" \
         "$T/models/quicktalk/checkpoints/auxiliary/models/buffalo_l/det_10g.onnx:InsightFace buffalo_l" \
         "$T/models/quicktalk/checkpoints/chinese-hubert-large/pytorch_model.bin:HuBERT" \
         "$T/models/local-audio/iic__SenseVoiceSmall/model.pt:SenseVoice" "$S/models/VoxCPM2/model.safetensors:VoxCPM2" \
         "$S/build/nanovllm-voxcpm/pyproject.toml:nanovllm-voxcpm 源码" "$S/presets/presets.json:预设音色"; do
  path=${p%%:*}; name=${p#*:}
  if have "$path"; then echo "  ok   $name"; else echo "  缺   $name  ($path)"; missing+=("$path"); fi
done
have "$S/build/flash_attn-*.whl" && echo "  ok   flash_attn wheel" || echo "  可选 flash_attn wheel（Ampere+ 建议有；Volta 不需要）"
[ ${#missing[@]} -eq 0 ] && { echo "都齐了，可以 deploy"; exit 0; }
[ $CHECK = 1 ] && exit 2

remote "mkdir -p $T/{models/quicktalk,models/local-audio,avatars,cache,certs,logs,state,data,build} $S/{build,models,presets,cache}"

if [ -n "$FROM" ]; then
  # ---------- 内网：从已有节点 rsync ----------
  SRC_T=${FROM_TALK_HOME:-}; SRC_S=${FROM_TTS_HOME:-}
  if [ -z "$SRC_T" ]; then      # 从 hosts/*.env 里找源机器的目录
    for f in "$HERE"/scripts/hosts/*.env; do
      if grep -q "^SSH=.*@$FROM\$" "$f"; then SRC_T=$(grep ^TALK_HOME= "$f" | cut -d= -f2); SRC_S=$(grep ^TTS_HOME= "$f" | cut -d= -f2); fi
    done
  fi
  [ -n "$SRC_T" ] || { echo "不知道 $FROM 上的目录，设 FROM_TALK_HOME / FROM_TTS_HOME"; exit 1; }
  echo "== 从 $FROM 同步（目标机直接拉源机，要求目标机能 ssh 到源机；不行就先 ssh-copy-id）"
  remote "command -v rsync >/dev/null || { echo '目标机没有 rsync'; exit 1; }"
  remote "rsync -a --info=progress2 -e 'ssh -o StrictHostKeyChecking=no' --exclude .git --exclude node_modules --exclude __pycache__ wwang@$FROM:$SRC_T/opentalking/ $T/opentalking/"
  remote "rsync -a --info=progress2 -e 'ssh -o StrictHostKeyChecking=no' wwang@$FROM:$SRC_T/models/ $T/models/"
  remote "rsync -a --info=progress2 -e 'ssh -o StrictHostKeyChecking=no' wwang@$FROM:$SRC_S/models/ $S/models/"
  remote "rsync -a --info=progress2 -e 'ssh -o StrictHostKeyChecking=no' wwang@$FROM:$SRC_S/presets/ $S/presets/"
  remote "rsync -a --info=progress2 -e 'ssh -o StrictHostKeyChecking=no' --exclude .git --include='*/' --include='*.whl' --include='nanovllm-voxcpm/**' --exclude='*' wwang@$FROM:$SRC_S/build/ $S/build/"
  # 形象目录不同步：节点上线后会从其他节点自动对账拉取（cluster.py）
else
  # ---------- 公网：逐项下载（下载器跑在一次性容器里，不依赖宿主机 python）----------
  DL='docker run --rm -e HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com} -e PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple -v '"$T"':/t -v '"$S"':/s python:3.11-slim bash -c'; FIX='&& chown -R $(id -u):$(id -g) /t /s'
  have "$T/opentalking/pyproject.toml" || remote "git clone https://github.com/datascale-ai/opentalking.git $T/opentalking && git -C $T/opentalking checkout $OT_COMMIT"
  have "$T/models/quicktalk/checkpoints/quicktalk.pth" || remote "$DL 'pip -q install huggingface_hub[cli] && hf download datascale-ai/quicktalk --local-dir /t/models/quicktalk/checkpoints $FIX'"
  have "$T/models/local-audio/iic__SenseVoiceSmall/model.pt" || remote "$DL 'pip -q install modelscope && python -c \"from modelscope import snapshot_download; snapshot_download(\\\"iic/SenseVoiceSmall\\\", local_dir=\\\"/t/models/local-audio/iic__SenseVoiceSmall\\\")\" $FIX'"
  have "$S/models/VoxCPM2/model.safetensors" || remote "$DL 'pip -q install modelscope && python -c \"from modelscope import snapshot_download; snapshot_download(\\\"OpenBMB/VoxCPM2\\\", local_dir=\\\"/s/models/VoxCPM2\\\")\" $FIX'"
  have "$S/build/nanovllm-voxcpm/pyproject.toml" || remote "git clone --depth 1 https://github.com/a710128/nanovllm-voxcpm.git $S/build/nanovllm-voxcpm"
  if ! have "$S/build/flash_attn-*.whl"; then
    echo "  flash_attn wheel 请手动下载（GitHub release 直连常断）："
    echo "  https://github.com/Dao-AILab/flash-attention/releases/download/v2.7.4.post1/flash_attn-2.7.4.post1+cu12torch2.5cxx11abiFALSE-cp311-cp311-linux_x86_64.whl → scp 到 $HOST:$S/build/"
  fi
  have "$S/presets/presets.json" || echo "  预设音色：从已有节点拷 $S/presets/（6MB），或部署后在 talk-tts 里跑 tts/make_presets.py 重新生成"
fi
echo "== 完成，再跑一次 --check 确认，然后 TARGET=$TARGET bash scripts/deploy.sh"
