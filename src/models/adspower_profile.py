"""AdsPower 环境映射模型：email ↔ profile_id。

存在理由是「登录态复用」：AdsPower 环境里的 Cookie 属于那个环境，换环境就等于重新登录。
映射落 DB（而非内存）才能跨进程重启保住这层复用。

同时它也是环境回收的选择依据——环境配额稀缺（实测上限 12），账号数远超它，所以
必须能回答「哪些环境属于已经跑完的账号、可以删」。
"""

# 可回收环境对应的账号状态，按回收优先级排序。判据是「这个环境里还有没有值得留的登录态」。
#
# failed/pending/rejected 排最前：注册压根没成功，环境里空无一物，删了零损失。
#   2026-08-03 踩过的坑：最初这三个不在列表里，于是一批注册失败的账号把环境永久占死，
#   配额卡在 11/12，后续每个账号都报「配额已满且无可回收」——整条流水线瘫痪。
# recharged 次之：充值成功是本项目的终点，登录态没有再用的价值。
# archived（余额够）、subscribed 再次之——这两类将来还可能被别的流程用到。
# flagged/banned/suspended 排最后：账号本身坏了、永远不会再跑，但删到这一档说明
#   可回收的环境已经见底，值得在日志里体现。
#
# 唯独 registered 绝不可回收：那是「已注册、等着登录充值」的账号，环境里的 GitHub
# 登录态正是下一步要用的东西。
RECLAIM_STATUS_ORDER = ('failed', 'pending', 'rejected',
                        'recharged', 'archived', 'subscribed',
                        'flagged', 'banned', 'suspended')


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
        """按回收优先级返回可删除的 (email, profile_id, status)。

        只看账号状态，不看「是否正在被 worker 使用」——运行时占用是内存态，DB 不知道，
        由调用方用 AccountRegistry 过滤。这里少一层过滤是刻意的：把两种判据混在一条
        SQL 里，会让「为什么这个环境没被回收」变得无法从 DB 单独复现。
        """
        marks = ','.join('?' * len(RECLAIM_STATUS_ORDER))
        order = ' '.join(
            f"WHEN ? THEN {i}" for i in range(len(RECLAIM_STATUS_ORDER)))
        rows = self.db.fetchall(
            f"SELECT p.email, p.profile_id, a.status FROM adspower_profiles p "
            f"JOIN accounts a ON a.email = p.email "
            f"WHERE a.status IN ({marks}) "
            f"ORDER BY CASE a.status {order} ELSE 99 END, p.last_used_at ASC "
            f"LIMIT ?",
            tuple(RECLAIM_STATUS_ORDER) + tuple(RECLAIM_STATUS_ORDER) + (limit,),
        )
        return [dict(r) for r in rows]
