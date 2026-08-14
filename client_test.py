#!/usr/bin/env python3
"""MCP 客户端测试脚本：连接本地 server.py，调用 analyze_image 工具。

用法：
    python client_test.py [图片路径或URL] ["提示词"]
示例：
    python client_test.py sample.png "请识别图中的文字"
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

DEFAULT_IMAGE = "sample.png"
DEFAULT_PROMPT = "请详细描述这张图片的内容。"


async def main() -> None:
    image = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_IMAGE
    prompt = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROMPT

    server_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
    params = StdioServerParameters(
        command=sys.executable,
        args=[server_path],
        env=dict(os.environ),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("已注册工具：", ", ".join(t.name for t in tools.tools))
            print(f"调用 analyze_image(images=[{image!r}], prompt={prompt!r}) ...\n")
            result = await session.call_tool(
                "analyze_image",
                {"images": [image], "prompt": prompt},
            )
            for item in result.content:
                if getattr(item, "type", None) == "text":
                    print(item.text)
                else:
                    print(item)


if __name__ == "__main__":
    asyncio.run(main())
