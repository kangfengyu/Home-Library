# -*- coding: utf-8 -*-
"""生成测试用 EPUB：交替的短图页 + 长文字章，用于验证滚动模式切章。"""
import sys
import zipfile
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "stub_mix.epub")

# 1x50 红色 PNG（最小合法 PNG，纯手工字节）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080200000090"
    "7753de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")

chapters = []  # (id, html)
for i in range(12):
    if i % 2 == 0:  # 短图页
        chapters.append((f"c{i}", f'<html><body><p>图页 {i}</p><img src="img.png" style="width:100%"/></body></html>'))
    else:           # 长文字章
        text = "<p>" + (f"这是第 {i} 章的正文内容，用来撑出足够高的滚动章节。" * 40) + "</p>"
        chapters.append((f"c{i}", f"<html><body><h1>第 {i} 章</h1>{text * 6}</body></html>"))

items = "\n".join(
    f'<item id="{cid}" href="{cid}.xhtml" media-type="application/xhtml+xml"/>' for cid, _ in chapters)
spine = "\n".join(f'<itemref idref="{cid}"/>' for cid, _ in chapters)
opf = f'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:br-stub-mix</dc:identifier>
<dc:title>混合测试书</dc:title><dc:creator>测试</dc:creator>
</metadata>
<manifest>{items}<item id="img" href="img.png" media-type="image/png"/>
<item id="nav" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
<spine toc="nav">{spine}</spine></package>'''
ncx = '''<?xml version="1.0"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="urn:uuid:br-stub-mix"/></head>
<docTitle><text>混合测试书</text></docTitle><navMap/></ncx>'''

with zipfile.ZipFile(OUT, "w") as z:
    z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
    z.writestr("META-INF/container.xml",
               '<?xml version="1.0"?><container version="1.0" '
               'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
               '<rootfiles><rootfile full-path="content.opf" '
               'media-type="application/oebps-package+xml"/></rootfiles></container>')
    z.writestr("content.opf", opf)
    z.writestr("toc.ncx", ncx)
    z.writestr("img.png", PNG)
    for cid, html in chapters:
        z.writestr(f"{cid}.xhtml", html)
print(OUT)
