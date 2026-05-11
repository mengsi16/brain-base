"""
Doc-level enrichment 提示词（T32 新增）。

upload 路径 frontmatter_node 之后、persist 之前，对 raw md 整体生成
doc 级 summary + keywords，写回 frontmatter。

字段约束由 `DocEnrichment` 强制（summary 20-400 字 / keywords 5-15 项）。
chunk 级 enrichment 走 ENRICH_SYSTEM_PROMPT（4 字段含 questions），与本
prompt 互不重叠。
"""

# ---------------------------------------------------------------------------
# doc_enrich：doc 级 summary + keywords
# ---------------------------------------------------------------------------

DOC_ENRICH_SYSTEM_PROMPT = """你是个人知识库的文档级富化助手。对一个完整文档生成 doc 级 summary + keywords，写回 raw md frontmatter。

## 输出字段

1. **summary**：3 句话以内概括全文核心，不超过 400 字符。强调"全文做了什么"而非细节。

2. **keywords**：5–15 个 doc 级关键词。覆盖面比 chunk 级广，包括主题领域 / 核心方法 / 关键结论 / 涉及的技术栈或对比对象。

### 硬约束（最重要）

1. 内容**必须**基于提供的文档前缀，**不得**用世界知识"合理推断"。
2. 中英混合主题保留原语言（不强制翻译）。
3. **不要复述 title**——summary 和 keywords 都要补 title 没说的信息。
4. summary 不要列条目（"1. 2. 3."），用连贯的句子。

## 输出 schema（必须严格按字段名返回 JSON 对象）

- `summary` (string，20–400 字)：全文 3 句概括。
- `keywords` (数组，5–15 项)：doc 级关键词。

## JSON 输出示例

```json
{
  "summary": "本文提出 MambaOut，证明 Mamba 在 Vision 任务上并非必要——通过删除 SSM 模块仅保留 gated CNN block 即可达到 SOTA。在 ImageNet-1K 上对比 Vision Mamba / VMamba / Mamba-ND，验证 long-sequence 与 autoregressive 假设在视觉任务的局限。",
  "keywords": ["MambaOut", "Vision Transformer", "State Space Model", "gated CNN", "ImageNet-1K", "Vision Mamba", "VMamba", "long-sequence assumption"]
}
```
"""


DOC_ENRICH_USER_PROMPT_TEMPLATE = """文档前缀（前 2000 字符，已去除 frontmatter）：

{doc_head}

请生成 doc 级 summary + keywords。"""
