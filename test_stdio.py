#!/usr/bin/env python3
"""stdio 真实验收：官方 Client SDK 握手 -> list_tools -> 3 个工具真实调用。"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "src", "web_extract_mcp", "server.py")
SCRATCH = "/root/.hermes/cache/scratch/mcp_pkg_test"
os.makedirs(SCRATCH, exist_ok=True)
PAGE = os.path.join(SCRATCH, "web_demo.html")
OUT_MD = os.path.join(SCRATCH, "web_demo.md")
OUT_XLSX = os.path.join(SCRATCH, "web_tables.xlsx")
OUT_LINKS = os.path.join(SCRATCH, "web_links.json")
BATCH_IN = os.path.join(SCRATCH, "web_pages")
BATCH_OUT = os.path.join(SCRATCH, "web_extracted")

HTML = """<html><head><title>Demo 页面</title>
<meta name="description" content="一个测试页">
<base href="https://example.com/a/">
<script type="application/ld+json">{"@type":"Article","name":"结构化"}</script>
<script>var ads=99;</script></head>
<body><script>var x=1;</script>
<h1>标题一</h1><p>第一段 <a href="/about">关于我们</a> <a href="https://b.com/x">外链</a></p>
<table><tr><th>姓名</th><th>金额</th></tr><tr><td>张三</td><td>100</td></tr>
<tr><td>李四</td><td>200</td></tr></table>
</body></html>"""

results = {}


def _prep():
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(HTML)
    os.makedirs(BATCH_IN, exist_ok=True)
    for nm in ("p1.html", "p2.htm"):
        with open(os.path.join(BATCH_IN, nm), "w", encoding="utf-8") as f:
            f.write(HTML.replace("Demo 页面", "批量-" + nm))


async def main():
    _prep()
    params = StdioServerParameters(command=sys.executable, args=[SERVER])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            results["initialize"] = {
                "serverName": init.server_info.name,
                "serverVersion": init.server_info.version,
                "protocol": init.protocol_version,
            }

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            results["list_tools"] = names
            assert set(names) == {"extract_page", "extract_batch", "selftest"}, names

            # 1) Markdown（含 frontmatter + JSON-LD）
            for p in (OUT_MD, OUT_XLSX, OUT_LINKS):
                if os.path.exists(p):
                    os.remove(p)
            r = await session.call_tool(
                "extract_page",
                {"input_path": PAGE, "output_path": OUT_MD, "mode": "all",
                 "include_script": True, "frontmatter": True},
            )
            md_res = json.loads(r.content[0].text)
            assert md_res["ok"] and os.path.isfile(OUT_MD), md_res
            assert md_res["title"] == "Demo 页面", md_res
            md = open(OUT_MD, encoding="utf-8").read()
            assert md.startswith("---\n") and 'title: "Demo 页面"' in md, "frontmatter"
            assert "结构化数据 (JSON-LD)" in md and '"@type":"Article"' in md, "JSON-LD"
            # include_script=True：body 内联 <script> 被捕获并渲染到"内联脚本"段，
            # 不混入正文段落（正文 "# Demo 页面" 到 "## 结构化数据" 之间）
            body_region = md.split("## 结构化数据")[0]
            assert "var x=1" not in body_region and "var ads=99" not in body_region, "脚本串入正文"
            assert "内联脚本 (include-script)" in md and "var x=1" in md, "内联脚本段"
            assert "关于我们" in md
            # 默认 include_script=False：全文不得含内联 JS
            r0 = await session.call_tool(
                "extract_page",
                {"input_path": PAGE, "output_path": OUT_MD + ".plain", "mode": "all"},
            )
            md_plain = open(OUT_MD + ".plain", encoding="utf-8").read()
            assert "var x=1" not in md_plain and "var ads=99" not in md_plain
            results["extract_markdown"] = {
                "size": md_res["output_size_bytes"],
                "title": md_res["title"],
                "links": md_res["links"],
                "tables": md_res["tables"],
                "jsonld": md_res["jsonld_blocks"],
                "frontmatter_ok": True,
                "body_script_separated": True,
                "default_strips_scripts": True,
            }

            # 2) 表格 xlsx + 用 openpyxl 复核
            r = await session.call_tool(
                "extract_page",
                {"input_path": PAGE, "output_path": OUT_XLSX, "mode": "tables"},
            )
            t_res = json.loads(r.content[0].text)
            assert t_res["ok"] and os.path.isfile(OUT_XLSX), t_res
            import openpyxl
            wb = openpyxl.load_workbook(OUT_XLSX)
            ws = wb.active
            data = [list(row) for row in ws.iter_rows(values_only=True)]
            assert data == [["姓名", "金额"], ["张三", "100"], ["李四", "200"]], data
            results["extract_tables_xlsx"] = {"disk_verify": data}

            # 3) 链接 JSON：相对链接按 <base> 转绝对
            r = await session.call_tool(
                "extract_page",
                {"input_path": PAGE, "output_path": OUT_LINKS, "mode": "links"},
            )
            l_res = json.loads(r.content[0].text)
            links = json.load(open(OUT_LINKS, encoding="utf-8"))
            hrefs = {x["text"]: x.get("href_abs", x["href"]) for x in links}
            assert hrefs["关于我们"] == "https://example.com/about", hrefs
            assert hrefs["外链"] == "https://b.com/x", hrefs
            results["extract_links"] = hrefs

            # 4) 批量
            r = await session.call_tool(
                "extract_batch",
                {"input_dir": BATCH_IN, "output_dir": BATCH_OUT,
                 "mode": "all", "frontmatter": True},
            )
            b_res = json.loads(r.content[0].text)
            assert b_res["processed"] == 2, b_res
            assert os.path.isfile(os.path.join(BATCH_OUT, "_batch_index.json"))
            assert os.path.isfile(os.path.join(BATCH_OUT, "p1", "p1.md"))
            assert os.path.isfile(os.path.join(BATCH_OUT, "p2", "p2.md"))
            results["batch"] = {"processed": b_res["processed"],
                                "index": b_res["index_path"]}

            # 5) 错误路径
            r = await session.call_tool(
                "extract_page",
                {"input_path": "/nonexistent/p.html",
                 "output_path": "/tmp/x.md", "mode": "all"},
            )
            assert bool(r.is_error)
            results["error_handling"] = {"is_error": True}

            # 6) 自检
            r = await session.call_tool("selftest", {})
            assert json.loads(r.content[0].text)["ok"] is True
            results["selftest"] = "pass"

            print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
            print("\nALL ASSERTIONS PASSED")


asyncio.run(main())
