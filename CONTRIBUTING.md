# 贡献指南

欢迎提 PR。这个文件讲清楚三件事：什么改动会被直接合并、本地怎么跑检查、PR 里写什么。

> 返回 [README](README.md) · 相关：[目录结构与测试](docs/development.md) · [部署](docs/deployment.md)

## 先对齐再动手

- 修错字、补文档、修明显的 bug：直接提 PR，不用先问。
- 改行为、加功能、动数据库表结构：先开一条 [issue](https://github.com/yjscloud/pvecenter/issues) 说清场景，等一句「可以」。省得你写完发现方向不对。
- 不要在一个 PR 里混三件事。功能 + 顺手重构 + 换格式，reviewer 没法判断哪部分该回退。

## 环境

后端 Python **3.11+**（3.9 / 3.10 会被 `start.sh` 和 `deploy.sh` 直接拒绝），数据库 MySQL 8 或 MariaDB，前端 Node **22.22+**。

开发模式跑起来：

```bash
./start.sh                 # 首次会自动建 .venv、装依赖，同时起后端和 Vite
```

只想连库跑后端的话，照 `backend/.env.example` 写一份 `backend/.env`，`DB_NAME` 必填。

## 提交前跑检查

CI（`.github/workflows/ci.yml`）跑的就是这两条，本地先过一遍能省一轮来回：

```bash
cd backend && ../.venv/bin/python -m pytest          # 不需要真机 PVE
npm run verify                                       # 类型检查 + 依赖图 + 构建
```

后端用例会连一个独立的 `<DB_NAME>_test` 库并自己建库；连不上数据库时相关用例会整体跳过 —— **跳过不等于通过**，要真跑就先确认 `backend/.env` 里的库能连上。

真机验证写清楚：改了虚拟化相关的东西，说明在哪个 PVE 版本、单机还是集群上试过；只跑了单测也照实说，这比含糊的「已测试」有用得多。

## 代码约定

- 提交信息沿用现有格式：`type(scope): 中文描述`，例如 `fix(ui): 可搜索下拉菜单改为浮层`、`feat(cert): 部署证书时先选目标 PVE 集群再选客户机`。type 用 `feat` / `fix` / `docs` / `refactor` / `test` / `chore`，能省掉 scope 就省。
- 注释写「为什么」，不写「这行在干什么」。仓库里那些长注释是在记踩过的坑，新坑也这么记。
- 业务代码写 SQL 占位符一律用 `?`，由 `backend/app/database.py` 翻成 `%s`，别直接写 `%s`。
- 新增写操作必须同时有：后端权限判断、审计记录。缺一个就会被 review 打回 —— 面板里每个改到 PVE 的动作都要能在审计里查到是谁做的。
- 涉及 PVE 接口的改动，说明依据：官方 API 索引、`pvesh`，还是实测。文档里没有、靠实测得出的，注明版本。

## PR 里写什么

按 [PR 模板](.github/pull_request_template.md) 填，重点是「怎么验证的」：跑了哪些用例、什么环境、哪部分是静态检查没上真机。界面改动附前后截图。

## 文档要跟着改

- 改了配置项、权限、接口、默认值：同步 README 的表格和 `docs/` 下对应页面。
- 中英文 README 一起改（`README.md` / `README.en.md`），两版口径不一致比只有中文更糟。
- 新增页面或功能，在 `docs/features.md` 补一节；英文大纲 `docs/features.en.md` 只翻标题层，功能变了记得一起更新。

## 安全漏洞

不要开公开 issue。按 [SECURITY.md](.github/SECURITY.md) 里的渠道私下报。

## 许可

项目以 [Apache-2.0](LICENSE) 发布。提交 PR 即表示同意你的改动以同一许可发布。
