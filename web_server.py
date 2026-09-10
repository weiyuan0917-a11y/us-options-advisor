#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股期权策略推荐器 - 本地 Web 服务
启动: python web_server.py [--port 8000]
浏览器打开 http://localhost:8000 即可使用交易向导
"""
import argparse
import datetime as dt
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import options_advisor as oa  # 复用策略引擎
import longbridge_api as lba  # LongBridge(LongPort) 可选集成: 未配置凭据时自动降级

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CACHE_TTL = 600  # 期权链缓存10分钟

_cache = {}          # {symbol: (ts, raw)}
_cache_lock = threading.Lock()


def get_raw(symbol: str) -> dict:
    """带缓存的期权链获取"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(symbol)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    raw = oa.fetch_cboe(symbol)
    with _cache_lock:
        _cache[symbol] = (now, raw)
    return raw


MULT = 100  # 每张合约对应 100 股
_OPT_RE = __import__("re").compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _parse_option_code(code: str):
    m = _OPT_RE.match(code or "")
    if not m:
        return None
    sym, ymd, kind, sk = m.groups()
    y = 2000 + int(ymd[0:2]); mo = int(ymd[2:4]); d = int(ymd[4:6])
    try:
        exp = dt.date(y, mo, d)
    except ValueError:
        return None
    return exp, kind, int(sk) / 1000.0


def build_iv_analysis(raw: dict, spot: float, max_expiries: int = 6) -> dict:
    """从 CBOE 原始数据按到期日聚合 IV 分布, 返回给前端作图.

    返回: {spot, atm_iv, term: [{dte, iv_atm}], skews: [{expiry, dte, kind,
        points:[{mny, strike, iv, delta, oi}]}]}
    """
    today = dt.date.today()
    d = raw.get("data", {})
    opts = d.get("options") or []

    # 按到期日聚合: {exp: {"C":[{strike,iv,delta,oi}], "P":[...]}}
    by_exp = {}
    for o in opts:
        r = _parse_option_code(o.get("option", ""))
        if not r:
            continue
        exp, kind, strike = r
        if exp < today:
            continue
        iv = o.get("iv")
        if not iv or iv <= 0 or iv > 5:  # 过滤异常(末日 deep OTM 噪声)
            continue
        delta = o.get("delta") or 0
        oi = o.get("open_interest") or 0
        if oi < 1:  # 零量合约 IV 不可信
            continue
        bucket = by_exp.setdefault(exp.isoformat(), {"call": [], "put": []})
        bucket["call" if kind == "C" else "put"].append(
            {"strike": strike, "iv": iv, "delta": delta, "oi": oi})

    # 选到期日: 按 DTE 排序, 取前 max_expiries 个 DTE>=7 天的
    items = []
    for exp, b in by_exp.items():
        dte = (dt.date.fromisoformat(exp) - today).days
        if dte < 7:
            continue
        items.append((exp, dte, b))
    items.sort(key=lambda x: x[1])
    items = items[:max_expiries]

    skews = []
    term = []
    for exp, dte, b in items:
        # ATM IV: 取 moneyness 最接近 1 (strike/spot) 的 call 平均两侧
        all_pts = []
        for k in ("call", "put"):
            pts = sorted(b[k], key=lambda p: abs(p["strike"] - spot))
            for p in pts:
                p2 = dict(p); p2["kind"] = k; p2["mny"] = p["strike"] / spot
                all_pts.append(p2)
        all_pts.sort(key=lambda p: abs(p["mny"] - 1))
        atm_iv = None
        if all_pts:
            # 取 moneyness∈[0.95,1.05] 内 IV 中位数
            inner = [p["iv"] for p in all_pts if 0.95 <= p["mny"] <= 1.05]
            atm_iv = round(sum(inner) / max(len(inner), 1), 4) if inner else round(all_pts[0]["iv"], 4)
        term.append({"dte": dte, "iv_atm": atm_iv})

        # 给这一到期日两条曲线: call/put 各自的 (moneyness, iv) 序列
        # 用 moneyness 而不是 strike 因为横轴范围标度统一
        for kind in ("call", "put"):
            pts = sorted(b[kind], key=lambda p: p["strike"])
            skews.append({
                "expiry": exp, "dte": dte, "kind": kind,
                "points": [{"mny": round(p["strike"] / spot, 3),
                            "iv": round(p["iv"], 4),
                            "delta": round(p["delta"], 3),
                            "oi": int(p["oi"])} for p in pts]
            })

    # 25-delta skew: 找 delta≈-0.25 (put) 与 +0.25 (call) 最接近的 IV 差
    skew_25 = None
    if items:
        last = items[0][2]
        try:
            iv_put_25 = min((p["iv"] for p in last["put"] if 0.20 <= abs(p["delta"]) <= 0.30),
                            default=None)
            iv_call_25 = min((p["iv"] for p in last["call"] if 0.20 <= abs(p["delta"]) <= 0.30),
                             default=None)
            if iv_put_25 is not None and iv_call_25 is not None:
                skew_25 = round((iv_put_25 - iv_call_25) * 100, 2)  # 百分比点
        except Exception:
            pass

    # iv30 兼容: 既是小数(0.26)又是百分数(26) 都用 26 作为百分比
    iv30_raw = d.get("iv30")
    iv30_pct = round(iv30_raw * 100, 2) if iv30_raw and iv30_raw <= 1 else (iv30_raw if iv30_raw else None)

    return {
        "spot": spot,
        "iv30_pct": iv30_pct,
        "term": term,
        "skews": skews,
        "skew_25_pct": skew_25,
    }


# ---------- LongBridge 可选参数解析 + AI 解读生成 ----------
def _parse_lp(q) -> dict:
    """从前端查询串解析可选 LongBridge 上下文(现金/一致目标价/财报日)。"""
    lp = {}
    try:
        v = q.get("lp_cash", [None])[0]
        if v not in (None, "", "null"):
            lp["cash"] = float(v)
    except (ValueError, TypeError):
        pass
    try:
        v = q.get("lp_target", [None])[0]
        if v not in (None, "", "null"):
            lp["target"] = float(v)
    except (ValueError, TypeError):
        pass
    try:
        v = q.get("lp_earn", [None])[0]
        if v not in (None, "", "null"):
            lp["earn"] = str(v)[:10]
    except (ValueError, TypeError):
        pass
    return lp


def _ins_cap_kind(note: str) -> str:
    note = note or ""
    if "全额现金担保" in note:
        return "cash"
    if ("价差" in note) or ("翼" in note):
        return "width"
    if ("正股" in note) or ("空头" in note):
        return "stock"
    return "cost"


def make_insight(rows, symbol, spot, target, view, iv, far_dte, near_dte,
                 position, lp=None) -> dict:
    """基于当前计算指标(IV面/期限结构/首选策略/资金占用) + 可选 LongBridge
    上下文(一致目标价/财报日/账户现金)生成人话解读。确定性规则引擎, 无外部LLM。
    返回 {"lines": [str, ...], "mode": "rules"}
    """
    lp = lp or {}
    spot = float(spot); target = float(target)
    lines = []

    # 1) 观点 + 目标价对比(如有券商一致目标价)
    chg = (target / spot - 1) * 100
    s1 = f"① 观点: {symbol} 现价 ${spot:,.2f}, 你的目标价 ${target:,.2f} 隐含 {chg:+.1f}% 空间, 判定为「{view}」观点。"
    ct = lp.get("target")
    if ct:
        cchg = (ct / float(spot) - 1) * 100
        if abs(target - ct) <= max(ct, spot) * 0.03:
            s1 += (f"券商一致目标价 ${ct:,.0f}(隐含 {cchg:+.1f}%)与你的目标基本一致, "
                   f"方向共识较强。")
        elif target > ct * 1.03:
            s1 += (f"券商一致目标价 ${ct:,.0f}(隐含 {cchg:+.1f}%)比你更保守——"
                   f"你的目标更激进, 需更强催化剂支撑, 建议分批或选盈亏平衡更近的结构。")
        else:
            s1 += (f"券商一致目标价 ${ct:,.0f}(隐含 {cchg:+.1f}%)比你更高——"
                   f"你的目标偏保守, 卖方/价差类结构会走得更从容。")
    lines.append(s1)

    # 2) 波动率环境: iv30 + 25Δ skew + 期限结构
    iv30 = skew = None
    term = []
    if iv:
        iv30 = iv.get("iv30_pct")
        skew = iv.get("skew_25_pct")
        term = iv.get("term") or []
    s2 = "② 波动率环境: "
    s2 += f"30日IV {iv30:.1f}% · " if iv30 else "30日IV 未知 · "
    if skew is None:
        s2 += "skew 数据不足; "
    elif skew > 0.5:
        s2 += (f"25Δ skew {skew:+.1f}pt 显示 put 端偏贵(避险溢价), "
               f"卖 put / 贷方价差等收租结构性价比更高, 买入保护成本偏高; ")
    elif skew < -0.5:
        s2 += (f"25Δ skew {skew:+.1f}pt 显示 call 端相对偏贵, "
               f"备兑看涨 / 卖出 call 更划算, 买入看涨需精挑行权价; ")
    else:
        s2 += "skew 两端定价均衡; "
    if len(term) >= 2 and term[0].get("iv_atm") and term[-1].get("iv_atm"):
        a = term[0]["iv_atm"] * 100; b = term[-1]["iv_atm"] * 100
        if a > b + 1.5:
            s2 += (f"期限结构倒挂(近月 {a:.0f}% > 远月 {b:.0f}%), "
                   f"日历/对角(卖近买远)的时间价值优势明显。")
        elif b > a + 1.5:
            s2 += f"期限结构正常上行(远月 {b:.0f}% > 近月 {a:.0f}%)。"
        else:
            s2 += f"期限结构平坦(近月 ~{a:.0f}%)。"
    lines.append(s2)

    # 3) 财报日历(如有 LongBridge 财报日)
    earn = lp.get("earn")
    if earn:
        try:
            edte = (dt.date.fromisoformat(str(earn)) - dt.date.today()).days
        except ValueError:
            edte = None
        if edte is not None:
            if edte <= near_dte:
                lines.append(f"③ 财报日历: {earn}(约 {edte} 天后)落在近月腿到期前——"
                             f"事件前 IV 高位、事后易 IV crush, 含近月腿的日历/对角与买跨式需规避。")
            elif edte <= far_dte:
                lines.append(f"③ 财报日历: {earn}(约 {edte} 天后)落在主到期前——"
                             f"标的存在跳空风险, 方向型策略建议错开或预留事件溢价。")
            else:
                lines.append(f"③ 财报日历: 到期窗口({far_dte} 天)内无财报"
                             f"(下次 {earn}, 约 {edte} 天后), 事件风险低。")

    # 4) 首选策略拆解(匹配观点第一个, 否则全表第一)
    top = next((r for r in rows if r.get("matched")), rows[0] if rows else None)
    if top:
        st, m = top["st"], top["m"]
        kind = _ins_cap_kind(st.capital_note)
        profit = m.get("profit_at_target")
        roi = m.get("roi_at_target")
        cap = getattr(st, "capital", 0) or 0
        np_ = getattr(st, "net_premium", 0) or 0
        s4 = (f"④ 首选策略「{st.name}」({st.bias} · 匹配「{view}」观点): "
              f"目标价到期收益 ${profit:,.0f} / ROI {roi * 100:+.1f}%。")
        if kind == "cost":
            s4 += f"这是借方结构, 净支出 ${abs(np_):,.0f} 即最大亏损上限, 盈亏清晰。"
        elif kind == "width":
            s4 += (f"贷方价差结构, 占用 ${cap:,.0f} 即封顶最大亏损, "
                   f"靠时间价值衰减获利, 只要到期价落在两腿之间即达标。")
        elif kind == "cash":
            s4 += (f"卖出 put 押现金担保 ${cap:,.0f}(到期释放), 最大亏损=(行权价−目标价) "
                   f"通常远小于押金, 风险收益比取决于你对该价位的信心。")
        else:
            if st.stock_qty and position >= abs(st.stock_qty):
                s4 += f"你已持有正股, 按 {getattr(st, 'groups', 1)} 组直接在现有仓位上搭建, 无需额外买股。"
            elif st.stock_qty:
                s4 += (f"当前无持仓, 需先建仓约 ${spot * 100:,.0f} 股本金"
                       f"(或按 {getattr(st, 'groups', 1)} 组放大)才能执行。")
            else:
                s4 += "资金占用为增量资本(带股结构), 与纯期权策略对比时注意分母口径。"
        lines.append(s4)

        # 5) 资金规模核对(如有账户现金)
        cash = lp.get("cash")
        if cash and cap:
            if cap > cash * 0.99:
                lines.append(f"⑤ 资金面: 该策略占用 ${cap:,.0f} 超过账户美元现金 ${cash:,.0f}, "
                             f"需减组数或换更窄价差; 卖权结构也可咨询券商用 Reg T 保证金降低现金占用。")
            else:
                lines.append(f"⑤ 资金面: 占用 ${cap:,.0f}, 约为账户美元现金 ${cash:,.0f} 的 "
                             f"{cap / cash * 100:.0f}%, 在可承受范围内。")

    # 6) 风控收尾
    lines.append("⑥ 风控: 中长期期权的主要风险是方向背离与财报跳空; 建议分 2~3 批建仓, "
                 "单策略占组合权益 ≤10%~20%, DTE<21 天时关注 gamma 与滚动窗口。")
    return {"lines": lines, "mode": "rules"}


def analyze(symbol: str, position: int, target_raw: str,
            dte: int = 180, min_dte: int = 90, near_dte: int = 45,
            lp: dict = None) -> dict:
    """执行分析并返回可 JSON 序列化的结果; lp 为可选 LongBridge 上下文"""
    raw = get_raw(symbol)
    d = raw.get("data", {})
    opts = d.get("options") or []
    if not opts:
        raise ValueError(f"{symbol} 无可用期权数据(CBOE)")
    spot = d.get("current_price") or d.get("last_trade_price") or d.get("close")
    if not spot:
        raise ValueError("无法获取标的现价")
    spot = round(float(spot), 2)

    # 目标价解析: 支持绝对价 / +10% / -5% / 空=现价
    if target_raw and str(target_raw).strip():
        target = oa.parse_target_input(str(target_raw), spot)
        if target is None or target <= 0:
            raise ValueError("目标价格式无效(示例: 350 / +10% / -5%)")
    else:
        target = spot

    today = dt.date.today()
    chain = oa.Chain(opts, today)
    far_exp = chain.pick_expiry(dte, min_dte)
    near_exp = chain.pick_expiry(near_dte, 7)
    if far_exp is None:
        raise ValueError("无可用到期日")

    strats = oa.build_strategies(chain, spot, far_exp, near_exp, position)
    rows, view = oa.rank_strategies(strats, spot, target)
    iv_data = build_iv_analysis(raw, spot)
    far_dte = chain.dte(far_exp)
    insight = make_insight(rows, symbol, spot, target, view, iv_data,
                           far_dte, chain.dte(near_exp), position, lp)

    out_rows = []
    for r in rows:
        st, m = r["st"], r["m"]
        # 降采样收益曲线(241->121点)减小JSON体积
        xs, ys = m["grid"]
        step = 2
        grid = {"x": [round(x, 2) for x in xs[::step]],
                "y": [round(y, 0) for y in ys[::step]]}
        out_rows.append({
            "name": st.name,
            "name_en": st.name_en,
            "bias": st.bias,
            "matched": r["matched"],
            "risk": st.risk_level,
            "shares_state": st.shares_state,
            "desc": st.desc,
            "legs": [{"action": "买入" if l.side > 0 else "卖出",
                      "kind": "Call" if l.kind == "call" else "Put",
                      "strike": l.strike, "premium": l.premium,
                      "expiry": l.expiry, "dte": l.dte,
                      "approx": l.approx, "iv": round(l.iv, 3) if l.iv else None,
                      "oi": l.oi} for l in st.legs],
            "stock_qty": st.stock_qty,
            "groups": st.groups,
            "net_premium": round(st.net_premium, 0),
            "capital": round(st.capital, 0),
            "capital_note": st.capital_note,
            "breakevens": m["breakevens"],
            "max_profit": m["max_profit"],
            "max_loss": m["max_loss"],
            "up_unlimited": m["up_unlimited"],
            "down_unlimited": m["down_unlimited"],
            "profit_at_target": m["profit_at_target"],
            "roi_at_target": round(m["roi_at_target"] * 100, 1) if m["roi_at_target"] is not None else None,
            "greeks": {k: round(v, 1) for k, v in (st.greeks or {}).items()},
            "grid": grid,
            "approx": st.approx,
        })

    return {
        "symbol": symbol,
        "spot": spot,
        "position": position,
        "target": target,
        "target_chg_pct": round((target / spot - 1) * 100, 1),
        "view": view,
        "far_expiry": far_exp,
        "far_dte": chain.dte(far_exp),
        "near_expiry": near_exp,
        "near_dte": chain.dte(near_exp),
        "data_time": d.get("last_trade_time") or "N/A",
        "iv30": d.get("iv30"),
        "iv_analysis": iv_data,
        "insight": insight,
        "lp": lp or {},
        "rows": out_rows,
    }


MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
        ".css": "text/css", ".json": "application/json", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 精简日志
        sys.stdout.write("[%s] %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))

    # ---- 响应工具
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---- 路由
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            try:
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "web/index.html 不存在".encode("utf-8"), "text/plain; charset=utf-8")
            return

        if path == "/api/analyze":
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("ticker", [""])[0] or "").strip().upper()
            if not (symbol.isalpha() and 1 <= len(symbol) <= 5):
                self._json({"error": "股票代码无效"}, 400)
                return
            try:
                position = int(str(q.get("position", ["0"])[0]).replace(",", "") or 0)
            except ValueError:
                self._json({"error": "持仓必须为整数"}, 400)
                return
            target_raw = q.get("target", [""])[0]
            try:
                dte = int(q.get("dte", ["180"])[0])
                min_dte = int(q.get("min_dte", ["90"])[0])
                near_dte = int(q.get("near_dte", ["45"])[0])
            except ValueError:
                self._json({"error": "天数参数必须为整数"}, 400)
                return
            try:
                lp = _parse_lp(q)
                self._json(analyze(symbol, position, target_raw, dte, min_dte, near_dte, lp))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # 网络错误等
                self._json({"error": f"分析失败: {e}"}, 502)
            return

        if path == "/api/lp/positions":
            """LongBridge: 读取账户持仓与现金(未配置凭据时返回 configured:false)"""
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("ticker", [""])[0] or "").strip().upper()
            if not (symbol.isalpha() and 1 <= len(symbol) <= 5):
                self._json({"error": "股票代码无效"}, 400)
                return
            try:
                self._json(lba.fetch_positions_cash(symbol))
            except Exception as e:
                self._json({"error": f"LongBridge 持仓接口异常: {e}"}, 502)
            return

        if path == "/api/lp/analyst":
            """LongBridge: 机构一致目标价 + 财报日(未配置凭据时返回 configured:false)"""
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("ticker", [""])[0] or "").strip().upper()
            if not (symbol.isalpha() and 1 <= len(symbol) <= 5):
                self._json({"error": "股票代码无效"}, 400)
                return
            try:
                self._json(lba.fetch_analyst(symbol))
            except Exception as e:
                self._json({"error": f"LongBridge 分析师接口异常: {e}"}, 502)
            return

        if path == "/api/lp/config":
            """LongBridge: 连接配置状态(脱敏; 供设置弹窗加载)"""
            try:
                self._json(lba.config_status())
            except Exception as e:
                self._json({"error": f"配置状态读取失败: {e}"}, 502)
            return

        if path == "/api/lp/oauth/status":
            """LongBridge: OAuth 授权状态轮询"""
            try:
                self._json(lba.oauth_status())
            except Exception as e:
                self._json({"error": f"OAuth 状态读取失败: {e}"}, 502)
            return

        if path == "/api/iv":
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("ticker", [""])[0] or "").strip().upper()
            if not (symbol.isalpha() and 1 <= len(symbol) <= 5):
                self._json({"error": "股票代码无效"}, 400)
                return
            try:
                max_exp = int(q.get("max_exp", ["6"])[0])
            except ValueError:
                self._json({"error": "max_exp 必须为整数"}, 400)
                return
            try:
                raw = get_raw(symbol)
                d = raw.get("data", {})
                if not (d.get("options") or []):
                    self._json({"error": f"{symbol} 无期权数据"}, 400); return
                spot = d.get("current_price") or d.get("last_trade_price") or d.get("close")
                if not spot:
                    self._json({"error": "无法获取现价"}, 400); return
                spot = round(float(spot), 2)
                self._json({
                    "symbol": symbol, "spot": spot,
                    "data_time": d.get("last_trade_time") or "N/A",
                    "iv": build_iv_analysis(raw, spot, max_expiries=max_exp),
                })
            except Exception as e:
                self._json({"error": f"IV 分析失败: {e}"}, 502)
            return

        if path == "/api/report":
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("ticker", [""])[0] or "").strip().upper()
            if not (symbol.isalpha() and 1 <= len(symbol) <= 5):
                self._json({"error": "股票代码无效"}, 400); return
            try:
                position = int(str(q.get("position", ["0"])[0]).replace(",", "") or 0)
            except ValueError:
                self._json({"error": "持仓必须为整数"}, 400); return
            target_raw = q.get("target", [""])[0]
            try:
                dte = int(q.get("dte", ["180"])[0])
                min_dte = int(q.get("min_dte", ["90"])[0])
                near_dte = int(q.get("near_dte", ["45"])[0])
                chart_top = int(q.get("chart_top", ["8"])[0])
            except ValueError:
                self._json({"error": "参数必须为整数"}, 400); return

            try:
                raw = get_raw(symbol)
                d = raw.get("data", {})
                if not (d.get("options") or []):
                    self._json({"error": f"{symbol} 无期权数据"}, 400); return
                spot = d.get("current_price") or d.get("last_trade_price") or d.get("close")
                if not spot:
                    self._json({"error": "无法获取现价"}, 400); return
                spot = round(float(spot), 2)
                if target_raw and str(target_raw).strip():
                    target = oa.parse_target_input(str(target_raw), spot)
                    if target is None or target <= 0:
                        self._json({"error": "目标价格式无效"}, 400); return
                else:
                    target = spot

                today = dt.date.today()
                chain = oa.Chain(d["options"], today)
                far_exp = chain.pick_expiry(dte, min_dte)
                near_exp = chain.pick_expiry(near_dte, 7)
                if far_exp is None:
                    self._json({"error": "无可用到期日"}, 400); return
                strats = oa.build_strategies(chain, spot, far_exp, near_exp, position)
                rows, view = oa.rank_strategies(strats, spot, target)
                iv_data = build_iv_analysis(raw, spot)
                data_time = d.get("last_trade_time") or "N/A"
                iv30 = d.get("iv30")
                lp = _parse_lp(q)
                insight = make_insight(rows, symbol, spot, target, view, iv_data,
                                       chain.dte(far_exp), chain.dte(near_exp),
                                       position, lp)

                html = oa.build_full_report(
                    rows, view, symbol, spot, position, target,
                    far_exp, chain.dte(far_exp), near_exp, data_time,
                    iv30=iv30, iv_analysis=iv_data, chart_top=chart_top,
                    insight_lines=insight.get("lines"))
                self._send(200, html.encode("utf-8"),
                           "text/html; charset=utf-8")
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"报告生成失败: {e}"}, 502)
            return

        if path == "/api/csv":
            q = urllib.parse.parse_qs(parsed.query)
            symbol = (q.get("ticker", [""])[0] or "").strip().upper()
            if not (symbol.isalpha() and 1 <= len(symbol) <= 5):
                self._json({"error": "股票代码无效"}, 400); return
            try:
                position = int(str(q.get("position", ["0"])[0]).replace(",", "") or 0)
            except ValueError:
                self._json({"error": "持仓必须为整数"}, 400); return
            target_raw = q.get("target", [""])[0]
            try:
                dte = int(q.get("dte", ["180"])[0])
                min_dte = int(q.get("min_dte", ["90"])[0])
                near_dte = int(q.get("near_dte", ["45"])[0])
            except ValueError:
                self._json({"error": "参数必须为整数"}, 400); return
            try:
                raw = get_raw(symbol)
                d = raw.get("data", {})
                if not (d.get("options") or []):
                    self._json({"error": f"{symbol} 无期权数据"}, 400); return
                spot = d.get("current_price") or d.get("last_trade_price") or d.get("close")
                if not spot:
                    self._json({"error": "无法获取现价"}, 400); return
                spot = round(float(spot), 2)
                if target_raw and str(target_raw).strip():
                    target = oa.parse_target_input(str(target_raw), spot)
                    if target is None or target <= 0:
                        self._json({"error": "目标价格式无效"}, 400); return
                else:
                    target = spot
                today = dt.date.today()
                chain = oa.Chain(d["options"], today)
                far_exp = chain.pick_expiry(dte, min_dte)
                near_exp = chain.pick_expiry(near_dte, 7)
                if far_exp is None:
                    self._json({"error": "无可用到期日"}, 400); return
                strats = oa.build_strategies(chain, spot, far_exp, near_exp, position)
                rows, view = oa.rank_strategies(strats, spot, target)
                lp = _parse_lp(q)
                insight = make_insight(rows, symbol, spot, target, view,
                                       build_iv_analysis(raw, spot),
                                       chain.dte(far_exp), chain.dte(near_exp),
                                       position, lp)
                csv_text = oa.build_csv_report(rows, view, symbol, spot, target,
                                               far_exp, chain.dte(far_exp),
                                               insight_lines=insight.get("lines"))
                # 文件名ASCII安全
                fname = f"{symbol}_options_{today.isoformat()}.csv"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(csv_text.encode("utf-8"))))
                self.send_header("Content-Disposition",
                                 f"attachment; filename=\"{fname}\"")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(csv_text.encode("utf-8-sig"))  # BOM 让 Excel 识别中文
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                self._json({"error": f"CSV 生成失败: {e}"}, 502)
            return

        # 静态文件(web/ 目录)
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(WEB_DIR, rel)
        if full.startswith(WEB_DIR) and os.path.isfile(full):
            ext = os.path.splitext(full)[1].lower()
            with open(full, "rb") as f:
                self._send(200, f.read(), MIME.get(ext, "application/octet-stream"))
            return
        self._send(404, b"Not Found", "text/plain")

    # ---- POST(仅本机服务; 用于 LongBridge 凭据配置与 OAuth 授权) ----
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path != "/api/lp/config":
            self._json({"error": "未知接口"}, 404)
            return
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            ln = 0
        if ln <= 0 or ln > 8192:
            self._json({"error": "请求体缺失或过大"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(ln).decode("utf-8"))
        except Exception:
            self._json({"error": "JSON 无效"}, 400)
            return
        action = payload.get("action")
        try:
            if action == "save_apikey":
                ak = str(payload.get("app_key") or "").strip()
                sk = str(payload.get("app_secret") or "").strip()
                tk = str(payload.get("access_token") or "").strip()
                if not (ak and sk and tk):
                    self._json({"error": "App Key / App Secret / Access Token 均不能为空"}, 400)
                    return
                lba.save_apikey(ak, sk, tk)
                self._json({"ok": True, "config": lba.config_status()})
            elif action == "save_oauth":
                cid = str(payload.get("client_id") or "").strip()
                if not cid:
                    self._json({"error": "Client ID 不能为空"}, 400)
                    return
                try:
                    port = int(payload.get("callback_port") or 60355)
                except (TypeError, ValueError):
                    port = 60355
                lba.save_oauth(cid)
                lba.oauth_begin(cid, port)
                self._json({"ok": True, "started": True,
                            "config": lba.config_status(),
                            "oauth": lba.oauth_status()})
            elif action == "register_oauth":
                # 动态注册 OAuth 客户端(无需后台申请), 自动填入并保存
                try:
                    port = int(payload.get("callback_port") or 60355)
                except (TypeError, ValueError):
                    port = 60355
                name = str(payload.get("client_name") or "us-options-advisor").strip()
                res = lba.oauth_register(name, port)
                if not res.get("ok"):
                    self._json({"error": "注册失败: " + str(res.get("error"))}, 502)
                    return
                cid = res["client_id"]
                lba.save_oauth(cid)
                lba.oauth_begin(cid, port)
                self._json({"ok": True, "client_id": cid, "started": True,
                            "config": lba.config_status(),
                            "oauth": lba.oauth_status()})
            elif action == "clear":
                lba.clear_creds()
                self._json({"ok": True, "config": lba.config_status()})
            else:
                self._json({"error": "未知 action"}, 400)
        except Exception as e:
            self._json({"error": f"配置操作失败: {e}"}, 500)


def main():
    ap = argparse.ArgumentParser(description="美股期权策略推荐器 Web 服务")
    ap.add_argument("--port", type=int, default=8000, help="监听端口(默认8000)")
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("=" * 52)
    print("美股期权策略推荐器 - 交易向导 Web 版")
    print(f"数据源: CBOE 延迟行情(约15分钟) | 仅供参考")
    print(f"请在浏览器打开: http://localhost:{args.port}")
    print("按 Ctrl+C 停止服务")
    print("=" * 52)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
