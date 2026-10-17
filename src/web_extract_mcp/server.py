#!/usr/bin/env python3
"""
web-extract MCP Server — 本地网页/文档数据提取（不联网、不爬站）

暴露工具：
  extract_page(input_path, output_path, mode="all", include_script=False,
               frontmatter=False, base_url="")
      解析本地 .html/.htm：mode=all 生成 Markdown；tables 导出 xlsx（output 以
      .xlsx 结尾）或 CSV 目录；links 导出链接 JSON（相对链接转绝对）。
  extract_batch(input_dir, output_dir, mode, recursive, include_script, frontmatter)
      批量目录处理，产物含 _batch_index.json。
  selftest()

基于 web-extract-skill（extract_core.py，Python 标准库 HTMLParser + openpyxl）。
传输：stdio（默认）/ streamable-http（--transport streamable-http --port N）
兼容 mcp SDK 1.x（FastMCP）与 2.x（MCPServer）双导入。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from . import extract_core as core
except ImportError:
    import extract_core as core

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

mcp = MCPServer(
    name="web-extract",
    instructions=(
        "本地已保存网页(.html/.htm)解析服务，绝不主动访问网络。"
        "extract_page 提取正文 Markdown / 表格(xlsx 或 csv 目录) / 链接 JSON，"
        "相对链接自动转绝对；extract_batch 批量处理目录。"
        "所有路径必须是服务器本地绝对路径。"
    ),
)

_MODES = {"all": "all", "text": "all", "tables": "tables", "links": "links"}


@mcp.tool(
    name="extract_page",
    description=(
        "解析单个本地 .html/.htm 文件并落盘。mode: 'all' 输出正文 Markdown；"
        "'tables' 输出表格——output_path 以 .xlsx 结尾时导出单工作簿多 sheet，"
        "否则输出到该目录下的 table1.csv/table2.csv…；'links' 输出链接清单 JSON。"
        "include_script 保留内联 JSON-LD/JS；frontmatter 给 Markdown 加 YAML 头；"
        "base_url 手工指定相对链接转绝对的基准 URL（页面有 <base> 时可省略）。"
    ),
)
def extract_page(
    input_path: str,
    output_path: str,
    mode: str = "all",
    include_script: bool = False,
    frontmatter: bool = False,
    base_url: str = "",
) -> dict:
    inp = os.path.abspath(os.path.expanduser(input_path))
    if not os.path.isfile(inp):
        raise FileNotFoundError(f"输入文件不存在: {inp}")
    if not inp.lower().endswith((".html", ".htm")):
        raise ValueError("仅支持 .html/.htm 文件")
    only = _MODES.get(mode)
    if only is None:
        raise ValueError(f"mode 必须是 {sorted(set(_MODES))}")
    out = os.path.abspath(os.path.expanduser(output_path))
    if only != "tables" or out.lower().endswith(".xlsx"):
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    res = core.process_one(
        inp, out, only=only, include_script=bool(include_script),
        frontmatter=bool(frontmatter), base_url=base_url or "",
    )
    result = {
        "ok": True,
        "input_path": inp,
        "output_path": out,
        "title": res["title"],
        "text_chars": len(res["text"]),
        "links": len(res["links"]),
        "tables": len(res["tables"]),
        "jsonld_blocks": len(res.get("jsonld", [])),
        "source_url": res.get("source_url", ""),
    }
    if only != "tables":
        result["output_size_bytes"] = os.path.getsize(out)
    elif out.lower().endswith(".xlsx"):
        result["output_size_bytes"] = os.path.getsize(out)
    return result


@mcp.tool(
    name="extract_batch",
    description=(
        "批量处理目录下所有 .html/.htm：每个文件在 output_dir 下建同名子目录，"
        "内含 <名>.md（mode=all）/ tables.xlsx / links.json，并生成 _batch_index.json。"
        "recursive=True 递归子目录。"
    ),
)
def extract_batch(
    input_dir: str,
    output_dir: str,
    mode: str = "all",
    recursive: bool = False,
    include_script: bool = False,
    frontmatter: bool = False,
) -> dict:
    ind = os.path.abspath(os.path.expanduser(input_dir))
    if not os.path.isdir(ind):
        raise FileNotFoundError(f"输入目录不存在: {ind}")
    outd = os.path.abspath(os.path.expanduser(output_dir))
    only = _MODES.get(mode)
    if only is None:
        raise ValueError(f"mode 必须是 {sorted(set(_MODES))}")
    import contextlib
    import io
    buf = io.StringIO()
    try:
        # run_batch 会往 stdout 打印进度，stdio 传输下必须吞掉
        with contextlib.redirect_stdout(buf):
            n = core.run_batch(
                ind, outd, only=only, include_script=bool(include_script),
                frontmatter=bool(frontmatter), recursive=bool(recursive),
            )
    except core.TierLimitError as e:
        raise RuntimeError(str(e))
    idx_path = os.path.join(outd, "_batch_index.json")
    return {
        "ok": True,
        "processed": n,
        "output_dir": outd,
        "index_path": idx_path if os.path.isfile(idx_path) else None,
    }


@mcp.tool(
    name="selftest",
    description="运行核心引擎自检（标题/正文/script剔除/链接绝对化/表格/JSON-LD/批量）。",
)
def selftest() -> dict:
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = bool(core._selftest())
    return {"ok": ok, "detail": buf.getvalue().strip()}


def main():
    import argparse

    ap = argparse.ArgumentParser(description="web-extract MCP server")
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "streamable-http", "sse"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8767)
    args = ap.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
