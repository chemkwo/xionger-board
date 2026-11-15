# -*- coding: utf-8 -*-
"""
GitHub Actions 月度更新：不依赖任何本机、不依赖腾讯文档。

流程：
  判断今天是不是本月最后一个工作日（北京时区）→ 抓腾讯财经当日收盘
  → 量级 / 单期涨跌幅校验 → 追加或覆盖历史最后一行 → 全序列重算
  → 写 history.json（只存原始输入）+ data.json（看板用的最近 10 期）

非月末、非交易日、数据未更新时 **不改任何文件** 并退出 0，
由 workflow 里的 `git diff --staged --quiet` 自动跳过提交，不会产生空 commit。

执行位置：仓库根下的 .github/scripts/ 目录，命令 `python monthly_update.py`。
"""
import datetime
import json
import os
import sys
import urllib.request

import signal_engine as se

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
HISTORY = os.path.join(ROOT, "history.json")
DATA = os.path.join(ROOT, "data.json")

TZ = datetime.timezone(datetime.timedelta(hours=8))
SOURCES = (("cyb", "sz399006", "创业板指"), ("hl", "sh000922", "中证红利"))
LIMITS = {"cyb": (800.0, 8000.0), "hl": (1000.0, 20000.0)}
MAX_MOVE = 0.25
RECENT = 10
SOURCE_LABEL = "GitHub 仓库 history.json（原腾讯文档跟踪表已归档，公式已逐位复现）"


def log(msg):
    print(msg, flush=True)


def last_workday_of_month(d):
    nxt = datetime.date(d.year, d.month, 1) + datetime.timedelta(days=32)
    last = datetime.date(nxt.year, nxt.month, 1) - datetime.timedelta(days=1)
    while last.weekday() >= 5:
        last -= datetime.timedelta(days=1)
    return last


def fetch_close(target):
    """抓指定日期两个指数收盘价；日期必须精确匹配。返回结构见腾讯 fqkline 接口。"""
    want = target.strftime("%Y-%m-%d")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    out = {}
    for key, code, name in SOURCES:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               "?param=%s,day,%s,%s,10,qfq" % (code, want, want))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=25) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
        node = ((payload.get("data") or {}).get(code) or {})
        quotes = node.get("day") or node.get("qfqday") or []
        hit = next((q[2] for q in quotes if q and q[0] == want), None)
        if hit is None:
            raise LookupError("%s(%s) 在 %s 没有收盘数据（非交易日或未更新）" % (name, code, want))
        out[key] = round(float(hit), 2)
        out[key + "_name"] = name
    return out


def validate(prices, prev):
    for key in ("cyb", "hl"):
        value, name = prices[key], prices[key + "_name"]
        lo, hi = LIMITS[key]
        if not (lo <= value <= hi):
            raise ValueError("%s %.2f 超出合理区间 [%g, %g]，疑似抓错标的" % (name, value, lo, hi))
        if prev and prev.get(key):
            old = float(prev[key])
            if old:
                move = value / old - 1
                if abs(move) > MAX_MOVE:
                    raise ValueError("%s %.2f 较上期 %.2f 变动 %+.1f%%，超过 %.0f%% 阈值，拒绝写入"
                                     % (name, value, old, move * 100, MAX_MOVE * 100))


def resolve_target():
    """返回 (目标日期, 是否手动指定)。手动指定（INPUT_DATE）时跳过月末与时间检查。"""
    override = (os.environ.get("INPUT_DATE") or "").strip()
    if override:
        return datetime.datetime.strptime(override, "%Y-%m-%d").date(), True
    now = datetime.datetime.now(TZ)
    today = now.date()
    last = last_workday_of_month(today)
    if today != last:
        log("SKIP 今天 %s（北京）不是本月最后一个工作日（%s）" % (today, last))
        return None, False
    if now.hour < 15:
        log("SKIP 未到北京 15:00（现在 %02d:%02d）" % (now.hour, now.minute))
        return None, False
    return today, False


def main():
    today, forced = resolve_target()
    if today is None:
        return 0
    if forced:
        log("手动指定日期：%s（跳过月末与收盘时间检查）" % today)

    with open(HISTORY, "r", encoding="utf-8") as f:
        hist = json.load(f)
    rows = hist.get("rows") or []
    if not rows:
        log("ERROR history.json 没有历史行")
        return 1

    try:
        prices = fetch_close(today)
    except LookupError as e:
        log("SKIP %s" % e)
        return 0

    log("抓取 %s：创业板指 %.2f / 中证红利 %.2f" % (today, prices["cyb"], prices["hl"]))
    validate(prices, rows[-1])

    stamp = today.strftime("%Y-%m-%d")
    entry = {"month": stamp, "cyb": prices["cyb"], "hl": prices["hl"]}
    lastrow = rows[-1]
    if lastrow["month"] == stamp:
        log("SKIP 该日期已是最新一行，无需更新")
        return 0
    if lastrow["month"][:7] == stamp[:7]:
        log("覆盖同月占位行 %s → %s" % (lastrow["month"], stamp))
        rows[-1] = entry
    else:
        log("追加新行 %s" % stamp)
        rows.append(entry)

    computed = se.compute(rows)
    cur = computed[-1]
    log("重算完成：比价 %.3f 下轨 %.3f 上轨 %.3f 信号 %s 标的 %s 分位 %d%%" % (
        cur["ratio"], cur["lower"], cur["upper"], cur["signal"], cur["target"], cur["pct"]))

    hist["rows"] = rows
    hist["meta"]["updated"] = stamp
    with open(HISTORY, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)

    with open(DATA, "r", encoding="utf-8") as f:
        data = json.load(f)
    recent = computed[-RECENT:]
    data["rows"] = recent
    data["updated"] = recent[-1]["month"]
    data["synced_at"] = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    data["source"] = SOURCE_LABEL
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log("已写 history.json（%d 期）与 data.json（最近 %d 期）" % (len(rows), len(recent)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("ERROR " + str(exc), file=sys.stderr)
        sys.exit(1)
