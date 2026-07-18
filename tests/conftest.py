"""pytest 共享夹具。

所有测试都在临时数据库上运行，绝不触碰 data/cloudflare_auto.db。
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.database import Database          # noqa: E402
from src.models.card_binding import CardBindingModel  # noqa: E402
from src.models.task import TaskModel             # noqa: E402


@pytest.fixture
def db():
    """全新的临时数据库（已跑完全部迁移）。"""
    fd, path = tempfile.mkstemp(suffix='.db', prefix='cf_test_')
    os.close(fd)
    os.remove(path)          # 让 Database 自己建，避免空文件干扰
    database = Database(path)
    yield database
    database.close()
    for suffix in ('', '-wal', '-shm'):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture
def card_model(db):
    return CardBindingModel(db)


@pytest.fixture
def task_id(db):
    return TaskModel(db).create('test', config={})


def make_cards(n, prefix='4111'):
    """生成 n 张结构合法的假卡（卡号唯一，便于断言无重复消费）。"""
    return [
        {
            'number': f'{prefix}{i:012d}',
            'expiry_month': '12',
            'expiry_year': '2030',
            'cvc': '123',
            'first_name': 'Test',
            'last_name': f'User{i}',
        }
        for i in range(n)
    ]
