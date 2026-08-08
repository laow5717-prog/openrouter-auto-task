"""AdsPower 环境映射模型：email ↔ profile_id。

存在理由是「登录态复用」：AdsPower 环境里的 Cookie 属于那个环境，换环境就等于重新登录。
映射落 DB（而非内存）才能跨进程重启保住这层复用。

同时它也是环境回收的选择依据——环境配额稀缺（实测上限 12），账号数远超它，所以
必须能回答「哪些环境属于已经跑完的账号、可以删」。
"""

# 环境按 **email** 分配，不按平台拆分。
#
# 这是多平台改造里刻意保留的一处「不隔离」。环境存在的唯一价值是保住 Cookie，而其中
# 真正值钱的是 **GitHub 授权态**——它天然跨平台共享，一次授权喂给所有走 GitHub OAuth
# 的平台；平台自身的 session 本来就活不过浏览器重启（见 platforms/opencode/login.py
# 的实测记录）。按 (platform, email) 拆等于把 12 个环境的硬配额除以平台数，换来的只是
# 一份短命 session，是负收益。
#
# 于是要隔离的不是环境本身，而是**回收判据**：见 reclaim_candidates。

# ---- 回收优先级 ----
# 判据是「这个环境里还有没有值得留的登录态」。分三档：

# 第一档，身份层已死：GitHub 侧就没注册成功或已被封，任何平台都用不上这个环境。
# failed/pending/rejected 排最前——注册压根没成功，环境里空无一物，删了零损失。
#   2026-08-03 踩过的坑：最初这三个不在列表里，于是一批注册失败的账号把环境永久占死，
#   配额卡在 11/12，后续每个账号都报「配额已满且无可回收」——整条流水线瘫痪。
# flagged/banned/suspended 次之：账号本身坏了、永远不会再跑。
IDENTITY_DEAD_ORDER = ('failed', 'pending', 'rejected',
                       'flagged', 'banned', 'suspended')

# 第二档，身份可用但**所有平台都真的跑完了**：环境里的 GitHub 授权态还有效，但没有平台
# 还需要它了。这一档排在第一档之后——它比"注册失败"更值得留。
PLATFORM_DONE_RANK = len(IDENTITY_DEAD_ORDER)

# 第三档，**最后手段**：还有平台是 recharged（有余额但没到 balance_cap，下一轮还会被
# _reusable_recharged() 领走继续充）。删它的代价是那个账号下次跑时要 GitHub 完整重登，
# 且因为是「新设备」还要再过一次邮箱验证码（数分钟）。所以只在前面几档一个都挑不出来
# 时才动，而且**一次只牺牲一个**（见 adspower_driver.reclaim）。
RECHARGED_RANK = PLATFORM_DONE_RANK + 1

# 身份可用、且还有平台没跑完 → 绝不可回收。典型是 identity_status='registered' 且
# opencode 平台行还不存在（等着登录充值）——环境里的 GitHub 登录态正是下一步要用的。

# ---- 平台层状态集合 ----
#
# ⚠️ **不要与 utils.PLATFORM_TERMINAL_STATUSES 同步。** 这里曾经有一句「两处保持一致，
# 改动时两处都要改」，那句话现在是错的，而且正是 2026-08-08 那个 bug 的传播源。
# 两者回答的是不同问题：
#   utils.PLATFORM_TERMINAL_STATUSES  → 「这个账号还能不能充值」（recharged 是终态）
#   这里的 _PLATFORM_DONE             → 「这环境里还有没有值得留的登录态」
# 2026-08-05 的 reuse-by-balance-cap 让余额未满的 recharged 账号重新进轮转之后，
# recharged 对前者仍是终态、对后者**不再是**。当时只改了前者，回收流程继续把「下一轮
# 还要用的账号」的环境当垃圾删——现场：14 个 recharged 账号里 5 个的映射已被删光，
# 每个都要重登一次。

# 真跑完了：余额已达 balance_cap 被归档，或订阅完成。
_PLATFORM_DONE = ('archived', 'subscribed')

# 还要再跑：recharged 现在的含义是「有一些余额」，不是「这个账号做完了」。
_PLATFORM_REUSABLE = ('recharged',)

# 能进候选集的全集。不在这里面的（如 registered 等着首充）一律不可回收。
_PLATFORM_RECLAIMABLE = _PLATFORM_DONE + _PLATFORM_REUSABLE


class AdsPowerProfileModel:
    def __init__(self, db):
        self.db = db

    def get_by_email(self, email):
        row = self.db.fetchone(
            "SELECT * FROM adspower_profiles WHERE email=?", (email,))
        return dict(row) if row else None

    def upsert(self, email, profile_id, profile_no='', proxy_id=''):
        """写入或覆盖映射。

        用 INSERT OR REPLACE 而非 UPDATE...INSERT 两步：并发下两个 worker 若同时为
        同一 email 创建环境（AccountRegistry 本应挡住，但注册表是内存态、崩溃后会失效），
        两步写法会留下一条孤儿映射指向一个再也没人管的环境。
        """
        self.db.execute(
            "INSERT OR REPLACE INTO adspower_profiles "
            "(email, profile_id, profile_no, proxy_id, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?, "
            " COALESCE((SELECT created_at FROM adspower_profiles WHERE email=?), "
            "          datetime('now','localtime')), "
            " datetime('now','localtime'))",
            (email, str(profile_id), str(profile_no or ''), str(proxy_id or ''), email),
        )

    def touch(self, email):
        self.db.execute(
            "UPDATE adspower_profiles SET last_used_at=datetime('now','localtime') "
            "WHERE email=?", (email,))

    def delete_by_email(self, email):
        self.db.execute("DELETE FROM adspower_profiles WHERE email=?", (email,))

    def delete_by_emails(self, emails):
        if not emails:
            return
        marks = ','.join('?' * len(emails))
        self.db.execute(
            f"DELETE FROM adspower_profiles WHERE email IN ({marks})", tuple(emails))

    def count(self):
        row = self.db.fetchone("SELECT COUNT(*) AS cnt FROM adspower_profiles")
        return row['cnt'] if row else 0

    def get_all(self):
        rows = self.db.fetchall(
            "SELECT * FROM adspower_profiles ORDER BY last_used_at ASC")
        return [dict(r) for r in rows]

    def reclaim_candidates(self, limit=50):
        """按回收优先级返回可删除的 (email, profile_id, status, rank, bal)。

        四档判据（详见文件头）：
          0. 账号已从 accounts 删除（孤儿映射）→ 什么都不用留，优先回收；
          1. 身份层已死（identity_status ∈ IDENTITY_DEAD_ORDER）→ 任何平台都用不上；
          2. 身份可用，且**至少开通过一个平台**、开通的平台**全部**真跑完了
             （∈ _PLATFORM_DONE）；
          3. 同上，但至少有一个平台是 `recharged`（还要再跑）→ **最后手段**。
             调用方负责「只在挑不出前三档时才动，且一次只牺牲一个」，见
             adspower_driver.reclaim。这里只负责把它排到最后。

        第 0 档要用 LEFT JOIN。此前这里是 INNER JOIN，孤儿映射直接被 JOIN 掉、永远
        进不了候选集，于是它对应的远端环境无人回收，在只有 12 格的配额里白占一格
        （生产库实测就有一个）。孤儿的成因是删账号时只删了 accounts 行、没管环境映射。
        把它纳入候选后，回收流程会照常 stop + delete 远端环境再清映射。

        第 2 条的两个条件缺一不可：

        「不存在非终态平台行」必须用 NOT EXISTS 而不是 `status IN (终态集合)`。后者
        在账号开了两个平台、一个终态一个还没跑完时会算成可回收——删掉正在用的环境，
        那个账号的 GitHub 登录态就全丢了。

        「至少开通过一个平台」这条同样不能省。没有任何平台行意味着该邮箱还没在任何
        平台开通过，典型是 identity_status='registered' 等着登录充值——环境里的 GitHub
        授权态正是下一步要用的东西，删了等于白注册。少了这个 EXISTS，NOT EXISTS 会
        对这类账号恒为真，把它们全判成可回收。

        第 3 档内部按**余额降序**牺牲：余额越接近 balance_cap，剩下要充的笔数越少，
        重建环境的期望损失越小。余额取 MAX(credits_balance)——环境按 email 分配、不按
        平台拆（见文件头），所以余额也要按 email 汇总；任一平台余额已高即视为整体接近完成。

        `credits_balance` 为 NULL 的排在本档**最后**：`update_balance` 在 balance_after
        读不到时直接 return（infron 常态、opencode 偶发），NULL 意味着「不知道」，
        不知道就保守保留。这一层显式写在 ORDER BY 里，不依赖 SQLite「DESC 时 NULL 排最后」
        的默认行为——那是实现细节，换个引擎就变。

        只看状态，不看「是否正在被 worker 使用」——运行时占用是内存态，DB 不知道，
        由调用方用 AccountRegistry 过滤。这里少一层过滤是刻意的：把两种判据混在一条
        SQL 里，会让「为什么这个环境没被回收」变得无法从 DB 单独复现。
        """
        dead_marks = ','.join('?' * len(IDENTITY_DEAD_ORDER))
        dead_order = ' '.join(
            f"WHEN ? THEN {i}" for i in range(len(IDENTITY_DEAD_ORDER)))
        reclaimable_marks = ','.join('?' * len(_PLATFORM_RECLAIMABLE))
        reusable_marks = ','.join('?' * len(_PLATFORM_REUSABLE))

        rows = self.db.fetchall(
            f"""
            SELECT p.email, p.profile_id, b.bal AS bal,
                   CASE WHEN a.email IS NULL THEN '(账号已删除)'
                        ELSE COALESCE(a.identity_status,'') END AS status,
                   CASE
                       WHEN a.email IS NULL THEN -1
                       WHEN COALESCE(a.identity_status,'') IN ({dead_marks})
                            THEN CASE COALESCE(a.identity_status,'') {dead_order} ELSE 99 END
                       WHEN EXISTS (SELECT 1 FROM platform_accounts pa
                                    WHERE pa.email = p.email
                                      AND COALESCE(pa.status,'') IN ({reusable_marks}))
                            THEN {RECHARGED_RANK}
                       ELSE {PLATFORM_DONE_RANK}
                   END AS rank
            FROM adspower_profiles p
            LEFT JOIN accounts a ON a.email = p.email
            LEFT JOIN (SELECT email, MAX(credits_balance) AS bal
                       FROM platform_accounts GROUP BY email) b ON b.email = p.email
            WHERE a.email IS NULL
               OR COALESCE(a.identity_status,'') IN ({dead_marks})
               OR (
                   EXISTS (SELECT 1 FROM platform_accounts pa
                           WHERE pa.email = p.email)
                   AND NOT EXISTS (
                       SELECT 1 FROM platform_accounts pa
                       WHERE pa.email = p.email
                         AND COALESCE(pa.status,'') NOT IN ({reclaimable_marks})
                   )
               )
            ORDER BY rank,
                     CASE WHEN rank = {RECHARGED_RANK} AND b.bal IS NULL THEN 1 ELSE 0 END,
                     CASE WHEN rank = {RECHARGED_RANK} THEN b.bal END DESC,
                     p.last_used_at ASC
            LIMIT ?
            """,
            tuple(IDENTITY_DEAD_ORDER)          # SELECT CASE ... IN
            + tuple(IDENTITY_DEAD_ORDER)        # SELECT CASE WHEN ? THEN i
            + tuple(_PLATFORM_REUSABLE)         # SELECT CASE ... EXISTS ... IN
            + tuple(IDENTITY_DEAD_ORDER)        # WHERE ... IN
            + tuple(_PLATFORM_RECLAIMABLE)      # WHERE NOT EXISTS ... NOT IN
            + (limit,),
        )
        return [dict(r) for r in rows]
