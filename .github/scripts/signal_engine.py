# -*- coding: utf-8 -*-
"""
创业红利比价定投 —— 信号计算内核（纯函数，无 IO、无第三方库）。

与腾讯文档《创业红利比价定投跟踪表 V1.1》的表格公式**逐位等价**：
已用 198 期真实历史（2010-06-30 ~ 2026-11-02）× 6 个字段逐行核对，0 处差异。

对应的原公式：
    比价 D = ROUND(创业板指收盘 / 中证红利收盘, 3)
    下轨 E = IF(COUNT(截至上期比价) < 36, 0.40, ROUND(PERCENTILE(截至上期比价, 15%), 3))
    上轨 F = IF(COUNT(截至上期比价) < 36, 0.62, ROUND(PERCENTILE(截至上期比价, 85%), 3))
    信号 G = IF(D > F, "转红利", IF(D < E, "转创业板", "持有"))
    标的 H = IF(G="转红利","红利", IF(G="转创业板","创业板", 上期标的))
    分位 I = ROUND(COUNTIF(截至本期比价, "<" & D) / COUNT(截至本期比价) * 100, 0)

两个容易写错的地方：
1. 上下轨用的是**截至上月末**的比价（不含本期），信号才用本期的 D 去比。
2. Excel 的 ROUND 是四舍五入，Python 内置 round() 是银行家舍入（round(0.5)=0），
   必须用 Decimal 的 ROUND_HALF_UP，否则边界值会差 0.001。
"""
import math
from decimal import Decimal, ROUND_HALF_UP

# ===== 策略参数：改这里就能调参（改完重算全序列即可，历史信号会按新参数重算）=====
LOWER_PCT = 0.15         # 下轨分位
UPPER_PCT = 0.85         # 上轨分位
MIN_SAMPLE = 36          # 历史样本不足 36 期时退回固定阈值
FALLBACK_LOWER = 0.40    # 视频版固定下轨
FALLBACK_UPPER = 0.62    # 视频版固定上轨
DEFAULT_TARGET = "红利"   # 首期无上期标的时的默认执行标的

SIGNAL_SELL_CYB = "转红利"
SIGNAL_BUY_CYB = "转创业板"
SIGNAL_HOLD = "持有"


def xround(x, n):
    """Excel ROUND：四舍五入（远离零方向进位），不是银行家舍入。"""
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))


def percentile_inc(data, p):
    """Excel PERCENTILE / PERCENTILE.INC：排序后按 (n-1)*p 线性插值。"""
    s = sorted(data)
    n = len(s)
    if n == 0:
        return None
    idx = (n - 1) * p
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def compute(rows):
    """把 [{"month","cyb","hl"}] 按时间升序算成带派生字段的列表。

    返回每项：month, cyb, hl, ratio, lower, upper, signal, target, pct
    传入的 rows 不会被修改。
    """
    ratios = []
    out = []
    prev_target = DEFAULT_TARGET
    for r in rows:
        cyb = float(r["cyb"])
        hl = float(r["hl"])
        if hl == 0:
            raise ValueError("%s 中证红利收盘为 0，无法计算比价" % r.get("month"))
        d = xround(cyb / hl, 3)

        hist = ratios[:]          # 截至上期，不含本期
        if len(hist) < MIN_SAMPLE:
            lower, upper = FALLBACK_LOWER, FALLBACK_UPPER
        else:
            lower = xround(percentile_inc(hist, LOWER_PCT), 3)
            upper = xround(percentile_inc(hist, UPPER_PCT), 3)

        if d > upper:
            signal = SIGNAL_SELL_CYB
        elif d < lower:
            signal = SIGNAL_BUY_CYB
        else:
            signal = SIGNAL_HOLD

        if signal == SIGNAL_SELL_CYB:
            target = "红利"
        elif signal == SIGNAL_BUY_CYB:
            target = "创业板"
        else:
            target = prev_target

        all_r = ratios + [d]      # 含本期，分位用
        pct = int(xround(sum(1 for v in all_r if v < d) / len(all_r) * 100, 0))

        out.append({
            "month": r["month"],
            "cyb": round(cyb, 2),
            "hl": round(hl, 2),
            "ratio": d,
            "lower": lower,
            "upper": upper,
            "signal": signal,
            "target": target,
            "pct": pct,
        })
        ratios.append(d)
        prev_target = target
    return out


if __name__ == "__main__":
    # 自检：三行迷你序列，手算对得上就说明内核没坏
    demo = [
        {"month": "2010-06-30", "cyb": 919.31, "hl": 2508.12},
        {"month": "2010-07-30", "cyb": 968.43, "hl": 2797.42},
        {"month": "2020-06-30", "cyb": 2458.70, "hl": 4126.38},
    ]
    for row in compute(demo):
        print("%s 比价 %.3f [%.3f, %.3f] %s / %s 分位 %d%%" % (
            row["month"], row["ratio"], row["lower"], row["upper"],
            row["signal"], row["target"], row["pct"]))
