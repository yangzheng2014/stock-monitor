"""v0.3.1 指数行情逻辑单元测试（stdlib unittest，无第三方依赖）"""
import importlib.util
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("app", os.path.join(BASE, "main_v0.3.1.py"))
app = importlib.util.module_from_spec(_spec)
sys.modules["app"] = app
_spec.loader.exec_module(app)


class TestIndexConfig(unittest.TestCase):
    """INDICES 配置完整性"""

    def test_all_have_secid_code_name(self):
        for idx in app.INDICES:
            self.assertIn("secid", idx)
            self.assertIn("code", idx)
            self.assertIn("name", idx)

    def test_secid_format(self):
        for idx in app.INDICES:
            secid = idx["secid"]
            prefix, _, code = secid.partition(".")
            self.assertIn(prefix, ("1", "0", "100"), f"{secid} 前缀非法")
            self.assertTrue(code, f"{secid} 缺代码")

    def test_known_indices_present(self):
        names = {i["name"] for i in app.INDICES}
        self.assertIn("上证指数", names)
        self.assertIn("恒生指数", names)
        self.assertIn("标普500", names)


class TestParseIndexDiff(unittest.TestCase):
    """parse_index_diff：东财 ulist.np 响应 → 有序指数行情"""

    def test_normal_diff(self):
        diff = [
            {"f12": "000001", "f13": 1, "f14": "上证指数", "f2": 3990.3,
             "f3": 0.19, "f4": 7.65, "f5": 511234287, "f6": 1135187666395.3,
             "f15": 3994.18, "f16": 3955.6, "f17": 3979.49, "f18": 3982.65,
             "f86": 24.71},
            {"f12": "HSI", "f13": 100, "f14": "恒生指数", "f2": 25471.15,
             "f3": 0.07, "f4": 17.92, "f5": 12659520768, "f6": 255540854784.0,
             "f15": 25518.03, "f16": 25240.62, "f17": 25368.87, "f18": 25453.23,
             "f86": "-"},
        ]
        rows = app.parse_index_diff(diff)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["code"], "000001")
        self.assertEqual(first["name"], "上证指数")
        self.assertEqual(first["secid"], "1.000001")
        self.assertEqual(first["price"], 3990.3)
        self.assertEqual(first["pct"], 0.19)
        self.assertEqual(first["high"], 3994.18)
        self.assertEqual(first["timestamp"], "24.71")

    def test_output_follows_INDICES_order(self):
        diff = [
            {"f12": "SPX", "f13": 100, "f14": "标普500", "f2": 7745.06,
             "f3": -0.52, "f4": -40.7, "f5": 2504913888, "f6": "-",
             "f15": 7790.68, "f16": 7744.88, "f17": 7790.68, "f18": 7785.76,
             "f86": "-"},
            {"f12": "000001", "f13": 1, "f14": "上证指数", "f2": 3990.3,
             "f3": 0.19, "f4": 7.65, "f5": 511234287, "f6": 1135187666395.3,
             "f15": 3994.18, "f16": 3955.6, "f17": 3979.49, "f18": 3982.65,
             "f86": 24.71},
        ]
        rows = app.parse_index_diff(diff)
        self.assertEqual(rows[0]["code"], "000001")   # INDICES 顺序优先
        self.assertEqual(rows[1]["code"], "SPX")

    def test_missing_code_skipped(self):
        diff = [
            {"f12": "399001", "f13": 0, "f14": "深证成指", "f2": 14622.5,
             "f3": -0.56, "f4": -81.77, "f5": 674616023, "f6": 1265587019543.0,
             "f15": 14733.9, "f16": 14459.72, "f17": 14692.03, "f18": 14704.27,
             "f86": 28.57},
        ]
        rows = app.parse_index_diff(diff)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "399001")

    def test_bad_types_tolerated(self):
        diff = [{"f12": "000001", "f13": 1, "f14": "上证指数", "f2": "abc",
                 "f3": None, "f4": None, "f5": None, "f6": "-",
                 "f15": None, "f16": None, "f17": None, "f18": None, "f86": None}]
        rows = app.parse_index_diff(diff)
        self.assertEqual(len(rows), 1)          # 坏类型行保留而非丢弃
        self.assertEqual(rows[0]["price"], 0.0)
        self.assertEqual(rows[0]["amount"], 0.0)  # "-" 容错为 0
        self.assertEqual(rows[0]["timestamp"], "")

    def test_empty_diff(self):
        self.assertEqual(app.parse_index_diff([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)