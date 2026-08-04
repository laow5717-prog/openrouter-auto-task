# AdsPower 配置迁到 UI（key/开关/地址）

## Goal

把 AdsPower 的 `api_key` / `enabled` / `base_url` 从 `config.yaml` 提到 UI 上可配、可保存，
落库覆盖 yaml 默认值，并让改动**当场生效**而不必重启进程。

## Background

现状是这三项只能改 `config.yaml`（`src/config.py::AdsPowerConfig`，`cfg.adspower.*`）。
打包分发后这个文件在 `~/.openrouter-auto-task/config.yaml`，换一台机器、换一个 AdsPower
账号，都得让用户手工找到并编辑一个 YAML 文件——这是本需求的由来。

三处关键现状：

| 事实 | 位置 | 为什么要紧 |
|---|---|---|
| `AdsPowerClient` 与环境池**惰性创建后缓存在共享状态上** | `src/web/app.py:286-307` | 不失效缓存的话，UI 存了新 key 进程仍用旧的，表现为「保存成功但毫无变化」 |
| `adspower_enabled` 直接读 `cfg.adspower.enabled` | `src/web/app.py:282-284` | 开关要能从 UI 改，这个读取点必须换成有效值 |
| 项目**没有任何 settings 表 / 配置接口** | — | 这套持久化要从零搭 |

## Requirements

### R1 — 三项配置可在 UI 上编辑并保存

- `api_key`（AdsPower 客户端「自动化 - API - API Key」）
- `enabled`（总开关；关掉即回退本地 Chrome 持久 profile，代码路径与接入前一致）
- `base_url`（本地 API 地址，默认 `http://local.adspower.net:50325`）

### R2 — 落库覆盖 yaml，yaml 保留为默认值

- 生效值 = **DB 有值则用 DB，否则用 `cfg.adspower.*`**。
- 不回写 `config.yaml`。那个文件是手写的、注释密集（`config.example.yaml` 里每项都有多行
  说明），`yaml.safe_dump` 会把注释全部抹掉——用户下次打开配置文件会发现说明没了。
- 未在 UI 上设置过的项必须原样回落 yaml，**不能**因为 DB 里没有就变成空串。

### R3 — 保存后当场生效，不需要重启

- 保存 `api_key` / `base_url` 后，下一次建浏览器必须用新值。
- 实现上要让缓存的 `AdsPowerClient` / `AdsPowerProfilePool` 失效并按新值重建。
- 有任务正在运行时保存：不打断在飞的会话（它们持有的 client 引用继续用到结束），
  新值对**之后**创建的会话生效。

### R4 — key 明文回显、明文提交

**本条在实现过程中被推翻过一次，记下缘由以免将来又绕回去。**

初版做的是掩码回显（`d6c9…4f8f`）+「提交掩码原值视为未修改」，理由是防止界面上
一长串明文 key 被连同截图发出去。用户明确要求改回明文，采纳，理由成立：

- 这是**本机单人**使用的工具，同一个库里 GitHub 密码、邮箱密码本来就明文躺着，
  单给这一个字段打码挡不住任何真实威胁；
- 掩码强制引入「提交上来的到底是新 key 还是掩码」的判断，判错一次就把用户的真 key
  覆盖成 `d6c9…4f8f` 这种串——为了防一个假想风险，造出一个真实的破坏路径；
- 明文回显还有实际好处：key 填错时一眼能看出来。

「密钥要打码」是个很强的直觉，所以配了一条测试钉住明文，免得将来被顺手加回去。

### R5 — 连通性自检（可选但推荐）

- 保存后能一键测试「客户端是否可达、key 是否有效」，直接给出可读结果。
- 没有它的话，用户填错 key 只会在下一次跑任务时看到一句浏览器起不来的报错，
  与配置页隔了十万八千里。

## Constraints

- **`enabled=false` 时的行为必须与接入前逐字一致**：`browser_factory()` 返回 `None`，
  下游一律走 `create_driver_vanilla` 本地 profile。这是唯一的回退手段。
- 数据库改动走既有迁移机制（`_MIGRATIONS` 加版本号，`ADD COLUMN` / `CREATE TABLE` 幂等）。
- 配置读取点是**跨平台共享**的（`AppSharedState`），改动不得让两个平台各拿一份。
- 不改 AdsPower 的配额仲裁、环境回收、代理绑定等既有逻辑。

## Non-Goals

- 不把 `group_id` / `reclaim_batch` / `total_quota` / `platform_quota` /
  `quota_wait_seconds` / `ua_systems` 搬到 UI（用户明确只要 key + 开关 + 地址）。
  但持久化层要设计成**通用键值**，将来加项只是多配一个字段，不用再改表结构。
- 不做配置项的权限控制 / 多用户隔离——本应用是单人本地使用。
- 不加密存储。DB 里已经明文存着 GitHub 密码与邮箱密码，单独给这一个字段加密是
  安全剧场，反而给人「其它字段是安全的」的错觉。

## Acceptance Criteria

- [ ] AC1：UI 上能看到并编辑 `api_key` / `enabled` / `base_url` 三项，保存后刷新页面仍在。
- [ ] AC2：DB 里没设置过的项，生效值等于 `config.yaml` 里的值（不是空串）。
- [ ] AC3：DB 里设置过的项，生效值等于 DB 的值，`config.yaml` 文件内容**未被修改**。
- [ ] AC4：保存新的 `api_key` 后，无需重启进程，下一次建浏览器使用新 key
      （缓存的 client/pool 已失效重建）。
- [ ] AC5：保存 `base_url` 同样触发重建。
- [ ] AC6：仅保存无关项（如只改 `enabled`）不应无谓地重建 client——避免打断正在跑的任务。
      注：`enabled` 由 `False→True` 时仍需保证下次能正常建池。
- [x] AC7：GET 配置时 `api_key` 返回**明文**（R4 已推翻掩码方案）。
      → `test_settings.py::test_api_key_is_returned_in_plaintext`
- [x] AC8：字段缺席=不动、传空串=清除覆盖回落 yaml、传值=覆盖，三者语义区分。
      漏掉这个区分的话，前端只想改开关也会把 key 一并抹掉。
- [ ] AC9：`enabled=false` 时 `browser_factory()` 返回 `None`，全链路走本地 profile。
- [ ] AC10：连通性自检能区分「客户端没开 / key 无效 / 正常」三种结果并给出可读文案。
- [ ] AC11：迁移在已有生产库副本上可重复执行（幂等），既有数据不受影响。
- [ ] AC12：既有测试全绿；新增测试覆盖覆盖层、掩码、缓存失效三处。
