# 卡池跨分组移动

## Goal

支持把卡池中指定桶（待验证 / 有效）的卡片，按数量批量移动到**已存在**的目标分组，无需新建分组。

现状：`POST /api/card-pool/merge` 只能「合并源分组到一个新建分组」，且固定搬运「非无效」全部卡片，无法指定已有目标分组、无法限制数量、无法只搬某一个桶。本次手工需求（从 payment 组挑 100 张待验证卡移入 bind 组）只能靠直接写 SQL 完成，需要产品化。

## Requirements

### 功能

- R1 新增后端端点：把源分组中符合条件的卡片移动到已存在的目标分组。
- R2 选卡条件为「桶 + 数量上限」：
  - 桶取值 `unverified` | `valid` | `non_invalid`（后者等价于现有 `move_non_invalid_to_group` 的范围）。
  - 数量上限 `limit` 为正整数；实际可移动数不足时移动全部，不报错。
  - 取卡顺序固定 `ORDER BY id ASC`（导入顺序最早优先），保证可复现。
- R3 目标分组必须已存在；源分组与目标分组不能相同。
- R4 卡号去重：目标分组已存在同一 `card_number` 时，跳过该卡（不移动、不删除源行），并在响应中单独计数。
  - 约束来源：`card_pool` 上的 `UNIQUE(card_number, group_id)`。
  - 注意：现有 `move_non_invalid_to_group` 的做法是「删除源行」，本端点改为「跳过」，避免用户在只想移动 N 张时意外丢卡。
- R5 前端卡池页面提供「移动到分组」入口：选择目标分组（下拉，排除当前分组）、选择桶、填写数量，提交后刷新列表并提示移动/跳过数量。

### 约束

- C1 「待验证」不是 `status` 字段值，而是派生桶，判定口径必须复用 `CardPoolModel._bucket_where()`，不得在新代码中重写该 SQL。
- C2 整个移动在单个事务内完成。
- C3 不修改 `POST /api/card-pool/merge` 的现有行为与前端合并功能（避免回归）。
- C4 跨分组同卡号不变量：现有 `add_cards()` 通过 `find_cards_in_other_groups()` 保证同一卡号不跨组存在。本端点因 R4 跳过重复卡而不会破坏该不变量，但需在实现中确认这一点。
- C5 前端改动后必须 `cd frontend && npm run build` 重新构建到 `static/`。

### 非目标

- 不做勾选具体卡片的多选移动。
- 不做移动历史记录 / 撤销功能。
- 不改动卡片状态字段或 `valid_cards` 表。

## Acceptance Criteria

- [x] AC1 调用新端点，`bucket=unverified`、`limit=100`，源分组 payment、目标分组 bind：恰好移动 100 张待验证卡，两个分组的桶计数变化对得上。
- [x] AC2 `limit` 大于可用卡数时，移动全部可用卡并正常返回，不抛异常。
- [x] AC3 目标分组含同卡号时，该卡被跳过，源行仍在源分组，响应 `skipped` 计数正确。
- [x] AC4 目标分组不存在 / 源目标相同 / `limit` 非正整数 / `bucket` 非法：均返回 400 且不产生任何数据变更。
- [x] AC5 被移动的卡片不包含任何 `status IN ('expired','invalid')` 的卡，也不包含 `bucket=unverified` 下已登记在 `valid_cards` 的卡。
- [x] AC6 前端卡池页面可完成一次完整移动操作，列表与计数刷新正确，前端已重新构建。
- [x] AC7 现有 `POST /api/card-pool/merge` 行为未变。

## Notes

- 本次手工操作已用 SQL 完成（group 4 → group 2，100 张待验证卡），迁移前备份见会话 scratchpad。该 SQL 的筛选逻辑即本端点的参考实现。
- 已知邻近隐患（不在本任务范围，但实现时勿踩）：`CardPoolModel.mark_status_by_number()` 按 `card_number` 全表更新、不带 `group_id` 条件，同卡号跨组存在时会被一起改。
