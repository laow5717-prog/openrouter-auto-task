# 每账号独立 HTTP 代理 IP

## Goal

每个账号处理(注册+充值)时用一个独立 HTTP 代理出口 IP,避免多账号同 IP 被风控关联。
用户提供 ~100 个 `user:pass@host:port` 代理(i-proxy 网关不同端口=不同出口 IP)。
完整技术方案见 plan 文件 hashed-dazzling-hamster.md。

## Requirements

- R1 proxies 表(database.py _SCHEMA_V11)+ ProxyModel:粘贴导入 `user:pass@host:port`
  (兼容纯冒号)、去重、增删查、可用列表。
- R2 ProxyRegistry(worker.py,仿 PaymentCardRegistry):运行时代理排他,
  acquire_free 领空闲、release 释放、并发不重复授予。
- R3 proxy dict `{server,username,password}` 从 pipeline 顶层透传到
  launch_persistent_context:driver 两入口 + registration/signup 中间层 + app.py 三顶层方法。
- R4 run_daily_pipeline._produce 领账号时同步领空闲代理(produce_lock 内),_do 用完释放;
  全忙时 account_id 取模兜底(循环复用);无代理则直连。注册与充值都走代理。
- R5 UI 代理管理页(Proxies.vue):粘贴导入 + 列表(凭据打码)+ 删除/清空;路由+导航。

## Acceptance Criteria

- [x] 导入用户真实格式(带 @)与纯冒号变体、去重;非法行跳过计数。
- [x] ProxyRegistry 并发排他:两 worker 领到不同代理,全忙返回 None,结束后池空。
- [x] driver 两栈(Patchright/原生)launch_persistent_context 接受 proxy;版本 1.61 支持。
- [x] 真机:2 worker 并行,W1/W2 各用不同端口代理(10000/10001),浏览器经代理初始化。
- [x] 代理连通性:不同端口→不同出口 IP(实测 35.151.253.35 vs 174.74.220.46)。
- [x] 单测 test_proxy.py 全过;test_daily_pipeline/registry/pipeline_concurrency 回归全过(58 passed)。

## 约束 / 风险

- 动态领取 → 同账号跨次可能换 IP(用户已知悉)。proxies 表留 assigned_email 便于后续切固定绑定。
- 代理凭据存 data/openrouter_auto.db(已 gitignore),不入 git;UI 列表打码。
- 代理健康检查/失活剔除:首版不做,连不上则该次处理失败记日志。
