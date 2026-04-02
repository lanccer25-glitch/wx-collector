from core.models.user import User
from core.models.article import Article
from core.models.config_management import ConfigManagement
from core.models.feed import Feed
from core.models.message_task import MessageTask
from core.models.cascade_node import CascadeNode, CascadeSyncLog
from core.models.cascade_task_allocation import CascadeTaskAllocation
from core.db import Db,DB
from core.config import cfg
from core.auth import pwd_context
import time
import os
from core.print import print_info, print_error

def init_user(_db: Db):
    try:
        username = os.getenv("USERNAME", "admin")
        password = os.getenv("PASSWORD", "admin@123")
        session = _db.get_session()
        existing = session.query(User).filter(User.username == username).first()
        if existing:
            print_info(f"用户 {username} 已存在，跳过初始化")
        else:
            session.add(User(
                id="0",
                username=username,
                password_hash=pwd_context.hash(password),
                is_active=True,
                role="admin",
            ))
            session.commit()
            print_info(f"初始化用户成功，请使用以下凭据登录：{username} / {password}")
    except Exception as e:
        print_error(f"初始化用户失败: {str(e)}")

def sync_models():
    from data_sync import DatabaseSynchronizer
    DB.create_tables()
    time.sleep(3)
    synchronizer = DatabaseSynchronizer(db_url=DB.connection_str or cfg.get("db",""))
    synchronizer.sync()
    print_info("模型同步完成")


def init():
    sync_models()
    init_user(DB)

if __name__ == '__main__':
    init()
