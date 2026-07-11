# Implementation Plan

## Step 1: 后端 Model 层 - 分页查询方法
- [ ] AccountModel 添加 `get_paginated()` 和 `count_filtered()`
- [ ] CardBindingModel 添加 `get_paginated_by_task()` 和 `count_filtered_by_task()`
- 验证: 方法可被正确调用

## Step 2: 后端 API 路由 - 分页参数
- [ ] `/api/accounts` 读取分页和筛选参数，调用新方法，返回分页格式
- [ ] `/api/card/status` 读取分页和筛选参数，调用新方法，返回分页格式
- 验证: curl 测试 API 返回分页数据

## Step 3: 前端 CSS - 筛选栏和分页样式
- [ ] 添加 `.filter-bar`, `.pagination`, `.table-loading`, `.table-empty` 样式

## Step 4: 前端 JS - 分页和筛选逻辑
- [ ] `renderPagination()` 通用分页渲染函数
- [ ] 改造 `loadAccounts()` 支持分页和筛选
- [ ] 改造 `loadCardStatus()` 支持分页和筛选
- [ ] 筛选条件变化时重置到第 1 页

## Step 5: 前端 HTML - 筛选栏和分页容器
- [ ] 账号管理页添加筛选栏（搜索 + 状态 + 日期范围）
- [ ] 账号管理页添加分页容器
- [ ] 绑卡记录页添加筛选栏（搜索 + 状态）
- [ ] 绑卡记录页添加分页容器

## Step 6: 验证
- [ ] 账号列表分页和筛选正常工作
- [ ] 绑卡记录分页和筛选正常工作
- [ ] 现有功能不受影响
