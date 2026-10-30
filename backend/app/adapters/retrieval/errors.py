"""检索链路的配置/契约错误。

抽到适配层是为了让适配器（VectorStore、BM25、融合排序）可以在不依赖 services
的前提下抛出统一异常；上层（staging_retrieval_service / assistant_service）按同一
语义处理——只有嵌入调用本身失败才中止，其余降级或跳过。
"""

from __future__ import annotations


class RetrievalConfigurationError(RuntimeError):
    """检索链无法按契约完成一次检索（嵌入失败、向量契约不合法、维度不一致）。"""
