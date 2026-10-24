#!/usr/bin/env bash
# 混合压测：IDLE 路只在线不说话 + TALK 路同时说话，看待机会话会不会拖慢说话会话。在 GPU 服务器宿主机上跑：
#   bash mixtest.sh <标签> <待机路数> <说话路数> [loadtest 其它参数...]
# 例：bash mixtest.sh mix-5i3t 5 3 --voice vox_f_gentle --avatar <形象ID>
set -uo pipefail
TAG=$1 IDLE=$2 TALK=$3; shift 3
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$(cd "$HERE/../.." && pwd)/bench; mkdir -p "$OUT"
docker cp "$HERE/loadtest.py" talk-demo:/tmp/loadtest.py >/dev/null
HOLD=${HOLD:-260}                            # 待机会话在线时长，要盖住说话那 3 轮
if [ "$IDLE" -gt 0 ]; then
  docker exec -e LOADTEST_HOLD=$HOLD talk-demo python /tmp/loadtest.py -n "$IDLE" --rounds 0 --ot https://127.0.0.1:8443/ot "$@" \
    > "$OUT/$TAG.idle.json" 2> "$OUT/$TAG.idle.err" &
  IPID=$!
  sleep 25                                   # 待机会话先连上、跑稳
fi
OTPID=$(docker top talk-demo -eo pid,args | awk '/opentalking-unified/{print $1; exit}')
echo "== 只有待机 $IDLE 路时：OpenTalking 进程 CPU"
top -b -n 2 -d 3 -p "$OTPID" | awk -v p="$OTPID" '$1==p{c=$9} END{print "  opentalking cpu=" c "%"}'
if [ "$TALK" -gt 0 ]; then
  echo "== 再加 $TALK 路同时说话"
  (sleep 30; top -b -n 2 -d 3 -p "$OTPID" | awk -v p="$OTPID" '$1==p{c=$9} END{print "  说话期间 opentalking cpu=" c "%"}') &
  TOPPID=$!
  bash "$HERE/bench.sh" "$TAG" -n "$TALK" --rounds 3 --ot https://127.0.0.1:8443/ot "$@" | grep -E "SUMMARY|AUDIO|render_chunk|首帧|TTS首包"
  wait "$TOPPID"
fi
if [ "$IDLE" -gt 0 ]; then
  wait "$IPID"
  echo "== 待机会话自己的画面"
  grep -E "^IDLE" "$OUT/$TAG.idle.json" || tail -3 "$OUT/$TAG.idle.err"
fi
