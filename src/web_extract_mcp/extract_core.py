#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""web_extract.py — 本地网页/文档数据提取（纯本地、无 API Key、不联网抓取）。

边界（重要）：
  本工具【不主动访问网络、不爬网站】。它只解析你已经保存到本地的文件，
  因此不涉及反爬、登录态、robots 或对方服务器负载问题。
  输入：浏览器"另存为"的 .html/.htm，或下载的 .json/.xml/.csv。
  输出：干净正文(Markdown)、表格(CSV/XLSX)、链接清单、元数据。

用法：
  python web_extract.py page.html out.md                 # 全量提取（Markdown）
  python web_extract.py page.html out.xlsx --only tables  # 只要表格
  python web_extract.py page.html links.json --only links
  python web_extract.py --batch html_dir/ out_dir/        # 批量：每个 html 一套产物
  python web_extract.py page.html out.md --include-script --frontmatter
  python web_extract.py --selftest
"""
import argparse, json, os, re, sys, csv as csvmod
from html.parser import HTMLParser
from urllib.parse import urljoin, unquote

# ── 分层（freemium）──
try:
    import tier_config as _tier  # type: ignore
    TIER = getattr(_tier, "TIER", "pro")
    MAX_ROWS = getattr(_tier, "MAX_ROWS", 0)
    FEATURES = getattr(_tier, "FEATURES", {}) or {}
    BATCH_ENABLED = getattr(_tier, "BATCH_ENABLED",
                            FEATURES.get("batch", TIER == "pro"))
except Exception:
    TIER, MAX_ROWS, FEATURES = "pro", 0, {}
    BATCH_ENABLED = True


class TierLimitError(RuntimeError):
    pass


def enforce_rows(n):
    if MAX_ROWS and n > MAX_ROWS:
        raise TierLimitError(f"🧊 Lite 版单次最多导出 {MAX_ROWS} 行（当前 {n} 行），批量/大文件请用 Pro。")


# ───────────────── HTML 解析（标准库，零第三方依赖） ─────────────────
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "br", "h1", "h2",
              "h3", "h4", "h5", "h6", "blockquote", "table"}


class PageParser(HTMLParser):
    def __init__(self, include_script=False):
        super().__init__(convert_charrefs=True)
        self.include_script = include_script
        self.title = ""
        self._in_title = False
        self.meta = {}
        self.base_href = ""
        self.parts = []
        self.links = []
        self.tables = []
        self.jsonld = []   # <script type="application/ld+json"> 原文
        self.scripts = []  # 其他内联脚本原文（include_script 时保留）
        self._cap = None   # ("jsonld"|"script", [chunks])
        self._skip = 0
        self._cur_table = None
        self._cur_row = None
        self._cur_cell = None
        self._cell_tag = None
        self._a_text = None
        self._a_href = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "base" and a.get("href"):
            self.base_href = a["href"].strip()
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description", "og:title", "keywords", "author"):
                self.meta[name] = (a.get("content") or "").strip()
        if tag == "script" and self.include_script and not a.get("src"):
            stype = (a.get("type") or "").lower()
            if "ld+json" in stype:
                self._cap = ["jsonld", []]
            elif not stype or "javascript" in stype:
                self._cap = ["script", []]
        if tag in SKIP_TAGS:
            self._skip += 1
        if self._skip:
            return
        if tag == "a":
            self._a_text, self._a_href = [], a.get("href", "")
        if tag == "table":
            self._cur_table = []
        elif tag == "tr" and self._cur_table is not None:
            self._cur_row = []
        elif tag in ("td", "th") and self._cur_row is not None:
            self._cur_cell, self._cell_tag = [], tag
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._cap is not None:
            kind, chunks = self._cap
            raw = "".join(chunks).strip()
            if raw:
                (self.jsonld if kind == "jsonld" else self.scripts).append(raw)
            self._cap = None
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if self._skip:
            return
        if tag == "a" and self._a_text is not None:
            txt = "".join(self._a_text).strip()
            if txt and self._a_href:
                self.links.append({"text": txt, "href": self._a_href})
            # 链接文字已随 handle_data 进正文，这里不再 append（避免重复）
            self._a_text = self._a_href = None
        elif tag in ("td", "th") and self._cur_cell is not None:
            self._cur_row.append(" ".join("".join(self._cur_cell).split()))
            self._cur_cell = None
        elif tag == "tr" and self._cur_row is not None:
            if any(c.strip() for c in self._cur_row):
                self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif tag == "table" and self._cur_table is not None:
            if self._cur_table:
                self.tables.append(self._cur_table)
            self._cur_table = None
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._cap is not None:
            self._cap[1].append(data)
            return  # 被捕获的脚本内容不进正文
        if self._skip:
            return
        if self._cur_cell is not None:
            self._cur_cell.append(data)
            return  # 表格数据单独收集，不混入正文
        if self._a_text is not None:
            self._a_text.append(data)
        self.parts.append(data)

    def text(self):
        s = "".join(self.parts)
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n\n", s)
        return s.strip()


def parse_html(html, base_url="", include_script=False):
    p = PageParser(include_script=include_script)
    p.feed(html)
    links = p.links
    eff_base = base_url or p.base_href
    if eff_base:
        for L in links:
            if not L["href"].startswith(("http://", "https://", "mailto:", "#", "javascript:")):
                L["href_abs"] = urljoin(eff_base, L["href"])
    return {"title": p.title.strip(), "meta": p.meta, "text": p.text(),
            "links": links, "tables": p.tables,
            "jsonld": p.jsonld, "scripts": p.scripts,
            "source_url": eff_base}


def _yaml_quote(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def to_markdown(res, frontmatter=False, source_file=""):
    L = []
    if frontmatter:
        fm = ["---", f"title: {_yaml_quote(res['title'] or '(无标题)')}"]
        if source_file:
            fm.append(f"source_file: {_yaml_quote(source_file)}")
        if res.get("source_url"):
            fm.append(f"source_url: {_yaml_quote(res['source_url'])}")
        if res["meta"].get("author"):
            fm.append(f"author: {_yaml_quote(res['meta']['author'])}")
        if res["meta"].get("keywords"):
            fm.append(f"tags: [{_yaml_quote(res['meta']['keywords'])}]")
        fm.append("---")
        L += fm
    L += [f"# {res['title'] or '(无标题)'}", ""]
    if res["meta"].get("description") or res["meta"].get("og:description"):
        L += [f"> {res['meta'].get('description') or res['meta'].get('og:description')}", ""]
    L.append(res["text"])
    if res.get("jsonld"):
        L += ["", "## 结构化数据 (JSON-LD)", ""]
        for j in res["jsonld"]:
            L += ["```json", j.strip(), "```", ""]
    if res.get("scripts"):
        L += ["", "## 内联脚本 (include-script)", ""]
        for s in res["scripts"]:
            L += ["```javascript", s.strip(), "```", ""]
    L += ["", f"\n\n---\n链接 {len(res['links'])} 个，表格 {len(res['tables'])} 个\n"]
    return "\n".join(L)


def write_tables_xlsx(tables, path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for i, t in enumerate(tables):
        ws = wb.create_sheet(f"table{i+1}"[:31])
        for row in t:
            ws.append(row)
    wb.save(path)


def write_tables_csv(tables, out_dir):
    paths = []
    for i, t in enumerate(tables):
        p = os.path.join(out_dir, f"table{i+1}.csv")
        with open(p, "w", encoding="utf-8-sig", newline="") as f:
            w = csvmod.writer(f)
            w.writerows(t)
        paths.append(p)
    return paths


def read_html_file(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def process_one(in_path, out_path, only="all", include_script=False,
                frontmatter=False, base_url=""):
    """处理单个 html。out_path 语义：all/links=文件；tables=.xlsx 文件或 csv 目录。"""
    html = read_html_file(in_path)
    res = parse_html(html, base_url=base_url, include_script=include_script)
    if only == "tables":
        enforce_rows(sum(len(t) for t in res["tables"]))
        if out_path.lower().endswith(".xlsx"):
            write_tables_xlsx(res["tables"], out_path)
        else:
            os.makedirs(out_path, exist_ok=True)
            write_tables_csv(res["tables"], out_path)
    elif only == "links":
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res["links"], f, ensure_ascii=False, indent=2)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        md = to_markdown(res, frontmatter=frontmatter,
                         source_file=os.path.basename(in_path))
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(md)
    return res


def run_batch(in_dir, out_dir, only="all", include_script=False,
              frontmatter=False, recursive=False):
    if not BATCH_ENABLED:
        raise TierLimitError("🧊 Lite 版不支持整目录批处理，请用 Pro 版。")
    os.makedirs(out_dir, exist_ok=True)
    exts = (".html", ".htm")
    if recursive:
        files = [os.path.join(r, f) for r, _, fs in os.walk(in_dir)
                 for f in fs if f.lower().endswith(exts)]
    else:
        files = [os.path.join(in_dir, f) for f in sorted(os.listdir(in_dir))
                 if f.lower().endswith(exts)]
    if not files:
        print(f"批量目录无 .html/.htm 文件：{in_dir}")
        return 0
    ok = 0
    index = []
    used = {}
    for fp in sorted(files):
        stem = os.path.splitext(os.path.basename(fp))[0]
        name = used.setdefault(stem, 0)
        if name:
            stem = f"{stem}_{name+1}"
        used[os.path.splitext(os.path.basename(fp))[0]] = name + 1
        sub = os.path.join(out_dir, stem)
        os.makedirs(sub, exist_ok=True)
        try:
            if only == "tables":
                target = os.path.join(sub, "tables.xlsx")
            elif only == "links":
                target = os.path.join(sub, "links.json")
            else:
                target = os.path.join(sub, stem + ".md")
            res = process_one(fp, target, only=only, include_script=include_script,
                              frontmatter=frontmatter)
            index.append({"input": fp, "output": target,
                          "title": res["title"], "links": len(res["links"]),
                          "tables": len(res["tables"])})
            ok += 1
        except Exception as e:
            index.append({"input": fp, "error": str(e)})
            print(f"⚠️  失败 {fp}: {e}")
    with open(os.path.join(out_dir, "_batch_index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"批量完成：{ok}/{len(files)} 个文件 → {out_dir}（索引 _batch_index.json）")
    return ok


# ───────────────── selftest ─────────────────
def _selftest():
    import tempfile
    fails = []
    html = """<html><head><title>Demo 页面</title>
    <meta name="description" content="一个测试页">
    <meta name="author" content="测试员">
    <base href="https://example.com/a/">
    <script type="application/ld+json">{"@type":"Article","name":"结构化"}</script>
    <script>var ads=99;</script></head>
    <body><script>var x=1;</script>
    <h1>标题一</h1><p>第一段 <a href="/about">关于我们</a></p>
    <table><tr><th>姓名</th><th>金额</th></tr><tr><td>张三</td><td>100</td></tr></table>
    </body></html>"""
    res = parse_html(html, base_url="https://example.com/a/")
    if "Demo 页面" not in res["title"]:
        fails.append("标题提取")
    if "var x=1" in res["text"]:
        fails.append("script 未剔除")
    if "关于我们" not in res["text"]:
        fails.append("链接正文丢失")
    if not res["links"] or res["links"][0].get("href_abs") != "https://example.com/about":
        fails.append(f"相对链接未转绝对: {res['links']}")
    if len(res["tables"]) != 1 or res["tables"][0][1] != ["张三", "100"]:
        fails.append(f"表格提取: {res['tables']}")
    md = to_markdown(res)
    if "# Demo 页面" not in md or "关于我们" not in md:
        fails.append("Markdown 生成")
    # include-script：JSON-LD 被捕获且不串正文；普通 JS 同理
    r2 = parse_html(html, include_script=True)
    if not r2.get("jsonld") or "Article" not in r2["jsonld"][0]:
        fails.append(f"JSON-LD 捕获: {r2.get('jsonld')}")
    if "结构化" in r2["text"] or "var ads" in r2["text"]:
        fails.append("捕获的脚本串入正文")
    if not any("var ads=99" in s for s in r2.get("scripts", [])):
        fails.append("内联脚本未保留")
    md2 = to_markdown(r2, frontmatter=True, source_file="demo.html")
    if not md2.startswith("---\n") or 'title: "Demo 页面"' not in md2 \
            or "source_file:" not in md2 or "结构化数据" not in md2 \
            or '"@type":"Article"' not in md2:
        fails.append("frontmatter/JSON-LD Markdown 输出")
    # base href 生效（不传 base_url 时用页面内 <base>）
    if res["links"][0].get("href_abs") != "https://example.com/about":
        fails.append("base href 未生效")
    # 批量模式
    with tempfile.TemporaryDirectory() as td:
        ind = os.path.join(td, "in"); outd = os.path.join(td, "out")
        os.makedirs(ind)
        for nm in ("a.html", "b.htm"):
            open(os.path.join(ind, nm), "w", encoding="utf-8").write(
                html.replace("Demo 页面", "P-" + nm))
        n = run_batch(ind, outd, only="all", frontmatter=True)
        if n != 2:
            fails.append(f"批量处理数: {n}")
        idx = json.load(open(os.path.join(outd, "_batch_index.json")))
        if len(idx) != 2 or not os.path.exists(os.path.join(outd, "a", "a.md")):
            fails.append("批量产物/索引")
        else:
            head = open(os.path.join(outd, "b", "b.md"), encoding="utf-8").read(600)
            if "---" not in head or "P-b.htm" not in head:
                fails.append("批量 frontmatter 未透传")
    if fails:
        print("selftest FAIL:", fails); return False
    print("selftest PASS: 标题/正文/script剔除/链接绝对化/base标签/表格/Markdown/"
          "JSON-LD捕获/frontmatter/批量目录")
    return True


def main():
    ap = argparse.ArgumentParser(description="本地网页文件数据提取（不联网）")
    ap.add_argument("input", nargs="?")
    ap.add_argument("output", nargs="?")
    ap.add_argument("--only", choices=["text", "tables", "links", "all"], default="all")
    ap.add_argument("--batch", action="store_true",
                    help="input/output 均为目录：批量处理目录下所有 .html/.htm")
    ap.add_argument("-r", "--recursive", action="store_true", help="批量时递归子目录")
    ap.add_argument("--include-script", action="store_true",
                    help="保留内联 JSON-LD/JS 原文（默认剔除 script）")
    ap.add_argument("--frontmatter", action="store_true",
                    help="Markdown 输出加 YAML frontmatter（便于 Obsidian/Hugo/Hexo）")
    ap.add_argument("--base-url", default="", help="手工指定相对链接转绝对所用的页面 URL")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if _selftest() else 1)
    if not a.input or not a.output:
        ap.error("需要 input output（文件或目录），或 --selftest")
    if a.batch:
        try:
            n = run_batch(a.input, a.output, only=a.only,
                          include_script=a.include_script, frontmatter=a.frontmatter,
                          recursive=a.recursive)
        except TierLimitError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        sys.exit(0 if n else 1)
    try:
        res = process_one(a.input, a.output, only=a.only,
                          include_script=a.include_script,
                          frontmatter=a.frontmatter, base_url=a.base_url)
    except TierLimitError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)
    print(f"完成：正文 {len(res['text'])} 字，链接 {len(res['links'])}，"
          f"表格 {len(res['tables'])} → {a.output}")


if __name__ == "__main__":
    main()
