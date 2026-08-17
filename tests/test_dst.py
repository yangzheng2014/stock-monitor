"""美股 DST 时区换算单元测试（stdlib unittest，无第三方依赖）。

加载 main_v0.2.4-us.py 为模块 "app"。核心验证：美东时间换算在
夏令时切换日（3 月第二个周日 / 11 月第一个周日）边界正确，
并验证 us_trade_status 据此返回正确的交易阶段。

切换瞬间说明（2026 年）：
- 春令时：3 月第二个周日 02:00 EST 拨快 1 小时 → 03:00 EDT（07:00 UTC 为切换点）
- 冬令时：11 月第一个周日 01:00 EDT 拨慢 1 小时 → 01:00 EST（06:00 UTC 为切换点）
旧实现按 UTC 日期朴素判 DST，会在切换日 07:00/06:00 UTC 之前的时段给出错误偏移。
"""
import importlib.util
import os
import sys
import unittest
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("app", os.path.join(BASE, "main_v0.2.4-us.py"))
app = importlib.util.module_from_spec(_spec)
sys.modules["app"] = app
_spec.loader.exec_module(app)

UTC = timezone.utc


def _utc(y, mo, d, h, mi=0):
    """构造 naive UTC 时刻（与现网 us_trade_status 兼容的输入形态）。"""
    return datetime(y, mo, d, h, mi)


class TestUsEtTime(unittest.TestCase):
    """us_et_time 美东时间换算：DST 切换日边界 + 正常日"""

    def assertEt(self, utc, exp_year, exp_mon, exp_day, exp_hour, exp_min, exp_tz):
        et = app.us_et_time(utc)
        self.assertEqual(et.year, exp_year)
        self.assertEqual(et.month, exp_mon)
        self.assertEqual(et.day, exp_day)
        self.assertEqual(et.hour, exp_hour)
        self.assertEqual(et.minute, exp_min)
        self.assertEqual(et.tzinfo, exp_tz)

    def test_spring_forward_before(self):
        # 2026-03-08 06:00 UTC = 01:00 EST（切换前，仍 -5）
        # 旧实现按 UTC 日期判为 DST，会错算成 02:00 → 此用例为 RED 关键
        self.assertEt(_utc(2026, 3, 8, 6), 2026, 3, 8, 1, 0, app.EST)

    def test_spring_forward_after(self):
        # 2026-03-08 08:00 UTC = 04:00 EDT（切换后，-4）
        self.assertEt(_utc(2026, 3, 8, 8), 2026, 3, 8, 4, 0, app.EDT)

    def test_fall_back_before(self):
        # 2026-11-01 05:00 UTC = 01:00 EDT（切换前，仍 -4）
        # 旧实现按 UTC 日期判为 EST，会错算成 00:00 → 此用例为 RED 关键
        self.assertEt(_utc(2026, 11, 1, 5), 2026, 11, 1, 1, 0, app.EDT)

    def test_fall_back_after(self):
        # 2026-11-01 07:00 UTC = 02:00 EST（切换后，-5）
        self.assertEt(_utc(2026, 11, 1, 7), 2026, 11, 1, 2, 0, app.EST)

    def test_normal_summer_edt(self):
        # 2026-06-15 12:00 UTC = 08:00 EDT
        self.assertEt(_utc(2026, 6, 15, 12), 2026, 6, 15, 8, 0, app.EDT)

    def test_normal_winter_est(self):
        # 2026-01-15 12:00 UTC = 07:00 EST
        self.assertEt(_utc(2026, 1, 15, 12), 2026, 1, 15, 7, 0, app.EST)

    def test_aware_input_passthrough(self):
        # aware datetime 输入：utcnow 直接传入也应正确换算
        now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
        et = app.us_et_time(now)
        self.assertEqual(et.hour, 8)
        self.assertEqual(et.tzinfo, app.EDT)


class TestUsTradeStatus(unittest.TestCase):
    """us_trade_status 按美东时间返回交易阶段"""

    def test_spring_forward_weekend(self):
        # 2026-03-08 是周日，无论 DST 与否都是休市日
        self.assertEqual(app.us_trade_status(_utc(2026, 3, 8, 12)), "休市日")

    def test_fall_back_weekend(self):
        # 2026-11-01 是周日
        self.assertEqual(app.us_trade_status(_utc(2026, 11, 1, 5)), "休市日")

    def test_normal_summer_premarket(self):
        # 2026-06-15(周一) 12:00 UTC = 08:00 EDT → 盘前
        self.assertEqual(app.us_trade_status(_utc(2026, 6, 15, 12)), "盘前")

    def test_normal_summer_open(self):
        # 2026-06-15(周一) 14:30 UTC = 10:30 EDT → 交易中
        self.assertEqual(app.us_trade_status(_utc(2026, 6, 15, 14, 30)), "交易中")

    def test_normal_winter_premarket(self):
        # 2026-01-15(周四) 12:00 UTC = 07:00 EST → 盘前
        self.assertEqual(app.us_trade_status(_utc(2026, 1, 15, 12)), "盘前")

    def test_normal_winter_open(self):
        # 2026-01-15(周四) 15:00 UTC = 10:00 EST → 交易中
        self.assertEqual(app.us_trade_status(_utc(2026, 1, 15, 15)), "交易中")


if __name__ == "__main__":
    unittest.main(verbosity=2)
