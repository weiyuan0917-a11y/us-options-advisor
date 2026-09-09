#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LongBridge(LongPort OpenAPI) 轻量集成: 账户持仓/现金、机构一致目标价、财报日。

凭据来源(优先级从高到低):
  1. 本机配置文件  ~/.longport/advisor_creds.json
     - API Key 直连: {"type":"apikey","app_key":..,"app_secret":..,"access_token":..}
     - OAuth 授权:   {"type":"oauth","client_id":..}  (token 由 SDK 缓存于 ~/.longport/openapi/tokens/)
  2. 环境变量 LONGPORT_APP_KEY / LONGPORT_APP_SECRET / LONGPORT_ACCESS_TOKEN
UI 通过 web_server 暴露的 /api/lp/config 读写本配置文件, 无需手工设置环境变量。

OAuth 授权流程(SDK OAuthBuilder, 需在 LongPort 开放平台开通 OAuth 应用取得 client_id):
  重要: OAuthBuilder.build() 在等待授权期间会阻塞持有 GIL, 因此授权交互必须在独立子进程执行,
  主进程只做状态同步(状态文件 ~/.longport/oauth_state.json)与快速路径 handle 构建。
    - oauth_begin(client_id, port): 写状态文件并 spawn 子进程跑 build(浏览器授权)
    - 子进程在授权 URL 就绪/完成/失败时写回状态文件, 主进程 oauth_status() 读取供前端轮询
    - 授权成功后 SDK 把 token 缓存到 ~/.longport/openapi/tokens/<client_id>, 有效期内 build 直接复用
    - 主进程首次需要连接时 ensure_oauth_handle(): 先 spawn --oauth-probe 子进程(12s 超时)验证缓存
      仍有效, 再在主进程做快速 build(毫秒级); 避免 token 失效时主进程被 build 卡死
  回调端口默认 60355, 需与开放平台注册的 redirect URI 一致。

未配置/未授权时所有对外函数返回 {"configured": False/True, "need_auth": .., ...},
主服务优雅降级, 不影响 CBOE 策略分析。

SDK 版本: pip install longport (v4.3.7, pyo3)
  - Config 用 Config.from_apikey(...) / Config.from_oauth(...) 创建(无 from_env, Config(...) 不可直接实例化)
  - TradeContext.stock_positions() -> StockPositionsResponse{channels:[{positions:[{symbol,quantity,currency}]}]}
  - TradeContext.account_balance() -> List[AccountBalance{currency,total_cash,...}]
  - FundamentalContext.institution_rating(sym) -> InstitutionRating{summary:{target:str,recommend,updated_at},latest:{target:{highest_price,lowest_price}}}
  - CalendarContext.finance_calendar(category, start:str, end:str, market) -> {list:[{date,infos:[{symbol,date}]}]}
参考: https://open.longbridge.cn · 已实测 pyi(v4.3.7) 字段
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time

_LP_LOCK = threading.RLock()
_CTX = {}            # name -> context 实例缓存
_OAUTH_LOCK = threading.RLock()
_OAUTH = {"state": "idle", "client_id": None, "url": None,
          "error": None, "handle": None}   # OAuth handle 缓存(主进程, 仅授权完成后建立)
_OAUTH_CHILD = None  # 正在执行的授权子进程 Popen(用于重复授权/清除时回收)

CREDS_FILE = os.path.join(os.path.expanduser("~"), ".longport", "advisor_creds.json")
_ST_FILE = os.path.join(os.path.expanduser("~"), ".longport", "oauth_state.json")


# ---------------- OAuth 状态文件(子进程 <-> 主进程 同步) ----------------
def _st_read() -> dict:
    try:
        with open(_ST_FILE, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _st_write(d: dict):
    try:
        os.makedirs(os.path.dirname(_ST_FILE), exist_ok=True)
        tmp = _ST_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _ST_FILE)
    except OSError:
        pass


# ---------------- 凭据管理 ----------------
def _file_creds() -> dict:
    """读配置文件; 格式非法/缺失返回 None"""
    try:
        if not os.path.isfile(CREDS_FILE):
            return None
        with open(CREDS_FILE, encoding="utf-8") as f:
            d = json.load(f) or {}
        t = str(d.get("type") or "").strip().lower()
        if t == "oauth":
            cid = str(d.get("client_id") or "").strip()
            return {"type": "oauth", "client_id": cid} if cid else None
        if t == "apikey":
            ak = str(d.get("app_key") or "").strip()
            sk = str(d.get("app_secret") or "").strip()
            tk = str(d.get("access_token") or "").strip()
            if ak and sk and tk:
                return {"type": "apikey", "app_key": ak,
                        "app_secret": sk, "access_token": tk}
        return None
    except Exception:
        return None


def _env_creds() -> dict:
    ak = (os.environ.get("LONGPORT_APP_KEY") or "").strip()
    sk = (os.environ.get("LONGPORT_APP_SECRET") or "").strip()
    tk = (os.environ.get("LONGPORT_ACCESS_TOKEN") or "").strip()
    if ak and sk and tk:
        return {"type": "apikey", "app_key": ak,
                "app_secret": sk, "access_token": tk}
    return None


def effective_creds() -> dict:
    """当前生效凭据: 文件 > 环境变量"""
    return _file_creds() or _env_creds()


def lp_enabled() -> bool:
    return effective_creds() is not None


def _write_file(d: dict):
    """写配置文件。优先原子替换(临时文件 + os.replace), 被占用时退化为直接写。
    原子替换可避免"半写"状态; 直接写兜底(实测稳定)。"""
    p = os.path.dirname(CREDS_FILE)
    os.makedirs(p, exist_ok=True)
    tmp = f"{CREDS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CREDS_FILE)
        return
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        with open(CREDS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)


def save_apikey(app_key: str, app_secret: str, access_token: str):
    """保存 API Key 直连凭据到本机配置文件, 并重置内存中的长连接。"""
    _write_file({"type": "apikey", "app_key": app_key.strip(),
                 "app_secret": app_secret.strip(),
                 "access_token": access_token.strip()})
    _reset_runtime()


def save_oauth(client_id: str):
    """保存 OAuth client_id 到本机配置文件(不启动授权, 需再调 oauth_begin)。"""
    _write_file({"type": "oauth", "client_id": client_id.strip()})
    _reset_runtime()


def _rm_retry(p: str, tries: int = 3) -> bool:
    """删除文件并重试(Windows 杀软/句柄可能瞬态锁定)"""
    for i in range(tries):
        try:
            os.remove(p)
            return True
        except OSError:
            if i == tries - 1:
                return False
            time.sleep(0.25)
    return False


def _rmtree_retry(p: str, tries: int = 3) -> bool:
    """删除目录树并重试(同上, 用于 OAuth SDK 令牌缓存)"""
    for i in range(tries):
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            return True
        except OSError:
            if i == tries - 1:
                return False
            time.sleep(0.25)
    return False


def clear_creds():
    """清除本机 LongBridge 配置与 OAuth 状态/令牌缓存(不影响环境变量)。
    采用「原子覆写为空 + 尽力物理删除」: 实测 Windows 下长驻服务进程删除自己刚写入的
    文件可能被杀软/沙箱瞬态锁定(os.remove 稳定失败, 外部进程却可删), 因此清除语义
    不依赖物理删除——空配置文件内容等同未配置(file 优先级失效, 自动回落环境变量)。"""
    fc = _file_creds()
    st = _st_read()
    cid = None
    if fc and fc.get("type") == "oauth":
        cid = fc.get("client_id")
    if not cid:
        cid = st.get("client_id")
    # 1) 内容清空(语义 = 未配置)
    _write_file({})
    # 2) 尽力物理删除(成功更好; 失败不影响语义)
    _rm_retry(CREDS_FILE)
    # 3) 删除 SDK 缓存的 OAuth 令牌 → 退出登录
    if cid:
        td = os.path.join(os.path.expanduser("~"), ".longport",
                          "openapi", "tokens", str(cid))
        _rmtree_retry(td)
    # 4) 运行时/状态重置(回收授权子进程, 清空 context/handle/状态文件)
    _reset_runtime()


def _reset_runtime():
    """凭据变化后: 回收授权子进程、清空 context/OAuth handle, 下次请求按新凭据重建。"""
    _kill_child()
    with _LP_LOCK:
        _CTX.clear()
    with _OAUTH_LOCK:
        _OAUTH.update({"state": "idle", "client_id": None,
                       "url": None, "error": None, "handle": None})
    _st_write({"state": "idle", "client_id": None, "url": None, "error": None})


def _mask(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if len(s) <= 6:
        return s[:2] + "*" * (len(s) - 2)
    return s[:4] + "*" * (len(s) - 6) + s[-2:]


def config_status() -> dict:
    """返回给 UI 的脱敏状态: {configured, mode, source, app_key, client_id, oauth:{...}}"""
    fc = _file_creds()
    ec = _env_creds() if not fc else None
    src = "file" if fc else ("env" if ec else None)
    creds = fc or ec
    if not creds:
        return {
            "configured": False, "mode": "none", "source": None,
            "app_key": "", "client_id": "",
            "oauth": {"state": "idle"}, "msg": "未配置。可在下方填入 API Key 或完成账号授权。",
        }
    if creds["type"] == "oauth":
        with _OAUTH_LOCK:
            oa = {"state": _OAUTH["state"], "client_id": _OAUTH["client_id"],
                  "error": _OAUTH["error"]}
        return {
            "configured": True, "mode": "oauth", "source": src,
            "app_key": "", "client_id": _mask(creds["client_id"]),
            "oauth": oa, "msg": "账号授权(OAuth)模式",
        }
    return {
        "configured": True, "mode": "apikey", "source": src,
        "app_key": _mask(creds["app_key"]), "client_id": "",
        "oauth": {"state": "idle"},
        "msg": f"API Key 直连 (来自{('配置文件' if src=='file' else '环境变量')})",
    }


# ---------------- OAuth 授权(子进程隔离, 防 GIL 卡死主服务) ----------------
def _spawn(args: list):
    """spawn 本模块的独立子进程(Windows 下不弹控制台窗口), 返回 Popen"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        return subprocess.Popen([sys.executable, os.path.abspath(__file__)] + args,
                                creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None


def _kill_child():
    """回收仍在运行的授权子进程(防止残留进程占端口/覆盖状态)"""
    global _OAUTH_CHILD
    c = _OAUTH_CHILD
    _OAUTH_CHILD = None
    if c is not None:
        try:
            if c.poll() is None:
                c.kill()
                c.wait(timeout=5)
        except Exception:
            pass


def oauth_begin(client_id: str, callback_port: int = 60355) -> dict:
    """启动账号授权: 写状态文件 + spawn 子进程跑 OAuthBuilder.build()。
    子进程在授权 URL 就绪(回调 on_open_url)/完成/失败时写回状态文件。"""
    global _OAUTH_CHILD
    _kill_child()
    _st_write({"state": "starting", "client_id": client_id, "url": None, "error": None})
    with _OAUTH_LOCK:
        _OAUTH.update({"state": "starting", "client_id": client_id,
                       "url": None, "error": None, "handle": None})
    _OAUTH_CHILD = _spawn(["--oauth-worker", client_id, str(callback_port)])
    return {"started": True}


def oauth_status() -> dict:
    """返回给前端轮询的授权状态(读子进程写回的状态文件)"""
    st = _st_read()
    with _OAUTH_LOCK:
        has_handle = _OAUTH.get("handle") is not None
    return {"state": st.get("state") or "idle",
            "client_id": st.get("client_id"),
            "url": st.get("url"), "error": st.get("error"),
            "has_handle": has_handle}


def _probe_ok(client_id: str, timeout: float = 12.0) -> bool:
    """在子进程验证 SDK 磁盘 token 缓存是否仍有效(build 走快速路径则秒退;
    若已失效会进入等待浏览器授权的阻塞分支, 由超时 kill 兜底, 不影响主进程)。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__),
                              "--oauth-probe", client_id],
                             creationflags=flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            return False
        return rc == 0
    except Exception:
        return False


def ensure_oauth_handle() -> bool:
    """确保主进程持有 OAuth handle(仅授权 done 后执行一次)。
    先 probe 验证缓存 token 有效, 再在主进程做快速 build——避免 token 已失效时
    build() 阻塞持 GIL 卡死整个服务。返回 True 表示 handle 可用。"""
    with _OAUTH_LOCK:
        if _OAUTH.get("handle") is not None:
            return True
    st = _st_read()
    if st.get("state") != "done":
        return False
    cid = st.get("client_id")
    if not cid:
        return False
    if not _probe_ok(cid):
        return False
    try:
        with _OAUTH_LOCK:
            if _OAUTH.get("handle") is None:
                from longport.openapi import OAuthBuilder
                # probe 刚确认有效, 此处快速返回, 不会进入等待授权分支
                _OAUTH["handle"] = OAuthBuilder(cid).build(lambda u: None)
                _OAUTH["state"] = "done"
        return _OAUTH.get("handle") is not None
    except Exception:
        return False


def _oauth_worker_main(client_id: str, port: int) -> int:
    """子进程: 执行授权 build, 状态写回 ~/.longport/oauth_state.json"""
    try:
        from longport.openapi import OAuthBuilder

        def on_url(u: str):
            _st_write({"state": "url_ready", "client_id": client_id,
                       "url": u, "error": None})

        OAuthBuilder(client_id, callback_port=int(port)).build(on_url)
        _st_write({"state": "done", "client_id": client_id, "url": None, "error": None})
        return 0
    except Exception as e:
        _st_write({"state": "error", "client_id": client_id,
                   "url": None, "error": str(e)})
        return 1


def _oauth_probe_main(client_id: str) -> int:
    """子进程: 探测缓存 token 是否有效。build 快速路径成功->0; 失败/异常->2"""
    try:
        from longport.openapi import OAuthBuilder
        OAuthBuilder(client_id).build(lambda u: None)
        return 0
    except Exception:
        return 2


# ---------------- 连接构建 ----------------
def _build_config():
    """按当前生效凭据构建 Config; 凭据缺失/OAuth 未完成授权时抛 RuntimeError。"""
    from longport.openapi import Config
    creds = effective_creds()
    if not creds:
        raise RuntimeError("LP_NOT_CONFIGURED")
    if creds["type"] == "oauth":
        if not ensure_oauth_handle():
            raise RuntimeError("OAUTH_NOT_AUTHORIZED")
        with _OAUTH_LOCK:
            h = _OAUTH.get("handle")
        return Config.from_oauth(h)
    return Config.from_apikey(app_key=creds["app_key"],
                              app_secret=creds["app_secret"],
                              access_token=creds["access_token"])


def _ctx(name: str):
    """惰性建连(低频调用, 全局 RLock 串行化足够)"""
    with _LP_LOCK:
        if name not in _CTX:
            from longport.openapi import (CalendarContext,
                                          FundamentalContext, TradeContext)
            cfg = _build_config()
            _CTX["trade"] = TradeContext(cfg)
            _CTX["fund"] = FundamentalContext(cfg)
            _CTX["cal"] = CalendarContext(cfg)
        return _CTX[name]


def _oauth_err() -> dict:
    return {"configured": True, "need_auth": True,
            "error": ("尚未完成 LongBridge 账号授权: 请打开页面右上/上方的"
                      "「连接设置 → 账号授权」, 打开授权链接并在浏览器中完成授权后重试。")}


def not_configured() -> dict:
    return {
        "configured": False,
        "reason": "LP_NOT_CONFIGURED",
        "msg": ("未配置 LongBridge 凭据: 点击页面「连接设置」填入 API Key "
                "或完成账号授权即可启用(亦可设置环境变量后重启服务)"),
    }


# ---------------- 业务读取 ----------------
def _pluck(o, *names, default=None):
    for n in names:
        v = None
        try:
            v = getattr(o, n, None)
        except Exception:
            v = None
        if v is not None:
            return v
        if isinstance(o, dict):
            v = o.get(n)
            if v is not None:
                return v
    return default


def _num(v):
    try:
        if v in (None, ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _rec_name(v):
    """枚举(如 InstitutionRecommend.Buy) 取可读名"""
    if v is None:
        return None
    s = str(v)
    s = s.rsplit(".", 1)[-1]
    return s if s and s != "Unknown" else None


def fetch_positions_cash(ticker: str) -> dict:
    """读取账户美股持仓与现金(美元优先, 港币兜底)。
    返回: {configured, symbol, matched_qty, cash:{USD,HKD}, stocks:[...], note, error}
    """
    if not lp_enabled():
        return not_configured()
    sym = (ticker or "").strip().upper()
    lp_sym = sym + ".US"
    try:
        with _LP_LOCK:
            resp = _ctx("trade").stock_positions()
            stocks = []
            for ch in (_pluck(resp, "channels") or []):
                for p in (_pluck(ch, "positions") or []):
                    s = _pluck(p, "symbol")
                    q = _pluck(p, "quantity")
                    if not s or q is None:
                        continue
                    try:
                        q = int(q)
                    except (TypeError, ValueError):
                        continue
                    stocks.append({"symbol": s, "quantity": q,
                                   "currency": _pluck(p, "currency", default="USD") or "USD"})
            cash = {}
            try:
                bal = _ctx("trade").account_balance()
                if not isinstance(bal, (list, tuple)):
                    bal = [bal]
                for b in bal:
                    c = _pluck(b, "currency")
                    cashv = _num(_pluck(b, "total_cash", "cash", "net_cash"))
                    if c and cashv is not None:
                        cash[str(c).upper()] = cashv
            except Exception:
                cash = {}
        matched = next((s for s in stocks if str(s["symbol"]).upper() == lp_sym), None)
        note = (f"账户持有 {matched['symbol']} {matched['quantity']} 股"
                if matched else f"账户未持有 {lp_sym}")
        return {
            "configured": True,
            "symbol": sym,
            "matched_qty": matched["quantity"] if matched else 0,
            "cash": cash,
            "stocks": stocks,
            "note": note,
        }
    except RuntimeError as e:
        if "OAUTH_NOT_AUTHORIZED" in str(e):
            return _oauth_err()
        return {"configured": True, "error": f"LongBridge 凭据无效: {e}"}
    except Exception as e:
        return {"configured": True, "error": f"LongBridge 持仓读取失败: {e}"}


def fetch_analyst(ticker: str, horizon_days: int = 210) -> dict:
    """机构一致目标价 + 下次财报日(财报尽力而为, 失败不影响目标价)。"""
    if not lp_enabled():
        return not_configured()
    sym = (ticker or "").strip().upper()
    lp_sym = sym + ".US"
    out = {"configured": True, "symbol": sym, "target": None,
           "recommend": None, "updated_at": None,
           "earnings": None, "earnings_dte": None}
    try:
        with _LP_LOCK:
            r = _ctx("fund").institution_rating(lp_sym)
            latest = _pluck(r, "latest") or {}
            summary = _pluck(r, "summary") or {}
            # 共识目标价: summary.target 为字符串(如 "350.5")
            target = _num(_pluck(summary, "target", default=None))
            if target is None:
                # 兜底: 用最新快照区间均值
                tgt_range = _pluck(latest, "target") or {}
                hi = _num(_pluck(tgt_range, "highest_price"))
                lo = _num(_pluck(tgt_range, "lowest_price"))
                if hi and lo:
                    target = round((hi + lo) / 2.0, 2)
            recommend = _rec_name(_pluck(summary, "recommend", default=None))
            updated = _pluck(summary, "updated_at", default=None)
            if updated is None:
                ev = _pluck(summary, "evaluate") or {}
                updated = _pluck(ev, "date", default=None)
            out.update({
                "target": round(target, 2) if target is not None else None,
                "recommend": recommend,
                "updated_at": str(updated) if updated is not None else None,
            })
    except RuntimeError as e:
        if "OAUTH_NOT_AUTHORIZED" in str(e):
            return _oauth_err()
        out["target_error"] = f"LongBridge 凭据无效: {e}"
    except Exception as e:
        out["target_error"] = f"机构目标价读取失败: {e}"

    # 财报日: 独立容错 (calendar.list -> [{date, infos:[{symbol, date}]}])
    try:
        from longport.openapi import CalendarCategory
        today = dt.date.today()
        with _LP_LOCK:
            cal = _ctx("cal").finance_calendar(
                CalendarCategory.Report,
                (today - dt.timedelta(days=3)).isoformat(),
                (today + dt.timedelta(days=horizon_days)).isoformat(),
                market="US")
            best = None
            for grp in (_pluck(cal, "list") or []):
                gdate = str(_pluck(grp, "date", default="") or "")
                infos = _pluck(grp, "infos") or []
                matched_infos = [i for i in infos
                                 if str(_pluck(i, "symbol", default="") or "").upper() == lp_sym]
                if not matched_infos:
                    continue
                dstr = gdate or str(_pluck(matched_infos[0], "date", default="") or "")
                d = None
                for f in ("%Y-%m-%d", "%Y.%m.%d", "%Y%m%d"):
                    try:
                        d = dt.datetime.strptime(dstr[:10].replace(".", "-")[:10], f).date()
                        break
                    except (ValueError, TypeError):
                        continue
                if d and d >= today:
                    if best is None or d < best:
                        best = d
        if best:
            out["earnings"] = best.isoformat()
            out["earnings_dte"] = (best - today).days
    except RuntimeError as e:
        if "OAUTH_NOT_AUTHORIZED" in str(e) and "target_error" not in out:
            return _oauth_err()
    except Exception:
        pass
    return out


# ---------------- CLI: 供子进程调用 ----------------
if __name__ == "__main__":
    _args = sys.argv[1:]
    if len(_args) >= 3 and _args[0] == "--oauth-worker":
        sys.exit(_oauth_worker_main(_args[1], int(_args[2])))
    if len(_args) >= 2 and _args[0] == "--oauth-probe":
        sys.exit(_oauth_probe_main(_args[1]))
    print("usage: longbridge_api.py --oauth-worker <client_id> <port> | --oauth-probe <client_id>")
    sys.exit(0)
