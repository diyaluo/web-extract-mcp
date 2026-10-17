# web-extract MCP

本地已保存网页解析的 MCP 封装（**不主动访问网络、不爬站**，只解析本地
`.html/.htm`）。核心引擎来自 `data/disk/projects/web-extract-skill`
（Python 标准库 HTMLParser，导出 xlsx 时才需 openpyxl）。

## 工具

| 工具 | 说明 |
|---|---|
| `extract_page(input_path, output_path, mode, include_script, frontmatter, base_url)` | 单文件：`mode=all`→Markdown；`tables`→xlsx（output 以 `.xlsx` 结尾）或 CSV 目录；`links`→链接 JSON |
| `extract_batch(input_dir, output_dir, mode, recursive, ...)` | 目录批量，每个 html 一个子目录 + `_batch_index.json` |
| `selftest()` | 核心引擎自检 |

自动剔除 script/style/noscript/svg；相对链接按页面 `<base>` 或 `base_url`
转绝对；表格与正文分离；可选保留 JSON-LD/内联 JS 与 YAML frontmatter。

## 运行

```bash
pip install -r requirements.txt
python server.py                      # stdio
python server.py --transport streamable-http --port 8767
```

## 实测

```bash
python test_stdio.py
```
