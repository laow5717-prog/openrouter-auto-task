"""
任务数据模型
"""

import json


class TaskModel:
    def __init__(self, db):
        self.db = db

    def create(self, task_type, config=None):
        config_json = json.dumps(config) if config else None
        cursor = self.db.execute(
            "INSERT INTO tasks (type, config_json) VALUES (?, ?)",
            (task_type, config_json),
        )
        return cursor.lastrowid

    def update_counts(self, task_id, success_count, fail_count):
        self.db.execute(
            "UPDATE tasks SET success_count=?, fail_count=?, total_count=?+? WHERE id=?",
            (success_count, fail_count, success_count, fail_count, task_id),
        )

    def finish(self, task_id, status='completed'):
        self.db.execute(
            "UPDATE tasks SET status=?, finished_at=datetime('now','localtime') WHERE id=?",
            (status, task_id),
        )

    def get(self, task_id):
        row = self.db.fetchone("SELECT * FROM tasks WHERE id=?", (task_id,))
        return dict(row) if row else None

    def get_all(self, limit=50):
        rows = self.db.fetchall("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]
