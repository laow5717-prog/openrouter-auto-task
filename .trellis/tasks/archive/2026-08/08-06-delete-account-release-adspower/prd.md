# 删除账号时同步释放 AdsPower 环境

## Goal

账号列表的「删除」按钮目前只清 DB（card_bindings / platform_accounts / accounts），
不碰 AdsPower。每删一个跑过的账号，就在只有 12 格的环境配额里留下一个**孤儿环境**：
本地映射被 `reclaim_candidates` 第 0 档标记为可回收，但那要等到下一次撞配额才会真正
触发；在此之前配额是白占的。

本任务让删除动作同步做掉「stop + delete 远端环境 + 清本地映射」，删账号即释放占用。

## Requirements

### R1 后端：批量释放

- `AdsPowerProfilePool` 新增 `release_many(emails)`，语义与现有 `release(email)`
  (src/browser/adspower_driver.py:241) 一致，但一次 `_stop_all` + 一次 `delete_profiles`
  覆盖整批 —— 逐个调 `release` 会为每个账号各 `sleep(1.5)`，删 20 个账号要卡半分钟。
- 顺序红线沿用 `reclaim`：**远端删除成功后才清本地映射**。先清本地再删远端，一旦远端
  失败就留下查不到映射、也没人再删的孤儿环境，配额被永久吃掉。
- 返回 `{"released": [...], "skipped_busy": [...], "failed": [...]}`，供上层回报。

### R2 后端：占用中的账号跳过环境、账号照删

- `_is_busy(email)`（注入自 `AccountRegistry.is_claimed`）为真的账号，**不删远端环境**：
  删掉正在用的环境会让那个 worker 的浏览器凭空消失。
- 但账号 DB 行照删（用户的删除意图不因此被拒）。其映射变成孤儿，由现有
  `reclaim_candidates` 第 0 档在跑完后回收。
- 这些 email 通过 `skipped_busy` 回报给前端。

### R3 后端：删除接口接入

- `POST /api/accounts/delete` (src/api/routes.py:562) 在删 DB 行**之前**先做环境释放，
  且释放是 best-effort：任何 AdsPower 异常都不得阻断账号删除。
- AdsPower 未启用 / 客户端不可用（`_ensure_adspower()` 返回 `(None, None)`）时跳过
  释放，账号照删。
- 响应扩展为 `{"deleted": n, "adspower": {...}}`，`adspower` 至少含
  `released` / `skipped_busy` / `failed` 三个 email 列表；跳过时给出 `reason`。
  原有 `deleted` 字段语义不变（向后兼容）。

### R4 前端

- 单删 / 批删的 confirm 文案补上「并释放其 AdsPower 浏览器环境」。
- 删除返回后，若 `skipped_busy` 或 `failed` 非空，用 alert 提示哪些账号的环境没能释放
  及原因（运行中 / 删除失败），不能静默 —— 静默会让用户以为配额已经腾出来了。

## Non-goals

- 不改 `reclaim` 的候选判据与优先级。
- 不做「删除前自动停止正在跑的任务」。
- 不动本地 `data/profiles/<email>` 目录（那是非 AdsPower 路径的产物）。

## Acceptance Criteria

- [ ] 删除一个有 AdsPower 映射且空闲的账号后：远端环境消失、`adspower_profiles`
      对应行消失、`accounts` 行消失，响应 `adspower.released` 含该 email。
- [ ] 批量删除 N 个账号时，AdsPower 侧只发生一次 stop 批 + 一次 delete 批（不是 N 次
      各带 1.5s 等待）。
- [ ] 删除一个正被 worker 占用的账号：账号行被删、远端环境仍在、映射仍在，响应
      `adspower.skipped_busy` 含该 email，前端弹出提示。
- [ ] AdsPower 开关关闭时删除账号：接口 200、账号被删、无异常日志，`adspower.reason`
      说明未启用。
- [ ] `delete_profiles` 抛 `AdsPowerError` 时：账号仍被删、本地映射**保留**（不产生
      查不到映射的孤儿环境），响应 `adspower.failed` 含该 email。
- [ ] 没有 AdsPower 映射的账号（从未跑过）删除时行为与改动前一致。

## Notes

- 相关既有实现：`AdsPowerProfilePool.release` / `reclaim` / `_stop_all`
  (src/browser/adspower_driver.py:177-254)；孤儿映射回收判据见
  `AdsPowerProfileModel.reclaim_candidates` 的第 0 档注释。
- `_ensure_adspower()` 与 `account_registry` 都挂在共享状态上（`AppState.shared`），
  用 `get_app_state()` 取即可，无需按平台取 ctx。
