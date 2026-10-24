#!/bin/sh
# keepalived 健康脚本：本机 talk-control 活着（/health 200）才有资格持有 VIP
curl -sk -m 2 -o /dev/null -w '%{http_code}' https://127.0.0.1:8443/health | grep -q '^200$'
