#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pymupdf>=1.24", "pillow>=10"]
# ///
"""用自己的樣式把題庫 MCP 的題組排成 PDF 題本。

輸入：build_practice_set(include_manifest=true) 的 structuredContent（存成 JSON 檔），
      或整個 tool result（含 structuredContent）也可以。
輸出：PDF。版面全部由樣式檔（JSON）決定，不需要經過網站。

  免安裝（推薦，uv 會自動準備相依套件）：
    uv run https://tiku.ptsfu.org/agent/render_workbook.py manifest.json -o 題本.pdf --title "資料結構題本"
  或自行安裝：
    pip install pymupdf pillow
    python render_workbook.py manifest.json -s style.json -o 題本.pdf --title "資料結構題本"
  樣式範本：https://tiku.ptsfu.org/agent/style.example.json、style.compact.json；-s 也可以直接給網址。

圖片直接從 image.url（img.ptsfu.org，CDN 快取）下載，存在本機快取目錄，
同一題不會重抓；不要改用 get_question include_image（那會逐題經過 MCP、吃限流）。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import pymupdf
except ImportError:  # 舊版只有 fitz 名稱
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:
        pymupdf = None
try:
    from PIL import Image, ImageChops
except ImportError:
    Image = None
if pymupdf is None or Image is None:
    sys.exit("缺少相依套件。擇一：\n"
             "  1. uv run https://tiku.ptsfu.org/agent/render_workbook.py …（自動安裝，推薦）\n"
             "  2. python -m pip install pymupdf pillow 後再執行")

MM = 72 / 25.4
PAGE_SIZES = {"A4": (595.28, 841.89), "Letter": (612, 792)}
IMG_HOST = re.compile(r"^https://img\.ptsfu\.org/[a-z0-9_]{1,150}\.webp$")
QID = re.compile(r"^[a-z0-9_]{1,150}$")

DEFAULT_STYLE: dict = {
    "page": {"size": "A4", "margin_mm": 15},
    "font": {"file": None},
    "image": {"source_dpi": 200, "max_scale": 1.6, "encode": "jpeg", "jpeg_quality": 88},
    "cover": {
        "enabled": True,
        "title": "{title}",
        "subtitle": "{date}　共 {total} 題",
        "show_unit_counts": True,
        "show_question_list": True,
        "note": "",
    },
    "header": [
        {"template": "{seq}. {unit}", "size": 13, "color": "#1d4ed8"},
        {"template": "{attribution}", "size": 12, "color": "#111111"},
        {"template": "{text}", "size": 9, "color": "#777777", "max_chars": 80},
    ],
    "rule": {"enabled": True, "color": "#bbbbbb", "width": 0.8},
    "layout": {"mode": "one_per_page", "gap_mm": 8},
    "answer_space": {"lines": 0, "line_gap_mm": 9, "color": "#dddddd"},
    "footer": {"left": "{category} / {source_pdf}", "right": "{seq}/{total}", "size": 8, "color": "#999999"},
}


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


class SafeDict(dict):
    def __missing__(self, key):  # 樣式寫錯欄位名稱時留空，不中斷
        return ""


def fill(template: str, ctx: dict, max_chars: int | None = None) -> str:
    s = template.format_map(SafeDict(ctx)).strip()
    if max_chars and len(s) > max_chars:
        s = s[: max_chars - 1] + "…"
    return s


# ── 下載（有本機快取＋ETag 重新驗證） ───────────────────────────────
def cache_dir() -> Path:
    d = Path(os.environ.get("TIKU_CACHE", Path.home() / ".cache" / "tiku"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_image(qid: str, url: str, revalidate: bool) -> bytes:
    if not QID.match(qid) or not IMG_HOST.match(url):
        raise ValueError(f"不接受的圖片網址：{url}")
    body, tag = cache_dir() / f"{qid}.webp", cache_dir() / f"{qid}.etag"
    if body.exists() and not revalidate:
        return body.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "tiku-render-workbook/1"})
    if body.exists() and tag.exists():
        req.add_header("If-None-Match", tag.read_text().strip())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
                raise ValueError(f"{qid} 不是 WebP")
            body.write_bytes(data)
            if r.headers.get("ETag"):
                tag.write_text(r.headers["ETag"])
            return data
    except urllib.error.HTTPError as e:
        if e.code == 304 and body.exists():
            return body.read_bytes()
        raise


def convert(webp: bytes, encode: str = "jpeg", quality: int = 88) -> tuple[bytes, int, int]:
    """WebP → JPEG／PNG（多數 PDF 工具不吃 WebP）。灰階掃描轉成單通道，檔案更小。"""
    im = Image.open(io.BytesIO(webp))
    im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
    if im.mode == "RGB":
        r, g, b = im.split()
        if max(ImageChops.difference(r, g).getextrema()[1], ImageChops.difference(g, b).getextrema()[1]) <= 12:
            im = im.convert("L")
    buf = io.BytesIO()
    if encode == "png":
        im.save(buf, "PNG", optimize=True)
    else:
        im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue(), im.width, im.height


# ── 排版 ─────────────────────────────────────────────────────────────
class Renderer:
    def __init__(self, style: dict):
        self.s = style
        self.W, self.H = PAGE_SIZES[style["page"]["size"]]
        self.M = style["page"]["margin_mm"] * MM
        self.doc = pymupdf.open()
        # 文字用 HTML box 排：自動換行、量高度，中英混排有正確的字距；
        # 沒指定字型時由 PyMuPDF 內建的 CJK fallback 字型處理中文。
        font_file = style["font"].get("file")
        self.archive, self.family = None, "sans-serif"
        if font_file:
            fp = Path(font_file).expanduser()
            self.archive = pymupdf.Archive(str(fp.parent))
            self.family = "tikufont"
            self.font_face = f"@font-face {{font-family: tikufont; src: url({fp.name});}}"
        else:
            self.font_face = ""

    def new_page(self):
        return self.doc.new_page(width=self.W, height=self.H)

    def text(self, page, y: float, s: str, size: float, color: str, x0=None, x1=None, align=0) -> float:
        """在 y 處寫一段可自動換行的文字，回傳寫完後的 y。"""
        if not s:
            return y
        x0 = self.M if x0 is None else x0
        x1 = self.W - self.M if x1 is None else x1
        rect = pymupdf.Rect(x0, y, x1, self.H)
        css = (f"{self.font_face} body, p {{font-family: {self.family}; font-size: {size}pt; color: {color};"
               f" line-height: 1.3; margin: 0; padding: 0; text-align: {['left', 'center', 'right'][align]};}}")
        html = "<p>" + s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</p>"
        spare, _ = page.insert_htmlbox(rect, html, css=css, archive=self.archive, scale_low=1)
        if spare < 0:
            raise ValueError(f"文字放不下：{s[:30]}")
        return y + (rect.height - spare) + size * 0.35

    def cover(self, items: list[dict], meta: dict):
        c = self.s["cover"]
        p = self.new_page()
        y = self.M + 60
        y = self.text(p, y, fill(c["title"], meta), 26, "#111111")
        y = self.text(p, y, fill(c["subtitle"], meta), 12, "#666666")
        if c.get("note"):
            y = self.text(p, y, fill(c["note"], meta), 11, "#b91c1c")
        p.draw_line((self.M, y + 4), (self.W - self.M, y + 4), color=(0, 0, 0), width=1.2)
        y += 18
        limit = self.H - self.M - 40
        if c.get("show_unit_counts"):
            counts = collections.Counter(q["unit"] for q in items).most_common()
            for k, (unit, n) in enumerate(counts):
                if y > limit:
                    y = self.text(p, y, f"…另有 {len(counts) - k} 個單元", 10, "#999999")
                    break
                self.text(p, y, unit, 11, "#1d4ed8", x1=self.W - self.M - 60)
                y = self.text(p, y, f"{n} 題", 11, "#444444", x0=self.W - self.M - 60, align=2)
            y += 10
        if c.get("show_question_list"):
            for k, q in enumerate(items):
                if y > limit:
                    if k and y < self.H - self.M:
                        self.text(p, y, f"…共 {len(items)} 題，其餘略", 9, "#999999")
                    break
                y = self.text(p, y, f"{q['seq']}. {q['attribution']}　{q['unit']}", 9, "#444444")

    def question_block_height(self, img_h: float) -> float:
        a = self.s["answer_space"]
        return img_h + (a["lines"] * a["line_gap_mm"] * MM if a["lines"] else 0)

    def draw_question(self, page, y: float, q: dict, png: bytes, pw: int, ph: int, fit_bottom: float) -> float:
        for line in self.s["header"]:
            y = self.text(page, y, fill(line["template"], q, line.get("max_chars")), line["size"], line["color"])
        r = self.s["rule"]
        if r["enabled"]:
            page.draw_line((self.M, y + 2), (self.W - self.M, y + 2), color=rgb(r["color"]), width=r["width"])
            y += 8
        im = self.s["image"]
        scale = min(72 / im["source_dpi"] * im["max_scale"], (self.W - 2 * self.M) / pw)
        a = self.s["answer_space"]
        answer_h = a["lines"] * a["line_gap_mm"] * MM if a["lines"] else 0
        room = fit_bottom - y - answer_h
        scale = min(scale, room / ph) if room > 0 else scale
        w, h = pw * scale, ph * scale
        x = (self.W - w) / 2
        page.insert_image(pymupdf.Rect(x, y, x + w, y + h), stream=png)
        y += h + 6
        for _ in range(a["lines"]):
            y += a["line_gap_mm"] * MM
            page.draw_line((self.M, y), (self.W - self.M, y), color=rgb(a["color"]), width=0.5)
        return y

    def footer(self, page, q: dict):
        f = self.s["footer"]
        y = self.H - self.M + 2
        if f.get("left"):
            self.text(page, y, fill(f["left"], q), f["size"], f["color"], x1=self.W * 0.75)
        if f.get("right"):
            self.text(page, y, fill(f["right"], q), f["size"], f["color"], x0=self.W * 0.75, align=2)

    def header_height(self, q: dict) -> float:
        tmp = pymupdf.open()
        p = tmp.new_page(width=self.W, height=self.H)
        y = 0.0
        for line in self.s["header"]:
            y = self.text(p, y, fill(line["template"], q, line.get("max_chars")), line["size"], line["color"])
        return y + (8 if self.s["rule"]["enabled"] else 0)

    def render(self, items: list[dict], images: dict[str, tuple[bytes, int, int]], meta: dict) -> bytes:
        if self.s["cover"]["enabled"]:
            self.cover(items, meta)
        bottom = self.H - self.M - 14
        mode = self.s["layout"]["mode"]
        gap = self.s["layout"]["gap_mm"] * MM
        page, y = None, 0.0
        for q in items:
            png, pw, ph = images[q["question_id"]]
            if mode == "flow" and page is not None:
                im = self.s["image"]
                scale = min(72 / im["source_dpi"] * im["max_scale"], (self.W - 2 * self.M) / pw)
                need = self.header_height(q) + self.question_block_height(ph * scale) + 6
                if y + gap + need > bottom:
                    page = None
                else:
                    y += gap
            if page is None:
                page, y = self.new_page(), self.M
            y = self.draw_question(page, y, q, png, pw, ph, bottom)
            self.footer(page, q)
            if mode != "flow":
                page = None
        try:
            self.doc.subset_fonts()  # 每個文字框都會嵌一次 CJK 字型；只留用到的字、合併重複
        except Exception as e:  # 少數 CFF／TTC 字型無法子集化，PDF 仍可用但較大
            print(f"警告：字型子集化失敗（{e}），PDF 會比較大；建議改用單一字面的 .ttf／.otf", file=sys.stderr)
        return self.doc.tobytes(garbage=4, deflate=True)


def load_style(src: str) -> dict:
    """樣式檔可以是本機路徑，或 https://tiku.ptsfu.org/agent/ 底下的範本網址。"""
    if src.startswith("https://"):
        if not src.startswith("https://tiku.ptsfu.org/agent/"):
            raise SystemExit("樣式網址只接受 https://tiku.ptsfu.org/agent/ 底下的範本；其他請先下載成本機檔案")
        req = urllib.request.Request(src, headers={"User-Agent": "tiku-render-workbook/1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads(Path(src).read_text(encoding="utf-8"))


def load_manifest(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data = data.get("structuredContent", data)
    if "questions" not in data or not data["questions"]:
        raise SystemExit("找不到 questions：請存 build_practice_set(include_manifest=true) 的結果")
    if not all("image" in q for q in data["questions"]):
        raise SystemExit("題目缺 image：呼叫 build_practice_set 時要加 include_manifest=true")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest")
    ap.add_argument("-s", "--style", help="樣式檔（JSON），沒寫的欄位用預設值")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--title", default="考古題題本")
    ap.add_argument("--note", default=None, help="封面說明（覆蓋樣式檔的 cover.note）")
    ap.add_argument("--revalidate", action="store_true", help="用 ETag 向 CDN 確認快取圖片是否更新")
    a = ap.parse_args(argv)

    style = merge(DEFAULT_STYLE, load_style(a.style) if a.style else {})
    if a.note is not None:
        style["cover"]["note"] = a.note
    data = load_manifest(a.manifest)
    qs = data["questions"]
    total = len(qs)
    items = []
    for n, q in enumerate(qs, 1):
        items.append({
            **q, "seq": n, "total": total,
            "unit": (q.get("original_units") or ["未分類"])[0],
            "units": " · ".join(q.get("original_units") or []),
            "subjects": " · ".join(q.get("subjects") or []),
            "source_pdf": (q.get("provenance") or {}).get("source_pdf") or "",
            "pages": ",".join(map(str, (q.get("provenance") or {}).get("pages") or [])),
            "year": q.get("year"), "school": q.get("school"),
        })
    today = dt.date.today()
    meta = {"title": a.title, "date": f"{today:%Y-%m-%d}（{'一二三四五六日'[today.weekday()]}）", "total": total,
            "revision": data.get("revision", "")[:12]}

    def get(q):
        raw = fetch_image(q["question_id"], q["image"]["url"], a.revalidate)
        return q["question_id"], convert(raw, style["image"]["encode"], style["image"]["jpeg_quality"])

    with ThreadPoolExecutor(max_workers=6) as ex:
        images = dict(ex.map(get, qs))
    pdf = Renderer(style).render(items, images, meta)
    out = a.out or f"題本_{today:%Y%m%d}_{total}題.pdf"
    Path(out).write_bytes(pdf)
    print(f"{out}：{total} 題，{len(pdf) // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
