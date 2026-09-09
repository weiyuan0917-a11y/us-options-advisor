#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
美股中长期期权策略推荐器 (US Mid/Long-term Options Strategy Advisor)

输入: 股票代码 + 当前持仓股数(可为0) + 预计到期股票价格(目标价)
输出: 全部适用的中长期期权策略, 每个策略的 成本/最大盈亏/盈亏平衡点/目标价收益与ROI
数据源: CBOE 全球延迟行情接口 (免费, 含期权链与Greeks, 约15分钟延迟)

用法:
  # 交互模式（推荐）: 逐项询问股票代码/持仓/目标价, 支持连续分析多只股票
  python options_advisor.py

  # 命令行模式: 参数可省略, 目标价支持绝对价或涨跌幅
  python options_advisor.py --ticker AAPL --position 0 --target-price +10%
  python options_advisor.py --ticker NVDA --position 200 --target-price 200 --dte 270
"""
import argparse
import datetime as dt
import json
import math
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

MULT = 100  # 美股期权合约乘数
CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
OPT_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


# ---------------------------------------------------------------- 数据获取
def fetch_cboe(symbol: str) -> dict:
    url = CBOE_URL.format(sym=symbol.upper())
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def mid_price(o: dict) -> Optional[float]:
    bid = o.get("bid") or 0
    ask = o.get("ask") or 0
    last = o.get("last_trade_price") or 0
    if bid > 0 and ask > 0 and ask >= bid:
        return round((bid + ask) / 2, 3)
    if last > 0:
        return round(last, 3)
    return None


# ---------------------------------------------------------------- 期权链
class Chain:
    def __init__(self, raw_options: list, today: dt.date):
        self.today = today
        self.by_exp = {}  # {expiry_iso: {"call": {strike: quote}, "put": {strike: quote}}}
        for o in raw_options:
            m = OPT_RE.match(o.get("option", ""))
            if not m:
                continue
            ymd = m.group(2)
            try:
                exp = dt.date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
            except ValueError:
                continue
            if exp < today:
                continue
            kind = "call" if m.group(3) == "C" else "put"
            strike = int(m.group(4)) / 1000.0
            px = mid_price(o)
            if px is None or px <= 0:
                continue
            self.by_exp.setdefault(exp.isoformat(), {"call": {}, "put": {}})
            self.by_exp[exp.isoformat()][kind][strike] = {
                "strike": strike, "premium": px,
                "bid": o.get("bid") or 0, "ask": o.get("ask") or 0,
                "iv": o.get("iv") or 0, "oi": o.get("open_interest") or 0,
                "volume": o.get("volume") or 0,
                "delta": o.get("delta") or 0, "gamma": o.get("gamma") or 0,
                "theta": o.get("theta") or 0, "vega": o.get("vega") or 0,
            }

    def expiries(self) -> list:
        return sorted(self.by_exp.keys())

    def dte(self, exp: str) -> int:
        return (dt.date.fromisoformat(exp) - self.today).days

    def pick_expiry(self, target_dte: int, min_dte: int) -> Optional[str]:
        cands = [e for e in self.expiries() if self.dte(e) >= min_dte]
        if not cands:
            all_exp = self.expiries()
            return all_exp[-1] if all_exp else None
        return min(cands, key=lambda e: abs(self.dte(e) - target_dte))

    def pick_strike(self, exp: str, kind: str, target_price: float,
                    min_oi: int = 20, need_bid: bool = False):
        """选行权价: 优先 流动性达标(OI>=min_oi且bid>0) 中最接近目标价; 退而求其次 有报价即可"""
        table = self.by_exp[exp][kind]
        for tier_need_oi in (True, False):
            best_k, best_q = None, None
            for k, q in table.items():
                if need_bid and q["bid"] <= 0:
                    continue
                if tier_need_oi and q["oi"] < min_oi:
                    continue
                if best_k is None or abs(k - target_price) < abs(best_k - target_price):
                    best_k, best_q = k, q
            if best_k is not None:
                return best_k, best_q
        return None, None


# ---------------------------------------------------------------- 策略模型
@dataclass
class Leg:
    kind: str          # call / put
    side: int          # +1 买入 / -1 卖出
    strike: float
    premium: float
    expiry: str
    dte: int
    approx: bool = False  # 近月腿近似估值
    oi: int = 0
    delta: float = 0.0
    theta: float = 0.0
    iv: float = 0.0


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(kind: str, s: float, k: float, t: float, sigma: float, r: float = 0.04) -> float:
    """Black-Scholes 欧式期权理论价; t为年化剩余时间; sigma<=0 时退化为内在价值"""
    if t <= 0 or sigma <= 0 or s <= 0:
        return max(s - k, 0.0) if kind == "call" else max(k - s, 0.0)
    sq = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r + sigma * sigma / 2.0) * t) / sq
    d2 = d1 - sq
    if kind == "call":
        return s * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * norm_cdf(-d1)


@dataclass
class Strategy:
    name: str
    name_en: str
    bias: str            # 看涨 / 看跌 / 中性 / 波动 / 对冲
    match_views: list
    desc: str
    legs: List[Leg]
    stock_qty: int = 0         # 涉及正股数量(±100)
    stock_entry: float = 0.0    # 正股成本=现价
    capital: float = 0.0
    capital_note: str = ""
    risk_level: str = "中"
    approx: bool = False        # 日历/对角: 按近月到期时点估值
    t_rem: float = 0.0          # 近月到期时远月腿的年化剩余时间
    r_free: float = 0.04
    shares_state: str = "无需持仓"
    groups: int = 1            # 组数(1组=100股+1手期权; 带股策略按持仓可支撑组数规模化)
    greeks: dict = field(default_factory=dict)

    @property
    def net_premium(self) -> float:
        """净支出权利金(正=支出, 负=收入), 已按 groups 组数缩放"""
        return sum(l.side * l.premium for l in self.legs) * MULT * self.groups

    def payoff(self, s: float) -> float:
        v = 0.0
        for l in self.legs:
            if self.approx and not l.approx:
                # 远月腿在近月到期时点的价值: BS模型(保留剩余时间价值)
                val = bs_price(l.kind, s, l.strike, self.t_rem, l.iv or 0.25, self.r_free)
            else:
                val = max(s - l.strike, 0.0) if l.kind == "call" else max(l.strike - s, 0.0)
            v += l.side * val * MULT
        v *= self.groups
        v -= self.net_premium
        if self.stock_qty:
            v += self.stock_qty * self.groups * (s - self.stock_entry)
        return v

    def attach_greeks(self):
        self.greeks = {
            "net_delta": (sum(l.side * l.delta for l in self.legs) * MULT + self.stock_qty) * self.groups,
            "net_theta": sum(l.side * l.theta for l in self.legs) * MULT * self.groups,
        }

    def metrics(self, target: float, spot: float = None):
        # 聚焦曲线 x 轴到「行权价/现价/目标价」附近, 避免 0 起点死区挤掉关键区间
        prices = [l.strike for l in self.legs]
        if spot is not None:
            prices.append(spot)
        if target:
            prices.append(target)
        if not prices:
            prices = [1.0]
        lo = min(prices) * 0.72
        hi = max(prices) * 1.28
        if hi <= lo:
            hi = lo + 1.0
        n = 241
        xs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]
        ys = [self.payoff(x) for x in xs]
        bes = []
        for i in range(1, n):
            if ys[i - 1] == 0:
                bes.append(xs[i - 1])
            elif ys[i - 1] * ys[i] < 0:
                t = -ys[i - 1] / (ys[i] - ys[i - 1])
                bes.append(xs[i - 1] + t * (xs[i] - xs[i - 1]))
        vmax, vmin = max(ys), min(ys)
        imax, imin = ys.index(vmax), ys.index(vmin)
        up_unlimited = imax >= n - 2 and vmax > ys[n - 6]
        down_unlimited = imin >= n - 2 and vmin < ys[n - 6]
        profit_t = self.payoff(target) if target else None
        roi_t = (profit_t / self.capital) if (target and self.capital > 0) else None
        return {
            "grid": (xs, ys),
            "breakevens": [round(b, 2) for b in bes][:4],
            "max_profit": round(vmax, 0),
            "max_loss": round(vmin, 0),
            "up_unlimited": up_unlimited,
            "down_unlimited": down_unlimited,
            "profit_at_target": round(profit_t, 0) if profit_t is not None else None,
            "roi_at_target": roi_t,
        }


def fmt_money(v, sign=False):
    if v is None:
        return "-"
    sgn = "+" if (v >= 0 and sign) else ""
    if abs(v) >= 1000:
        return f"{sgn}${v:,.0f}"
    if abs(v) >= 100:
        return f"{sgn}${v:,.0f}"
    return f"{sgn}${v:,.2f}"


def fmt_pct(v):
    return "-" if v is None else f"{v * 100:+.1f}%"


# ---------------------------------------------------------------- 策略生成
def build_strategies(chain: Chain, spot: float, far_exp: str, near_exp: str,
                     position: int) -> List[Strategy]:
    S = spot
    out: List[Strategy] = []

    def opt(exp, kind, target_price, side, approx=False, need_bid=False):
        k, qq = chain.pick_strike(exp, kind, target_price, need_bid=need_bid)
        if qq is None:
            return None
        return Leg(kind, side, k, qq["premium"], exp, chain.dte(exp),
                   approx=approx, oi=qq["oi"], delta=qq["delta"], theta=qq["theta"],
                   iv=qq["iv"])

    def add(st: Strategy):
        st.attach_greeks()
        out.append(st)

    # ---------- 看涨: 无需持仓 ----------
    for tag, m_ in [("实值", 0.92), ("平值", 1.00), ("虚值", 1.08)]:
        leg = opt(far_exp, "call", S * m_, +1)
        if leg:
            add(Strategy(f"买入长期看涨期权({tag})", "Long Call", "看涨", ["看涨"],
                         "付出权利金换取到期上行的全部收益, 风险限于权利金", [leg],
                         capital=leg.premium * MULT, capital_note="权利金", risk_level="低"))
    l1 = opt(far_exp, "call", S, +1); l2 = opt(far_exp, "call", S * 1.12, -1, need_bid=True)
    if l1 and l2:
        add(Strategy("牛市看涨价差", "Bull Call Spread", "看涨", ["看涨"],
                     "买平值Call卖高行权价Call, 降低成本但封顶收益", [l1, l2],
                     capital=abs(l1.premium - l2.premium) * MULT, capital_note="净支出", risk_level="低"))
    l1 = opt(far_exp, "put", S * 0.93, -1, need_bid=True); l2 = opt(far_exp, "put", S * 0.80, +1)
    if l1 and l2:
        add(Strategy("牛市看跌价差", "Bull Put Spread", "看涨", ["看涨"],
                     "卖出虚值Put收权利金, 买更虚值Put封顶风险", [l1, l2],
                     capital=abs(l1.strike - l2.strike) * MULT, capital_note="价差保证金", risk_level="中"))
    l1 = opt(far_exp, "put", S * 0.93, -1, need_bid=True)
    if l1:
        add(Strategy("现金担保卖Put", "Cash-Secured Put", "看涨", ["看涨"],
                     "卖出虚值Put, 愿意以行权价接货; 不跌破则赚权利金", [l1],
                     capital=l1.strike * MULT, capital_note="全额现金担保", risk_level="中"))
    l1 = opt(far_exp, "call", S * 0.90, +1); l2 = opt(near_exp, "call", S * 1.06, -1, approx=True, need_bid=True)
    if l1 and l2:
        add(Strategy("穷人的备兑看涨(PMCC)", "Diagonal / PMCC", "看涨", ["看涨"],
                     "用深度实值长期Call替代正股+卖近月Call收租, 资金效率高", [l1, l2],
                     capital=l1.premium * MULT, capital_note="长期腿权利金", risk_level="中", approx=True,
                     t_rem=(chain.dte(far_exp) - chain.dte(near_exp)) / 365.0))
    l1 = opt(near_exp, "call", S, -1, approx=True, need_bid=True); l2 = opt(far_exp, "call", S, +1)
    if l1 and l2:
        add(Strategy("日历价差(卖近买远)", "Calendar Spread", "中性", ["中性", "看涨"],
                     "卖近月平值Call+买远月同行权价Call, 赚时间价值衰减差", [l1, l2],
                     capital=abs(l2.premium - l1.premium) * MULT, capital_note="净支出", risk_level="中", approx=True,
                     t_rem=(chain.dte(far_exp) - chain.dte(near_exp)) / 365.0))

    # ---------- 看跌: 无需持仓 ----------
    for tag, m_ in [("平值", 1.00), ("虚值", 0.92)]:
        leg = opt(far_exp, "put", S * m_, +1)
        if leg:
            add(Strategy(f"买入长期看跌期权({tag})", "Long Put", "看跌", ["看跌"],
                         "付权利金对冲下行或做空, 风险限于权利金", [leg],
                         capital=leg.premium * MULT, capital_note="权利金", risk_level="低"))
    l1 = opt(far_exp, "put", S, +1); l2 = opt(far_exp, "put", S * 0.88, -1, need_bid=True)
    if l1 and l2:
        add(Strategy("熊市看跌价差", "Bear Put Spread", "看跌", ["看跌"],
                     "买平值Put卖低行权价Put, 降低做空成本但封底收益", [l1, l2],
                     capital=abs(l1.premium - l2.premium) * MULT, capital_note="净支出", risk_level="低"))
    l1 = opt(far_exp, "call", S * 1.07, -1, need_bid=True); l2 = opt(far_exp, "call", S * 1.20, +1)
    if l1 and l2:
        add(Strategy("熊市看涨价差", "Bear Call Spread", "看跌", ["看跌"],
                     "卖出虚值Call收权利金, 买更高Call封顶风险", [l1, l2],
                     capital=abs(l2.strike - l1.strike) * MULT, capital_note="价差保证金", risk_level="中"))

    # ---------- 波动/中性 ----------
    l1 = opt(far_exp, "call", S, +1); l2 = opt(far_exp, "put", S, +1)
    if l1 and l2:
        add(Strategy("多头跨式", "Long Straddle", "波动", ["波动"],
                     "同时买平值Call+Put, 赌大幅突破, 方向不限", [l1, l2],
                     capital=(l1.premium + l2.premium) * MULT, capital_note="权利金", risk_level="低"))
    l1 = opt(far_exp, "call", S * 1.08, +1); l2 = opt(far_exp, "put", S * 0.92, +1)
    if l1 and l2:
        add(Strategy("多头宽跨式", "Long Strangle", "波动", ["波动"],
                     "买两边虚值, 成本比跨式低但需要更大波动", [l1, l2],
                     capital=(l1.premium + l2.premium) * MULT, capital_note="权利金", risk_level="低"))
    lc1 = opt(far_exp, "call", S * 1.10, -1, need_bid=True); lc2 = opt(far_exp, "call", S * 1.22, +1)
    lp1 = opt(far_exp, "put", S * 0.90, -1, need_bid=True); lp2 = opt(far_exp, "put", S * 0.78, +1)
    if all([lc1, lc2, lp1, lp2]):
        wing = max(lc2.strike - lc1.strike, lp1.strike - lp2.strike)
        add(Strategy("铁鹰式", "Iron Condor", "中性", ["中性"],
                     "两边卖虚值收双份权利金, 标的横盘则全收", [lp2, lp1, lc1, lc2],
                     capital=wing * MULT, capital_note="最宽翼保证金", risk_level="中"))
    sc = opt(far_exp, "call", S, -1, need_bid=True); sp_ = opt(far_exp, "put", S, -1, need_bid=True)
    wc = opt(far_exp, "call", S * 1.15, +1); wp = opt(far_exp, "put", S * 0.85, +1)
    if all([sc, sp_, wc, wp]):
        wing = min(wc.strike - sc.strike, sp_.strike - wp.strike)
        add(Strategy("铁蝶式", "Iron Butterfly", "中性", ["中性"],
                     "卖平值跨式+两边保护翼, 收入高但赌到期钉在平值", [wp, sp_, sc, wc],
                     capital=wing * MULT, capital_note="保护翼保证金", risk_level="中"))
    b1 = opt(far_exp, "call", S * 0.95, +1); b2 = opt(far_exp, "call", S, -1); b3 = opt(far_exp, "call", S * 1.05, +1)
    if all([b1, b2, b3]):
        debit = (b1.premium - 2 * b2.premium + b3.premium) * MULT
        add(Strategy("多头蝶式看涨", "Long Call Butterfly", "中性", ["中性"],
                     "买低卖双平买高, 低成本赌标的停在行权价附近", [b1, b2, b2, b3],
                     capital=max(debit, 1), capital_note="净支出", risk_level="低"))

    # ---------- 与持仓相关 ----------
    has_long = position >= 100
    has_short = position <= -100
    # 规模化: 持仓可支撑的完整组数 (1组=100股+1手期权); 持仓不匹配该方向时按 1 组(buy-write)测算
    g_long = (position // 100) if has_long else 1
    g_short = (-position // 100) if has_short else 1
    long_state = "匹配当前持仓" if has_long else ("持仓不足100股" if position > 0 else "需买入100股正股")
    short_state = "匹配当前空头持仓" if has_short else "需卖出100股正股"

    l1 = opt(far_exp, "call", S * 1.10, -1, need_bid=True)
    if l1:
        add(Strategy("备兑看涨" + ("" if has_long else "(买要Buy-Write)"), "Covered Call", "看涨", ["看涨", "中性"],
                     "持有/买入100股正股+卖出虚值Call收租, 股价横盘或小涨最优", [l1],
                     stock_qty=100, stock_entry=S, capital=S * MULT * g_long,
                     capital_note="正股价值" if has_long else "正股价值-权利金收入", risk_level="中",
                     shares_state=long_state, groups=g_long))
    l1 = opt(far_exp, "put", S * 0.92, +1)
    if l1:
        add(Strategy("保护性看跌", "Protective Put", "对冲", ["对冲", "看跌"],
                     "为持仓买保险, 锁定最大回撤同时保留上行", [l1],
                     stock_qty=100, stock_entry=S, capital=(S * MULT + l1.premium * MULT) * g_long,
                     capital_note="正股价值+保险费", risk_level="低", shares_state=long_state, groups=g_long))
    l1 = opt(far_exp, "put", S * 0.88, +1); l2 = opt(far_exp, "call", S * 1.10, -1, need_bid=True)
    if l1 and l2:
        add(Strategy("领口保护", "Collar", "对冲", ["对冲", "中性"],
                     "买Put卖Call双翼锁定持仓盈亏区间, 近零成本保险", [l1, l2],
                     stock_qty=100, stock_entry=S, capital=S * MULT * g_long, capital_note="正股价值", risk_level="低",
                     shares_state=long_state, groups=g_long))
    l1 = opt(far_exp, "put", S * 0.90, -1, need_bid=True)
    if l1:
        add(Strategy("备兑看跌", "Covered Put", "看跌", ["看跌"],
                     "持有空头+卖出虚值Put收租, 横盘或小跌最优", [l1],
                     stock_qty=-100, stock_entry=S, capital=S * MULT * g_short, capital_note="空头保证金(简化按正股价值)",
                     risk_level="高", shares_state=short_state, groups=g_short))
    l1 = opt(far_exp, "call", S * 1.08, +1)
    if l1:
        add(Strategy("保护性看涨", "Protective Call", "对冲", ["对冲", "看涨"],
                     "为空头持仓买Call保险, 锁定轧空风险", [l1],
                     stock_qty=-100, stock_entry=S, capital=(S * MULT + l1.premium * MULT) * g_short,
                     capital_note="空头保证金+保险费", risk_level="中", shares_state=short_state, groups=g_short))

    return out


# ---------------------------------------------------------------- 观点与排序
def implied_view(spot: float, target: float) -> str:
    chg = target / spot - 1
    if chg >= 0.05:
        return "看涨"
    if chg <= -0.05:
        return "看跌"
    return "中性"


def rank_strategies(strats: List[Strategy], spot: float, target: float):
    view = implied_view(spot, target)
    big_move = abs(target / spot - 1) >= 0.12
    rows = []
    for st in strats:
        m = st.metrics(target, spot)
        matched = (view in st.match_views
                   or ("波动" in st.match_views and big_move)
                   or ("对冲" in st.match_views and st.shares_state.startswith("匹配")))
        rows.append({"st": st, "m": m, "matched": matched})
    rows.sort(key=lambda r: (not r["matched"],
                             -(r["m"]["roi_at_target"] if r["m"]["roi_at_target"] is not None else -9e9)))
    return rows, view


# ---------------------------------------------------------------- 控制台输出
def print_console(rows, view, spot, target, symbol, position, far_exp, far_dte, near_exp):
    print("=" * 108)
    print(f"{symbol} | 现价 ${spot:,.2f} | 持仓 {position} 股 | 目标价 ${target:,.2f} ({view}, {target/spot-1:+.1%})")
    print(f"主到期日 {far_exp} (DTE {far_dte}天) | 近月腿 {near_exp} | 每组合约=100股 | 带股策略按持仓×N组规模化 | 排序: 匹配观点+目标价ROI")
    print("=" * 108)
    print(f"{'策略':<26}{'观点':<4}{'现金流':>10}{'资金占用':>10}{'盈亏平衡':>16}{'最大盈利':>12}{'最大亏损':>12}{'目标价收益':>11}{'ROI':>8}")
    print("-" * 108)
    for r in rows:
        st, m = r["st"], r["m"]
        mark = "★" if r["matched"] else " "
        be = "/".join(f"{b:g}" for b in m["breakevens"]) or "-"
        mp = f"≥{m['max_profit']:,.0f}↑" if m["up_unlimited"] else f"{m['max_profit']:,.0f}"
        ml = f"{m['max_loss']:,.0f}↓" if m["down_unlimited"] else f"{m['max_loss']:,.0f}"
        print(f"{mark}{st.name:<25}{st.bias:<4}{fmt_money(-st.net_premium, True):>10}{st.capital:>10,.0f}"
              f"{be:>16}{mp:>12}{ml:>12}{fmt_money(m['profit_at_target'], True):>11}{fmt_pct(m['roi_at_target']):>8}")
    print("-" * 108)
    print("注: 现金流正=收入权利金, 负=支出; 损益为每1组合(100股)口径; 数据源 CBOE 延迟行情")


# ---------------------------------------------------------------- HTML 报告
HTML_HEAD = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
body{font-family:"Microsoft YaHei","PingFang SC",Arial,sans-serif;background:#f5f6f8;color:#1c2430;margin:0;padding:24px;}
.wrap{max-width:1100px;margin:0 auto;}
h1{font-size:24px;margin:8px 0 4px;}
.sub{color:#5b6472;font-size:13px;margin-bottom:16px;}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:14px 0;}
.card{flex:1;min-width:200px;background:#fff;border-radius:10px;padding:14px 16px;box-shadow:0 1px 4px rgba(20,30,50,.08);border-top:3px solid #3a6df0;}
.card .k{font-size:12px;color:#5b6472;}
.card .v{font-size:22px;font-weight:700;margin-top:4px;}
.card .n{font-size:12px;color:#8a93a3;margin-top:2px;line-height:1.6;}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px;}
th{background:#eef1f6;padding:8px 10px;text-align:right;font-weight:600;white-space:nowrap;}
th:first-child,td:first-child{text-align:left;}
td{padding:7px 10px;border-bottom:1px solid #eef0f4;text-align:right;white-space:nowrap;}
tr.match{background:#f2f7ff;}
.pos{color:#c62f2f;font-weight:600;}
.neg{color:#0d7a43;font-weight:600;}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;background:#eef1f6;color:#44506a;}
.tag.bull{background:#fdeaea;color:#c62f2f;}
.tag.bear{background:#e8f5ee;color:#0d7a43;}
.tag.vol{background:#f3ecfb;color:#6b3fa0;}
.tag.hedge{background:#fff4e3;color:#a06a00;}
.star{color:#e5a000;font-weight:700;}
.sec{background:#fff;border-radius:10px;padding:16px 18px;margin:18px 0;box-shadow:0 1px 4px rgba(20,30,50,.08);}
.sec h2{font-size:17px;margin:2px 0 12px;border-left:4px solid #3a6df0;padding-left:10px;}
.strat-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px;}
.legs{margin:10px 0 4px;}
.chart{width:100%;height:340px;}
.metric{display:inline-block;margin:6px 18px 2px 0;}
.metric .k{font-size:12px;color:#5b6472;}
.metric .v{font-size:16px;font-weight:700;}
.note{font-size:12px;color:#8a93a3;line-height:1.7;margin-top:10px;}
.foot{font-size:12px;color:#8a93a3;border-top:1px solid #e3e6ec;margin-top:20px;padding-top:12px;line-height:1.8;}
</style>
</head>
<body><div class="wrap">
"""

HTML_FOOT = """
<div class="foot">
<b>计算假设</b>：所有损益按到期日计算（欧式近似）；多腿价差按内在价值结算；日历/对角策略按"近月到期时点"估值——近月腿按内在价值、远月腿按 Black-Scholes 模型（用 CBOE 隐含波动率）保留剩余时间价值；忽略佣金、滑点、分红除权与提前行权；每1组合约=100股；无风险利率按 4% 计。<br>
<b>数据来源</b>：CBOE 全球延迟行情接口（cdn.cboe.com），约15分钟延迟；数据时点见页面顶部标注。权利金取买卖价中间价（无买卖价时用最新成交价）。<br>
<b>免责声明</b>：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。
</div></div>
<script>var CHARTS=__CHARTS__;</script>
<script>
(function(){
  function renderLine(elId, series, yLabel){
    var el=document.getElementById(elId); if(!el) return;
    var chart=echarts.init(el);
    var csi=[];
    Object.keys(CHARTS).forEach(function(k){
      if(k.indexOf(series)==0){var d=CHARTS[k]; if(d.x&&d.x.length>1)
        csi.push({name:d.label||k, type:"line", data:d.x.map(function(x,i){return[x,d.y[i]];}),
                  smooth:true, symbol:"none", lineStyle:{width:1.6}});}
    });
    if(csi.length===0) return;
    chart.setOption({
      tooltip:{trigger:"axis", formatter:function(ps){var p=ps[0]; return "<b>"+p.value[0].toFixed(2)+"</b><br/>"+ps.map(function(q){return q.marker+q.seriesName+": "+q.value[1].toFixed(1)+"%";}).join("<br/>");}},
      legend:{top:2, type:"scroll"},
      grid:{left:60,right:18,top:34,bottom:38},
      xAxis:{type:"value", name:"Moneyness K/spot", min:function(v){return Math.max(0.85,Math.floor(v.min*100)/100);},
             max:function(v){return Math.min(1.15,Math.ceil(v.max*100)/100);}},
      yAxis:{type:"value", name:yLabel, axisLabel:{formatter:"{value}%"}},
      series:csi
    });
  }
  function renderTerm(){
    var el=document.getElementById("iv_term_chart"); if(!el) return;
    var d=CHARTS["iv_term"]; if(!d||!d.dte.length) return;
    var data=d.dte.map(function(v,i){return [v,d.iv[i]];});
    var marker=null;
    if(d.iv30){
      marker={data:[{coord:[30,d.iv30], label:{formatter:"iv30 "+d.iv30+"%",position:"top",color:"#3a6df0"}}],
              symbol:"diamond", symbolSize:10, itemStyle:{color:"#3a6df0"}};
    }
    echarts.init(el).setOption({
      tooltip:{trigger:"axis", formatter:function(p){return "DTE "+p.value[0]+"<br/>ATM IV: "+p.value[1].toFixed(1)+"%";}},
      grid:{left:60,right:18,top:24,bottom:34},
      xAxis:{type:"value", name:"DTE (天)"},
      yAxis:{type:"value", name:"ATM IV", axisLabel:{formatter:"{value}%"}},
      series:[{type:"line", data:data, smooth:true, symbol:"circle", symbolSize:6,
               lineStyle:{color:"#3a6df0", width:2}, itemStyle:{color:"#3a6df0"},
               areaStyle:{color:"rgba(58,109,240,0.12)"},
               markPoint:marker}]
    });
  }
  function renderPayoff(elId, d){
    var el=document.getElementById(elId); if(!el) return;
    var chart=echarts.init(el);
    var data=d.x.map(function(x,i){return [x, d.y[i]];});
    var markLineData=[{xAxis:d.spot,lineStyle:{color:"#44506a",type:"dashed"},label:{formatter:"现价 $"+d.spot}},
                      {xAxis:d.target,lineStyle:{color:"#3a6df0",type:"dashed"},label:{formatter:"目标 $"+d.target}}];
    chart.setOption({
      tooltip:{trigger:"axis", formatter:function(ps){var p=ps[0]; return "<b>$"+p.value[0].toFixed(0)+"</b><br/>损益: $"+Math.round(p.value[1]).toLocaleString("en-US");}},
      grid:{left:76,right:24,top:34,bottom:44},
      xAxis:{type:"value", name:"股价($)", axisLabel:{formatter:"${value}"}},
      yAxis:{type:"value", name:"损益($)"},
      series:[{type:"line", data:data, smooth:false, symbol:"none",
               lineStyle:{color:"#3a6df0", width:2},
               areaStyle:{color:"rgba(58,109,240,0.10)"},
               markLine:{symbol:"none", data:markLineData}}]
    });
  }
  // 损益曲线策略
  document.querySelectorAll(".chart").forEach(function(el){
    var k=el.id, d=CHARTS[k];
    if(!d) return;
    if(d.profit && d.loss){ // 旧式损益：盈利/亏损 split
      chart=echarts.init(el);
      chart.setOption({
        tooltip:{trigger:"axis",valueFormatter:function(v){return v===null?"-":"$"+v.toFixed(0);}},
        grid:{left:76,right:24,top:34,bottom:44},
        xAxis:{type:"category",data:d.x,axisLabel:{formatter:function(v){return "$"+v;}}},
        yAxis:{type:"value",name:"损益($)",axisLine:{show:true}},
        series:[
          {name:"盈利",type:"line",data:d.profit,symbol:"none",connectNulls:false,
           itemStyle:{color:"#c62f2f"},lineStyle:{color:"#c62f2f",width:2},
           areaStyle:{color:"rgba(198,47,47,0.10)"},
           markLine:{symbol:"none",data:[
             {yAxis:0,lineStyle:{color:"#98a1b0",type:"dashed"},label:{formatter:"盈亏平衡"}},
             {yAxis:d.spot,lineStyle:{color:"#44506a",type:"dashed"},label:{formatter:"现价"}},
             {yAxis:d.target,lineStyle:{color:"#3a6df0",type:"dashed"},label:{formatter:"目标价"}}]}},
          {name:"亏损",type:"line",data:d.loss,symbol:"none",connectNulls:false,
           itemStyle:{color:"#0d7a43"},lineStyle:{color:"#0d7a43",width:2},
           areaStyle:{color:"rgba(13,122,67,0.10)"}}
        ]});
    }
  });
  renderLine("iv_call_chart", "iv_call_", "IV(%)");
  renderLine("iv_put_chart",  "iv_put_",  "IV(%)");
  renderTerm();
})();
</script>
</body></html>
"""


def cls_for_bias(bias):
    return {"看涨": "bull", "看跌": "bear", "中性": "tag", "波动": "vol", "对冲": "hedge"}.get(bias, "tag")


def build_html(rows, view, symbol, spot, position, target, far_exp, far_dte,
               near_exp, data_time, chart_top=6, out_path="report.html",
               iv30=None, iv_analysis=None, insight_lines=None):
    top = rows[:chart_top]
    matched = [r for r in rows if r["matched"]]
    best = matched[0] if matched else (rows[0] if rows else None)

    chg = target / spot - 1
    cards = f"""
<div class="cards">
  <div class="card"><div class="k">当前价格 (CBOE延迟)</div><div class="v">${spot:,.2f}</div><div class="n">{symbol} · 数据时点 {data_time}</div></div>
  <div class="card"><div class="k">目标价（预计到期价）</div><div class="v">${target:,.2f}</div><div class="n">{chg:+.1%} · 隐含观点：{view}</div></div>
  <div class="card"><div class="k">当前持仓</div><div class="v">{position} 股</div><div class="n">可支撑 {abs(position)//100} 组备兑/对冲</div></div>
  <div class="card"><div class="k">主到期日</div><div class="v">{far_exp}</div><div class="n">DTE {far_dte} 天 · 近月腿 {near_exp}</div></div>
</div>"""
    if best:
        _g = best['st'].groups
        _gs = " / 组" if _g == 1 else f"（按 {_g} 组测算）"
        cards += f"""
<div class="cards">
  <div class="card" style="border-top-color:#e5a000"><div class="k">匹配观点的首选策略</div><div class="v">{best['st'].name}</div><div class="n">{best['st'].desc}<br>目标价收益 {fmt_money(best['m']['profit_at_target'],True)}{_gs} · ROI {fmt_pct(best['m']['roi_at_target'])} · 资金占用 ${best['st'].capital:,.0f}</div></div>
</div>"""

    # AI 策略解读板块(可选, 插入到汇总卡片之后、策略表之前)
    if insight_lines:
        _esc = (lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        _ins_rows = "".join(
            '<div style="padding:9px 13px;margin:5px 0;background:#f5f8ff;'
            'border-left:3px solid #3a6df0;border-radius:0 8px 8px 0;'
            'font-size:13px;line-height:1.75;color:#1c2430">' + _esc(ln) + "</div>"
            for ln in insight_lines if str(ln).strip())
        cards += f"""
<div class="sec"><h2>🤖 AI 策略解读</h2>
<p class="note" style="margin:0 0 6px 0">结合你的观点与目标价、IV 面（skew + 期限结构）、券商一致目标价、财报日历与账户资金面自动生成（确定性规则，仅供参考）：</p>
{_ins_rows}
</div>"""

    trs = []
    for r in rows:
        st, m = r["st"], r["m"]
        star = '<span class="star">★</span>' if r["matched"] else ""
        be = " / ".join(f"${b:g}" for b in m["breakevens"]) or "-"
        mp = (f"≥${m['max_profit']:,.0f}" if m["up_unlimited"] else f"${m['max_profit']:,.0f}")
        ml = (f"${m['max_loss']:,.0f}↓" if m["down_unlimited"] else f"${m['max_loss']:,.0f}")
        pt = m["profit_at_target"]
        pt_cls = "pos" if (pt or 0) > 0 else ("neg" if (pt or 0) < 0 else "")
        roi = m["roi_at_target"]
        roi_cls = "pos" if (roi or 0) > 0 else ("neg" if (roi or 0) < 0 else "")
        flow = -st.net_premium
        flow_s = f'<span class="{"pos" if flow>0 else "neg"}">{fmt_money(flow, True)}</span>'
        nd = st.greeks.get("net_delta", 0)
        trs.append(
            f'<tr class="{"match" if r["matched"] else ""}"><td>{star}{st.name}</td>'
            f'<td><span class="tag {cls_for_bias(st.bias)}">{st.bias}</span></td>'
            f'<td>{st.shares_state}</td><td>{flow_s}</td><td>${st.capital:,.0f}</td>'
            f'<td>{nd:+.0f}</td><td>{be}</td>'
            f'<td>{mp}</td><td>{ml}</td>'
            f'<td class="{pt_cls}">{fmt_money(pt, True)}</td>'
            f'<td class="{roi_cls}">{fmt_pct(roi)}</td></tr>')
    table = f"""
<div class="sec"><h2>全部策略对比（1组合约=100股 · 带股策略按持仓×N组规模化，目标价 ${target:,.2f} 测算）</h2>
<table><thead><tr><th>策略</th><th>观点</th><th>适用性</th><th>现金流</th><th>资金占用</th><th>净Delta</th><th>盈亏平衡</th><th>最大盈利</th><th>最大亏损</th><th>目标价收益</th><th>ROI</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<div class="note">现金流正=收入权利金、负=支出；★=与「{view}」观点匹配；净Delta为Greeks聚合（正=看涨敞口，含正股）；含近月腿的策略（日历/对角）为近似估值。带股策略×N组：N=当前持仓可支撑的整百股组数（持仓不足或无持仓时按1组 Buy-Write 测算），金额、损益、Delta 均随 N 放大，ROI 不变。</div></div>"""

    charts_data = {}
    detail = ""
    for i, r in enumerate(top):
        st, m = r["st"], r["m"]
        legs_rows = "".join(
            f"<tr><td>{'买入' if l.side>0 else '卖出'}{'（近月·近似）' if l.approx else ''}</td>"
            f"<td>{'Call' if l.kind=='call' else 'Put'}</td><td>${l.strike:g}</td>"
            f"<td>{l.expiry}</td><td>${l.premium:,.2f}</td><td>{l.oi}</td></tr>"
            for l in st.legs)
        if st.stock_qty:
            _g = f"×{st.groups}组 · " if st.groups > 1 else ""
            _side = "按现价计" if st.stock_qty > 0 else "空头"
            stock_note = f"<div class='note'>组合含{_g}{'+' if st.stock_qty>0 else '-'}{abs(st.stock_qty)*st.groups}股（{_side}）</div>"
        else:
            stock_note = ""
        approx_note = ("<div class='note'>⚠ 本策略含近月腿：损益曲线按近月到期时点测算——"
                       "近月腿按内在价值、远月腿按BS模型保留剩余时间价值（隐含波动率取自CBOE）。</div>") if st.approx else ""
        mp = (f"≥${m['max_profit']:,.0f}（上行理论无限）" if m["up_unlimited"] else f"${m['max_profit']:,.0f}")
        ml = (f"${m['max_loss']:,.0f}（随价格继续扩大）" if m["down_unlimited"] else f"${m['max_loss']:,.0f}")
        pt = m["profit_at_target"]
        pt_cls = "pos" if (pt or 0) > 0 else ("neg" if (pt or 0) < 0 else "")
        roi = m["roi_at_target"] or 0
        roi_cls = "pos" if roi > 0 else ("neg" if roi < 0 else "")
        be_s = " / ".join(f"${b:g}" for b in m["breakevens"]) or "-"
        detail += f"""
<div class="sec">
  <div class="strat-head"><h2 style="margin:0;border:none;padding:0">{'★ ' if r['matched'] else ''}{st.name} <span class="tag {cls_for_bias(st.bias)}">{st.bias}</span></h2>
  <span class="sub">{st.name_en} · {st.shares_state} · 风险 {st.risk_level} 级</span></div>
  <div class="note" style="margin-top:2px">{st.desc}</div>
  <table class="legs"><thead><tr><th>方向</th><th>类型</th><th>行权价</th><th>到期日</th><th>权利金(中间价)</th><th>未平仓OI</th></tr></thead><tbody>{legs_rows}</tbody></table>
  {stock_note}
  <div style="margin-top:6px">
    <span class="metric"><span class="k">净现金流</span><br><span class="v">{fmt_money(-st.net_premium, True)}</span></span>
    <span class="metric"><span class="k">资金占用</span><br><span class="v">${st.capital:,.0f}</span></span>
    <span class="metric"><span class="k">盈亏平衡</span><br><span class="v">{be_s}</span></span>
    <span class="metric"><span class="k">最大盈利</span><br><span class="v">{mp}</span></span>
    <span class="metric"><span class="k">最大亏损</span><br><span class="v">{ml}</span></span>
    <span class="metric"><span class="k">目标价 ${target:,.2f} 收益</span><br><span class="v {pt_cls}">{fmt_money(pt, True)}</span></span>
    <span class="metric"><span class="k">目标价 ROI</span><br><span class="v {roi_cls}">{roi*100:+.1f}%</span></span>
    <span class="metric"><span class="k">净Delta</span><br><span class="v">{st.greeks.get('net_delta',0):+.0f}</span></span>
    <span class="metric"><span class="k">净Theta/日</span><br><span class="v">{st.greeks.get('net_theta',0):+.0f}</span></span>
  </div>
  {approx_note}
  <div id="c{i}" class="chart"></div>
</div>"""
        xs, ys = m["grid"]
        step = max(1, len(xs) // 121)
        gx, gprofit, gloss = [], [], []
        for x, y in zip(xs[::step], ys[::step]):
            gx.append(f"{x:.2f}".rstrip("0").rstrip(".") if x < 100 else f"{x:.0f}")
            gprofit.append(round(y) if y > 0 else None)
            gloss.append(round(y) if y <= 0 else None)
        charts_data[f"c{i}"] = {"x": gx, "profit": gprofit, "loss": gloss,
                                "spot": round(spot, 2), "target": round(target, 2)}

    # ---------------- IV 分析区块(可选)
    charts_data["iv_spot"] = round(spot, 2)
    charts_data["iv_target"] = round(target, 2)

    if iv_analysis and iv_analysis.get("skews"):
        # 按到期日聚合, 构造每条曲线
        from collections import OrderedDict
        per_exp_calls = OrderedDict()
        per_exp_puts = OrderedDict()
        for sk in iv_analysis["skews"]:
            exp = sk["expiry"]
            tgt = per_exp_calls if sk["kind"] == "call" else per_exp_puts
            tgt.setdefault(exp, {"dte": sk["dte"], "pts": sk["points"]})

        def to_xy(points):
            xs, ys = [], []
            for p in points:
                if 0.85 <= p["mny"] <= 1.15 and p["iv"] > 0:
                    xs.append(round(p["mny"], 3))
                    ys.append(round(p["iv"] * 100, 1))  # 百分比
            return xs, ys

        for k, d_ in per_exp_calls.items():
            xs, ys = to_xy(d_["pts"])
            charts_data[f"iv_call_{k}"] = {"x": xs, "y": ys,
                                          "label": f"Call {k} (DTE {d_['dte']})"}
        for k, d_ in per_exp_puts.items():
            xs, ys = to_xy(d_["pts"])
            charts_data[f"iv_put_{k}"] = {"x": xs, "y": ys,
                                          "label": f"Put {k} (DTE {d_['dte']})"}

        # 期限结构
        if iv_analysis.get("term"):
            charts_data["iv_term"] = {
                "dte": [t["dte"] for t in iv_analysis["term"]],
                "iv": [round(t["iv_atm"] * 100, 1) for t in iv_analysis["term"]],
                "iv30": round(iv_analysis["iv30_pct"], 1) if iv_analysis.get("iv30_pct") else None
            }

        sk25 = iv_analysis.get("skew_25_pct")
        iv30_pct = iv_analysis.get("iv30_pct")
        iv_section = f"""
<div class="sec">
  <h2>📊 隐含波动率分析</h2>
  <div class="cards" style="margin-top:4px">
    <div class="card"><div class="k">30日 IV (CBOE)</div><div class="v">{iv30_pct if iv30_pct else '-'}%</div><div class="n">BS模型校准参考</div></div>
    <div class="card" style="border-top-color:#c62f2f"><div class="k">25-Delta Skew</div><div class="v">{('+' if (sk25 or 0)>=0 else '')}{sk25 if sk25 is not None else '-'}%</div><div class="n">25-Δ Put IV − 25-Δ Call IV（正=避险需求大，偏看跌）</div></div>
    <div class="card" style="border-top-color:#0d7a43"><div class="k">到期数</div><div class="v">{len(per_exp_calls)}</div><div class="n">已分析 {len(per_exp_calls)} 个到期日</div></div>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px">
    <div><div class="note" style="margin-top:0">📈 Call IV Skew（按 Moneyness = K/spot）</div><div id="iv_call_chart" class="chart"></div></div>
    <div><div class="note" style="margin-top:0">📉 Put IV Skew（左=虚值put）</div><div id="iv_put_chart" class="chart"></div></div>
  </div>
  <div><div class="note">⏱ Term Structure（ATM IV vs DTE）</div><div id="iv_term_chart" class="chart" style="height:260px"></div></div>
  <div class="note">读法: Skew 向左高=深度虚值put贵(避险溢价大), 向右高=深度虚值call贵(疯牛溢价); Term 向下=contango(常态), 向上=backwardation(短期恐慌). 数据按OI≥1过滤，剔除冷门合约噪声。</div>
</div>"""
        html = (HTML_HEAD.replace("__TITLE__", f"{symbol} 中长期期权策略推荐")
                + f"<h1>{symbol} · 中长期期权策略推荐</h1>"
                + f"<div class='sub'>生成时间 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据源 CBOE 延迟行情 · 主到期日 {far_exp}（DTE {far_dte}天）</div>"
                + cards + table + detail + iv_section + HTML_FOOT)
    else:
        html = (HTML_HEAD.replace("__TITLE__", f"{symbol} 中长期期权策略推荐")
                + f"<h1>{symbol} · 中长期期权策略推荐</h1>"
                + f"<div class='sub'>生成时间 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据源 CBOE 延迟行情 · 主到期日 {far_exp}（DTE {far_dte}天）</div>"
                + cards + table + detail + HTML_FOOT)

    html = html.replace("__CHARTS__", json.dumps(charts_data, ensure_ascii=False))
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
    return html


def build_full_report(rows, view, symbol, spot, position, target, far_exp, far_dte,
                      near_exp, data_time, iv30=None, iv_analysis=None,
                      chart_top=8, out_path=None, insight_lines=None):
    """导出用完整报告(HTML): 含全部策略 + 损益曲线 + IV 面分析 + AI 解读"""
    html = build_html(rows, view, symbol, spot, position, target, far_exp, far_dte,
                     near_exp, data_time, chart_top=chart_top, out_path=out_path,
                     iv30=iv30, iv_analysis=iv_analysis, insight_lines=insight_lines)
    return html


def build_csv_report(rows, view, symbol, spot, target, far_exp, far_dte,
                     insight_lines=None):
    """生成 CSV 报告: 策略明细 + Greeks, 供 Excel/Numbers 打开"""
    import csv
    import io
    buf = io.StringIO()
    # 前置元信息作为注释行
    buf.write(f"# {symbol} 美股期权策略推荐 导出\n")
    buf.write(f"# 生成时间: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · 数据源: CBOE 延迟行情\n")
    buf.write(f"# 现价: ${spot:.2f}  目标价: ${target:.2f} ({(target/spot-1)*100:+.1f}%)  观点: {view}\n")
    buf.write(f"# 主到期日: {far_exp} (DTE {far_dte}天)\n")
    buf.write(f"# 合计策略数: {len(rows)}\n")
    if insight_lines:
        for ln in insight_lines:
            if str(ln).strip():
                buf.write(f"# AI解读: {ln}\n")
    buf.write(f"# 金额口径: 带股策略按持仓可支撑组数×N(1组=100股+1手期权), 其余策略按1组; ROI 不受组数影响\n")
    buf.write("\n")
    # 策略汇总表
    w = csv.writer(buf)
    w.writerow(["序号", "策略", "观点", "是否匹配", "风险", "适用性",
                "现金流($)", "资金占用($)", "净Delta", "净Theta/日",
                "盈亏平衡", "最大盈利", "最大亏损",
                "目标价收益($)", "ROI", "腿明细"])
    for i, r in enumerate(rows, 1):
        st, m = r["st"], r["m"]
        be = " / ".join(f"${b:.2f}" for b in m["breakevens"]) or "-"
        legs_s = "; ".join(
            f"{'买入' if l.side>0 else '卖出'}{l.kind} ${l.strike:.0f}@{l.premium:.2f}"
            f"({l.expiry},DTE{l.dte},OI{l.oi})"
            for l in st.legs)
        if st.stock_qty:
            legs_s += f"; {'正股' if st.stock_qty>0 else '空头'} {abs(st.stock_qty)*st.groups}股"
            if st.groups > 1:
                legs_s += f"(×{st.groups}组)"
        w.writerow([
            i, st.name, st.bias, "是" if r["matched"] else "", st.risk_level, st.shares_state,
            round(-st.net_premium, 2), round(st.capital, 2),
            st.greeks.get("net_delta", 0), st.greeks.get("net_theta", 0),
            be,
            f"≥{m['max_profit']:.0f}" if m["up_unlimited"] else f"{m['max_profit']:.0f}",
            f"≤{m['max_loss']:.0f}" if m["down_unlimited"] else f"{m['max_loss']:.0f}",
            round(m["profit_at_target"] or 0, 2),
            round((m["roi_at_target"] or 0) * 100, 2),
            legs_s
        ])
    return buf.getvalue()


# ---------------------------------------------------------------- 交互输入
def parse_target_input(raw: str, spot: float) -> Optional[float]:
    """解析目标价输入: 支持绝对价格(350)、百分比(+10% / -5%)、空值返回None"""
    s = raw.strip().replace(" ", "").replace("％", "%")
    if not s:
        return None
    if s.endswith("%"):
        pct = s[:-1]
        try:
            if pct.startswith(("+", "-")):
                return round(spot * (1 + float(pct) / 100.0), 2)
            return round(spot * float(pct) / 100.0, 2)  # "10%" 也按涨幅理解
        except ValueError:
            return None
    try:
        v = float(s.replace(",", "").replace("$", ""))
        return round(v, 2) if v > 0 else None
    except ValueError:
        return None


def ask_int(prompt: str, default: int) -> int:
    while True:
        raw = input(prompt).strip()
        if not raw:
            return default
        try:
            return int(raw.replace(",", ""))
        except ValueError:
            print("  请输入整数（负数=空头持仓）。")


def ask_target(spot: float) -> float:
    print(f"\n请输入预计到期目标价（现价 ${spot:,.2f}）:")
    print("  - 绝对价格: 如 350")
    print("  - 涨跌幅:   如 +10% 或 -5%")
    print("  - 直接回车 = 维持现价不变")
    while True:
        raw = input("目标价: ")
        if not raw.strip():  # 回车 = 现价
            print(f"  -> 目标价 ${spot:,.2f}（维持现价）")
            return spot
        v = parse_target_input(raw, spot)
        if v is not None and v > 0:
            pct = (v / spot - 1) * 100
            print(f"  -> 目标价 ${v:,.2f}（较现价 {pct:+.1f}%）")
            return v
        print("  输入无效，请重试（示例: 350 / +10% / -5% / 回车）。")


def run_analysis(args) -> None:
    """执行一次完整分析（供交互模式循环调用）"""
    symbol = args.ticker.upper()
    print(f"\n[1/4] 获取 {symbol} 期权链 (CBOE) ...")
    raw = getattr(args, "_prefetched", None)
    if raw is None:
        if args.data_file:
            with open(args.data_file, encoding="utf-8") as f:
                raw = json.load(f)
        else:
            raw = fetch_cboe(symbol)
    d = raw.get("data", {})
    opts = d.get("options") or []
    if not opts:
        print(f"错误: {symbol} 无可用期权数据(CBOE)。")
        return
    spot = d.get("current_price") or d.get("last_trade_price") or d.get("close")
    if not spot:
        print("错误: 无法获取标的现价。")
        return
    spot = round(float(spot), 2)
    data_time = d.get("last_trade_time") or d.get("timestamp") or "N/A"
    print(f"    现价 ${spot:,.2f}  合约数 {len(opts)}  数据时点 {data_time}")

    target = args.target_price if args.target_price else ask_target(spot)

    today = dt.date.today()
    chain = Chain(opts, today)
    far_exp = chain.pick_expiry(args.dte, args.min_dte)
    near_exp = chain.pick_expiry(args.near_dte, 7)
    if far_exp is None:
        print("错误: 无可用到期日。")
        return
    far_dte = chain.dte(far_exp)
    print(f"[2/4] 主到期日 {far_exp}（DTE {far_dte}天）, 近月腿 {near_exp}（DTE {chain.dte(near_exp)}天）")

    print(f"[3/4] 生成策略 (目标价 ${target:,.2f}) ...")
    strats = build_strategies(chain, spot, far_exp, near_exp, args.position)
    rows, view = rank_strategies(strats, spot, target)

    print_console(rows, view, spot, target, symbol, args.position, far_exp, far_dte, near_exp)

    out_path = args.out or f"reports/{symbol}_{today.isoformat()}.html"
    print(f"[4/4] 生成 HTML 报告 ...")
    build_html(rows, view, symbol, spot, args.position, target, far_exp, far_dte,
               near_exp, data_time, chart_top=args.chart_top, out_path=out_path)
    print(f"完成: {out_path}")


def interactive(args) -> None:
    """交互式向导: 逐项询问股票代码/持仓/目标价, 支持连续分析多只股票"""
    print("=" * 56)
    print("美股中长期期权策略推荐器 - 交互模式")
    print("数据源: CBOE 延迟行情(约15分钟) | 仅供参考, 不构成投资建议")
    print("=" * 56)
    while True:
        while True:
            raw = input("\n请输入股票代码 (如 AAPL, 输入 q 退出): ").strip().upper()
            if raw.lower() == "q":
                return
            if raw.isalpha() and 1 <= len(raw) <= 5:
                break
            print("  代码格式无效，请输入 1-5 个字母，如 AAPL / NVDA / TSLA。")
        args.ticker = raw
        print("\n请输入当前持仓股数（0=无持仓, 100=持有1手, 负数=空头, 回车=0）:")
        args.position = ask_int("持仓股数: ", 0)
        args.target_price = None  # 强制在拉到现价后询问
        try:
            run_analysis(args)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"网络错误: {e}")
        again = input("\n继续分析下一只股票? (y/n, 回车=y): ").strip().lower()
        if again in ("n", "no", "不", "否"):
            return


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="美股中长期期权策略推荐器 (无参数运行=交互模式)")
    ap.add_argument("--ticker", default=None, help="股票代码, 如 AAPL (省略则进入交互模式)")
    ap.add_argument("--position", type=int, default=0, help="当前持仓股数(可为0或负=空头)")
    ap.add_argument("--target-price", default=None, help="目标价: 绝对价如350, 或涨跌幅如+10%%/-5%%, 省略=现价")
    ap.add_argument("--dte", type=int, default=180, help="主到期日目标天数(默认180)")
    ap.add_argument("--min-dte", type=int, default=90, help="中长期最短天数过滤(默认90)")
    ap.add_argument("--near-dte", type=int, default=45, help="日历/对角策略近月腿天数(默认45)")
    ap.add_argument("--chart-top", type=int, default=6, help="HTML中展示收益曲线的策略数")
    ap.add_argument("--data-file", default=None, help="调试: 本地CBOE JSON文件")
    ap.add_argument("--out", default=None, help="HTML报告输出路径")
    args = ap.parse_args()

    if args.ticker is None:
        interactive(args)
        return

    # 命令行模式: 支持目标价写法 +10% / -5%
    if isinstance(args.target_price, str):
        # 需要现价才能换算百分比 —— 先拉数据
        symbol = args.ticker.upper()
        raw = json.load(open(args.data_file, encoding="utf-8")) if args.data_file else fetch_cboe(symbol)
        dd = raw.get("data", {})
        spot0 = float(dd.get("current_price") or dd.get("last_trade_price") or dd.get("close") or 0)
        if spot0 <= 0:
            print("错误: 无法获取标的现价, 百分比目标价无法换算。")
            sys.exit(1)
        args.target_price = parse_target_input(args.target_price, spot0)
        if args.target_price is None:
            print("错误: 目标价格式无效 (示例: 350 / +10% / -5%)。")
            sys.exit(1)
        args._prefetched = raw  # 复用已拉取的数据

    run_analysis(args)


if __name__ == "__main__":
    main()
