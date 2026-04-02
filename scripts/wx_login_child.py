#!/usr/bin/env python3
"""
wx_login_child.py — 腾讯云子节点微信公众平台扫码登录工具

用法:
    cd /path/to/wx-collector
    python3 scripts/wx_login_child.py

流程:
    1. 无头 Playwright 打开微信公众平台，截取二维码
    2. 临时 HTTP server 监听 9999 端口，提供 /qr.png
    3. 用手机微信扫描 http://<公网IP>:9999/qr.png 完成登录
    4. 自动保存 session 到 data/key.lic，关闭 HTTP server
    5. 重启子节点进程即可开始采集

注意:
    - 腾讯云安全组需放行 TCP 9999 端口（临时放行即可，扫码后可关闭）
    - 与主进程使用相同的 SAFE_LIC_KEY 环境变量（默认 RACHELOS）
"""

import os
import sys
import time
import threading
import re
import socket

# 把项目根目录加到 Python 路径（脚本从项目根运行）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

QR_PATH = "/tmp/wx_qr.png"
HTTP_PORT = 9999
WX_LOGIN_URL = "https://mp.weixin.qq.com/"
WX_HOME = "https://mp.weixin.qq.com/cgi-bin/home"


# ─────────────────────── 工具函数 ───────────────────────

def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _check_existing_session() -> bool:
    """检查 data/key.lic 中的 session 是否仍然有效"""
    try:
        from driver.store import Store
        from driver.cookies import expire
        cookies = Store.load()
        if not cookies:
            return False
        exp = expire(cookies)
        if exp and exp.get("remaining_seconds", 0) > 0:
            print(f"[INFO] 已有有效 session，过期时间: {exp['expiry_time']} "
                  f"(剩余 {exp['remaining_seconds'] // 3600:.1f} 小时)")
            return True
        print("[WARN] 已有 session 但已过期")
        return False
    except Exception as e:
        print(f"[WARN] 读取现有 session 失败: {e}")
        return False


def _start_http_server():
    """在后台线程启动简单 HTTP server 提供二维码图片"""
    import http.server
    import socketserver

    class _QRHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/qr.png"):
                try:
                    with open(QR_PATH, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except FileNotFoundError:
                    self.send_error(404, "QR code not generated yet, please wait")
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            pass  # 静默 HTTP 访问日志

    socketserver.TCPServer.allow_reuse_address = True
    server = socketserver.TCPServer(("0.0.0.0", HTTP_PORT), _QRHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ─────────────────────── 主登录流程 ───────────────────────

def login() -> bool:
    os.makedirs("data", exist_ok=True)
    os.makedirs("static", exist_ok=True)

    local_ip = _get_local_ip()

    print("\n[INFO] 启动临时 HTTP server (port 9999) ...")
    http_server = _start_http_server()
    print(f"[INFO] 二维码访问地址: http://{local_ip}:{HTTP_PORT}/qr.png")
    print("[INFO] 正在启动无头浏览器，请稍候...\n")

    try:
        from driver.playwright_driver import PlaywrightController
        ctrl = PlaywrightController()
        ctrl.start_browser(headless=True, dis_image=False, anti_crawler=False)
        page = ctrl.page

        print("[INFO] 正在打开微信公众平台...")
        page.goto(WX_LOGIN_URL, timeout=30000)
        page.wait_for_load_state("networkidle", timeout=30000)

        # ── 截取二维码 ──
        qr_selector = ".login__type__container__scan__qrcode"
        try:
            page.wait_for_selector(qr_selector, timeout=15000)
            qr_el = page.query_selector(qr_selector)
            qr_el.screenshot(path=QR_PATH)
            # 同时保存一份到 static 目录，方便 debug
            qr_el.screenshot(path="static/wx_qrcode.png")
        except Exception as e:
            print(f"[ERROR] 获取二维码失败: {e}")
            ctrl.cleanup()
            http_server.shutdown()
            return False

        size = os.path.getsize(QR_PATH)
        if size <= 364:
            print("[ERROR] 二维码图片为空（可能页面未加载完成），请重试")
            ctrl.cleanup()
            http_server.shutdown()
            return False

        print("\n" + "=" * 60)
        print("  请用手机微信扫描以下链接中的二维码：")
        print(f"  http://{local_ip}:{HTTP_PORT}/qr.png")
        print("  （超时 90 秒）")
        print("=" * 60 + "\n")

        # ── 等待扫码跳转到 home 页面 ──
        try:
            page.wait_for_url(f"{WX_HOME}**", timeout=90000)
        except Exception:
            try:
                def _is_home(frame):
                    return WX_HOME in (frame.url or "")
                page.wait_for_event("framenavigated", predicate=_is_home, timeout=90000)
            except Exception as e2:
                print(f"[ERROR] 等待登录超时: {e2}")
                ctrl.cleanup()
                http_server.shutdown()
                return False

        print("[OK] 扫码成功！正在提取 cookie...")

        # ── 提取 token（从 URL）──
        token_match = re.search(r"token=([^&]+)", page.url)
        token = token_match.group(1) if token_match else ""

        # ── 获取并保存 cookie ──
        from driver.store import Store
        cookies = ctrl.context.cookies()
        Store.save(cookies)

        # ── 验证保存结果 ──
        from driver.cookies import expire
        loaded = Store.load()
        exp = expire(loaded) if loaded else None
        if exp:
            print(f"[OK] Session 保存成功！过期时间: {exp['expiry_time']}")
        else:
            print("[WARN] Cookie 已保存，但未检测到 slave_sid 过期信息")

        if token:
            print(f"[INFO] Token: {token}")

        ctrl.cleanup()
        http_server.shutdown()

        print("\n" + "=" * 60)
        print("  登录完成，请重启子节点进程：")
        print("    pm2 restart all")
        print("  或: python3 main.py -job True -init True")
        print("=" * 60 + "\n")
        return True

    except Exception as e:
        print(f"[ERROR] 登录流程异常: {e}")
        try:
            http_server.shutdown()
        except Exception:
            pass
        return False


# ─────────────────────── 入口 ───────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  微信公众平台扫码登录 — 子节点登录工具")
    print("=" * 60)

    # 先检查已有 session
    has_valid = _check_existing_session()
    if has_valid:
        print()
        try:
            ans = input("已有有效 session，是否仍要重新登录？(y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans != "y":
            print("[INFO] 退出，继续使用现有 session。")
            sys.exit(0)

    success = login()
    sys.exit(0 if success else 1)
