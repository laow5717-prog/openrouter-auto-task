"""
信用卡绑定数据模型

每行带 platform：绑卡任务是平台各自的，A 平台的 worker 不能领走 B 平台任务的卡，
「已成功绑过」「已被拒付」这两个派生集合也各算各的。

一处例外是 get_stripe_field_error_card_numbers——那类失败说的是卡数据本身有问题
（卡号/有效期/地址填不进去），换哪个平台都一样，故**保持全局**。

另一处例外是 reap_stale / reset_all_processing：它们回收的是「worker 失联/进程重启」
留下的占位，语义就是全量，不按平台过滤。本次只跑单平台，全量重置是安全的；将来
真要跨平台并发，得先把这两处改成按平台，否则一条流水线重启会清掉另一条的占位。
"""

import json


class CardBindingModel:
    def __init__(self, db):
        self.db = db

    def create_batch(self, platform, task_id, cards):
        """批量创建绑定记录，返回 record id 列表"""
        ids = []
        for card in cards:
            display = card['number'][-4:] if len(card.get('number', '')) >= 4 else card.get('number', '????')
            card_json = json.dumps(card)
            cursor = self.db.execute(
                "INSERT INTO card_bindings (platform, task_id, card_display, card_data_json) "
                "VALUES (?, ?, ?, ?)",
                (platform, task_id, display, card_json),
            )
            ids.append(cursor.lastrowid)
        return ids

    def mark_success(self, binding_id, email):
        self.db.execute(
            "UPDATE card_bindings SET status='success', bound_to_email=?, attempted_at=datetime('now','localtime') WHERE id=?",
            (email, binding_id),
        )

    def mark_failed(self, binding_id, error=''):
        self.db.execute(
            "UPDATE card_bindings SET status='failed', error=?, attempted_at=datetime('now','localtime') WHERE id=?",
            (error, binding_id),
        )

    def get_pending(self, task_id):
        rows = self.db.fetchall(
            "SELECT * FROM card_bindings WHERE task_id=? AND status='pending' ORDER BY id",
            (task_id,),
        )
        return self._with_parsed_card(rows)

    # ==================== 并发领取（processing 中间态）====================
    # 状态机: pending -> processing -> success | failed
    #                        `-> pending (release_unused / reap_stale)
    #
    # 原子性说明：Database 全进程共享单连接 + 单个 threading.Lock（database.py），
    # 每次 db.execute 天然串行，因此单条 UPDATE 即为原子操作，无需 BEGIN IMMEDIATE
    # 或乐观锁重试。此前提随 Database 实现绑定——若将来改为连接池或多进程，
    # 下列 claim_batch 必须改写为显式事务。

    @staticmethod
    def _with_parsed_card(rows):
        result = []
        for r in rows:
            d = dict(r)
            d['card'] = json.loads(d['card_data_json']) if d['card_data_json'] else {}
            result.append(d)
        return result

    def claim_batch(self, task_id, worker_id, limit):
        """原子领取至多 limit 张 pending 卡，返回该 worker 已占住的记录列表。

        与 get_pending 的区别：get_pending 只读不占位，并发下多个 worker 会拿到
        同一批卡；claim_batch 在同一条语句内完成"选中+占位"，是并发场景下的唯一
        正确入口。

        不按 platform 过滤是有意的：一个 task 天然只属于一个平台（建任务时就定死了），
        task_id 已经把范围收到单平台内。再加一层 platform 条件不增加任何隔离，却会在
        create_batch 万一把 platform 写空时让领卡静默返回空——那种故障比多余的防御难查得多。"""
        if limit <= 0:
            return []
        self.db.execute(
            """UPDATE card_bindings
               SET status='processing', worker_id=?, claimed_at=datetime('now','localtime')
               WHERE id IN (
                   SELECT id FROM card_bindings
                   WHERE task_id=? AND status='pending' ORDER BY id LIMIT ?
               )""",
            (worker_id, task_id, limit),
        )
        rows = self.db.fetchall(
            "SELECT * FROM card_bindings WHERE task_id=? AND worker_id=? AND status='processing' ORDER BY id",
            (task_id, worker_id),
        )
        return self._with_parsed_card(rows)

    def release_unused(self, task_id, worker_id):
        """把该 worker 仍处于 processing 的卡退回 pending，返回释放行数。

        worker 正常结束或异常退出时必须调用，否则卡会滞留到超时回收才释放。"""
        cursor = self.db.execute(
            """UPDATE card_bindings SET status='pending', worker_id='', claimed_at=NULL
               WHERE task_id=? AND worker_id=? AND status='processing'""",
            (task_id, worker_id),
        )
        return cursor.rowcount

    def reap_stale(self, timeout_minutes):
        """回收超时未完成的 processing 记录（worker 疑似失联），返回回收行数。

        已知限制：无法区分"worker 已死"与"worker 很慢"。超时阈值需显著大于单账号
        正常处理耗时，否则慢 worker 的卡会被回收并可能被另一 worker 重复尝试
        （不会写坏数据——mark_success 按 binding_id 更新——但会浪费一次绑卡机会）。"""
        cursor = self.db.execute(
            """UPDATE card_bindings SET status='pending', worker_id='', claimed_at=NULL
               WHERE status='processing'
                 AND claimed_at IS NOT NULL
                 AND claimed_at < datetime('now','localtime',?)""",
            (f'-{int(timeout_minutes)} minutes',),
        )
        return cursor.rowcount

    def reset_all_processing(self):
        """启动时全量重置 processing -> pending，返回重置行数。

        进程重启意味着所有 worker 都已消失，其领取的卡必须无条件释放。"""
        cursor = self.db.execute(
            "UPDATE card_bindings SET status='pending', worker_id='', claimed_at=NULL WHERE status='processing'"
        )
        return cursor.rowcount

    def get_all_by_task(self, task_id):
        rows = self.db.fetchall(
            "SELECT id, task_id, card_display, status, bound_to_email, error, attempted_at FROM card_bindings WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]

    def get_stripe_field_error_card_numbers(self):
        """获取所有因 Stripe 字段错误而失败的卡号（跨所有任务、**跨所有平台**）。

        刻意不按平台过滤：这类失败说的是卡数据本身有问题（卡号/有效期/账单地址填不
        进去），换个平台一样填不进去。按平台隔离反而会让同一张脏卡在每个平台各踩一遍坑。
        """
        rows = self.db.fetchall(
            "SELECT card_data_json FROM card_bindings WHERE status='failed' AND error LIKE '[Stripe字段错误]%' AND card_data_json IS NOT NULL"
        )
        numbers = set()
        for r in rows:
            try:
                card = json.loads(r['card_data_json'])
                if card.get('number'):
                    numbers.add(card['number'])
            except (json.JSONDecodeError, TypeError):
                pass
        return numbers

    def get_successfully_bound_card_numbers(self, platform):
        """获取该平台所有已成功绑定的卡号（跨该平台的所有任务）"""
        rows = self.db.fetchall(
            "SELECT card_data_json FROM card_bindings "
            "WHERE platform=? AND status='success' AND card_data_json IS NOT NULL",
            (platform,),
        )
        numbers = set()
        for r in rows:
            try:
                card = json.loads(r['card_data_json'])
                if card.get('number'):
                    numbers.add(card['number'])
            except (json.JSONDecodeError, TypeError):
                pass
        return numbers

    def mark_declined_by_number(self, platform, card_number, reason=''):
        """把指定卡号的**成功绑定**记录标记为失效（Top-up 因卡本身被拒）。

        复用 status+error 前缀模式（与 [Stripe字段错误] 同类）：置 status='failed'、
        error 以 '[充值拒付]' 开头，使该卡：①不再计入 count_by_emails（status='success'）、
        ②被 get_declined_card_numbers 收集从而在补绑阶段被跳过。返回受影响行数（幂等）。"""
        if not card_number:
            return 0
        rows = self.db.fetchall(
            "SELECT id, card_data_json FROM card_bindings "
            "WHERE platform=? AND status='success' AND card_data_json IS NOT NULL",
            (platform,),
        )
        ids = []
        for r in rows:
            try:
                card = json.loads(r['card_data_json'])
            except (json.JSONDecodeError, TypeError):
                continue
            if card.get('number') == card_number:
                ids.append(r['id'])
        for bid in ids:
            self.db.execute(
                "UPDATE card_bindings SET status='failed', error=?, attempted_at=datetime('now','localtime') WHERE id=?",
                (f"[充值拒付] {reason}"[:200], bid),
            )
        return len(ids)

    def get_declined_card_numbers(self, platform):
        """获取该平台因 Top-up 拒付被标记失效的卡号，补绑阶段据此跳过。

        按平台算：拒付是发卡行对**这个商户**的判断，换个平台可能就过了。
        """
        rows = self.db.fetchall(
            "SELECT card_data_json FROM card_bindings "
            "WHERE platform=? AND status='failed' AND error LIKE '[充值拒付]%' "
            "AND card_data_json IS NOT NULL",
            (platform,),
        )
        numbers = set()
        for r in rows:
            try:
                card = json.loads(r['card_data_json'])
                if card.get('number'):
                    numbers.add(card['number'])
            except (json.JSONDecodeError, TypeError):
                pass
        return numbers

    def get_paginated_by_task(self, task_id, page=1, page_size=20, status='', keyword=''):
        conditions = ["task_id=?"]
        params = [task_id]
        if status:
            conditions.append("status=?")
            params.append(status)
        if keyword:
            conditions.append("(card_display LIKE ? OR bound_to_email LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])

        where = " WHERE " + " AND ".join(conditions)
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM card_bindings{where}", params
        )
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT id, task_id, card_display, status, bound_to_email, error, attempted_at FROM card_bindings{where} ORDER BY id LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def get_by_email(self, email):
        rows = self.db.fetchall(
            "SELECT id, card_display, status, error, attempted_at, card_data_json FROM card_bindings WHERE bound_to_email=? ORDER BY id",
            (email,),
        )
        result = []
        for r in rows:
            d = dict(r)
            card = {}
            if d.get('card_data_json'):
                try:
                    card = json.loads(d['card_data_json'])
                except (json.JSONDecodeError, TypeError):
                    pass
            d['card_number'] = card.get('number', '')
            d['card_holder'] = f"{card.get('first_name', '')} {card.get('last_name', '')}".strip()
            d['expiry_month'] = card.get('expiry_month', '')
            d['expiry_year'] = card.get('expiry_year', '')
            d['cvc'] = card.get('cvc', '')
            d['country'] = card.get('country', '')
            d['address'] = card.get('address', '')
            d['address2'] = card.get('address2', '')
            d['city'] = card.get('city', '')
            d['state'] = card.get('state', '')
            d['zip'] = card.get('zip', '')
            d['company'] = card.get('company', '')
            del d['card_data_json']
            result.append(d)
        return result

    def count_by_emails(self, emails):
        if not emails:
            return {}
        placeholders = ','.join(['?'] * len(emails))
        rows = self.db.fetchall(
            f"SELECT bound_to_email, COUNT(*) as cnt FROM card_bindings WHERE status='success' AND bound_to_email IN ({placeholders}) GROUP BY bound_to_email",
            list(emails),
        )
        return {r['bound_to_email']: r['cnt'] for r in rows}

    def get_all_paginated(self, page=1, page_size=20, status='', keyword='', date_from='', date_to=''):
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if keyword:
            conditions.append("(card_display LIKE ? OR bound_to_email LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if date_from:
            conditions.append("attempted_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("attempted_at <= ?")
            params.append(f"{date_to} 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        offset = (page - 1) * page_size

        total_row = self.db.fetchone(
            f"SELECT COUNT(*) as cnt FROM card_bindings{where}", params
        )
        total = total_row['cnt'] if total_row else 0

        rows = self.db.fetchall(
            f"SELECT id, task_id, card_display, card_data_json, status, bound_to_email, error, attempted_at FROM card_bindings{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        return [dict(r) for r in rows], total

    def get_global_summary(self):
        # pending 口径 = 未完成（pending + processing）。被 worker 领取的卡仍属"待处理"，
        # 若把 processing 排除在外，前端进度会在并发运行时凭空掉数。
        # processing 单列一项供需要区分的场景使用。
        row = self.db.fetchone(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status IN ('pending','processing') THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) as processing
            FROM card_bindings"""
        )
        return dict(row) if row else {"total": 0, "success": 0, "failed": 0, "pending": 0, "processing": 0}

    def get_all_filtered(self, status='', keyword='', date_from='', date_to=''):
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if keyword:
            conditions.append("(card_display LIKE ? OR bound_to_email LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if date_from:
            conditions.append("attempted_at >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("attempted_at <= ?")
            params.append(f"{date_to} 23:59:59")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.db.fetchall(
            f"SELECT id, task_id, card_display, card_data_json, status, bound_to_email, error, attempted_at FROM card_bindings{where} ORDER BY id DESC",
            params,
        )
        return [dict(r) for r in rows]

    def delete_pending_by_task(self, task_id):
        """删除指定任务的所有未完成记录（pending + processing），返回删除行数。

        流水线收尾时调用，此时所有 worker 已退出，残留的 processing 等同于 pending。"""
        cursor = self.db.execute(
            "DELETE FROM card_bindings WHERE task_id=? AND status IN ('pending','processing')",
            (task_id,)
        )
        return cursor.rowcount

    def cleanup_stale_pending(self, active_task_id=None):
        """清理属于已完成/停止/僵尸任务的未完成记录（pending + processing），返回删除行数。

        active_task_id 可以是单个 id，也可以是 id 的可迭代集合——多平台并发时
        可能**同时有多个任务在跑**，只保护其中一个会把另一个的绑卡记录删掉。
        """
        ids = active_task_id
        if ids is not None and not isinstance(ids, (list, tuple, set, frozenset)):
            ids = [ids]
        ids = [i for i in (ids or []) if i is not None]

        if ids:
            marks = ','.join('?' * len(ids))
            cursor = self.db.execute(
                f"""DELETE FROM card_bindings
                   WHERE status IN ('pending','processing') AND (
                       task_id IN (SELECT id FROM tasks WHERE status IN ('stopped', 'completed'))
                       OR (task_id IN (SELECT id FROM tasks WHERE status='running')
                           AND task_id NOT IN ({marks}))
                   )""",
                tuple(ids)
            )
        else:
            cursor = self.db.execute(
                "DELETE FROM card_bindings WHERE status IN ('pending','processing')"
            )
        return cursor.rowcount

    def get_summary(self, task_id):
        # pending 口径同 get_global_summary：含 processing（未完成）
        row = self.db.fetchone(
            """SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as success,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN status IN ('pending','processing') THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN status='processing' THEN 1 ELSE 0 END) as processing
            FROM card_bindings WHERE task_id=?""",
            (task_id,),
        )
        return dict(row) if row else {"total": 0, "success": 0, "failed": 0, "pending": 0, "processing": 0}
