"""书籍自动标签（方案 A：规则词典 + 路径/元数据信号打分）。

信号与权重（每命中一个关键词）：
    文件路径目录段 +3（用户自建的目录结构是强信号，如 技术/Python、文学/小说）
    书名           +2
    作者           +1
    简介/元数据     +1
格式：CBZ/CBR 直接给「漫画」+6。
得分 >=3 的标签按分排序取前 3 个，写入 books.tags（JSON 数组字符串）。
"""
from __future__ import annotations

import json
import logging
import re

import database

log = logging.getLogger("tagger")

# 类目 → 关键词（小写匹配，中文直接包含，英文按词边界）
TAG_RULES = [
    ("编程开发", ["python", "java", "javascript", "typescript", "golang", "rust", "c++", "c#",
                  "编程", "代码", "程序", "算法", "架构", "开发", "前端", "后端", "全栈",
                  "数据库", "sql", "mysql", "redis", "linux", "docker", "api", "设计模式", "重构"]),
    ("互联网科技", ["互联网", "科技", "人工智能", "ai", "机器学习", "深度学习", "大数据",
                    "区块链", "产品经理", "黑客", "网络安全", "加密"]),
    ("文学小说", ["小说", "文学", "散文", "诗歌", "推理", "悬疑", "武侠", "言情",
                  "名著", "随笔", "杂文", "古文", "诗词", "诗"]),
    ("科幻奇幻", ["科幻", "奇幻", "魔幻", "星际", "末日", "赛博", "太空", "外星", "未来世界"]),
    ("历史人文", ["历史", "史记", "王朝", "通史", "文明", "考古", "古代", "近代", "战争史",
                  "哲学", "思想", "国学", "论语", "哲学史"]),
    ("心理励志", ["心理", "情绪", "认知", "自控", "性格", "治愈", "幸福", "内向", "沟通",
                  "成长", "思维", "习惯", "效率", "精进", "自律"]),
    ("生活百科", ["生存", "百科", "生活", "家居", "烹饪", "菜谱", "健康", "养生", "急救",
                  "户外", "探险", "旅行", "园艺", "宠物"]),
    ("经济管理", ["经济", "管理", "商业", "投资", "金融", "创业", "营销", "经济学",
                  "财富", "理财", "股票"]),
    ("人物传记", ["传记", "自传", "回忆录", "生平", "日志"]),
    ("社会科学", ["社会", "政治", "法律", "教育", "军事", "哲学入门", "人类学"]),
    ("漫画", ["漫画", " comic", "manga"]),
]

# 命中正则：英文按词边界，中文按包含
_KW_RE = {}


def _kw_matcher(kw: str):
    if kw not in _KW_RE:
        if re.fullmatch(r"[a-z0-9 +#]+", kw):
            _KW_RE[kw] = re.compile(r"\b" + re.escape(kw.strip()) + r"\b", re.IGNORECASE)
        else:
            _KW_RE[kw] = re.compile(re.escape(kw), re.IGNORECASE)
    return _KW_RE[kw]


def _count_hits(text: str, keywords: list) -> int:
    if not text:
        return 0
    return sum(1 for kw in keywords if _kw_matcher(kw).search(text))


def score_book(rel_path: str, title: str, author: str, fmt: str, description: str) -> list:
    """返回 [(标签, 得分)]，按得分降序。"""
    scores = {}
    path_str = rel_path.lower()
    segments = [seg.lower() for seg in rel_path.replace("\\", "/").split("/")[:-1]]
    title_l = (title or "").lower()
    author_l = (author or "").lower()
    desc_l = (description or "").lower()

    for tag, keywords in TAG_RULES:
        s = 0
        for kw in keywords:
            m = _kw_matcher(kw)
            for seg in segments:
                if m.search(seg):
                    s += 3
            if m.search(title_l):
                s += 2
            if m.search(author_l):
                s += 1
            if m.search(desc_l):
                s += 1
        if s:
            scores[tag] = s
    # 漫画格式强信号
    if fmt in ("cbz", "cbr"):
        scores["漫画"] = scores.get("漫画", 0) + 6

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return ranked


def apply_tags(book_id: int) -> list:
    """为书籍打标并写库，返回标签列表。"""
    row = database.get_book(book_id)
    if row is None:
        return []
    ranked = score_book(row["rel_path"], row["title"], row["author"],
                        row["format"], row["description"])
    tags = [t for t, s in ranked if s >= 3][:3]
    database.execute("UPDATE books SET tags=? WHERE id=?",
                     (json.dumps(tags, ensure_ascii=False), book_id))
    return tags
