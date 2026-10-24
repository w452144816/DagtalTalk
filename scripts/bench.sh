#!/usr/bin/env bash
# 在 205 宿主机上跑：bash bench.sh <标签> <loadtest 参数...>
# 压测期间每秒采样 nvidia-smi，结束后汇总显存/利用率 + OpenTalking 日志里的渲染耗时
set -uo pipefail
TAG=$1; shift
HOME_DIR=$(cd "$(dirname "$0")/../.." && pwd)     # 脚本在 <TALK_HOME>/build/scripts 下
LOGS=$HOME_DIR/logs
OUT=$HOME_DIR/bench; mkdir -p "$OUT"
docker cp "$(dirname "$0")/loadtest.py" talk-demo:/tmp/loadtest.py >/dev/null
declare -A OFF
for f in "$LOGS"/opentalking*.log; do OFF[$f]=$(stat -c %s "$f"); done
nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader,nounits -l 1 > "$OUT/$TAG.gpu" &
SMI=$!
docker exec talk-demo python /tmp/loadtest.py "$@" > "$OUT/$TAG.json" 2>"$OUT/$TAG.err"
kill $SMI
grep -E "^(SUMMARY|AUDIO|IDLE)" "$OUT/$TAG.json"
grep -v "^SUMMARY" "$OUT/$TAG.json" | grep -q error && grep -o '"error[^}]*' "$OUT/$TAG.json" | head -3
awk -F', ' '{u[$1]+=$2; n[$1]++; if($2>pu[$1])pu[$1]=$2; if($3>pm[$1])pm[$1]=$3}
  END{for(g in n) printf "  GPU%s util_avg=%.0f%% util_max=%d%% mem_peak=%dMiB\n", g, u[g]/n[g], pu[g], pm[g]}' "$OUT/$TAG.gpu"
for f in "$LOGS"/opentalking*.log; do
  tail -c +$((${OFF[$f]} + 1)) "$f" | grep -o "render_chunk: total=[0-9]*" | cut -d= -f2 | sort -n |
    awk -v f="$(basename "$f")" '{a[NR]=$1; s+=$1} END{if(NR) printf "  %s render_chunk(1120ms音频) n=%d avg=%dms p50=%dms p90=%dms max=%dms over1120=%d\n", f, NR, s/NR, a[int(NR*.5)+1], a[int(NR*.9)+1], a[NR], c}
    {if($1>1120)c++}'
  tail -c +$((${OFF[$f]} + 1)) "$f" | grep -o "first_frame_from_api_wall_ms=[0-9]*" | cut -d= -f2 | sort -n |
    awk '{a[NR]=$1; s+=$1} END{if(NR) printf "  首帧(speak→第一帧) n=%d avg=%dms p50=%dms max=%dms\n", NR, s/NR, a[int(NR*.5)+1], a[NR]}'
  tail -c +$((${OFF[$f]} + 1)) "$f" | grep -o "TTS sentence done.*first_pcm=[0-9]*" | grep -o "first_pcm=[0-9]*" | cut -d= -f2 | sort -n |
    awk '{a[NR]=$1; s+=$1} END{if(NR) printf "  TTS首包 n=%d avg=%dms p50=%dms max=%dms\n", NR, s/NR, a[int(NR*.5)+1], a[NR]}'
done
