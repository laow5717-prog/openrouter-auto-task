# 历史信用卡绑定记录页面

## Goal

新增独立的"绑卡记录"页面，跨所有任务查看全部信用卡绑定历史，支持多维度筛选、分页和导出。

## Requirements

### 前端
- 侧边栏新增"绑卡记录"菜单入口（路由 `/#/card-history`）
- 页面顶部统计卡片：总记录数、成功、失败、待处理
- 筛选栏：状态下拉、时间范围、关键词搜索（卡号/绑定邮箱）
- 分页表格列：序号、卡号(后4位)、状态、绑定账号、错误信息、处理时间、任务批次
- 导出当前筛选结果为 Excel

### 后端
- `GET /api/card/history` - 分页跨任务查询，支持 status/keyword/date_from/date_to
- `POST /api/card/history/export` - 导出筛选结果 Excel
- `CardBindingModel.get_all_paginated()` - 跨任务分页查询方法

## Acceptance Criteria

- [ ] 侧边栏可点击进入绑卡记录页面
- [ ] 显示所有历史任务的绑卡记录，默认按时间倒序
- [ ] 筛选条件组合生效，分页正常
- [ ] 导出 Excel 含完整卡信息、中文表头
- [ ] 前端构建无报错，页面正常渲染
