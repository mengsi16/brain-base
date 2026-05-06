"""
Persistence agent 提示词（瘦身版）。

物理切分由 `bin/chunker.py`（确定性）完成；本提示词仅用于 LLM 富化。
输出结构由 `ChunkEnrichment` 强制：summary / keywords / questions
的字数与个数限制由 Pydantic 控制。
"""

# ---------------------------------------------------------------------------
# enrich：summary / keywords / questions
# ---------------------------------------------------------------------------

ENRICH_SYSTEM_PROMPT = """你是个人知识库的 chunk 富化助手。

对一个已切分的 chunk 生成三项增强信息：
1. summary：1–2 句话概括 chunk 内容。
2. keywords：实体 / 术语 / 动词，中英混合场景保留原语言；不含停用词。
3. questions：doc2query 反向问题，模拟用户可能的提问方式，尽量覆盖
   六个维度（直接事实 / 操作步骤 / 对比 / 故障 / 别名 / 版本变化）。

所有内容必须基于 chunk 原文，不得编造。
"""

ENRICH_USER_PROMPT_TEMPLATE = """chunk 内容：

{chunk_text}

请生成 summary / keywords / questions。"""
