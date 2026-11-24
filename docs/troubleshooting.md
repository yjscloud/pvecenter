# 排查问题

按症状排，不按模块排。下面这些是实际踩过的，先对号，再动手。

> 返回 [README](../README.md) · 相关：[部署](deployment.md) · [配置项](configuration.md)

**面板能登录，但虚拟机、存储列表全是空的。** 先点「设置 → 环境自检」。这是 Proxmox API Token 最容易踩的坑：在 Web 界面创建 Token 时默认勾「特权分离」，这样的 Token 一开始没有任何权限。需要特权的读取返回 403，而 `/storage` 这类接口返回 200 加空数组，看着就像面板坏了。自检会向 PVE 要该凭据的有效权限，并列出该执行哪条命令：

```bash
pveum acl modify / --tokens 'root@pam!panel' --roles PVEVMAdmin
pveum acl modify / --tokens 'root@pam!panel' --roles PVEDatastoreUser
pveum acl modify / --tokens 'root@pam!panel' --roles PVEAuditor   # 节点指标需要 Sys.Audit
```

判断特征：`pveum token list` 里 `privsep` 为 1，且 `/access/permissions` 返回空。

**连接报 502，但 ping 得通 Proxmox。** 看跑面板的机器有没有设 `HTTP_PROXY` / `HTTPS_PROXY`，面板默认不继承这些变量。确实要过代理就把 `PVE_TRUST_ENV` 设为 `true`，否则把 Proxmox 地址加进 `NO_PROXY`。

**配置是对的，但连不上集群。** 连接配置的优先级是「数据库 > `.env`」，之前用别的地址保存过的话，`.env` 里的新值不生效。去「设置」页改，或删掉 `settings` 表里的 `pve_connection` 记录再重启。Proxmox 不可达不会阻止面板启动，只在日志里留一条警告 —— 这样才有机会进界面改回来。

**接了两套以上 PVE，某个操作报 `hostname lookup 'node02' failed`。** 节点名在每台 PVE 上都是本地的，这句话通常不是 DNS 坏了，而是请求打到了**另一台** PVE，那台上没这个节点名，PVE 就把它当主机名去解析。节点作用域的接口（存储、备份、控制台、模板向导、下载任务等）会按节点所属连接自动纠正，前端调用也会带 `X-PVE-Connection`。真遇到这个报错，先看该节点属于哪条连接，必要时在「节点」页确认连接状态，或把顶栏集群作用域切到对应那套再试。

**控制台打不开**：Proxmox 的设计限制，API Token 访问不了 `vncproxy`，要在设置里补一个 PVE 账号密码（建议只给 `PVEVMUser`）。**连上但黑屏**：面板建的模板只挂 `serial0`，**不会**把 `vga` 指过去 —— 指过去 VNC 里就只剩串口输出，而没配 `console=ttyS0` 的镜像从 GRUB 之后一片全黑。要看串口日志就在控制台里切到 xterm，别改 `vga`。

**模板建好了，克隆出来的机器停在 `Boot failed: not a bootable disk`**：镜像的扩展名和真实格式对不上。Ubuntu 官方的 `*-cloudimg-amd64.img` 实际是 qcow2，PVE 认不出这种后缀，把容器按裸盘整块复制成了系统盘，第一扇区不是引导扇区。用 `qemu-img info <镜像>` 看一眼格式，转成裸盘（`qemu-img convert -O raw`）或把后缀改成 `.qcow2` 后重建模板。想看盘本身对不对：

```bash
qemu-img info <卷路径>        # 虚拟容量应是镜像的虚拟大小（Ubuntu 云镜像 3.5G），不是文件字节数
qemu-img dd -f qcow2 if=<卷路径> bs=512 count=1 of=/tmp/mbr.bin && hexdump -C /tmp/mbr.bin | tail -2
```

末两字节应当是 `55 aa`（开头 `eb 63 90` 是 GRUB 的 MBR）；出现 `51 46 49 fb` 说明整只 qcow2 容器被原样写进了盘。

**Docker 部署，容器起不来或反复重启**：多半是数据库口令对不上。`docker compose logs panel | tail -30` 找 `Access denied` / `Can't connect`，改法见 [Docker 部署](deployment.md#docker-部署) 里那两条注意事项。

**面板跑在 Python 3.9 / 3.10 上，某些功能不正常（典型：告警邮件发不出、更新失败）。** 整套依赖按 Python 3.11+ 固定，最硬的限制是 Pillow 12 要求 ≥3.10 —— 3.9 上 `pip install -r backend/requirements.txt` 直接解析失败，依赖停在旧版本，一键更新也卡在装依赖这步。新版 `start-prod.sh` / `run.py` 会直接拒绝低版本解释器并给出安装命令；启动日志里的「运行环境：Python x.y.z」会写出实际解释器版本。装好 3.11+ 后 `rm -rf .venv` 再启动即可（数据库与配置不受影响）。

**主机安全页面读不到数据**：容器里 `/var/log`、`/proc` 属于容器而不是宿主机，页面会说明，需要时按 `docker-compose.yml` 里注释的挂载项只读挂进去。
