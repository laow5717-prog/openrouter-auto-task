"""HTTP 代理池模型。

每账号处理时领一个代理出口 IP（反关联）。导入格式 `user:pass@host:port`
（兼容纯冒号 `user:pass:host:port`）。凭据存 DB（data/openrouter_auto.db，已 gitignore）。
"""


def parse_proxy_line(line):
    """解析一行代理为 dict(host, port, username, password)；非法返回 None。

    兼容两种写法：
      user:pass@host:port   —— @ 左为凭据（首个冒号分 user/pass）、右为 host:port（末冒号分）
      user:pass:host:port   —— 无 @：末段 port、其前 host、再前用首个冒号分 user/pass
    也接受无凭据的 host:port。
    """
    line = (line or '').strip()
    if not line:
        return None
    # 去掉可能的 scheme 前缀
    for scheme in ('http://', 'https://', 'socks5://', 'socks5h://'):
        if line.lower().startswith(scheme):
            line = line[len(scheme):]
            break

    username = password = ''
    if '@' in line:
        cred, hostport = line.rsplit('@', 1)
        if ':' in cred:
            username, password = cred.split(':', 1)
        else:
            username = cred
    else:
        parts = line.split(':')
        if len(parts) >= 4:
            # user:pass:host:port —— 末两段是 host:port，前面 user:pass
            username, password, hostport = parts[0], parts[1], ':'.join(parts[2:])
        else:
            hostport = line   # host:port（无凭据）

    if ':' not in hostport:
        return None
    host, port_s = hostport.rsplit(':', 1)
    host = host.strip()
    try:
        port = int(port_s.strip())
    except ValueError:
        return None
    if not host or not (0 < port < 65536):
        return None
    return {'host': host, 'port': port, 'username': username, 'password': password}


class ProxyModel:
    def __init__(self, db):
        self.db = db

    def add_proxies(self, text):
        """批量导入：逐行解析，按 (host,port,username) 去重插入。返回 (added, skipped)。"""
        added = 0
        skipped = 0
        for line in (text or '').splitlines():
            p = parse_proxy_line(line)
            if not p:
                if line.strip():
                    skipped += 1
                continue
            try:
                self.db.execute(
                    "INSERT INTO proxies (host, port, username, password) VALUES (?, ?, ?, ?)",
                    (p['host'], p['port'], p['username'], p['password']),
                )
                added += 1
            except Exception:
                skipped += 1   # UNIQUE 冲突等
        return added, skipped

    def get_all(self, page=1, page_size=50):
        offset = (page - 1) * page_size
        total_row = self.db.fetchone("SELECT COUNT(*) as cnt FROM proxies")
        total = total_row['cnt'] if total_row else 0
        rows = self.db.fetchall(
            "SELECT * FROM proxies ORDER BY id ASC LIMIT ? OFFSET ?",
            (page_size, offset),
        )
        return [dict(r) for r in rows], total

    def get_usable_list(self):
        """运行时取所有可用代理（status≠invalid），供 ProxyRegistry 领取。"""
        rows = self.db.fetchall(
            "SELECT * FROM proxies WHERE COALESCE(status,'') != 'invalid' ORDER BY id ASC"
        )
        return [dict(r) for r in rows]

    def count(self):
        row = self.db.fetchone("SELECT COUNT(*) as cnt FROM proxies")
        return row['cnt'] if row else 0

    def delete(self, proxy_id):
        self.db.execute("DELETE FROM proxies WHERE id=?", (proxy_id,))

    def clear(self):
        self.db.execute("DELETE FROM proxies")

    def mark_status(self, proxy_id, status):
        self.db.execute("UPDATE proxies SET status=? WHERE id=?", (status, proxy_id))
