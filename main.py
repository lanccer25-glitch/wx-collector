import uvicorn
from core.config import cfg
from core.print import print_warning, print_info, print_success
import threading
from driver.auth import *
import os



def _diag_playwright_env():
    """生产环境 Playwright 诊断：检查 apt、Nix wrapper、系统库是否就绪。"""
    import subprocess, shutil, glob as _glob, re

    print("=== Playwright 生产环境诊断 ===")

    # 1. apt-get 是否可用
    print(f"apt-get 存在: {shutil.which('apt-get') is not None}")

    # 2. Nix chrome wrapper
    nix_chrome = os.environ.get("REPLIT_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")
    print(f"Nix chrome 路径: {nix_chrome}")
    print(f"Nix chrome 存在: {os.path.exists(nix_chrome)}")
    if nix_chrome and os.path.exists(nix_chrome):
        try:
            content = open(nix_chrome, 'r', errors='replace').read()
            for line in content.split('\n'):
                if '.chrome-wrapped' in line:
                    print(f"Wrapper 引用: {line.strip()}")
                    for p in re.findall(r'/nix/store/[^\s"]+\.chrome-wrapped', line):
                        print(f"  .chrome-wrapped 路径: {p}")
                        print(f"  .chrome-wrapped 存在: {os.path.exists(p)}")
        except Exception as e:
            print(f"读取 wrapper 失败: {e}")

    # 3. playwright 已下载的 chrome
    pw_chrome = os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome")
    found = _glob.glob(pw_chrome)
    print(f"Playwright 下载的 chrome: {found}")

    # 4. 关键系统库
    libs = ["libatk-1.0.so", "libgbm.so", "libasound.so", "libxkbcommon.so", "libXcomposite.so"]
    for lib in libs:
        try:
            r = subprocess.run(
                ["find", "/usr/lib", "/nix/store", "-name", f"{lib}*", "-type", "f"],
                capture_output=True, text=True, timeout=5
            )
            paths = [p for p in (r.stdout.strip().split('\n') if r.stdout.strip() else []) if p]
            print(f"  {lib}: {paths[:3]}")
        except Exception:
            print(f"  {lib}: 查找超时/失败")

    print("=== 诊断结束 ===")


def _patch_playwright_node():
    """让 playwright 使用系统 Node.js 而非自带的 ELF 二进制。
    pip 包附带的 node ELF 在 Nix 生产容器中触发 stack smashing 崩溃；
    Nix 自带的系统 node 已为该环境正确编译，无此问题。
    方案一（首选）：设置 PLAYWRIGHT_NODEJS_PATH 环境变量，无需修改任何文件。
    方案二（备选）：把 driver/node 替换为指向系统 node 的 shell wrapper。
    """
    import shutil
    from pathlib import Path

    # ── 找系统 Node.js ──────────────────────────────────────────────
    system_node = shutil.which("node")
    if not system_node:
        import glob as _g
        candidates = (_g.glob("/nix/store/*nodejs-20*/bin/node") +
                      _g.glob("/nix/store/*nodejs-18*/bin/node"))
        system_node = next(
            (c for c in candidates if os.path.isfile(c) and os.access(c, os.X_OK)),
            None
        )
    if not system_node:
        print_warning("找不到系统 Node.js，跳过 playwright node 修复")
        return

    # ── 方案一：PLAYWRIGHT_NODEJS_PATH 环境变量（无需写文件，最可靠）──
    os.environ["PLAYWRIGHT_NODEJS_PATH"] = system_node
    print_success(f"PLAYWRIGHT_NODEJS_PATH → {system_node}")

    # ── 方案二：替换 driver/node 文件（双保险，允许失败）──────────────
    try:
        import playwright as _pw
        driver_dir = Path(_pw.__file__).parent / "driver"
        bundled_node = driver_dir / "node"

        if not bundled_node.exists():
            return

        if bundled_node.stat().st_size < 10_000:
            print_info("playwright driver/node 已是 wrapper 脚本，跳过文件替换")
            return

        backup = driver_dir / "node.orig"
        if not backup.exists():
            shutil.copy2(str(bundled_node), str(backup))

        bundled_node.write_text(f'#!/bin/sh\nexec "{system_node}" "$@"\n')
        bundled_node.chmod(0o755)
        print_success(f"playwright driver/node 已替换为系统 Node wrapper")

    except Exception as e:
        print_info(f"driver/node 文件替换跳过（{e}），依赖环境变量方案")


def _build_nix_ld_path():
    """从 Nix store 收集所有相关包的 /lib 路径，用于注入 LD_LIBRARY_PATH"""
    nix_packages = [
        "atk", "at-spi2-atk", "at-spi2-core", "mesa", "libxkbcommon",
        "alsa-lib", "pango", "cairo", "gdk-pixbuf", "gtk+3", "gtk-3",
        "nss", "nspr", "libXcomposite", "libXdamage", "libXrandr",
        "libXfixes", "libxcb", "libX11", "dbus", "expat", "cups",
        "libdrm", "glib", "libgbm"
    ]
    lib_paths = set()
    try:
        for root, dirs, files in os.walk("/nix/store"):
            depth = root.count("/")
            if depth > 6:
                dirs.clear()
                continue
            if root.endswith("/lib") and any(pkg in root for pkg in nix_packages):
                lib_paths.add(root)
    except Exception:
        pass
    return ":".join(lib_paths)


def _ensure_playwright_browsers():
    """启动时确保Playwright浏览器真实可用。
    开发环境：Nix路径有效，直接返回（Nix管理所有依赖）。
    生产环境：Nix实际二进制缺失，自动安装到本地可写路径，并安装系统依赖库。
    """
    import re as _re, glob as _glob, sys, subprocess

    _patch_playwright_node()   # 先替换 node 二进制，再做其他检查
    _diag_playwright_env()

    # 提前检查 .chrome-wrapped 是否可用；若不可用（生产容器 Nix hash 不同），注入 Nix 系统库路径
    _exe = os.getenv("REPLIT_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")
    _chrome_wrapped_ok = False
    if _exe and os.path.isfile(_exe):
        try:
            _m = _re.search(r'"(/nix/store/[^"]+)"', open(_exe, errors='replace').read())
            if _m and os.path.isfile(_m.group(1)):
                _chrome_wrapped_ok = True
        except Exception:
            pass

    if not _chrome_wrapped_ok:
        _nix_lib_paths = _build_nix_ld_path()
        if _nix_lib_paths:
            _current_ld = os.environ.get("LD_LIBRARY_PATH", "")
            os.environ["LD_LIBRARY_PATH"] = f"{_nix_lib_paths}:{_current_ld}" if _current_ld else _nix_lib_paths
            print_info(f"已注入 LD_LIBRARY_PATH，共 {len(_nix_lib_paths.split(':'))} 个路径")

    browser_name = os.getenv("BROWSER_TYPE", "chromium").lower()
    local_browsers = os.path.join(
        os.getenv("REPL_HOME", "/home/runner/workspace"),
        ".playwright-browsers"
    )

    # 1. 检查本地路径是否已有可用浏览器（之前安装过）
    local_patterns = {
        "chromium": "chromium-*/chrome-linux/chrome",
        "firefox":  "firefox-*/firefox/firefox",
        "webkit":   "webkit-*/minibrowser-gtk/MiniBrowser",
    }
    pat = local_patterns.get(browser_name, f"{browser_name}*/**/{browser_name}")
    local_candidates = _glob.glob(os.path.join(local_browsers, pat))
    if local_candidates:
        print_success(f"使用本地已安装Playwright浏览器: {local_candidates[0]}")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_browsers
        for k in ("REPLIT_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "PLAYWRIGHT_EXECUTABLE_PATH"):
            os.environ.pop(k, None)
        return

    # 2. 检查Nix wrapper脚本所指向的实际二进制是否存在
    exe_path = os.getenv("REPLIT_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")
    if exe_path and os.path.isfile(exe_path):
        try:
            content = open(exe_path).read()
            m = _re.search(r'"(/nix/store/[^"]+)"', content)
            if m and os.path.isfile(m.group(1)):
                print_success(f"Playwright浏览器Nix路径可用: {m.group(1)}")
                return  # Nix环境依赖由Nix管理，无需安装系统库
            elif m:
                print_warning(f"Playwright wrapper存在但实际二进制缺失: {m.group(1)}")
        except Exception:
            pass

    # 2.5 扫描 Nix store 里已有的 playwright chromium（生产 hash 与 dev 不同但二进制一定存在）
    nix_chrome_candidates = _glob.glob(
        "/nix/store/*playwright-browsers*/chromium-*/chrome-linux/chrome"
    ) + _glob.glob(
        "/nix/store/*playwright-chromium*/chrome-linux/chrome"
    )
    # 过滤出真实可执行文件（排除 wrapper 脚本）
    nix_chrome_real = [p for p in nix_chrome_candidates
                       if os.path.isfile(p) and os.access(p, os.X_OK) and not p.endswith(".sh")]
    if nix_chrome_real:
        real_bin = nix_chrome_real[0]
        # PLAYWRIGHT_BROWSERS_PATH 需指向 chromium-XXXX 目录的父目录
        # 路径格式: /nix/store/HASH-playwright-browsers.../chromium-XXXX/chrome-linux/chrome
        browsers_root = os.path.dirname(os.path.dirname(os.path.dirname(real_bin)))
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_root
        for k in ("REPLIT_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "PLAYWRIGHT_EXECUTABLE_PATH"):
            os.environ.pop(k, None)
        print_success(f"Playwright 在 Nix store 找到 chromium: {real_bin}")
        print_success(f"PLAYWRIGHT_BROWSERS_PATH 设为: {browsers_root}")
        return

    # 3. Nix store 也找不到，尝试安装到本地可写路径
    os.makedirs(local_browsers, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = local_browsers
    print_info(f"正在安装 Playwright {browser_name} 到本地路径（首次约需1-2分钟）...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", browser_name],
            env=env, capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_browsers
            for k in ("REPLIT_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "PLAYWRIGHT_EXECUTABLE_PATH"):
                os.environ.pop(k, None)
            print_success(f"Playwright {browser_name} 安装成功")
        else:
            print_warning(f"Playwright安装失败:\n{result.stderr[-500:]}\n{result.stdout[-200:]}")
    except subprocess.TimeoutExpired:
        print_warning("Playwright安装超时（300秒），将在请求时再次尝试")
    except Exception as e:
        print_warning(f"Playwright安装异常: {e}")


def _ensure_cascade_task():
    """确保存在至少一个启用的级联采集任务；首次部署时自动创建「全量采集」任务。"""
    from core.db import DB
    from core.models.message_task import MessageTask
    import uuid
    from datetime import datetime

    session = DB.get_session()
    try:
        count = session.query(MessageTask).filter(MessageTask.status == 0).count()
        if count > 0:
            print_info(f"[级联] 已有 {count} 个启用任务，跳过自动创建")
            return
        print_info("[级联] 未发现启用任务，自动创建「全量采集」任务…")
        now = datetime.utcnow()
        task = MessageTask(
            id=str(uuid.uuid4()),
            name="全量采集",
            message_type=1,
            cron_exp="0 * * * *",
            status=0,
            mps_id="[]",
            message_template="",
            web_hook_url="",
            headers="",
            cookies="",
            created_at=now,
            updated_at=now,
        )
        session.add(task)
        session.commit()
        print_success(f"[级联] 已自动创建「全量采集」任务 (id: {task.id})")
    except Exception as e:
        try:
            session.rollback()
        except Exception:
            pass
        print_warning(f"[级联] 自动创建任务失败: {str(e)}")
    finally:
        session.close()


def _run_init_and_jobs():
    """在后台线程中执行所有初始化和定时任务启动，让 uvicorn 立即绑定端口。"""
    if cfg.args.init == "True":
        import init_sys as init
        init.init()

    cascade_service_started = False
    if cfg.get("cascade.enabled", False) and cfg.get("cascade.node_type") == "child":
        try:
            from jobs.cascade_sync import cascade_sync_service
            from jobs.cascade_task_dispatcher import start_child_task_worker
            import asyncio

            cascade_sync_service.initialize()
            if cascade_sync_service.sync_enabled:
                def run_sync():
                    asyncio.run(cascade_sync_service.start_periodic_sync())

                sync_thread = threading.Thread(target=run_sync, daemon=True)
                sync_thread.start()

                poll_interval = cfg.get("cascade.task_poll_interval", 30)

                def run_task_worker():
                    asyncio.run(start_child_task_worker(poll_interval=poll_interval))

                task_worker_thread = threading.Thread(target=run_task_worker, daemon=True)
                task_worker_thread.start()

                cascade_service_started = True
                print_success(f"级联同步服务已启动，任务拉取间隔: {poll_interval}秒")
        except Exception as e:
            print_warning(f"启动级联同步服务失败: {str(e)}")
    else:
        print_info("级联模式未启用或当前节点为父节点")

    if not cascade_service_started:
        print_info("启动网关定时调度服务")
        from jobs.cascade_task_dispatcher import cascade_schedule_service

        # 自动创建全量采集任务（若 message_tasks 为空时）
        if cfg.get("cascade.enabled", False):
            _ensure_cascade_task()

        cascade_schedule_service.start()

    if cfg.args.job == "True" and cfg.get("server.enable_job", False):
        from jobs import start_job
        threading.Thread(target=start_job, daemon=False).start()
        print_success("已开启定时任务")
    else:
        print_warning("未开启定时任务")

    if cfg.get("gather.content_auto_check", False):
        from jobs import start_fix_article
        start_fix_article()
        print_success("已开启自动修正文章任务")
    else:
        print_warning("未开启自动修正文章任务")

    from jobs.heartbeat import start_heartbeat
    start_heartbeat()


if __name__ == '__main__':
    print("环境变量:")
    for k, v in os.environ.items():
        print(f"{k}={v}")

    # 确保Playwright浏览器可用（生产环境首次启动时自动安装）
    _ensure_playwright_browsers()

    # 所有初始化在后台线程执行，uvicorn 立即启动绑定端口，避免部署健康检查超时
    threading.Thread(target=_run_init_and_jobs, daemon=False).start()

    print("启动服务器")
    AutoReload = cfg.get("server.auto_reload", False)
    thread = cfg.get("server.threads", 1)
    reload_dirs = ["apis", "core", "driver", "jobs", "schemas", "tools", "views", "web_ui"]
    uvicorn.run("web:app", host="0.0.0.0", port=int(cfg.get("port", 8001)),
                reload=AutoReload,
                reload_dirs=reload_dirs,
                reload_excludes=['static', 'data', 'node_modules', '*.pnpm*'],
                workers=thread,
                )
