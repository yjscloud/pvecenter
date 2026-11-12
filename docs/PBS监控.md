# PBS 监控

面板对 Proxmox Backup Server（PBS）的接入分两层。理解这两层的差别，是判断"为什么某个数字看不到"的前提。

| | L1 间接（通过 PVE） | L2 直连（PBS API） |
|---|---|---|
| 前置条件 | PVE 里把 PBS 注册成存储 | 额外登记 PBS 只读 token |
| 凭据 | 无（复用 PVE token） | PBS 自己的 token |
| 能看到 | 容量 / 使用率、最近备份时间、归档数、校验状态、控制台地址 | 仓库容量与 GC 计划、节点负载与内存、最近备份/校验任务、磁盘健康、任务日志 |
| 看什么页面 | 存储页、备份页 | 备份页顶部「PBS 备份服务器」 |
| 失效时 | 退回"未对接" | 只影响 L2 区块，L1 照常 |

## 一、L1：什么都不用配

在 PVE 里 `数据中心 → 存储 → 添加 → Proxmox Backup Server` 之后：

- 存储页的「类型」列会显示蓝色 **PBS** 徽标；
- 「最近备份」列显示该存储里最新一份归档的时间与归档总数（非 PBS 存储显示 `—`）；
- 操作列多一个外链图标，直接打开 PBS 控制台；
- 备份页的「归档」表多一列「校验」：`已验证` / `校验失败` / `未校验`。目录型存储显示 `—`（它没有这个概念，不是"没验证"）。

这一层的读取都走 PVE，单节点或单存储读失败只影响它自己那一行。

## 二、L2：登记只读 token

### 1. 在 PBS 上建 token

```bash
# 1. 建用户（已存在时 PBS 会报 “already exists”，跳过即可）
proxmox-backup-manager user create pvecenter@pbs --password '<随机口令>'

# 2. 生成 token（已存在时先删再建；secret 只显示一次）
proxmox-backup-manager user delete-token pvecenter@pbs readonly
proxmox-backup-manager user generate-token pvecenter@pbs readonly

# 3. 授权给 token（ugid 必须带 !readonly 后缀）
proxmox-backup-manager acl update /datastore DatastoreAudit --auth-id 'pvecenter@pbs!readonly'
proxmox-backup-manager acl update /system    Audit          --auth-id 'pvecenter@pbs!readonly'

# 4. 确认权限真的落到了 token 上（输出为空就是没落上）
proxmox-backup-manager user permissions 'pvecenter@pbs!readonly'
```

其中 `generate-token` 的输出形如：

```
Result: {
  "tokenid": "pvecenter@pbs!readonly",
  "value": "0a4bb8ad-4636-4c24-addd-abf6ad4a7900"
}
```

**注意字段名是 `value`，不是 `secret`。** 面板里 Token ID 填 `tokenid`，Secret 填 `value`。secret 只在生成时显示一次；token 已存在时再执行 `generate-token` 只会报 `already exists` 而**不会**给出值，所以拿不到就先 `delete-token` 再建。

#### 授权必须落在 token 上

PBS 的 API token 默认开启权限分离（`privsep`）：**token 不继承用户的 ACL**，只认自己名下的条目。所以下面这种写法对 token 完全无效：

```bash
proxmox-backup-manager acl update /datastore DatastoreAudit --auth-id pvecenter@pbs   # 用户，不是 token
```

`acl list` 里能看到这条记录、"看着已经授权了"，但 token 调 API 仍然 403。这是最难查的一类：分开看两个地方都像是对的，只有用 token 实际请求才暴露。

唯一可靠的判定：

```bash
proxmox-backup-manager user permissions 'pvecenter@pbs!readonly'
```

输出只有表头 `Privileges with (*) have the propagate flag set` 后面空无一物，就是权限没落到 token 上。

#### 角色怎么选

`proxmox-backup-manager` **没有 `role list` 子命令**（会报 `no such command 'role'`），`SysAudit` / `PVEAuditor` 这两个名字也不存在 —— 写进去会报 `value 'SysAudit' is not defined in the enumeration`。PBS 4.x 实际可分配的角色是：

```
Audit  DatastoreAudit  DatastoreReader  DatastoreBackup  DatastoreAdmin
DatastorePowerUser  RemoteAudit  RemoteAdmin  RemoteSyncOperator
TapeAudit  TapeOperator  TapeAdmin  TapeReader  Admin  NoAccess
```

只读接入用这两个就够，**不需要 `Admin`**：

| 授权 | 得到的权限 | 换来什么 |
|---|---|---|
| `/datastore DatastoreAudit` | `Datastore.Audit` | 仓库列表与容量、GC 计划、校验状态、任务列表 |
| `/system Audit` | `Datastore.Audit` + `Sys.Audit` | 节点负载 / 内存 / 运行时长、磁盘健康 |

`Audit` 是纯审计角色，只含这两个 `.Audit` 权限，**不含 `Sys.Modify` / `Datastore.Modify`** —— 这是它与 `Admin` 的关键差别。本手册早期版本写过"PBS 没有只读的 system 角色，只能用 `Admin`"，那是错的：`Audit` 一直存在，只是没有 `role list` 能把它列出来。

> 想确认某个 auth-id 最终拿到什么权限，一律用 `user permissions <auth-id>` 看，别凭角色名猜：
>
> ```bash
> proxmox-backup-manager user permissions 'pvecenter@pbs!readonly'
> # Path: /datastore
> # - Datastore.Audit (*)
> # Path: /system
> # - Datastore.Audit (*)
> # - Sys.Audit (*)
> ```

#### 一个不影响使用的已知限制

`GET /nodes`（节点**列表**）在只授 `Audit` 时仍返回 403 —— 它要的权限比 `Sys.Audit` 更多，补上去就得引入写权限，不值得。这**不影响面板功能**：

- 采集按「配置里的节点 → `/nodes` 第一台 → `localhost`」回退，节点名填对即可；
- `/nodes/{node}/status`、`/nodes/{node}/tasks`、`/nodes/{node}/disks/list` 都只要 `Sys.Audit`，实测全部 200。

所以看到这个 403 不要急着加 `Admin`。

#### 其它注意

- **任务列表只需要 `Datastore.Audit`**：它走 `/nodes/{node}/tasks`，与 `Sys.Audit` 无关。只授 `/datastore` 时任务照常显示，不要因为"节点空着"就以为任务也读不到。
- **只授 `/system`、漏掉 `/datastore`** 时：`GET /admin/datastore` 返回**空数组而不是 403**，界面显示"没有仓库"，而 `GET /admin/datastore/{store}/status` 单独调是 403。遇到"有仓库但看不到容量"，先用 `acl list` 确认 `/datastore` 那一行存在。
- 面板的「测试」会分别探测仓库列表、仓库容量、任务、节点状态四项能力并附错误码（`pbs_permission_denied` / `HTTP 403` / `no_datastore_audit`），不会让你对着空白猜。仓库列表为空且权限表里没有 `/datastore` 时，概览会直接写"token 在 /datastore 上没有 Datastore.Audit"，而不是"没有仓库"。
- **不要给 `Datastore.Modify` / `Datastore.Prune`**。面板只发 GET，多出来的写权限没有任何用处，只会在凭据泄露时放大损失。
- **排查时不要拿数据库里的 secret 去 curl**。面板落库前会加密（`settings.pbs_connections` 里约 99 字符密文，解密后才是 36 位 UUID），直接拿去请求必然 401。要么用 `generate-token` 输出的明文，要么走面板的「测试连接」。

### 2. 在面板上登记

`节点 → 连接配置 → PBS 只读接入 → 添加 PBS`：

| 字段 | 说明 |
|---|---|
| 连接名称 | 用于区分多台 PBS；留空回落到主机地址（与 PVE 连接同名同义） |
| 区域 / 机房 | 与 PVE 连接的 `region` 同一语义：纯分组标签，留空 = 未分区；落库为连接的一个普通字段，不参与连接逻辑，老记录无此键时按空串读 |
| 地址 / 端口 | PBS 的 Web 地址；默认 8007 |
| Token ID | 形如 `pvecenter@pbs!readonly` |
| Token Secret | 只读 token 的 secret；保存后不再回显，编辑时留空表示沿用 |
| 证书 / CA（PEM） | PBS 自签时粘贴证书，用于钉扎；比关掉校验安全 |
| 校验证书 | 默认开。关掉存在中间人风险，仅建议隔离网络临时使用 |
| 节点 | 多节点 PBS 时指定读哪台；留空取第一台。**填错不会清零指标**：采集时按「填的节点 → `/nodes` 第一台 → `localhost`」依次回退，实际用了哪个写在概览行的 `node.name`，被忽略的名字记在 `node.name_ignored` |

> 节点名的回退顺序不是随手写的：`/nodes` 需要 Sys.Audit，缺权限时它直接 403，
> 拿不到真实节点名；而 `localhost` 是 PBS 对本地节点的官方别名（一台 PBS 只有一个
> 节点，它永远指向本机）。所以**手填的节点名只是偏好**，写错（比如把仓库名
> `pbs_backup` 填进节点）会被 PBS 回 400 —— 早先的实现在这种情况下把内存、负载、
> 运行时长、磁盘健康、任务与 GC 一起清零了，看起来就像「监控全丢了」。

点「测试」会依次调 `/version`、仓库列表、首个仓库的容量、任务与节点状态 —— 这几步都是 GET，不触发 PBS 侧的重活。返回的 `capabilities` 与 `probe_errors` 能直接指出是认证失败还是某一项权限缺失；认证失败时还会附一份凭据形状诊断（区分"填成账号密码" / "填成 token ID" / "整行粘贴带前缀"）。「保存」后概览缓存立即作废。

### 3. 先手工验证一次（可选，但能省很多时间）

在面板所在机器上直接打几次 API，最快区分「认证」「权限」「网络」三类问题。

**请求头是 `PBSAPIToken=<id>:<secret>`（冒号）**，照抄 PVE 的 `PVEAPIToken=<id>=<secret>`（等号）会一律 401 —— 这是排查时最常见的一次性错误。

```bash
TOK='pvecenter@pbs!readonly:<secret>'
BASE=https://10.0.0.5:8007/api2/json

curl -k -s -o /dev/null -w 'version      %{http_code}\n' -H "Authorization: PBSAPIToken=$TOK" $BASE/version
curl -k -s -w 'permissions  %{http_code}\n' -H "Authorization: PBSAPIToken=$TOK" $BASE/access/permissions
curl -k -s -w 'datastore    %{http_code}\n' -H "Authorization: PBSAPIToken=$TOK" $BASE/admin/datastore
curl -k -s -o /dev/null -w 'node status  %{http_code}\n' -H "Authorization: PBSAPIToken=$TOK" $BASE/nodes/localhost/status
```

判读：

| 结果 | 含义 | 下一步 |
|---|---|---|
| `/version` 200，`permissions` 返回 `{"data":{}}` | 认证通过，但 token 没有任何 ACL | 回到第 1 步，把 ACL 授给带 `!tokenname` 的 auth-id |
| `/version` 200，`permissions` 只有 `/datastore` | 缺 `Sys.Audit`，节点负载与磁盘健康为空 | `acl update /system Audit --auth-id '…!tokenname'` |
| `/admin/datastore` 返回空数组 | 缺 `Datastore.Audit`，或该 PBS 上确实没有仓库 | 先补 `/datastore` 授权；补完仍为空才是真没有仓库 |
| `/version` 401 | token ID / secret 不对 | `!tokenname` 是否漏了；secret 是否被换行污染；改过 ID 后是否重填 |
| `/version` 000 或连接被拒 | 网络 / 端口 / 防火墙 | 加 `--max-time 5` 重试；用 `cat < /dev/null > /dev/tcp/<host>/8007` 探端口 |

`000` 之外还有一种常见形态：**`No route to host` 或 ICMP 100% 丢包**（`ping` 全丢）—— 那是主机层不可达（机器关机 / 换了 IP），不是端口问题，调防火墙没有用。

### 4. 暂停接入（不删配置地停用）

维护、更换 token 期间不想让面板继续打这台 PBS 时，用卡片上的「暂停接入」，
不要删连接 —— 删了要重填 secret（它只显示一次）。

**为什么叫「暂停接入」而不是「维护模式」**：这个开关只作用于面板 —— 停发请求、
不告警、不计离线，PBS 本身该跑 GC 还跑。叫「维护模式」有两处误解风险：一是
PBS 仓库自身的 `maintenance_mode` 在概览里已经叫「维护模式」，同一个屏幕会出现
两个同名的东西；二是会让人以为去重、校验、GC 等上游动作也停了。真正的
「机器在维护」由上游自己上报，两者不要混用同一个词。

暂停后：

- **不再发任何采集请求**：概览、GC、磁盘、仓库容量全停，PBS 运行状态巡检
  （GC 失败 / 磁盘健康 / 容量越线）也跳过这台；
- **配置与凭据原样保留**：地址、token、区域、节点名、CA 都在，恢复后下一轮采集
  自动重连，不需要重新填写；
- **界面显示灰色「已暂停」**：不是红的「不可达」。概览的在线率、节点页排序、
  聚合带健康判定都不把它算进去 —— 主动停用不该被读成一台机器挂了；
- **多节点时自动折叠**：节点对比表里暂停的连接默认收起（只剩名称、状态徽标与
  各列摘要），正常接入的全部展开。暂停的连接不再产生数据，摊开只是一片「已暂停」
  占位，会把正常节点的明细挤出一屏。开关状态变化时折叠状态跟着变，但手动折叠过
  的连接不会被轮询刷新重新撑开；
- **活动告警保持原状**：暂停期间既不发「已恢复」也不清掉，恢复接入后重新判定
  （先清掉等于让一条真实故障静默消失）。

这条开关走的是独立端点 `POST /api/pbs/config/{id}/pause`，只改一条记录的一个字段：
整份 `PUT /config` 会覆盖所有连接，两个人同时编辑时开关会被对方那份旧数据带回旧值。
改动记进审计（`pbs.connection.pause` / `pbs.connection.resume`），只对 `pbs.manage` 开放。

> 与「不可达」的区别：不可达是**读失败**（网络、凭据、证书），需要排查；已暂停是
> **主动停用**，不需要处理。两者在界面上必须是两种颜色，否则维护窗口一开，
> 值班的人会去查一台根本没在用的服务器。

## 三、面板展示什么

备份页顶部是一张合并后的监控面板：上半部分是 PBS（连接 / 仓库 / 主机指标格 / 运行状态 / 最近任务），
下半部分直接接备份 KPI（备份文件、占用空间、重置虚拟机、近期成功率）——PBS 未对接时这张卡只留 KPI。
下面描述的是**单节点**的完整形态；登记了两台及以上 PBS 时，会先出现聚合带与节点对比表，
点行展开就是这一套（见下文「多节点」）。

- **连接行**：名称、在线状态、版本、地址、控制台外链，以及数据新鲜度；
- **仓库**：左侧主仓库——使用率大字 + 右侧三联容量（已用 / 剩余 / 总容量）+ 进度条，
  下方一行 GC 计划与是否"新备份自动校验"，维护模式在仓库名旁标胶囊；多仓库时其余仓库按行排列；
- **主机指标格**：右侧 2×2——内存（附 3px 用量细条 + 百分比）、1/5/15 分钟负载、节点名与核数、运行时长与在线状态；
- **运行状态**：面板下方整行两项常驻，左侧色点与数值同色：
  - 磁盘健康：正常（绿）显示「N 块盘正常」+ 容量 / 盘型，有盘 `health` 不是 `PASSED` 时转红并逐块列出；
  - 垃圾回收：最近一次 GC 的结果与开始时间（成功（绿）/ 失败（红，带原因）/ 进行中（蓝）/ 暂无记录（灰））。
  两项都常驻、且「读不到」用灰色而非绿色，是有意的 —— 只在异常时出现的话，
  「一切正常」和「这两项没采到」在界面上完全一样。
- **最近任务**：只列备份与校验（`backup` / `verify`），最多 8 条，点任意一条打开日志弹窗
  （只读，按需拉取，不缓存）；宽屏（≥1100px）自动排两列，任务行只有五段短信息，单列铺满会空掉右半边；
- **容量告警**：仓库使用率 ≥ 85% 标黄、≥ 95% 标红，顶部压成一条告警带
  （仓库名 · 使用率 · 阈值），正常时不占版面。

> **「仓库」是什么**：PBS 里的 datastore，即存放备份数据的目录（可为 ZFS 数据集、目录或
> 外挂盘）。备份、校验、GC 都以仓库为组织单位，所以容量与使用率是**按仓库**统计的，
> 一台 PBS 可以有多个仓库。它和 PVE 的「存储（storage）」不是一回事：PVE 存储是
> "PVE 往哪写"，PBS 仓库是"PBS 把备份存到哪"。
>
> 界面在仓库列表下方常驻一行小字说明这一点，避免新用户把两者混为一谈。

任务列表**只保留备份相关任务**：`backup`（备份）与 `verify`（校验）。`garbage_collection`、
`prune`、`sync`、`forget` 等仓库维护类任务不在此列 —— 它们的状态在 PBS 控制台看更合适，
混进来会让「最近备份成不成」这个问题被无关条目淹没。后端先取 60 条再过滤，避免过滤后
凑不满 8 条。

### 备份 KPI 的「占用空间」口径

早先这个数是**把归档行里的 `size` 直接相加**，PBS 上会明显偏大：PBS 返回的 `size` 是
**去重前**的逻辑数据量，10 份互相共享数据块的归档加起来能到 334 GB，而仓库实际只占
567 MB —— 同一批数据块被重复计数，甚至出现「占用空间 > 仓库总容量」的荒谬数字。

现在的口径是「盘上真实占用」，按存储类型分别取：

| 存储类型 | 数字来源 | 说明 |
|---|---|---|
| PBS（`type=pbs`） | 存储状态里的 `used` | PBS 已去重 / 压缩，就是仓库的真实占用，同一仓库只算一次 |
| 目录 / NFS / CIFS | 该存储上归档文件大小之和 | 本地文件本来就没有去重，`size` 就是真实字节 |

- **不跨节点累加**：共享存储（尤其 PBS）会被每个节点各报一行，按「连接 + 存储名」取最大值；
  累加会把一份容量算成 N 份。
- **读不到时如实标估算**：缺 `storage.view`、存储已卸载、仓库容量 403 时，退回归档大小之和，
  卡片 hint 会写「部分存储读不到实际占用，按归档大小估算」——不给出看起来精确的假数字。
- 卡片 hint 会写明当前是哪种口径（PBS 已去重 / 归档实际大小），因为去重后的数字
  通常比「加起来」小一个数量级，不解释反而像丢了数据。
- 列表里那一列改名为**「数据量」**并加了悬停说明：它仍然是归档自身的数据量（PBS 为去重前），
  不是盘上占用 —— 盘上占用看 KPI 卡。

### 多节点（登记了 ≥2 台 PBS）

一个 PBS 连接 = 一台 PBS 服务器 = 一个节点。**只有一台时保持上面的单体面板不变**
（没有可对比的对象，多一层表格只是噪音）；两台及以上自动切换成「聚合带 + 节点对比表 +
按需展开明细」三层结构。

**一、整体结构（自上而下）**

| 层 | 内容 | 回答什么问题 |
|---|---|---|
| 聚合带 | 在线节点数、仓库总数、**跨节点加权**容量与使用率、磁盘异常总数、GC 失败台数 | 全部节点加起来好不好 |
| 节点对比表 | 一行一台：健康点 · 名称 · 在线 · 版本 / 使用率条 + 百分比 / 剩余 / 磁盘 / GC / 最近任务 | 哪一台不一样 |
| 展开区 | 该节点的完整单体面板（主仓库三联容量、2×2 指标格、运行状态条、任务列表） | 这一台具体怎么回事 |

**二、节点间的区分与对比**

- **一眼定位问题**：每行开头是健康点 —— 红（离线 / 坏盘 / GC 失败 / 仓库越过 95%）、
  橙（仓库越过 85%）、灰（容量或磁盘状态读不到）、绿（正常）。
- **排序即优先级**：按「红 → 橙 → 灰 → 绿」排序，同色按名称；
  排序稳定，刷新不会让行来回跳。
- **列位置固定**：使用率、剩余、磁盘、GC 永远在同一列，纵向扫视即可横向比较；
  百分比按该节点**最紧张的仓库**上色，而不是看平均值。
- **默认展开第一行**：进来就有一份完整明细可看；其余行点一下展开，再点收起。
- **颜色只有一个来源**：`connDiskState` / `connGcState` / `connHealth` 三个判定同时喂给
  聚合带、表格行与单节点状态条 —— 避免「聚合说 2 块盘异常、行里却是绿的」。

**三、聚合与单节点明细的边界**

- 聚合容量是 **Σ已用 / Σ总容量**（加权），不是把各节点百分比取平均：
  一台 90% + 一台 10% 若按平均会显示成 50%，那是假象。
- 容量读不到的仓库（`total = 0`）不计入聚合，避免把「没权限」算成「0 容量」拉低使用率；
  一个仓库都读不到时聚合带直接显示「该版本不提供仓库容量」。
- 聚合只做**计数与求和**，不做跨节点去重：同一台 PBS 被登记两次的情况后端已按
  `host:port` 合并，不会在表里出现两行。

**四、节点增加后的扩展方式**

- 新增一台 PBS = 表里多一行，无需改布局；聚合带数字现算，不维护额外状态。
- 行高固定、不换行：列宽用 `minmax()+fr`，空间不足时按重要性**逐级收起**次要列
  （≤1100px 收起「最近任务」，≤900px 收起「剩余」，≤640px 折成三行：名称 / 使用率 / 状态点），
  不会出现横向滚动条。
- 明细按需渲染：只有展开的节点才挂完整的单体面板，节点多时也不会有 N 份图表同时渲染。
- 单节点 → 多节点的切换是自动的（看 `connections.length`），不需要用户手动选模式；
  从多节点退回一台时自动恢复单体布局。

### 集群节点页的 PBS 分组

节点页回答「机器活没活」，备份链路活没活是同一层问题 —— PBS 作为**最后一组**排在
所有 PVE 分组**下面**（`src/components/PbsStatusStrip.tsx`，由 `Nodes.tsx` 在主体
条件之后渲染）。原来它插在子页签正下方、抢在 PVE 前面，视觉上像另一个功能。

**交互与 PVE 节点一致**（这是这一版的重点）：

- **分组头** = `.tree-head.is-region`，整条可点、箭头旋转。右侧只有「刷新 / 管理」两个操作按钮，
  **不再挂**在线率 / 仓库数 / 容量 / 更新时间那一串统计徽章 —— 与上面 PVE 分组头保持
  同一节奏，明细都在展开后的卡片里。
  开合状态交给节点页的 `collapsedGroups`，所以页头「全部展开 / 收起」也管得到它。
- **三级与 PVE 完全同构**（区域 → 连接 → 节点）：
  - **二级 · 连接** = `PbsConnectionRow`，一条 `.tree-head`（与 PVE 连接行同款：整条可点、
    箭头随开合旋转），右侧是「区域 / PBS 徽章 + 在线 + 节点数 + 仓库越线」，默认收起；
  - **三级 · 节点** = `PbsNodeCard`，一张与 `NodeCard` 同款的 `Card`（`defaultOpen={false}`）：
    卡头是节点名 + 在线 + 越线 + CA 钉扎，副标题是 `host:port`、版本、读取节点、证书策略；
    卡体是三根 `.node-metric` 指标条（仓库使用率 / 内存 / 磁盘健康）+ 右侧 GC 与最近任务，
    「查看详情」在卡头右侧 —— **点行不再直接跳走**。
- 排序：不可达 → 仓库越线 → 其余，同档按名称；在线徽章、越线徽章
  （≥85% 黄、≥95% 红）与 PVE 节点卡同一套语义。

| 层级 | 内容 | 说明 |
|---|---|---|
| 连接行 | 名称 + 区域 / PBS 徽章 + 在线徽章 + 节点数 + 越线徽章 | 副标题（title）是 `host:port`、版本 |
| 节点卡头 | 节点名 + 在线 + 越线 + CA 钉扎 | 副标题是 `host:port`、版本、读取节点、证书策略 |
| 节点卡体左 | 仓库使用率 / 内存 / 磁盘健康三根条 | 磁盘健康用「正常盘占比」，非 100% 转红 |
| 节点卡体右 | 垃圾回收结果、最近任务、不可达原因 | 与 PVE 节点卡的运行时长 / 备注同一位置 |
| 分组头外侧 | 刷新 | 按钮不能嵌进 `tree-head` 那个 button；管理入口只在连接配置页，节点页不重复 |

- **指标读不到 ≠ 0**：内存与磁盘健康读的是 `/nodes/{node}/status` 与
  `/nodes/{node}/disks/list`，两者都要 `Sys.Audit`。PBS 回 403 时后端在概览行里写
  `status_hint` / `disks_hint = "no_permission"`，其它失败写 `error`；界面据此在标题栏
  挂「缺 Sys.Audit」徽章、并在对应指标下方写明原因，而不是只留一个「—」和「读不到」——
  后者会被当成面板故障。仓库容量与任务列表走 `Datastore.Audit`，不受影响。
- **不在这里做编辑**：凭据清洗、401/403 诊断、CA PEM 校验都在连接页，两套表单必然漂移；
  而且这一页面向所有有 `node.view` 的用户，token_id 属于凭据面。
- **权限与降级**：整块只在 `pbs.view` 时请求（默认仅管理员），无权限 / 403 / 上游失败
  一律不渲染；从未对接 PBS 时也不渲染；PVE 挂了 PBS 存储但没有直连凭据时只显示一枚
  「未登记只读凭据」徽章。PVE 查询加载中 / 报错时 PBS 分组照样显示 ——
  两条链路互不依赖。
- **响应式**：卡片走全局 `.card` / `.node-card-body` 的断点，不再另写一套 PBS 行样式；
  ≤1000px 分组头折成「通栏标题条 / 刷新一行」，避免按钮把
  「PBS 备份服务器」挤断。
- **刷新联动**：页头「刷新」会一并 invalidate `['pbs','overview']`。

### 连接配置页：卡片 / 列表切换

`/nodes/connections` 顶部加了 `SegmentedControl`，PVE 连接与 PBS 连接两张卡共用
同一个 `viewMode`（`localStorage` 记住偏好）。两种视图**共用同一份 DOM**：编辑、自检、
删除、节点展开按钮都带状态，复制一套 JSX 迟早会漏一个 —— 列表模式只是给外层 `Card`
挂 `.conn-view-list`，CSS 把同一张 `.set-conn-card` 重排成「身份 / 凭据标签 / 操作」
三段的一行；≤1000px 标签落第二行，≤640px 三段纵向排开。卡片视图保持原样。

### 阈值与告警

| 指标 | 警戒（黄） | 紧张（红） |
|---|---|---|
| 备份盘使用率 | ≥ 85% | ≥ 95% |

- 级别由后端判定（`pbs.py::usage_level`），随响应下发 `level` 与
  `thresholds`，前端只上色 —— 避免同一个 85% 在多个组件里各写一遍。
- 任一仓库越过警戒线时，连接标题下会出现一条 `Notice`，直接写明「哪块盘、用到多少、
  超过哪条线」；每块盘自身的边框也会跟着变色。

### 数据新鲜度

- 标题栏显示「更新于 x 分钟前」，时间取后端采集时刻 `generated_at`（未被 20 秒缓存放大），
  鼠标悬停显示绝对时间；旁边有手动刷新。
- 轮询间隔跟随顶栏的「刷新设置」（可关掉自动刷新）；后端另有 20 秒缓存兜底。

同一台 PBS 被多条配置登记时，后端按 `host:port` 合并成一行，并在标题后提示被合并的名称 —— 否则容量会看起来翻倍。

## 四、告警

两个后台巡检共用同一套告警通道（飞书 / 邮件 / Webhook / 站内信），可在告警页的「通知来源」里单独静默：

| 来源 | 频率 | 触发条件 | 走哪条链路 |
|---|---|---|---|
| 备份校验巡检 | 5 分钟 | 最近一份归档 `verification.state = failed` | PVE（无需 PBS 凭据） |
| PBS 运行状态 | 10 分钟 | 最近一次 **已结束** 的 GC 任务状态不是 `OK`；磁盘 `health` 异常；或备份盘（datastore）使用率 ≥ 85% / ≥ 95% | PBS 直连（需 L2 凭据） |

两者都在故障消失后发恢复通知（`PBS 运行状态` 的磁盘项在换盘、GC 项在重新 GC 成功后恢复，
容量项在清理 / 扩容后使用率回落到预警线以下恢复）。

每条告警正文都写清三件事：**异常类型**（GC 失败 / 磁盘健康 / 备份盘空间不足）、
**发生时间**（触发时刻）、**受影响对象**（PBS 主机 + 仓库名或磁盘设备）。

有意不报的：维护模式（用户自己设的）、订阅/版本提示、备份任务失败（PVE 侧的 `backup` 规则已覆盖）。

## 五、口径与已知限制

- **"最近备份"取的是最新 `ctime` 的那份归档**，不是"最近一次备份任务"；被 `forget` 掉的归档不算。
- **校验列只反映最新归档**。旧归档失败、新归档成功时会显示"已验证" —— 需要逐份核对请到 PBS 控制台。
- **namespace**：PVE 8.4+ / PBS 3.x 会在 volid 里多一段 `ns/<ns>/`。面板的类型判定只认 `/ct|vm/<vmid>/` 结构，不锚定 `backup/`，因此两种排法都能识别。
- **版本差异**：`/admin/datastore/{store}/status` 在较老版本上没有，此时仓库容量显示"该 PBS 版本不提供"，其余字段照常。
- **「总容量」= `avail` + `used`，不是 `status.total`**：PBS 的 `total` 是**文件系统**总块数，含 ext4 默认留给 root 的保留块（线上那台 294 GiB 里有 15 GiB 永远写不进去）。直接用它会出现「已用 259 MiB + 剩余 279 GiB ≠ 总容量 294 GiB」，看着像算错。PBS 自己的界面也是 `avail + used` 算总量（`total` 列是 calculate 出来的），面板跟随同一口径，两个页面的数字才对得上；容量告警的百分比分母也是这个自洽值。
- **不做磁盘 I/O 展示**：读写速率 / IOPS / 延迟属于主机指标，PBS API 多数版本也不下发 IOPS 与
  延迟；为了不让「备份盘还剩多少」被无关曲线挤走，这一块不再采集、不再展示。
- **多套 PVE 共用一台 PBS**：PVE 侧按 `server:port` 聚合，直连侧按 `host:port` 去重；告警按归档 volid / 磁盘设备去重，不会因为多套 PVE 重复轰炸。
- **网络**：面板后端必须能访问 PBS（8007）。PBS 只在内网时，L2 不可用属正常，L1 仍然工作。
- **只读**：面板不提供 prune / GC / verify / sync 的写操作，也不会去调这些端点。

## 六、排查

| 现象 | 先看什么 |
|---|---|
| 备份页没有 PBS 卡片 | 是否已对接（PVE 侧有没有 pbs 存储）；账号是否有 `pbs.view`（默认只给管理员） |
| 卡片显示"不可达" | 面板到 PBS 的 8007 是否通；token 是否过期；证书是否可信 |
| 测试连接返回 **401** | Token ID 是否形如 `user@realm!tokenname`（少 `!tokenname`、或 realm 写错都会 401）；Secret 是否为 `generate-token` 的一次性输出（**不是账号密码**）；复制时是否带了换行 / 空格；改过 Token ID 后是否重填了 Secret。手工 curl 时确认头是 `PBSAPIToken=<id>:<secret>`（冒号），别抄 PVE 的等号写法 |
| 测试连接返回 **403** | token 存在但权限不足。先跑 `proxmox-backup-manager user permissions 'user@realm!tokenname'`：**输出为空** = ACL 授给了用户而不是 token（token 默认权限分离）；只有 `/datastore` = 缺 `Sys.Audit` |
| 授权了但仍是 403 | 看 `acl list` 的 `ugid` 列有没有 `!tokenname` 后缀 —— 授给 `pvecenter@pbs` 对 token 无效 |
| 节点列表 403 / `/nodes` 读不到 | 预期行为，`Audit` 角色不含该接口所需权限；面板会自动回退到配置里的节点名或 `localhost`，不影响展示 |
| 手工 curl 一直 401，但面板「测试」是通的 | secret 用了数据库里的密文（`settings.pbs_connections` 里约 99 字符）。用 `generate-token` 输出的 36 位明文，或走面板「测试」 |
| 有仓库但没容量 | PBS 版本是否提供 `datastore/status`；token 是否有 `Datastore.Audit` |
| 有节点没磁盘 | token 是否缺 `Sys.Audit`（在 `/system` 授 `Audit` 角色即可，不必用 `Admin`）；PBS 版本是否提供 `/nodes/{node}/disks` |
| 任务日志打不开 | 多连接时必须带 `connection_id`（前端已自动带上）；日志接口不缓存，任务还在跑时可能只有几行 |
| 刚保存的凭据没生效 | 概览有 20 秒缓存，保存会立即作废；前端 60 秒轮询一次，可手动刷新 |
| 显示「已暂停」但没数据 | 这台被暂停接入了（不发请求是预期行为）；在连接配置页点「恢复接入」，下一轮采集自动重连 |
