# 路线图

按重要性排，不代表排期，也没有时间承诺。

> 返回 [README](../README.md)

下一步想做的：

- SDN 网络。现在「网络」页管的是节点级 bridge / bond / VLAN 与 IP 地址池，PVE 8.x / 9.x 的 SDN（zone / vnet / subnet / controller / IPAM）还没有入口。它是集群级对象，一份配置作用于整个集群，所以底线是「读」永远可用、「写」必须有 diff 预览和二次确认，也不提供「一键重建 SDN」这类不可回滚的动作。
- 计费与用量。前置已经就绪：指标按周期落 `metrics_history`，额度按 CPU / 内存 / 磁盘统计；缺的是「按谁、按多少、值多少钱」这层计价与账单。最容易做错的是口径：PVE 给的网络流量是累计计数器，会因重启、迁移、驱动重置归零，必须靠采样差分并丢弃计数器回退的样本，否则会算出负数或暴涨的流量。
- 备份可验证的下一步是对接 PBS。现在已有「登记 + 指纹核对 + 面板层拒删 + 异常告警」，能发现删除与替换；静默位翻转得拿到 PBS 的 verification 校验和与 immutability 才看得见。
- 资源池（PVE pool）。租户隔离已经有一部分（归属表 + 区域管理员 + 额度 + 节点可见范围），缺的是池的管理界面和「按池隔离」；目前只有透传的只读 `GET /api/cluster/pools`。
- 其它候选：报表订阅（定时周报 / 月报，指标历史已经够用）、告警补 Telegram 原生通道（企业微信 / 钉钉 / Slack 现在用 Webhook 预设就行）、Prometheus 指标导出、审计外送 SIEM、LDAP / OIDC 单点登录、深色主题与主题切换、PWA。

已知的界面缺口（接口有，页面没做）：Ceph 集群状态（`GET /api/cluster/ceph/status` 已透传）；IP 池只有「从分给自己的池里挑地址」，没有「申请—审批—分配」流程。

想催哪一条，去 [Issues](https://github.com/yjscloud/pvecenter/issues) 新开一条，把使用场景写清楚，比在群里问有用。立项时理由和验收标准会补在同一条 issue 里。
