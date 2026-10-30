"""词法通道的分词与重叠度量（中英混排，零依赖、确定性）。

BM25 与降级排序共用同一套 token 口径：
- ASCII 词（含代码标识符/英文术语）按 ``[A-Za-z0-9_]`` 整词；
- 连续汉字串切 2-gram 与 3-gram（中文没有空格边界，n-gram 是免词典的通用近似）。

刻意不引入 jieba：n-gram 无词典、无新词歧义、跨环境完全确定，且 2/3-gram 对
「考点名 → 陈述正文」这类短查询的召回已足够（旧实现实测覆盖率区分度 0.18）。
"""

from __future__ import annotations

import re

_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_CJK_SEQUENCE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_DEDUP_STRIP_RE = re.compile(r"[\s_＿—－\-]+")


def tokenize(text: str) -> list[str]:
    """返回带重复的 token 列表（BM25 需要词频，故不用 set）。"""

    tokens = [match.group(0).lower() for match in _ASCII_TOKEN_RE.finditer(text)]
    for sequence in _CJK_SEQUENCE_RE.findall(text):
        for width in (2, 3):
            tokens.extend(
                sequence[index : index + width]
                for index in range(len(sequence) - width + 1)
            )
    return tokens


def lexical_overlap(left: str, right: str) -> float:
    """Jaccard 重叠（历史口径，仅供离线脚本对比用）。"""

    left_tokens = set(tokenize(left))
    right_tokens = set(tokenize(right))
    union = left_tokens | right_tokens
    if not union:
        return 0.0
    return len(left_tokens & right_tokens) / len(union)


def query_token_coverage(query: str, content: str) -> float:
    """查询侧覆盖率：|查询 ∩ 内容| / |查询|（分母与内容长度无关，长正文不吃亏）。"""

    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0
    content_tokens = set(tokenize(content))
    return len(query_tokens & content_tokens) / len(query_tokens)


def dedup_key(text: str) -> str:
    """同文折叠键：忽略空白与表单下划线串，跨文档模板碎片（表头/填空线）归一。"""

    return _DEDUP_STRIP_RE.sub("", text)
