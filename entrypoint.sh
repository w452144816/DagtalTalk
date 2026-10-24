#!/usr/bin/env bash
# 容器入口，按 ROLE 起不同进程（同一个镜像）：
#   node    ：本机每张渲染卡一个 OpenTalking 实例（127.0.0.1:8000、8001……）+ 节点网关（内网 http 8301：心跳、反代、识别、TTS/LLM 回调）
#   control ：对外 https 8443 的控制面（页面、调度、素材分发），不用 GPU
set -euo pipefail
ROLE=${ROLE:-node}
mkdir -p /w/avatars /w/certs /w/logs /w/state /w/cache /w/data

if [ "$ROLE" = control ]; then
  if [ ! -f /w/certs/cert.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=talk" \
      -addext "subjectAltName=IP:${PUBLIC_HOST:-127.0.0.1},IP:127.0.0.1,DNS:localhost${VIP:+,IP:$VIP}" \
      -keyout /w/certs/key.pem -out /w/certs/cert.pem 2>/dev/null
  fi
  cd /app/gateway
  GATEWAY_ROLE=control uvicorn app:app --host 0.0.0.0 --port "${GATEWAY_PORT:-8443}" \
    --ssl-certfile /w/certs/cert.pem --ssl-keyfile /w/certs/key.pem --log-level info \
    > >(tee -a /w/logs/control.log) 2>&1 &
  GW=$!
  trap 'kill $GW 2>/dev/null' TERM INT
  wait $GW
  exit $?
fi

# ---------- node ----------
if [ -z "$(ls -A /w/avatars 2>/dev/null)" ]; then
  echo "seeding avatars from examples"
  cp -r /app/opentalking/examples/avatars/. /w/avatars/
fi

for f in quicktalk.pth repair.npy chinese-hubert-large/pytorch_model.bin auxiliary/models/buffalo_l/det_10g.onnx; do
  [ -e "$OPENTALKING_QUICKTALK_ASSET_ROOT/checkpoints/$f" ] || echo "WARN: missing QuickTalk weight $f"
done
# HuBERT：transformers 不让 torch<2.6 读 .bin，首次启动把 pytorch_model.bin 转成 model.safetensors（新机器 bootstrap 后自动做）
HUB="$OPENTALKING_QUICKTALK_ASSET_ROOT/checkpoints/chinese-hubert-large"
if [ -f "$HUB/pytorch_model.bin" ] && [ ! -f "$HUB/model.safetensors" ]; then
  echo "converting HuBERT pytorch_model.bin -> model.safetensors"
  python - "$HUB" <<'PY' || echo "WARN: HuBERT conversion failed"
import sys, torch
from safetensors.torch import save_file
d = sys.argv[1]
sd = torch.load(f"{d}/pytorch_model.bin", map_location="cpu", weights_only=True)
sd = {k: v.contiguous() for k, v in sd.items()}
save_file(sd, f"{d}/model.safetensors", metadata={"format": "pt"})
print("ok", len(sd), "tensors")
PY
fi

cd /app/opentalking
# OT_GPUS（容器内卡号，逗号分隔）：每张渲染卡起一个 OpenTalking 实例，端口 8000、8001……
# 不设则只起一个实例，用 .env 里的 OPENTALKING_*_DEVICE
OT_PIDS=()
if [ -n "${OT_GPUS:-}" ]; then
  IFS=, read -ra GPUS <<< "$OT_GPUS"
  URLS=()
  for i in "${!GPUS[@]}"; do
    port=$((8000 + i)); log=/w/logs/opentalking.log; [ "$i" -gt 0 ] && log=/w/logs/opentalking-$i.log
    CUDA_VISIBLE_DEVICES=${GPUS[$i]} OPENTALKING_UNIFIED_PORT=$port OPENTALKING_API_PORT=$port \
      OPENTALKING_TORCH_DEVICE=cuda:0 OPENTALKING_QUICKTALK_DEVICE=cuda:0 OPENTALKING_STT_SENSEVOICE_DEVICE=cuda:0 \
      opentalking-unified > >(tee -a "$log") 2>&1 &
    OT_PIDS+=($!)
    URLS+=("http://127.0.0.1:$port")
  done
  export OT_URLS=$(IFS=,; echo "${URLS[*]}")
  echo "opentalking instances: $OT_URLS (gpus $OT_GPUS)"
else
  opentalking-unified > >(tee -a /w/logs/opentalking.log) 2>&1 &
  OT_PIDS+=($!)
  export OT_URLS=http://127.0.0.1:${OPENTALKING_UNIFIED_PORT:-8000}
fi

cd /app/gateway
# 节点网关：0.0.0.0:8301。/internal/* 要 X-Node-Token；/v1/*（OpenTalking 的 TTS/LLM 回调）只认 127.0.0.1
export NODE_URL=${NODE_URL:-http://${PUBLIC_HOST:-127.0.0.1}:${GATEWAY_LOCAL_PORT:-8301}}
GATEWAY_ROLE=node uvicorn app:app --host 0.0.0.0 --port "${GATEWAY_LOCAL_PORT:-8301}" --log-level info \
  > >(tee -a /w/logs/node.log) 2>&1 &
GW=$!

trap 'kill ${OT_PIDS[*]} $GW 2>/dev/null' TERM INT
wait -n "${OT_PIDS[@]}" $GW
echo "a process exited, stopping container"
kill "${OT_PIDS[@]}" $GW 2>/dev/null || true
exit 1
