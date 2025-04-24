# -*- coding: utf-8 -*-
import datetime
from fastmcp import FastMCP
# py310a C:\Users\admin\.conda\envs\py310a\python.exe

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Demo")


@mcp.tool()
def add_2_numbers(a: int, b: int) -> int:
    """两个数字相加"""
    return a + b


@mcp.tool()
def get_current_time() -> str:
    """获取并格式化当前时间"""
    now = datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    # 运行 FastMCP 服务器
    # 你可以通过 Claude Desktop 或 MCP Inspector 来调用这个工具
    # 或者使用 fastmcp 客户端
    print("Starting FastMCP server for time service...")
    mcp.run(transport='stdio')
