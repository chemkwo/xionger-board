# -*- coding: utf-8 -*-
"""
monthly_update.resolve_target 的离线测试：不联网、不改任何文件。

覆盖场景：
  1. 普通月份，目标日之前 / 当天未收盘 / 当天已收盘 / 之后
  2. 2 月「最后交易日早于 28 号」——旧 cron 窗口会永久丢失的月份
  3. 目标日当天接口没数据 → 次日、第三日自动重试；超过回补窗口报警
  4. 已入库的月份不重复写
  5. 目标日落在周末 / 节假日
运行：python test_resolve_target.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("INPUT_DATE", None)

import monthly_update as mu

TZ = mu.TZ
FAIL = []


def at(y, m, d, hh=16, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=TZ)


def case(name, rows, now, want_date, want_forced=False):
    got_date, got_forced = mu.resolve_target(rows, now=now)
    want = want_date.strftime("%Y-%m-%d") if want_date else None
    got = got_date.strftime("%Y-%m-%d") if got_date else None
    ok = (got == want) and (got_forced == want_forced)
    print("%s %-52s now=%s  期望=%-10s 实际=%s" % (
        "PASS" if ok else "FAIL", name, now.strftime("%Y-%m-%d %H:%M"), want or "None", got or "None"))
    if not ok:
        FAIL.append(name)


def hist(last):
    """构造一个只有最后一行已知的 rows。"""
    return [{"month": "2010-06-30", "cyb": 919.31, "hl": 2508.12},
            {"month": last, "cyb": 2458.70, "hl": 5587.66}]


H_1102 = hist("2026-11-02")

print("=== 场景 1：普通月份（2026-11，最后交易日 11-30） ===")
case("11-28 未到月末", H_1102, at(2026, 11, 28), None)
case("11-29 未到月末", H_1102, at(2026, 11, 29), None)
case("11-30 但未收盘（14:00）", H_1102, at(2026, 11, 30, 14, 0), None)
case("11-30 已收盘（15:30）", H_1102, at(2026, 11, 30, 15, 30), datetime.date(2026, 11, 30))

print()
print("=== 场景 2：2 月最后交易日早于 28 号（旧 cron 永久丢失） ===")
case("2026-02-27 周五 16:00", hist("2026-01-30"), at(2026, 2, 27), datetime.date(2026, 2, 27))
case("2027-02-26 周五 16:00", hist("2027-01-29"), at(2027, 2, 26), datetime.date(2027, 2, 26))
case("2026-02-28 周六（已过目标日，仍补）", hist("2026-01-30"), at(2026, 2, 28), datetime.date(2026, 2, 27))

print()
print("=== 场景 3：目标日当天接口没数据 → 后续自动重试 ===")
case("11-30 当天（数据缺，重试里也一样目标）", H_1102, at(2026, 11, 30, 15, 30), datetime.date(2026, 11, 30))
case("12-01 月初回补", H_1102, at(2026, 12, 1, 16, 0), datetime.date(2026, 11, 30))
case("12-03 仍在回补窗口内", H_1102, at(2026, 12, 3, 16, 0), datetime.date(2026, 11, 30))
case("12-05 窗口最后一天", H_1102, at(2026, 12, 5, 16, 0), datetime.date(2026, 11, 30))
case("12-06 超窗，放弃并报警", H_1102, at(2026, 12, 6, 16, 0), None)

print()
print("=== 场景 4：已入库不重复写 ===")
case("11-30 已入库，当天再跑", hist("2026-11-30"), at(2026, 11, 30, 16, 0),
     datetime.date(2026, 11, 30))  # 目标仍返回，main 里靠「已是最新一行」跳过
case("12-01 已入库，不回补", hist("2026-11-30"), at(2026, 12, 1, 16, 0), None)
case("12-10 已入库，正常 SKIP", hist("2026-11-30"), at(2026, 12, 10, 16, 0), None)
case("11-05 已入库（月中占位），不回补 10 月", hist("2026-11-02"), at(2026, 11, 5, 16, 0), None)

print()
print("=== 场景 5：目标日撞周末 / 节假日 ===")
case("2026-09-30 周三（10/1 起国庆）", hist("2026-08-31"), at(2026, 9, 30, 16, 0), datetime.date(2026, 9, 30))
case("2026-10-01 国庆当天，目标已是 10-30", hist("2026-09-30"), at(2026, 10, 1, 16, 0), None)
case("2026-10-30 周五（10-31 周六）", hist("2026-09-30"), at(2026, 10, 30, 16, 0), datetime.date(2026, 10, 30))
case("2027-10-01 国庆当天，目标 10-29", hist("2027-09-30"), at(2027, 10, 1, 16, 0), None)

print()
print("=== 场景 6：手动指定日期（INPUT_DATE）跳过所有检查 ===")
os.environ["INPUT_DATE"] = "2026-11-13"
case("INPUT_DATE=2026-11-13", H_1102, at(2026, 11, 5, 10, 0), datetime.date(2026, 11, 13), True)
os.environ.pop("INPUT_DATE")

print()
print("=== 场景 7：跨年（12 月末 → 1 月初回补） ===")
case("2027-01-02 回补 2026-12-31", hist("2026-11-30"), at(2027, 1, 2, 16, 0), datetime.date(2026, 12, 31))
case("2027-01-04 回补（含元旦，day<=5）", hist("2026-11-30"), at(2027, 1, 4, 16, 0), datetime.date(2026, 12, 31))

print()
if FAIL:
    print("FAILED %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
