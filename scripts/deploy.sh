#!/usr/bin/env bash
# 本地 → GPU 服务器：同步 talk/ 源码，生成服务器 .env（注入 key + 机器配置），构建并重启容器。
# 用法：[TARGET=205|206] [SERVICES="talk tts"] bash scripts/deploy.sh [--no-build]
# 每台机器的目录、显卡分配、IP 在 scripts/hosts/<TARGET>.env，覆盖进 .env（compose 也从 .env 读变量）
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TARGET=${TARGET:-205}
HOSTENV="$HERE/scripts/hosts/$TARGET.env"
[ -f "$HOSTENV" ] || { echo "没有 $HOSTENV"; exit 1; }
# shellcheck disable=SC1090
source "$HOSTENV"
HOST=$SSH
REMOTE=$TALK_HOME
TTS=$TTS_HOME

getkey() { grep -h "^$1=" "$ROOT/grader/.env" "$ROOT/hz/1/.env" 2>/dev/null | head -1 | cut -d= -f2- ; }

env_tmp="$(mktemp)"
trap 'rm -f "$env_tmp"' EXIT
cp "$HERE/.env.example" "$env_tmp"
for k in ARK_API_KEY MINIMAX_API_KEY; do
  v="$(getkey "$k")"
  [ -n "$v" ] && sed -i "s|^$k=.*|$k=$v|" "$env_tmp"
done
while IFS= read -r line; do           # 集群配置 + 机器配置：已有的键替换，没有的追加
  case "$line" in ''|'#'*|SSH=*|DEPLOY_SERVICES=*) continue ;; esac
  k=${line%%=*}
  if grep -q "^$k=" "$env_tmp"; then sed -i "s|^$k=.*|$line|" "$env_tmp"; else echo "$line" >> "$env_tmp"; fi
done < <(cat "$HERE/scripts/hosts/cluster.env" "$HOSTENV")

ssh "$HOST" "mkdir -p $REMOTE/{models,avatars,cache,certs,logs,state,data,build}"
tar -C "$HERE" -czf - Dockerfile docker-compose.yml entrypoint.sh .dockerignore gateway patches scripts/smoke.py scripts/loadtest.py scripts/v1test.py scripts/personatest.py scripts/bench.sh scripts/ttsbench.py | ssh "$HOST" "tar -C $REMOTE/build -xzf -"
scp -q "$env_tmp" "$HOST:$REMOTE/build/.env"
ssh "$HOST" "chmod 600 $REMOTE/build/.env; [ -d $REMOTE/build/opentalking ] || cp -r $REMOTE/opentalking $REMOTE/build/opentalking"

# 本地合成服务（talk-tts）：构建上下文里的 nanovllm-voxcpm/（和 Ampere 用的 flash_attn wheel）是一次性准备的，见 tts/Dockerfile
ssh "$HOST" "mkdir -p $TTS/{build,models,presets,cache}"
tar -C "$HERE/tts" -czf - Dockerfile server.py make_presets.py dialect_samples.py patch_volta.py attention_sdpa.py | ssh "$HOST" "tar -C $TTS/build -xzf -"
ssh "$HOST" "[ -d $TTS/build/nanovllm-voxcpm ] && [ -f $TTS/models/VoxCPM2/model.safetensors ]" \
  || echo "WARN: $TTS 下缺 nanovllm-voxcpm 源码 / VoxCPM2 权重，talk-tts 起不来"

# 评估中的渲染服务（flashhead / omnirt）：构建上下文在各自 HOME/build（源码和权重运行时挂载，见 docker-compose.yml）
FH=${FLASHHEAD_HOME:-/data/flashhead}; OM=${OMNIRT_HOME:-/data/omnirt}
case " ${SERVICES:-${DEPLOY_SERVICES:-}} " in *" flashhead "*)
  ssh "$HOST" "mkdir -p $FH/build $FH/cache"
  tar -C "$HERE/flashhead" -czf - Dockerfile flashhead_server.py | ssh "$HOST" "tar -C $FH/build -xzf -" ;;
esac
case " ${SERVICES:-${DEPLOY_SERVICES:-}} " in *" omnirt "*)
  ssh "$HOST" "mkdir -p $OM/build $OM/work $OM/checkpoints"
  tar -C "$HERE/omnirt" -czf - . | ssh "$HOST" "tar -C $OM/build -xzf -" ;;
esac

COMPOSE='cd '"$REMOTE"'/build && export TALK_UID=$(id -u) TALK_GID=$(id -g) && docker compose'
SVC=${SERVICES:-${DEPLOY_SERVICES:-}}   # 这台机器要起哪些服务（hosts/<机器>.env 的 DEPLOY_SERVICES），可用 SERVICES=talk 只部署部分
if [ "${1:-}" != "--no-build" ]; then
  # 不用 compose build：新版 compose 走 buildx bake，compose 里的 network: host 要 --allow=network.host 授权会直接失败；
  # 直接 docker build --network=host（镜像名和 compose 里的 image: 一致，up 用 --no-build）
  for s in ${SVC:-control talk tts}; do
    case "$s" in
      control|talk) [ "${built_demo:-}" ] || { ssh "$HOST" "docker build --network=host -t talk-demo:latest $REMOTE/build"; built_demo=1; } ;;
      tts) ssh "$HOST" "docker build --network=host -t talk-tts:latest $TTS/build" ;;
      flashhead) ssh "$HOST" "docker build --network=host -t talk-flashhead:latest $FH/build" ;;
      omnirt) ssh "$HOST" "docker build --network=host -t talk-omnirt:latest $OM/build" ;;
    esac
  done
fi
ssh "$HOST" "$COMPOSE up -d --force-recreate --no-build --remove-orphans $SVC && $COMPOSE ps"
