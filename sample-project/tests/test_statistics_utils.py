"""示例项目的可执行验收标准，不依赖第三方库。"""

import unittest

from statistics_utils import summarize


class SummaryTests(unittest.TestCase):
    """覆盖普通列表、一次性迭代器与空输入。"""

    def test_list(self):
        """正常列表维持既有结果。"""
        self.assertEqual(summarize([1, 2, 3]), {"count": 3, "total": 6, "mean": 2})

    def test_iterator(self):
        """生成器不能被重复消费。"""
        self.assertEqual(summarize(iter([2, 4, 6])), {"count": 3, "total": 12, "mean": 4})

    def test_empty(self):
        """空输入的均值明确为 None，不得除零。"""
        self.assertEqual(summarize([]), {"count": 0, "total": 0, "mean": None})

    def test_generator(self):
        """真正的生成器只需消费一次。"""
        self.assertEqual(summarize(value for value in [2, 4, 6]),
                         {"count": 3, "total": 12, "mean": 4})

    def test_empty_generator(self):
        """空生成器与空列表具有相同结果。"""
        self.assertEqual(summarize(value for value in []),
                         {"count": 0, "total": 0, "mean": None})

    def test_single_value(self):
        """单元素生成器的均值等于该元素。"""
        self.assertEqual(summarize(value for value in [-3]),
                         {"count": 1, "total": -3, "mean": -3})

    def test_zero_total(self):
        """非空输入总和为零时均值仍为零。"""
        self.assertEqual(summarize([-2, 0, 2]),
                         {"count": 3, "total": 0, "mean": 0})


if __name__ == "__main__":
    unittest.main()
