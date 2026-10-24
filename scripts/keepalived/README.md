# 控制面 VIP（keepalived）—— 需要 root，手动装一次

现在没有 VIP 时走的是应用层主备：`scripts/hosts/cluster.env` 的 `CONTROL_URLS` 第一个是主（205）、第二个是备（206），
节点同时向两个控制面心跳，备机探测主机 `/health` 连续 10 秒不可达就开始受理，主机回来就让回去；后端配同样的两个地址自己切。

有了 VIP 后端只配一个地址，切换对后端透明。两台机各执行一次（都要 sudo 密码）：

```bash
sudo apt-get install -y keepalived
# 205（主）
sudo install -m 644 keepalived-205.conf /etc/keepalived/keepalived.conf
# 206（备）
sudo install -m 644 keepalived-206.conf /etc/keepalived/keepalived.conf
sudo install -m 755 check_control.sh /etc/keepalived/check_control.sh
sudo systemctl enable --now keepalived
ip -4 addr show | grep <CONTROL_VIP>     # 主机上应能看到 VIP
```

然后把 `cluster.env` 改成 `CONTROL_URLS=https://<CONTROL_VIP>:8443`（只留 VIP），
两台机的 `PUBLIC_HOST` 不变（节点地址和 WebRTC 用的还是本机 IP），重新 `deploy.sh --no-build`。
控制面自签证书的 SAN 要包含 VIP：删掉两台机 `talk/certs/cert.pem` 让 entrypoint 重新生成（`PUBLIC_HOST` 之外还会加 `VIP` 环境变量里的地址）。

- `nopreempt`：205 恢复后不抢回，避免来回切；要切回去 `sudo systemctl restart keepalived`（在 206 上）
- `check_control.sh` 每 2 秒查本机 `talk-control` 的 `/health`，进程挂了也会漂，不只看机器通不通
- VIP <CONTROL_VIP> 选的是 2026-10-18 ping 不通的地址，正式用前和网管确认
