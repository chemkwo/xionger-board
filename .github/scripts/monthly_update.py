# -*- coding: utf-8 -*-
"""
GitHub Actions 月度更新：不依赖任何本机、不依赖腾讯文档。

流程：
  判断目标交易日（北京时区）→ 抓腾讯财经当日收盘 → 量级 / 单期涨跌幅校验
  → 追加或覆盖历史最后一行 → 全序列重算
  → 写 history.json（只存原始输入）+ data.json（看板用的最近 10 期）

设计原则：**cron 每天叫我一次，该不该跑由脚本自己判断。**
  早期版本用 cron "28-31 号" 限定窗口，踩了两个坑：
    1) 2026-02 最后交易日 2/27、2027-02 是 2/26，cron 根本不触发 → 该月永久丢失；
    2) 月末当天 15:30 若腾讯尚未出 K 线，SKIP 退出 0，次日重跑时 today != last
       又 SKIP → 同样永久丢失，2026-2027 两年有 19 个月属于这种「无重试机会」。
  现在改成：目标日一旦过去，只要 history 里还没有这一天，就继续尝试（回补窗口 5 天）。

不改任何文件时退出 0，由 workflow 的 `git diff --staged --quiet` 跳过提交，不产生空 commit。

执行位置：仓库根下的 .github/scripts/ 目录，命令 `python monthly_update.py`
自检命令：`python monthly_update.py --calendar` 打印两年交易日历与月末判定结果。
"""
import calendar
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
CLOSE_HOUR = 15      # 北京 15:00 收盘，之后才算「当日已定盘」
CATCHUP_DAYS = 5     # 目标日过去后，还能重试多少天（覆盖接口延迟 / 临时故障）
SOURCE_LABEL = "GitHub 仓库 history.json（原腾讯文档跟踪表已归档，公式已逐位复现）"

# ---------------------------------------------------------------------------
# A 股休市日（只登记「落在周一至周五」的法定节假日，周末本就休市）
#
# 数据来源（逐条核对官方公告，不是凭记忆填的）：
#   2026 —— 上证公告〔2025〕45号《关于上海证券交易所2026年部分节假日休市安排的通知》
#             https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
#   2027 —— 国办发明电〔2026〕10号《国务院办公厅关于2027年部分节假日安排的通知》
#             https://www.gov.cn/zhengce/content/202611/content_7085101.htm
#             交易所细则通常次年 12 月发布，届时应按上证公告复核一次。
#
# 维护提示：新增年份时，用 `python monthly_update.py --calendar` 自检，
#   它会校验每一条都落在工作日、并列出每月最后交易日。
# 覆盖边界：只收录 2026 年起。2026 年以前的历史行已经入库、不再重算，
#   所以缺表不影响；但若真要补跑 2025 年及更早的某个月，month_end_error()
#   可能因为不知道当年春节而误判——那种情况请先补齐对应年份再补跑。
# ---------------------------------------------------------------------------
HOLIDAYS = {
    # ---- 2026 ----
    "2026-01-01": "元旦", "2026-01-02": "元旦",
    "2026-02-16": "春节", "2026-02-17": "春节", "2026-02-18": "春节",
    "2026-02-19": "春节", "2026-02-20": "春节", "2026-02-23": "春节",
    "2026-04-06": "清明",
    "2026-05-01": "劳动节", "2026-05-04": "劳动节", "2026-05-05": "劳动节",
    "2026-06-19": "端午",
    "2026-09-25": "中秋",
    "2026-10-01": "国庆", "2026-10-02": "国庆", "2026-10-05": "国庆",
    "2026-10-06": "国庆", "2026-10-07": "国庆",
    # ---- 2027 ----
    "2027-01-01": "元旦",
    "2027-02-05": "春节", "2027-02-08": "春节", "2027-02-09": "春节",
    "2027-02-10": "春节", "2027-02-11": "春节", "2027-02-12": "春节",
    "2027-04-05": "清明",
    "2027-05-03": "劳动节", "2027-05-04": "劳动节", "2027-05-05": "劳动节",
    "2027-06-09": "端午",
    "2027-09-15": "中秋",
    "2027-10-01": "国庆", "2027-10-04": "国庆", "2027-10-05": "国庆",
    "2027-10-06": "国庆", "2027-10-07": "国庆",
}

# 临时休市（极端天气、临时公告等），格式同上，默认空
EXTRA_CLOSED = {}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------
def is_trading_day(d):
    """A 股是否开市：排除周末 + 法定节假日 + 临时休市。"""
    if d.weekday() >= 5:
        return False
    key = d.strftime("%Y-%m-%d")
    return key not in HOLIDAYS and key not in EXTRA_CLOSED


def last_trading_day(year, month):
    """当月最后一个交易日。"""
    nxt = datetime.date(year, month, 1) + datetime.timedelta(days=32)
    last = datetime.date(nxt.year, nxt.month, 1) - datetime.timedelta(days=1)
    while not is_trading_day(last):
        last -= datetime.timedelta(days=1)
    return last


def prev_month(year, month):
    """上一个月的 (年, 月)。"""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def month_end_error(d):
    """目标日必须是当月最后交易日；是则返回 None，否则返回错误说明。

    策略只在「每月最后一个交易日收盘」定盘，月中日期写进来会变成一行假的比价。
    这条曾经被人为绕过：腾讯文档里遗留过 2026-11-02（差月末 28 天），
    手动补跑也误填过 2026-11-12，页面上就会显示一个不存在的月度信号。
    """
    last = last_trading_day(d.year, d.month)
    if d != last:
        return "%s 不是 %d 月的最后交易日（%s），差 %d 天；策略只在月末定盘" % (
            d, d.month, last, (last - d).days)
    return None


def resolve_target(rows, now=None):
    """
    返回 (目标交易日, 是否手动指定)；无需执行时返回 (None, False)。

    now 参数只为单元测试注入，生产环境恒为 None（取北京当前时间）。
    """
    override = (os.environ.get("INPUT_DATE") or "").strip()
    if override:
        return datetime.datetime.strptime(override, "%Y-%m-%d").date(), True

    if now is None:
        now = datetime.datetime.now(TZ)
    today = now.date()
    last_have = rows[-1]["month"] if rows else ""

    target = last_trading_day(today.year, today.month)
    if today >= target:
        # 当月目标日已到（或已过）。当天必须等收盘定盘，之后随时可补。
        if today == target and now.hour < CLOSE_HOUR:
            log("SKIP 今天是本月最后交易日 %s，但未到北京 %d:00（现在 %02d:%02d）"
                % (target, CLOSE_HOUR, now.hour, now.minute))
            return None, False
        return target, False

    # 本月还没到月末 → 检查上个月是否漏填（cron 中断、接口延迟、仓库无活动被暂停等）
    p_year, p_month = prev_month(today.year, today.month)
    prev = last_trading_day(p_year, p_month)
    if last_have >= prev.strftime("%Y-%m-%d"):
        log("SKIP 今天 %s 不是本月最后交易日（%s），上月 %s 已入库"
            % (today, target, prev))
        return None, False
    if today.day <= CATCHUP_DAYS:
        log("回补 上月最后交易日 %s 未入库（history 最后一行 %s），今天 %s 在回补窗口内"
            % (prev, last_have or "空", today))
        return prev, False

    log("WARN 上月最后交易日 %s 仍未入库（history 最后一行 %s），已超出 %d 天回补窗口，需人工补跑"
        % (prev, last_have or "空", CATCHUP_DAYS))
    return None, False


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------
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
            raise LookupError("%s(%s) 在 %s 没有收盘数据（非交易日或接口未更新）" % (name, code, want))
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def write_data(data, path):
    """data.json 落盘。顶层缩进便于人工核对，series 单独压成单行。

    series 有 197 行，若跟着 indent 展开会多出十几 KB 纯空白；
    手机每天要拉好几次，没必要为可读性付这个流量。
    """
    series = data.pop("series", None)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if series is not None:
        data["series"] = series
        compact = json.dumps(series, ensure_ascii=False, separators=(",", ":"))
        text = text.rstrip()
        assert text.endswith("}")
        text = text[:-1].rstrip() + ',\n  "series": ' + compact + "\n}"
        text += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def write_outputs(hist, rows, stamp):
    """把 rows 落盘：history.json 存原始输入，data.json 存最近 RECENT 期派生结果。"""
    computed = se.compute(rows)
    cur = computed[-1]
    log("重算完成：比价 %.3f 下轨 %.3f 上轨 %.3f 信号 %s 标的 %s 分位 %d%%" % (
        cur["ratio"], cur["lower"], cur["upper"], cur["signal"], cur["target"], cur["pct"]))

    hist["rows"] = rows
    hist["meta"]["updated"] = stamp
    # newline="\n"：本地（Windows）也写 LF，与云端 Linux runner 输出字节一致。
    # 否则本地一次 CRLF、云端一次 LF，每次跑完 git 都会整文件重写，diff 全红。
    with open(HISTORY, "w", encoding="utf-8", newline="\n") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)

    with open(DATA, "r", encoding="utf-8") as f:
        data = json.load(f)
    recent = computed[-RECENT:]
    data["rows"] = recent
    data["updated"] = recent[-1]["month"]
    data["synced_at"] = datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    data["source"] = SOURCE_LABEL
    # 交易日历一并下发给看板，让「距月末判定」用的休市表只有这一个事实源
    data["holidays"] = sorted(HOLIDAYS)
    # 全序列下发（给看板画 16 年走势与分位通道）。
    # 只带绘制必需的四个数，197 期约 7KB；不要下发 cyb/hl 原文，那会把体积翻倍。
    data["series"] = [[c["month"], c["ratio"], c["lower"], c["upper"]] for c in computed]
    write_data(data, DATA)
    log("已写 history.json（%d 期）与 data.json（最近 %d 期 + 全序列 %d 点）"
        % (len(rows), len(recent), len(data["series"])))
    return cur


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--calendar" in argv:
        return dump_calendar()

    with open(HISTORY, "r", encoding="utf-8") as f:
        hist = json.load(f)
    rows = hist.get("rows") or []
    if not rows:
        log("ERROR history.json 没有历史行")
        return 1

    if "--rebuild" in argv:
        # 不联网、不判日期：只把 history.json 全量重算并刷新 data.json
        # 用途：改了 signal_engine 参数、或手工补了历史行之后同步下游
        log("REBUILD 从 history.json 重算（%d 期，不抓行情）" % len(rows))
        write_outputs(hist, rows, rows[-1]["month"])
        return 0

    today, forced = resolve_target(rows)
    if today is None:
        return 0
    if forced:
        log("手动指定日期：%s" % today)

    # 无论手动还是自动，目标必须是当月最后交易日。手动补跑最容易在这里出错。
    err = month_end_error(today)
    if err:
        log("ERROR " + err)
        log("      请在 Actions 的 date 里填该月最后交易日，或留空让脚本自动判断。")
        return 1

    if not is_trading_day(today):
        log("SKIP %s 不是交易日（周末或休市日）" % today)
        return 0

    try:
        prices = fetch_close(today)
    except LookupError as e:
        # 目标日的收盘价还没出 → 退出 0，明天的 cron 会继续尝试（回补窗口内）
        log("RETRY %s；明天会重试" % e)
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

    write_outputs(hist, rows, stamp)
    return 0


def dump_calendar():
    """自检：校验节假日表并打印 2026-2027 每月最后交易日。"""
    bad = 0
    for key in sorted(set(HOLIDAYS) | set(EXTRA_CLOSED)):
        d = datetime.datetime.strptime(key, "%Y-%m-%d").date()
        if d.weekday() >= 5:
            bad += 1
            print("  !! %s(%s) 落在周末，属冗余登记" % (key, HOLIDAYS.get(key, EXTRA_CLOSED.get(key))))
    print("节假日表：%d 条，冗余 %d 条" % (len(HOLIDAYS) + len(EXTRA_CLOSED), bad))
    print()
    print("年份  月份  最后交易日    星期  说明")
    for year in sorted({int(k[:4]) for k in HOLIDAYS}):
        for month in range(1, 13):
            last = last_trading_day(year, month)
            note = []
            if last.day < 28:
                note.append("早于 28 号")
            adj = datetime.date(year, month, calendar.monthrange(year, month)[1])
            while adj.weekday() >= 5:
                adj -= datetime.timedelta(days=1)
            if adj != last:
                note.append("被假期挪动 %d 天" % (adj - last).days)
            print("%4d  %2d    %s  %s  %s" % (
                year, month, last, "一二三四五六日"[last.weekday()], "、".join(note)))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("ERROR " + str(exc), file=sys.stderr)
        sys.exit(1)
