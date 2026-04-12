# milvus-rag 目录说明

这个目录保存的是 **knowledge-base 项目内适配入口**，不是 Milvus 官方原生 MCP Server。

## 官方接入方式

1. 通过插件根目录 `.mcp.json` 接入官方 `zilliztech/mcp-server-milvus`。
2. `.mcp.json` 使用 `uv --directory ... run server.py` 的 stdio 配置，这与官方 README 的推荐方式一致。
3. 先克隆官方仓库到项目路径：`mcp/mcp-server-milvus/`，再加载插件。

官方仓库：

- `https://github.com/zilliztech/mcp-server-milvus`

## 本目录的角色

1. 作为项目内兼容层与过渡工具，不再宣称是官方 MCP 方案。
2. 用于复用本项目 `bin/milvus-cli.py` 的配置和检索行为。
3. 正式生产使用优先走官方 MCP server。
