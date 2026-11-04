"""生成测试样例书库：EPUB / PDF / CBZ（含中文路径），用于自检与演示。

用法：python tools/make_samples.py
"""
import posixpath
import struct
import sys
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import config  # noqa: E402

ROOT = config.LIBRARY_ROOT


# ---------- 纯 Python 生成 PNG（无需 Pillow） ----------

def make_png(w: int, h: int, rgb: tuple, marks: int = 0) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter: none
        r0, g0, b0 = rgb
        for x in range(w):
            # 底部叠加深色页码条带，模拟漫画页
            rr, gg, bb = r0, g0, b0
            if marks and y > h - 60:
                rr, gg, bb = int(r0 * 0.4), int(g0 * 0.4), int(b0 * 0.4)
            raw += bytes((rr, gg, bb))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


# ---------- EPUB 构造 ----------

def build_epub(path: Path, title: str, author: str, desc: str, cover_rgb: tuple, chapters=4):
    path.parent.mkdir(parents=True, exist_ok=True)
    cover = make_png(600, 900, cover_rgb)
    ch = []
    for i in range(1, chapters + 1):
        ch.append(
            f"<?xml version='1.0' encoding='utf-8'?>"
            f"<html xmlns='http://www.w3.org/1999/xhtml'><head><title>第{i}章</title></head>"
            f"<body><h1>{title} · 第{i}章</h1>"
            + "".join(f"<p>这是《{title}》第{i}章的第{j}个段落。山川湖海，昼夜星辰，皆入文中。</p>"
                      for j in range(1, 9))
            + "</body></html>"
        )
    manifest = ("".join(
        f"<item id='ch{i}' href='Text/ch{i}.xhtml' media-type='application/xhtml+xml'/>"
        for i in range(1, chapters + 1))
        + "<item id='coverpage' href='Text/cover.xhtml' media-type='application/xhtml+xml'/>"
        + "<item id='cover' href='Images/cover.png' media-type='image/png' properties='cover-image'/>"
        + "<item id='ncx' href='toc.ncx' media-type='application/x-dtbncx+xml'/>")
    spine = "<itemref idref='coverpage'/>" + "".join(
        f"<itemref idref='ch{i}'/>" for i in range(1, chapters + 1))
    # 封面页：img 硬编码 width/height 100%，复现“封面被拉宽拉扁”的阅读器问题
    cover_page = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>封面</title></head>"
        "<body style='margin:0;padding:0'>"
        "<img src='../Images/cover.png' alt='cover' style='width:100%;height:100%;display:block'/>"
        "</body></html>")
    navpoints = "".join(
        f"<navPoint id='np{i}' playOrder='{i}'><navLabel><text>第{i}章</text></navLabel>"
        f"<content src='Text/ch{i}.xhtml'/></navPoint>"
        for i in range(1, chapters + 1))
    opf = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<package xmlns='http://www.idpf.org/2007/opf' version='2.0' unique-identifier='bid'>"
        "<metadata xmlns:dc='http://purl.org/dc/elements/1.1/' xmlns:opf='http://www.idpf.org/2007/opf'>"
        f"<dc:title>{title}</dc:title>"
        f"<dc:creator opf:role='aut'>{author}</dc:creator>"
        f"<dc:description>{desc}</dc:description>"
        "<dc:language>zh-CN</dc:language>"
        f"<dc:identifier id='bid'>urn:uuid:test-{abs(hash(title))}</dc:identifier>"
        "<meta name='cover' content='cover'/>"
        "</metadata>"
        f"<manifest>{manifest}</manifest>"
        f"<spine toc='ncx'>{spine}</spine>"
        "</package>")
    ncx = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<ncx xmlns='http://www.daisy.org/z3986/2005/ncx/' version='2005-1'>"
        "<head/><docTitle><text>" + title + "</text></docTitle>"
        f"<navMap>{navpoints}</navMap></ncx>")

    with zipfile.ZipFile(path, "w") as zf:
        zi = zipfile.ZipInfo("mimetype")
        zf.writestr(zi, "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml",
                    "<?xml version='1.0' encoding='UTF-8'?>"
                    "<container version='1.0' xmlns='urn:oasis:names:tc:opendocument:xmlns:container'>"
                    "<rootfiles><rootfile full-path='OEBPS/content.opf' "
                    "media-type='application/oebps-package+xml'/></rootfiles></container>")
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/toc.ncx", ncx)
        zf.writestr("OEBPS/Text/cover.xhtml", cover_page)
        zf.writestr("OEBPS/Images/cover.png", cover)
        for i, body in enumerate(ch, 1):
            zf.writestr(f"OEBPS/Text/ch{i}.xhtml", body)


# ---------- PDF 构造（PyMuPDF） ----------

def build_pdf(path: Path, title: str, pages: int = 6):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    doc.set_metadata({"title": title, "author": "测试作者", "subject": "样例 PDF"})
    for i in range(pages):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((72, 120), f"{title} - Page {i + 1}", fontsize=24, fontname="china-s")
        page.insert_text((72, 180), f"这是第 {i + 1} 页的示例文字，用于验证在线阅读与缩略图。",
                         fontsize=13, fontname="china-s")
        page.draw_rect(pymupdf.Rect(72, 220, 523, 320), color=(0.3, 0.4, 0.9), width=1.5)
    doc.save(str(path))
    doc.close()


# ---------- CBZ 构造 ----------

def build_cbz(path: Path, colors, size=(900, 1300)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, rgb in enumerate(colors, 1):
            zf.writestr(f"pages/page_{i:03d}.png", make_png(*size, rgb, marks=1))


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    build_epub(ROOT / "文学/小说/三体 - 刘慈欣.epub",
               "三体", "刘慈欣", "文化大革命期间，军方绝秘计划“红岸工程”取得突破，人类第一次收到了来自宇宙的讯号。",
               (30, 41, 89))
    build_epub(ROOT / "文学/小说/百年孤独 - 加西亚·马尔克斯.epub",
               "百年孤独", "加西亚·马尔克斯", "布恩迪亚家族七代人的传奇故事，加勒比海沿岸小镇马孔多的百年兴衰。",
               (120, 53, 15))
    build_epub(ROOT / "技术/Python/流畅的Python - Luciano Ramalho.epub",
               "流畅的Python", "Luciano Ramalho", "By this book, you will learn to write effective, idiomatic Python code.",
               (6, 78, 57))
    build_pdf(ROOT / "技术/架构/系统设计入门.pdf", "系统设计入门")
    build_pdf(ROOT / "文学/散文/浮生六记.pdf", "浮生六记", pages=4)
    build_cbz(ROOT / "漫画/国漫/星海镖师 第1卷.cbz",
              [(196, 78, 82), (86, 140, 200), (240, 200, 90), (94, 190, 120),
               (150, 100, 200), (210, 140, 60), (70, 170, 180), (200, 90, 150)])
    build_cbz(ROOT / "漫画/日漫/海街日记 01.cbz",
              [(120, 180, 140), (200, 160, 120), (90, 120, 180), (180, 100, 100),
               (140, 200, 160), (220, 180, 100)])
    print(f"样例书库已生成: {ROOT}")
    for p in sorted(ROOT.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(ROOT)}  ({p.stat().st_size:,} B)")


if __name__ == "__main__":
    main()
