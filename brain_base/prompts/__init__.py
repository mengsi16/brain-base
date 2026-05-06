"""
brain_base prompts 包。

集中管理所有 agent/node 的提示词，从旧 skills/*/SKILL.md 迁移而来。

组织方式：
- 每个 agent 对应一个 prompts 文件
- 每个文件内按节点分 SYSTEM_PROMPT / USER_PROMPT_TEMPLATE 常量
- 节点函数从此模块 import 提示词
"""
