# -*- coding: utf-8 -*-
"""
日 / 周比价走势数据：trend.json

只从腾讯财经拉两个指数的日线与周线，算比价（创业板指 ÷ 中证红利），
**不碰 history.json / data.json 的月度录入行** —— 月末数据仍由
monthly_update.py 独占写入，这里只加一条并行的走势数据链。

为什么单独一个文件：
  周线 844 点、日线 1250 点，比月度 197 期大一个量级。塞进 data.json 会让
  看板每次轮询（公网手机端 1h 一次）都多拉几十 KB，而这两张图只有第 3 页用得上。

数据格式（紧凑，省流量）：
  weekly / daily 都是 [[YYYYMMDD, 比价 ×1000 取整], ...]
  比价 0.432 → 432，前端除 1000。三位小数与月度口径一致。

接口限制（实测，2026-11）：
  fqkline 单次最多返回 2000 根，count ≥ 3000 直接返回空数组；
  且 start/end 参数会被忽略，永远返回「最近 N 根」。
  周线 16 年只要 844 根，一次拉完；日线 2000 根约 8 年，再截取最近 DAY_KEEP 根。

用法：
  python trend_update.py              # 生成 / 更新 trend.json
  python trend_update.py --dry-run    # 只打印不写盘
"""
import datetime
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(ROOT, "trend.json")

BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SOURCES = (("cyb", "sz399006", "创业板指"), ("hl", "sh000922", "中证红利"))

WEEK_BARS = 2000   # 接口上限 2000；周线全历史只有 844 根
DAY_BARS = 2000    # 接口上限，约 8 年
DAY_KEEP = 1250    # 日线只留最近 1250 根（约 5 年），图上再长就看不清了

# 比价合理区间：低于 0.05 或高于 50 基本可以断定两个标的抓错了
RATIO_RANGE = (0.05, 50.0)


def log(msg):
    print(msg, flush=True)


def fetch(code, period, count):
    """拉一组 K 线，返回 [(date, close)]；code 形如 sz399006。"""
    url = "%s?param=%s,%s,,,%d,qfq" % (BASE, code, period, count)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with opener.open(req, timeout=40) as r:
        payload = json.loads(r.read().decode("utf-8", "replace"))
    node = (payload.get("data") or {}).get(code) or {}
    quotes = node.get(period) or node.get("qfq" + period) or []
    out = []
    for q in quotes:
        if not q or len(q) < 3:
            continue
        try:
            d = datetime.datetime.strptime(str(q[0]), "%Y-%m-%d").date()
            c = float(q[2])
        except (ValueError, TypeError):
            continue
        if c > 0:
            out.append((d, c))
    if not out:
        raise LookupError("%s 的 %s 数据为空（接口异常或标的代码变更）" % (code, period))
    out.sort(key=lambda x: x[0])
    return out


def merge(a, b):
    """按日期取交集求比价，返回 [[ymd:int, ratio*1000:int]]。

    只有两边都有收盘价的日期才算一个点 —— 停牌、指数编制日不同都会造成缺口，
    这种时候宁缺一个点，也不要拿前值硬填，那会在图上伪造出一段横线。
    """
    mb = dict(b)
    pts = []
    for d, c in a:
        other = mb.get(d)
        if not other:
            continue
        r = c / other
        if not (RATIO_RANGE[0] <= r <= RATIO_RANGE[1]):
            raise ValueError("%s 比价 %.3f 超出合理区间 %s，疑似抓错标的"
                             % (d, r, RATIO_RANGE))
        pts.append([int(d.strftime("%Y%m%d")), int(round(r * 1000))])
    pts.sort(key=lambda x: x[0])
    return pts


def dump(path, obj):
    """weekly / daily 压成单行：1250 个点若跟着 indent 展开会多出十几 KB 空白。"""
    parts = []
    for k in ("updated", "note", "source", "weekly", "daily"):
        if k not in obj:
            continue
        v = obj[k]
        if isinstance(v, list):
            parts.append('  "%s": %s' % (k, json.dumps(v, ensure_ascii=False,
                                                       separators=(",", ":"))))
        else:
            parts.append('  "%s": %s' % (k, json.dumps(v, ensure_ascii=False)))
    text = "{\n" + ",\n".join(parts) + "\n}\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def fmt(pts):
    return "%s → %s（%d 点）" % (pts[0][0], pts[-1][0], len(pts))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    wk = {}
    for key, code, name in SOURCES:
        bars = fetch(code, "week", WEEK_BARS)
        wk[key] = bars
        log("周线 %-8s %s → %s  %d 根" % (name, bars[0][0], bars[-1][0], len(bars)))
    weekly = merge(wk["cyb"], wk["hl"])

    dy = {}
    for key, code, name in SOURCES:
        bars = fetch(code, "day", DAY_BARS)
        dy[key] = bars
        log("日线 %-8s %s → %s  %d 根" % (name, bars[0][0], bars[-1][0], len(bars)))
    daily = merge(dy["cyb"], dy["hl"])[-DAY_KEEP:]

    # 点数下限：少于这些基本是接口给了残缺数据，宁可不写也不要用半截数据覆盖
    if len(weekly) < 700:
        log("ERROR 周线只有 %d 点，少于 700，拒绝写入" % len(weekly))
        return 1
    if len(daily) < 400:
        log("ERROR 日线只有 %d 点，少于 400，拒绝写入" % len(daily))
        return 1

    log("周线比价 %s　最新 %.3f" % (fmt(weekly), weekly[-1][1] / 1000.0))
    log("日线比价 %s　最新 %.3f" % (fmt(daily), daily[-1][1] / 1000.0))

    data = {
        "updated": str(daily[-1][0]),
        "note": "比价 = 创业板指 ÷ 中证红利，×1000 取整；weekly 全历史，daily 近 5 年",
        "source": "腾讯财经 前复权 K 线",
        "weekly": weekly,
        "daily": daily,
    }

    if "--dry-run" in argv:
        log("DRY-RUN 不写盘")
        return 0

    dump(OUT, data)
    size = os.path.getsize(OUT)
    log("已写 trend.json（%d 字节）" % size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
