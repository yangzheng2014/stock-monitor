"""v0.2.4 东财新闻搜索 JSONP 解析单元测试（stdlib unittest，无第三方依赖）"""
import importlib.util
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("app", os.path.join(BASE, "main_v0.2.4.py"))
app = importlib.util.module_from_spec(_spec)
sys.modules["app"] = app
_spec.loader.exec_module(app)


def _wrap(items):
    """构造东财 JSONP 响应文本。"""
    import json
    payload = {"code": 0, "result": {"cmsArticleWebOld": items}, "hitsTotal": len(items)}
    return "cb(" + json.dumps(payload, ensure_ascii=False) + ")"


class TestParseEmNewsJsonp(unittest.TestCase):
    """东财新闻搜索 JSONP 响应解析"""

    def test_normal_item(self):
        text = _wrap([{
            "title": "三环集团将实施第二期回购",
            "date": "2026-07-30 21:43:00",
            "mediaName": "中国基金报",
            "url": "http://finance.eastmoney.com/a/1.html",
            "content": "三环集团发布公告称，公司拟回购公司股份。",
        }])
        news = app.parse_em_news_jsonp(text)
        self.assertEqual(len(news), 1)
        self.assertEqual(news[0]["title"], "三环集团将实施第二期回购")
        self.assertEqual(news[0]["time"], "2026-07-30 21:43:00")
        self.assertEqual(news[0]["source"], "中国基金报")
        self.assertEqual(news[0]["url"], "http://finance.eastmoney.com/a/1.html")

    def test_em_tag_stripped(self):
        """标题中的 <em> 高亮标签应被移除。"""
        text = _wrap([{"title": "千亿龙头<em>300408</em>，再回购<em>5</em>亿",
                       "date": "", "mediaName": "", "url": "", "content": ""}])
        news = app.parse_em_news_jsonp(text)
        self.assertEqual(news[0]["title"], "千亿龙头300408，再回购5亿")

    def test_summary_html_stripped(self):
        """摘要 content 中的 HTML 标签应被移除并截断。"""
        text = _wrap([{"title": "测试", "date": "", "mediaName": "", "url": "",
                       "content": "<p>三环集团</p><em>公告</em>" + "长" * 200}])
        news = app.parse_em_news_jsonp(text)
        self.assertNotIn("<", news[0]["summary"])
        self.assertNotIn(">", news[0]["summary"])
        self.assertLessEqual(len(news[0]["summary"]), 120)

    def test_empty_items(self):
        self.assertEqual(app.parse_em_news_jsonp(_wrap([])), [])

    def test_invalid_text(self):
        """非法文本不应抛异常，返回空列表。"""
        self.assertEqual(app.parse_em_news_jsonp("not json at all"), [])
        self.assertEqual(app.parse_em_news_jsonp(""), [])

    def test_missing_result(self):
        import json
        text = "cb(" + json.dumps({"code": 1, "msg": "err"}) + ")"
        self.assertEqual(app.parse_em_news_jsonp(text), [])


if __name__ == "__main__":
    unittest.main()
