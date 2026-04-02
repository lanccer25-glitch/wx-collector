__package__ = "driver"
from core.config import Config,cfg
import os
import json

from core.print import print_success, print_warning
from core.redis_client import redis_client

REDIS_TOKEN_PREFIX = "werss:token:"
DB_TOKEN_KEY = "wx_token_data"

lic_path="./data/wx.lic"
os.makedirs(os.path.dirname(lic_path), exist_ok=True)
if not os.path.exists(lic_path):
    with open(lic_path, "w") as f:
        f.write("{}")
wx_cfg = Config(lic_path)

def set_token(data:any,ext_data:any=None):
    """
    设置微信登录的Token和Cookie信息
    :param data: 包含Token和Cookie信息的字典
    """
    if data.get("token", "") == "":
        return

    token_data = {
        "token": data.get("token", ""),
        "cookie": data.get("cookies_str", ""),
        "fingerprint": data.get("fingerprint", ""),
        "expiry": data.get("expiry", {}),
    }
    if ext_data is not None:
        token_data["ext_data"] = ext_data

    # 存储到Redis
    if redis_client.is_connected:
        try:
            redis_client._client.set(REDIS_TOKEN_PREFIX + "data", json.dumps(token_data))
            print_success("Token已存储到Redis")
        except Exception as e:
            print_warning(f"Redis存储失败: {e}")

    # 存储到本地文件
    _save_to_local(token_data)

    # 存储到数据库（持久化，生产环境重启后恢复）
    _save_to_db(token_data)

    print_success(f"Token:{data.get('token')} \n到期时间:{data.get('expiry')['expiry_time']}\n")
    from jobs.notice import sys_notice


def _save_to_local(token_data: dict):
    """保存到本地文件"""
    try:
        wx_cfg.set("token_data", token_data)
        wx_cfg.save_config()
        wx_cfg.reload()
    except Exception as e:
        print_warning(f"本地文件存储失败: {e}")


def _save_to_db(token_data: dict):
    """保存到数据库（用于生产环境持久化）"""
    try:
        from core.db import DB
        from core.models.config_management import ConfigManagement
        session = DB.get_session()
        value_str = json.dumps(token_data)
        existing = session.query(ConfigManagement).filter(
            ConfigManagement.config_key == DB_TOKEN_KEY
        ).first()
        if existing:
            existing.config_value = value_str
        else:
            session.add(ConfigManagement(
                config_key=DB_TOKEN_KEY,
                config_value=value_str
            ))
        session.commit()
        print_success("Token已存储到数据库")
    except Exception as e:
        print_warning(f"数据库存储Token失败: {e}")


def _load_from_db() -> dict | None:
    """从数据库加载Token"""
    try:
        from core.db import DB
        from core.models.config_management import ConfigManagement
        session = DB.get_session()
        row = session.query(ConfigManagement).filter(
            ConfigManagement.config_key == DB_TOKEN_KEY
        ).first()
        if row and row.config_value:
            return json.loads(row.config_value)
    except Exception as e:
        print_warning(f"数据库读取Token失败: {e}")
    return None


def get(key:str,default:str="")->str:
    """从整体token_data中获取指定字段"""
    token_data = _get_token_data()
    if token_data is None:
        return default
    value = token_data.get(key, default)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value) if value is not None else default


def _get_token_data() -> dict | None:
    """获取整体token_data，优先级: Redis > 本地文件 > 数据库"""
    # 1. 优先从Redis获取
    if redis_client.is_connected:
        try:
            value = redis_client._client.get(REDIS_TOKEN_PREFIX + "data")
            if value is not None:
                return json.loads(value)
        except Exception as e:
            print_warning(f"Redis读取失败: {e}")

    # 2. 尝试本地文件
    local_data = wx_cfg.get("token_data", None)
    if local_data is not None:
        return local_data

    # 3. 回退到数据库（生产环境重启后恢复）
    db_data = _load_from_db()
    if db_data is not None:
        print_success("从数据库恢复Token")
        # 写回本地文件缓存
        _save_to_local(db_data)
        return db_data

    return None
