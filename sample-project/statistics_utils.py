"""协作验收示例：让工程师修复并由另一个独立 AI 审查。"""


def summarize(values):
    """汇总 values；返回 count、total、mean，需正确支持一次性迭代器及空输入。"""
    values = list(values)  # 一次性读取输入，供求和与计数共同使用。
    total = sum(values)  # 输入数值总和。
    count = len(values)  # 输入元素数量。
    return {"count": count, "total": total, "mean": total / count if count else None}
