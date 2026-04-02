"""
微信 Session 心跳保活模块
每隔固定时间主动 ping 一次微信，防止 Session 过期
"""
import threading
import time
from core.log import logger
from core.config import cfg


def _do_heartbeat():
    """执行一次心跳检测与保活"""
    try:
        from driver.wx_api import WeChat_api
        from driver.token import get as get_token

        if WeChat_api.is_login_valid():
            # Session 有效：主动访问首页，刷新服务器端过期计时
            try:
                url = f"{WeChat_api.home_url}?token={WeChat_api.token}&t=home/index&lang=zh_CN&f=json&ajax=1"
                resp = WeChat_api.session.get(url, timeout=15)
                logger.info(f"[心跳] Session 有效，保活请求状态: {resp.status_code}")
            except Exception as e:
                logger.warning(f"[心跳] 保活请求失败（不影响主流程）: {e}")
        else:
            # Session 失效：尝试用存储的 Token 自动恢复
            logger.warning("[心跳] Session 已失效，尝试 Token 恢复登录…")
            token = get_token("token")
            cookies_str = get_token("cookie")

            if token:
                cookies = {}
                for pair in cookies_str.split(";"):
                    pair = pair.strip()
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        cookies[k.strip()] = v.strip()

                from driver.wx_api import login_with_token
                ok = login_with_token(token, cookies)
                if ok:
                    logger.info("[心跳] Token 恢复登录成功 ✓")
                else:
                    logger.warning("[心跳] Token 恢复失败，请到管理后台重新扫码登录")
            else:
                logger.warning("[心跳] 无存储 Token，请到管理后台扫码登录")

    except Exception as e:
        logger.error(f"[心跳] 检测异常: {e}")


def _heartbeat_loop(interval_sec: int):
    """心跳定时循环（守护线程）"""
    # 首次延迟 60 秒，等待系统初始化完成
    time.sleep(60)
    while True:
        _do_heartbeat()
        time.sleep(interval_sec)


def start_heartbeat():
    """启动心跳保活后台线程"""
    # 间隔分钟数，默认 20 分钟，可通过配置文件 heartbeat.interval_minutes 调整
    interval_min = int(cfg.get("heartbeat.interval_minutes", 20))
    interval_sec = interval_min * 60

    t = threading.Thread(
        target=_heartbeat_loop,
        args=(interval_sec,),
        name="wx-heartbeat",
        daemon=True,
    )
    t.start()
    logger.info(f"[心跳] 保活服务已启动，检测间隔: {interval_min} 分钟")
