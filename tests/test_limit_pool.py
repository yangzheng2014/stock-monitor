"""v0.2.3 涨停/跌停池逻辑单元测试（stdlib unittest，无第三方依赖）"""
import importlib.util
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("app", os.path.join(BASE, "main_v0.2.3.py"))
app = importlib.util.module_from_spec(_spec)
sys.modules["app"] = app
_spec.loader.exec_module(app)


class TestParsePoolItem(unittest.TestCase):
    """涨停/跌停池单条数据解析"""

    def test_full_item(self):
        raw = {"c": "000936", "m": 0, "n": "华西股份", "p": 6970,
               "zdp": 9.936908721923829, "lbc": 3, "fbt": 92500}
        item = app.parse_pool_item(raw)
        self.assertEqual(item["code"], "000936")
        self.assertEqual(item["name"], "华西股份")
        self.assertEqual(item["price"], 6.97)      # p 是价格×1000
        self.assertEqual(item["pct"], 9.94)        # 涨跌幅保留两位
        self.assertEqual(item["lbc"], 3)
        self.assertEqual(item["fbt"], "09:25:00")  # 92500 → 09:25:00

    def test_missing_fbt(self):
        item = app.parse_pool_item({"c": "600519", "n": "贵州茅台", "p": 1355290,
                                    "zdp": 9.999, "lbc": 1})
        self.assertEqual(item["fbt"], "")
        self.assertEqual(item["price"], 1355.29)

    def test_zero_lbc_default(self):
        item = app.parse_pool_item({"c": "000001", "n": "平安银行", "p": 1100,
                                    "zdp": 5.0})
        self.assertEqual(item["lbc"], 0)


class TestPoolCacheAge(unittest.TestCase):
    """缓存有效期：交易中 30 秒，非交易 300 秒"""

    def test_trading_fresh(self):
        self.assertTrue(app.pool_cache_age_ok(10, True))
        self.assertFalse(app.pool_cache_age_ok(31, True))

    def test_closed_fresh(self):
        self.assertTrue(app.pool_cache_age_ok(31, False))
        self.assertFalse(app.pool_cache_age_ok(301, False))


if __name__ == "__main__":
    unittest.main(verbosity=2)