# Design: 前端后台管理系统升级

## 技术方案

### 后端改动

#### `/api/accounts` 分页 & 筛选
- 新增查询参数: `page`(default 1), `page_size`(default 20), `status`, `keyword`, `date_from`, `date_to`
- AccountModel 新增 `get_paginated(page, page_size, **filters)` 和 `count_filtered(**filters)` 方法
- 返回格式: `{ data: [...], total, page, page_size }`

#### `/api/card/status` 分页 & 筛选
- 新增查询参数: `page`(default 1), `page_size`(default 20), `status`, `keyword`
- CardBindingModel 新增 `get_paginated_by_task(task_id, page, page_size, **filters)` 和 `count_filtered_by_task(task_id, **filters)` 方法
- summary 数据保持不变，额外返回分页信息

### 前端改动

#### 分页逻辑 (script.js 内函数)
- `renderPagination(containerId, { total, page, page_size }, onPageChange)` 渲染分页控件
- 包含: 上一页/下一页、页码按钮、总条数、每页条数选择器
- 最多显示 5 个页码按钮，超出用省略号

#### 筛选栏
- 账号管理: 搜索框 + 状态下拉 + 日期范围 + 查询按钮
- 绑卡记录: 搜索框 + 状态下拉 + 查询按钮

#### 样式
- CSS 新增: `.filter-bar`, `.pagination`, `.table-loading`, `.table-empty` 样式
- 保持现有设计语言（Inter 字体、orange 主色、圆角卡片）

## 数据流
1. 用户修改筛选条件 -> 重置 page=1 -> 调用 API
2. 用户点击分页 -> 保持筛选条件 -> 改变 page -> 调用 API
3. API 返回分页数据 -> 渲染表格 + 分页控件

## 兼容性
- 不改变现有 API 默认行为（不传分页参数时返回全部数据）
- 不影响轮询 `/api/status` 和任务启停逻辑
