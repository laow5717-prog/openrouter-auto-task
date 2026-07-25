# 技术设计：账号一次性充值（账号驱动 + hCaptcha 自动解 + 余额写回）

## 0. 设计原则

- **镜像订阅路径**：`_subscribe_one_account` / `run_daily_subscribe_pipeline` 已是「账号驱动 + 原生栈 + hCaptcha 自动解 + 逐卡消耗 + 成功写状态」的成熟骨架，本任务照搬骨架、把订阅动作替换为充值动作。
- **对现有共享代码只做向后兼容增强**：`opencode_billing.py` 被卡池驱动 `recharge_account` 复用，改动一律「新增可选参数（默认关）+ 返回加字段」，默认路径行为零变化。
- **不重复造轮子**：hCaptcha 求解、原生 driver、登录、选卡、卡状态/记账模型全部复用现有。

## 1. 浏览器层改造（`src/browser/opencode_billing.py`）

唯一需要动的现有共享文件。两处增强，均向后兼容。

### 1.1 `detect_payment_result` —— 结构化余额 + 可选 hCaptcha 自动解

现签名：`detect_payment_result(session, wid, balance_before, monitor, timeout=120)`，返回 `{outcome, detail}`。

变更：
1. 新增参数 `auto_solve_captcha=False, captcha_server="api.multibot.cloud"`。
2. 返回 dict 增加 `balance` 字段：成功时为读到的新余额 float（现有 `grew` 变量），非成功为 `None`。
   - 三处 success 返回点（余额判定成功处）统一带上 `"balance": grew`。
3. hCaptcha 分支：当前检测到 hCaptcha 只 `saw_captcha=True` 停等，超时 → `needs_captcha`。
   - 当 `auto_solve_captcha=True` 时，镜像 `detect_subscribe_result` 的 captcha 处理：
     `reinstall_hcaptcha_hook(session)` → `captcha_solver.solve_hcaptcha(session)`（最多 3 次），每次注入后 `time.sleep(4)` 等余额到账；3 次仍未过且无拒付文案 → 提前返回 `needs_captcha`。
   - `auto_solve_captcha=False`（默认）时保持现状（停等 → needs_captcha），**卡池驱动路径不受影响**。
4. import：函数内 `from src.services import captcha as captcha_solver`（与 opencode_subscribe 一致，避免顶层循环依赖）。

> 说明：hCaptcha token 注入只在**原生 Playwright 栈 + 已装 hook** 时生效（Patchright 阉割 add_init_script）。故 `auto_solve_captcha=True` 仅在本任务的原生栈编排里传入；卡池驱动仍用 Patchright 栈、保持 `False`，自动解不会误触发。

### 1.2 `recharge_via_stripe` —— 透传开关 + 返回余额

现签名：`recharge_via_stripe(session, card, wid, amount=20, monitor=None, should_stop=None)`，返回 `{ok, outcome, mode, err, last4, steps}`。

变更：
1. 新增参数 `auto_solve_captcha=False, captcha_server="api.multibot.cloud"`，透传给 `detect_payment_result`。
2. 返回 dict 增加 `balance` 字段（取自 `result.get("balance")`，成功时为新余额，否则 None）。
3. 其余不变。加字段不破坏 `registration.recharge_account` 的现有读取（它只读 ok/outcome/err/last4）。

## 2. 编排层：新增 `_recharge_one_account_oneshot`（`src/web/app.py`）

镜像 `_subscribe_one_account` 的**订阅分支（B 段，app.py:687+）**，去掉注册分支（A 段），把订阅动作换成充值：

```
def _recharge_one_account_oneshot(self, acct, payment_group_id, captcha_api_key,
                                  worker=None, captcha_server="api.multibot.cloud"):
    # 返回 (result, detail)，result ∈ {"recharged","registered_only","skipped","failed"}
    from src.browser.driver import create_driver_vanilla, close_driver
    from src.browser.opencode_login import login_and_open_own_go
    from src.browser.opencode_billing import recharge_via_stripe
    from src.services import captcha as captcha_solver

    email = acct['email']; status = (acct.get('status') or '')
    # 只处理已能登录的账号：无 login_password → skipped（不在本流程注册）
    if not acct.get('login_password'):
        return "skipped", "无 login_password（一次性充值不注册新号）"
    if captcha_api_key:
        captcha_solver.init_solver(captcha_api_key, server=captcha_server)

    session = create_driver_vanilla(profile_id=email)
    monitor = worker.make_monitor(self); worker.set_active_driver(session)
    try:
        if captcha_solver.is_available():
            captcha_solver.install_hcaptcha_hook(session)
        lg = login_and_open_own_go(session)        # 复用订阅同款登录
        if not lg.get('ok'):
            if lg.get('flagged'):
                self.models['account'].update_status(email, 'flagged')
                return "skipped", "GitHub 被 flagged，无法授权"
            return "failed", f"登录失败: {lg.get('detail')}"
        wid = lg['wid']

        cards = self._eligible_cards(payment_group_id) if payment_group_id else []
        if not cards:
            return "registered_only", "无可选卡"
        cards = cards[:self.SUBSCRIBE_MAX_CARDS_PER_ACCOUNT]   # 复用单账号试卡上限
        for i, card in enumerate(cards, 1):
            if self.stop_requested: raise InterruptedError("用户请求停止")
            num = card.get('number',''); last4 = str(num)[-4:]
            log_id = self.models['recharge_log'].create(email, num, amount=20)
            res = recharge_via_stripe(session, card, wid, amount=20,
                                      monitor=monitor,
                                      should_stop=lambda: self.stop_requested,
                                      auto_solve_captcha=True, captcha_server=captcha_server)
            oc = res.get('outcome')
            if oc in ('success', 'topup') and res.get('ok'):
                self.models['card_pool'].mark_status_by_number(num, 'paid')
                try: self.models['valid_card'].record(card, source_type='payment', source_email=email)
                except Exception: pass
                self.models['account'].update_status(email, 'recharged')
                bal = res.get('balance')
                if bal is not None:
                    self.models['account'].update_balance(email, bal)   # R4 余额写回
                self.models['recharge_log'].mark_success(log_id, api_response={"result": res})
                return "recharged", f"****{last4} 余额=${bal}"
            elif oc == 'failed':
                # 曾成功有效卡再拒 → 24h 冷却；坏卡 → invalid（镜像订阅/registration）
                if self.models['valid_card'].is_valid(num):
                    self.models['card_state'].set_cooldown(num, hours=24, reason='曾成功卡本次失败，速率冷却')
                else:
                    self.models['card_pool'].mark_invalid_by_number(num)
                self.models['recharge_log'].mark_failed(log_id, error=res.get('err',''), api_response={"result": res})
            elif oc == 'needs_captcha':
                self.models['recharge_log'].mark_failed(log_id, error='hCaptcha 未过', api_response={"result": res})
            else:  # error / unknown：不耗卡换下一张
                self.models['recharge_log'].mark_failed(log_id, error=res.get('err','') or oc, api_response={"result": res})
        return "registered_only", "账号内可选卡试尽未成功"
    except InterruptedError: raise
    except Exception as e:
        return "failed", str(e)[:200]
    finally:
        worker.clear_active_driver(); close_driver(session)
```

设计要点：
- **不新增卡状态/记账模型**，全部复用（mark_status_by_number / mark_invalid_by_number / valid_card / card_state.set_cooldown / recharge_log）。
- `recharge_via_stripe` 对未充值账号自动走 `mode="first"`（billing 页显示 Enable Billing），符合「首充 $20」。
- 余额写回：成功分支读 `res['balance']`（浏览器层新返回字段）→ `update_balance`。
- **权衡**：此函数与 `_subscribe_one_account` 的逐卡消耗段有结构性重复。MVP 阶段接受该重复以隔离风险、不动订阅代码；后续可抽 `_consume_card_loop(...)` 公共助手（记 implement 的后续项，不在本任务强制）。

## 3. Pipeline：新增 `run_oneshot_recharge_pipeline`（`src/web/app.py`）

镜像 `run_daily_subscribe_pipeline`（app.py:769），仅两处不同：账号动作换 `_recharge_one_account_oneshot`；终态集换成充值语义。

```
def run_oneshot_recharge_pipeline(self, group_id, captcha_api_key=None,
                                  captcha_server="api.multibot.cloud"):
    # is_running/stop/计数/截图/兜底/收尾 全部照搬 run_daily_subscribe_pipeline
    _DONE = ('recharged', 'banned', 'suspended', 'flagged')   # 只改这里
    def _needing():
        return [a for a in account_model.get_all(order_desc=False)
                if (a.get('status') or '') not in _DONE and a.get('login_password')]
    # 轮转循环：pool.map(accounts, _do)；_do 内 claim → _recharge_one_account_oneshot → 计数
    # 进展 = 本轮有 recharged 成功 或 可选卡减少；零进展兜底结束
    # 成功计 success_count；结果字符串 "每账号一次性充值完成（成功 N / 未成 M / 剩余可选卡 K）"
```

- 串行 `WorkerPool(self, 1)`，与订阅一致。
- 「每账号只充一次」由 `_DONE` 含 `recharged` + 成功即 `update_status('recharged')` 共同保证：成功账号下一轮被 `_needing()` 剔除。
- 兜底：`MAX_ROUNDS = len(_needing())*5+5`；整轮零进展且卡池非空 → 结束防死循环。

## 4. 路由：新增 `POST /api/oneshot-recharge/start`（`src/api/routes.py`）

镜像 `/api/daily/subscribe/start`（routes.py:986）：

```
@api.route('/api/oneshot-recharge/start', methods=['POST'])
def start_oneshot_recharge_pipeline():
    data = request.get_json(silent=True) or {}
    group_id = data.get('group_id')
    if not group_id: return jsonify({"error":"缺少 group_id"}), 400
    if state.is_running: return jsonify({"error":"已有任务在运行"}), 409
    captcha_api_key = data.get('captcha_api_key') or os.environ.get('CAPTCHA_API_KEY','')
    captcha_server = data.get('captcha_server') or 'api.multibot.cloud'
    cards = state._eligible_cards(group_id)
    if not cards: return jsonify({"error":"分组内无可选卡"}), 400
    accts = state.models['account'].get_all(order_desc=False)
    pending = [a for a in accts if (a.get('status') or '') not in
               ('recharged','banned','suspended','flagged') and a.get('login_password')]
    if not pending: return jsonify({"error":"无待充值账号（都已 recharged/banned/... 或无密码）"}), 400
    threading.Thread(target=lambda: state.run_oneshot_recharge_pipeline(
        group_id, captcha_api_key, captcha_server), daemon=True).start()
    return jsonify({"status":"started","eligible_cards":len(cards),"pending_accounts":len(pending)})
```

## 5. 前端（`frontend/src/views/Workbench.vue` + `src/api/index.js`）

- `api/index.js` 新增：`export const startOneshotRecharge = (payload) => client.post('/api/oneshot-recharge/start', payload).then(r => r.data)`。
- `Workbench.vue`：在「开始每日任务」旁新增「一次性充值」按钮 → `handleStartOneshot`（镜像 `handleStart`，改调 `api.startOneshotRecharge`，成功提示 `pending_accounts` 个待充值账号）。
- 共用 `settings.selectedGroupId` / `captchaApiKey` / `captchaServer`，无需新 settings 键。
- 构建：`cd frontend && npm run build`，产物落 `static/`。

## 6. 兼容性与回滚

- **兼容**：`opencode_billing` 仅新增默认关的参数与返回字段；`recharge_account`（卡池驱动）、`_recharge_one_account`（手动）、`run_daily_pipeline`、订阅链路全部零改动。
- **数据**：不改表结构。复用 `accounts.status`/`credits_balance`/`balance_updated_at`、`recharge_logs`、`card_pool.status`、`card_payment_state`。
- **回滚**：改动集中在 `opencode_billing.py`、`app.py`（两个新函数）、`routes.py`（一个新路由）、`Workbench.vue`、`api/index.js`。新增为主，`git revert` 即可整体回退，无迁移脚本。

## 7. 风险

- **首充 hCaptcha 频率**：首充（Enable Billing→Checkout）是否高频弹 hCaptcha 未实测；已接自动解降低人工，但求解成功率取决于 2captcha/Multibot 与 rqdata 配对（复用订阅已验证的 `_extract_hcaptcha_params`）。
- **原生栈隐蔽性弱于 Patchright**：`create_driver_vanilla` 仅用于本付款流程，与订阅同等取舍。
- **登录复用**：`login_and_open_own_go` 面向已注册账号；未注册/被 flag 账号按 skipped/flagged 收敛，不阻塞轮转。
- **状态单值耦合**：`status` 单列，若某账号既被订阅线又被本线处理，后写覆盖前值。MVP 可接受（用户当前聚焦充值线）；如需并存精确统计，后续用 `credits_balance`/`recharge_logs(amount=20)` 作二次判据。
