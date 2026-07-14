"""
检索结果数据结构

定义统一的搜索结果格式
"""

from dataclasses import dataclass
from typing import Dict, Any, Iterator, List, Literal, Optional, overload


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


SearchStatus = Literal["success", "no_results", "invalid_query", "error"]


@dataclass(frozen=True)
class SearchResponse:
    """
    检索响应数据类。

    保留 list-like 行为以兼容旧调用方，同时通过 status/error_* 区分
    “无结果”和“检索失败”。
    """

    results: List[SearchResult]
    status: SearchStatus
    error_message: Optional[str] = None
    error_type: Optional[str] = None

    @property
    def ok(self) -> bool:
        """检索是否成功完成（无结果也算成功完成）。"""
        return self.status in {"success", "no_results", "invalid_query"}

    @property
    def failed(self) -> bool:
        """检索是否发生异常。"""
        return self.status == "error"

    def __bool__(self) -> bool:
        return bool(self.results)

    def __len__(self) -> int:
        return len(self.results)

    def __iter__(self) -> Iterator[SearchResult]:
        return iter(self.results)

    @overload
    def __getitem__(self, index: int) -> SearchResult:
        ...

    @overload
    def __getitem__(self, index: slice) -> List[SearchResult]:
        ...

    def __getitem__(self, index: int | slice) -> SearchResult | List[SearchResult]:
        return self.results[index]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.results == other
        if isinstance(other, SearchResponse):
            return (
                self.results == other.results
                and self.status == other.status
                and self.error_message == other.error_message
                and self.error_type == other.error_type
            )
        return NotImplemented
