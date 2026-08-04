# 技术设计

## 改动边界

只碰一个函数：`src/api/routes.py::start_daily_pipeline`。
不碰 `src/web/app.py`（流水线内部逻辑本来就是对的）、不碰订阅端点、不碰模型层。

## 判据从哪来

流水线的权威判据在 `AppState.run_daily_pipeline` 内的闭包 `_registerable_imported()`：

```python
return [
    a for a in account_model.get_all(order_desc=False)
    if (a.get('identity_status') or '') == 'imported'
    and a['email'] not in done
    and self._hotmail_for_account(a)
]
```

它是**运行期闭包**（依赖 `done` 这个本次运行的终结集合），无法直接被 API 层调用。
启动门是运行**之前**的判断，`done` 恒为空集，因此启动门复刻剩下两条即可：

```python
(a.get('identity_status') or '') == 'imported' and state._hotmail_for_account(a)
```

`_hotmail_for_account` 是 `AppState` 的普通方法，`start_daily_pipeline` 里已有
`state = get_ctx(platform)`，直接调用即可，无需新增依赖。

### 为什么不能只判 `identity_status == 'imported'`

会造出这个 bug 的镜像版：门放行、流水线领不走。表现是任务起来后立刻
「无可充值账号…任务结束」，用户看到的是「启动成功但什么都没干」，比 400 更难查。
收码数据（`email_verify_link` 或 `hotmail.xlsx` 命中）是注册流程拿验证码的前提，
没有它的 imported 账号在流水线眼里根本不存在。

### `_hotmail_for_account` 的开销

第一路径读账号自带的 `email_verify_link`（纯内存）；只有 link 为空时才回退
`_hotmail_by_email`，后者惰性加载 `hotmail.xlsx` 并缓存进 `self._hotmail_map`，
整个进程只读一次盘。在启动门里逐账号调用是可接受的——账号量是几十级别，
且这条路径每次启动任务只走一遍。

## 判定与文案

三类计数并列，全 0 才拒：

```python
if account_count == 0 and registerable_count == 0 and reusable_count == 0:
    return jsonify({"error": "无可充值账号（需有登录密码、身份与平台状态均非终态）、"
                             "无待注册 imported 账号（需有收码链接）、"
                             "也无余额未满的已充值账号可复用，无事可做"}), 400
```

## 响应体

新增 `registerable_accounts` 字段，与既有 `accounts` / `reusable_accounts` 并列。
纯增量，前端不读也不会坏。

## 兼容性与回滚

- 只放宽启动条件，不收紧任何既有路径：原先能启动的组合一律仍能启动（AC5）。
- 无 DB 迁移、无配置变更、无接口破坏性改动。
- 回滚 = 还原这一个函数的 diff。

## 风险

放宽后，「有 imported 但注册全失败」的情形会从「启动就被拒」变成「起任务、补号全失败、
收敛结束」。这是**预期行为**，与流水线自身的启动门语义一致，且过程有日志可查
（`补号 xxx: failed`）；比在门口一刀切拒绝更符合用户「用账号列表的邮箱去注册充值」的诉求。
