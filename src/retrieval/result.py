"""
检索结果数据结构

定义统一的搜索结果格式
"""

from dataclasses import dataclass
from typing import Dict, Any


@dataclass(frozen=True)
class SearchResult:
    """
    搜索结果数据类

    Attributes:
        knowledge_id: 知识条目 ID
        title: 标题
        score: 相关性分数 (0.0-1.0)
        highlight: 摘要或高亮片段
        metadata: 额外元数据（来源、标签、原始分数等）
    """
    knowledge_id: int
    title: str
    score: float
    highlight: str
    metadata: Dict[str, Any]

    def __post_init__(self):
        """验证分数范围"""
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"分数必须在 [0.0, 1.0] 范围内，当前值: {self.score}")
