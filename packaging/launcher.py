# -*- coding: utf-8 -*-
"""
美股期权策略推荐器 — 桌面启动器
============================================================
双击安装后的快捷方式即运行本启动器，它会：

1. 把工作目录切到用户可写目录（避免写入 Program Files 被拒）
2. 自动挑选可用端口（默认 8123，被占用则 8124、8125… 顺延）
3. 在后台线程启动 Web 服务
4. 自动打开系统默认浏览器
5. 保留控制台窗口显示实时日志，关闭窗口即退出服务

打包方式：PyInstaller onedir（见 us-options-advisor.spec）
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

APP_TITLE = "美股期权策略推荐器"
DEFAULT_PORT = 8123
PORT_SPAN = 40


# --------------------------------------------------------------------------
# 路径处理
# --------------------------------------------------------------------------
def resource_dir() -> str:
    """程序资源根目录。

    PyInstaller onedir 打包后，`sys._MEIPASS` 指向 `_internal` 目录，
    其中同时包含 Python 模块（web_server.py 等）与 `web/` 前端资源，
    因此 `web_server.WEB_DIR` 能正确解析到 `_internal/web`。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.dirname(os.path.abspath(__file__))


def work_dir() -> str:
    """可写工作目录：%LOCALAPPDATA%\\USOptionsAdvisor（装到 Program Files 也能写）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "USOptionsAdvisor")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.expanduser("~")
    return d


# --------------------------------------------------------------------------
# 单实例控制
# --------------------------------------------------------------------------
_MUTEX_NAME = "Local\\USOptionsAdvisor_SingleInstance"
_ERROR_ALREADY_EXISTS = 183
_mutex_handle = None  # 全局持有，进程存活期间不释放


def acquire_single_instance() -> bool:
    """尝试成为唯一实例。返回 False 表示已有实例在运行。"""
    global _mutex_handle
    if os.name != "nt":
        return True
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        _mutex_handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        return k32.GetLastError() != _ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _port_file() -> str:
    return os.path.join(work_dir(), "port.txt")


def write_port(port: int) -> None:
    try:
        with open(_port_file(), "w", encoding="utf-8") as f:
            f.write(str(port))
    except OSError:
        pass


def read_port() -> int | None:
    try:
        with open(_port_file(), encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def probe(port: int, timeout: float = 1.0) -> bool:
    """探测本机端口是否有服务在监听。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# --------------------------------------------------------------------------
# 端口探测
# --------------------------------------------------------------------------
def pick_port(preferred: int = DEFAULT_PORT, span: int = PORT_SPAN) -> int:
    """从 preferred 起找一个未被占用的本机端口。"""
    for p in range(preferred, preferred + span):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return preferred


# --------------------------------------------------------------------------
# 控制台辅助
# --------------------------------------------------------------------------
def _init_console() -> None:
    """让 Windows 控制台正确输出中文，并设置窗口标题。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    if os.name == "nt":
        try:
            os.system(f"title {APP_TITLE} - 服务运行中（关闭本窗口即退出）")
        except Exception:
            pass


def _banner(url: str) -> None:
    line = "=" * 56
    print(line)
    print(f"  {APP_TITLE}  ·  交易向导 Web 版")
    print(line)
    print("  数据源 : CBOE 延迟行情（约 15 分钟延迟，仅供参考）")
    print(f"  访问地址: {url}")
    print("  浏览器应已自动打开；若未打开，请手动复制上面的地址。")
    print(line)
    print("  提示：关闭本窗口即停止服务。")
    print(line)
    sys.stdout.flush()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    _init_console()

    # 0) 单实例：若已有实例在跑，直接打开它的页面后退出
    if not acquire_single_instance():
        prev = read_port()
        if prev and probe(prev):
            url = f"http://127.0.0.1:{prev}/"
            print(f"程序已在运行，正在为你打开：{url}")
            try:
                webbrowser.open(url)
            except Exception:
                pass
        else:
            print("程序似乎已在运行，但服务尚未就绪。")
            print("请稍候重试；若持续如此，可在任务管理器中结束 "
                  "USOptionsAdvisor.exe 后再启动。")
        time.sleep(2)
        return 0

    # 1) 切到可写目录，保证相对路径写入不触碰安装目录
    try:
        os.chdir(work_dir())
    except OSError:
        pass

    # 2) 保证能 import 到打包进去的模块
    res = resource_dir()
    if res not in sys.path:
        sys.path.insert(0, res)

    try:
        import web_server  # noqa: E402  （PyInstaller 已静态分析该模块）
        from http.server import ThreadingHTTPServer  # noqa: E402
    except Exception as exc:  # pragma: no cover - 仅在打包缺件时触发
        print(f"[错误] 加载服务模块失败：{exc}")
        input("按回车键退出…")
        return 1

    # 3) 起服务
    port = pick_port()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), web_server.Handler)
    except OSError as exc:
        print(f"[错误] 端口 {port} 无法监听：{exc}")
        input("按回车键退出…")
        return 1

    threading.Thread(target=srv.serve_forever, name="http-server", daemon=True).start()
    write_port(port)

    url = f"http://127.0.0.1:{port}/"
    _banner(url)

    # 4) 打开浏览器（失败不影响服务）
    try:
        webbrowser.open(url)
    except Exception:
        pass

    # 5) 常驻，直到用户关闭窗口（Ctrl+C 亦可）
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在停止服务…")
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
