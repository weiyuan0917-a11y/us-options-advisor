# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 美股期权策略推荐器（onedir 模式）
============================================================
产出：dist/USOptionsAdvisor/USOptionsAdvisor.exe  +  _internal/（运行时与资源）

用法（在项目根目录）：
    pyinstaller --clean --noconfirm packaging/us-options-advisor.spec

要点：
  * 入口为 packaging/launcher.py（起服务 + 开浏览器 + 常驻控制台）
  * web/ 前端资源作为数据文件打进 _internal/web，
    与 web_server.WEB_DIR 的解析结果一致
  * longbridge / longport 为 Rust 扩展模块，显式收集子模块避免漏打
"""
import os

HERE = os.path.abspath(SPECPATH)                      # noqa: F821  (PyInstaller 注入)
PROJ = os.path.abspath(os.path.join(HERE, os.pardir))

# ---- 数据文件：前端资源 ----
datas = [
    (os.path.join(PROJ, "web"), "web"),
]

# ---- 隐式导入 ----
hiddenimports = [
    "web_server",
    "options_advisor",
    "longbridge_api",
]
for _pkg in ("longbridge", "longport"):
    try:
        from PyInstaller.utils.hooks import collect_submodules
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        pass

# ---- 裁剪明显用不到的大件，控制体积 ----
excludes = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "lib2to3",
    "test",
    "distutils",
    "setuptools",
    "pip",
    "sqlite3",
    "xmlrpc",
    "pdb",
    "doctest",
]

a = Analysis(
    [os.path.join(HERE, "launcher.py")],
    pathex=[PROJ, HERE],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="USOptionsAdvisor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                 # 保留控制台显示访问地址与日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="USOptionsAdvisor",
)
