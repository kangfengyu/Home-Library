"""元数据与封面提取：EPUB / CBZ / CBR / PDF，及豆瓣简介（异步、失败优雅降级）。

本模块的解析函数均为同步实现，由 scanner 通过 asyncio.to_thread 在工作线程中调用；
fetch_douban 为异步实现，在事件循环中直接 await。
"""
from __future__ import annotations

import logging
import posixpath
import re
import time
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import config
import database

log = logging.getLogger("metadata")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------- 工具函数


def sniff_image_ext(data: bytes) -> str:
    """按魔数判断图片扩展名。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:3] == b"GIF":
        return "gif"
    return "jpg"


def sniff_image_mime(name: str, data: bytes = b"") -> str:
    ext = Path(name or "").suffix.lower()
    if ext in config.IMAGE_MIME:
        return config.IMAGE_MIME[ext]
    return config.IMAGE_MIME.get("." + sniff_image_ext(data), "image/jpeg")


def natural_key(s: str):
    """自然排序键：chapter2 排在 chapter10 之前。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _image_names(names: list) -> list:
    out = []
    for n in names:
        if n.endswith("/") or "__MACOSX" in n:
            continue
        if Path(n).suffix.lower() in config.IMAGE_EXTS:
            out.append(n)
    out.sort(key=natural_key)
    return out


def _zip_read(zf: zipfile.ZipFile, name: str) -> bytes:
    """读取 zip 成员；对缺失 UTF-8 标志位导致的 cp437 乱码名做修复重试。"""
    try:
        return zf.read(name)
    except KeyError:
        try:
            fixed = name.encode("cp437").decode("utf-8", "ignore")
            return zf.read(fixed)
        except (KeyError, UnicodeError):
            # 最后按无路径基名匹配
            base = posixpath.basename(name).lower()
            for n in zf.namelist():
                if posixpath.basename(n).lower() == base:
                    return zf.read(n)
            raise


def guess_from_filename(name: str) -> tuple:
    """从文件名推断 (书名, 作者)，如 “三体 - 刘慈欣.epub”。"""
    stem = Path(name).stem
    stem = re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", " ", stem)  # 去括号注记
    stem = re.sub(r"\s{2,}", " ", stem).strip()
    m = re.match(r"^(?P<t>.+?)\s*[-–—]\s*(?P<a>[^（）()\[\]]{1,40})$", stem)
    if m:
        return m.group("t").strip(), m.group("a").strip()
    return stem, ""


# ---------------------------------------------------------------- EPUB


def extract_epub(path: Path) -> dict:
    """解析 EPUB（zip + ElementTree，无第三方依赖）：书名/作者/简介/封面。"""
    out: dict = {}
    with zipfile.ZipFile(path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = None
        for el in container.iter():
            if el.tag.rsplit("}", 1)[-1] == "rootfile":
                rootfile = el.get("full-path")
                break
        if not rootfile:
            raise ValueError("container.xml 中缺少 rootfile")

        opf = ET.fromstring(zf.read(rootfile))
        base = posixpath.dirname(rootfile)

        # -- metadata：忽略命名空间，按本地名匹配
        meta_el = None
        for el in opf.iter():
            if el.tag.rsplit("}", 1)[-1] == "metadata":
                meta_el = el
                break
        title = author = desc = ""
        cover_id = None
        if meta_el is not None:
            for el in meta_el.iter():
                tag = el.tag.rsplit("}", 1)[-1]
                text = (el.text or "").strip()
                if tag == "title" and not title:
                    title = text
                elif tag == "creator" and not author:
                    author = text
                elif tag == "description" and not desc:
                    desc = re.sub(r"<[^>]+>", " ", text).strip()
                elif tag == "meta" and (el.get("name") or "").lower() == "cover":
                    cover_id = el.get("content")

        # -- manifest：id -> (href, media-type, properties)
        manifest = {}
        for el in opf.iter():
            if el.tag.rsplit("}", 1)[-1] == "item" and el.get("id") and el.get("href"):
                manifest[el.get("id")] = (
                    el.get("href"),
                    el.get("media-type") or "",
                    el.get("properties") or "",
                )

        if cover_id is None:
            for _id, (_href, _mtype, props) in manifest.items():
                if "cover-image" in props:
                    cover_id = _id
                    break

        href = None
        if cover_id and cover_id in manifest:
            href = manifest[cover_id][0]
        if href is None:
            images = [(h, m) for h, m, _p in manifest.values() if m.startswith("image/")]
            for h, _m in images:  # 优先文件名含 cover 的
                if "cover" in h.lower():
                    href = h
                    break
            if href is None and images:
                href = images[0][0]

        out.update(title=title, author=author, description=desc)
        if href:
            arc = posixpath.normpath(
                posixpath.join(base, urllib.parse.unquote(href)).replace("\\", "/")
            )
            out["cover"] = _zip_read(zf, arc)
    return out


# ---------------------------------------------------------------- 压缩包（CBZ/CBR）


def _extract_archive(path: Path, fmt: str) -> dict:
    out: dict = {}
    if fmt == "cbz":
        with zipfile.ZipFile(path) as zf:
            names = _image_names(zf.namelist())
            out["pages"] = len(names)
            if names:
                out["cover"] = _zip_read(zf, names[0])
        return out
    # cbr
    import rarfile  # 延迟导入，缺失时给出明确错误

    with rarfile.RarFile(str(path)) as rf:
        names = _image_names(rf.namelist())
        out["pages"] = len(names)
        if names:
            try:
                out["cover"] = rf.read(names[0])
            except Exception as e:  # unrar 工具缺失等
                log.warning("CBR 封面提取失败 %s: %s", path.name, e)
        return out


def extract_cbz(path: Path) -> dict:
    return _extract_archive(path, "cbz")


def extract_cbr(path: Path) -> dict:
    return _extract_archive(path, "cbr")


def comic_pages(path: Path, fmt: str) -> list:
    """返回压缩包内按自然排序的图片成员名列表。"""
    if fmt == "cbz":
        with zipfile.ZipFile(path) as zf:
            return _image_names(zf.namelist())
    if fmt == "cbr":
        import rarfile

        with rarfile.RarFile(str(path)) as rf:
            return _image_names(rf.namelist())
    return []


def comic_page(path: Path, fmt: str, index: int) -> tuple:
    """按需提取第 index 张图片，返回 (bytes, mime)。逐页解压，避免整包载入内存。"""
    names = comic_pages(path, fmt)
    if index < 0 or index >= len(names):
        raise IndexError(f"页码越界: {index}/{len(names)}")
    name = names[index]
    if fmt == "cbz":
        with zipfile.ZipFile(path) as zf:
            data = _zip_read(zf, name)
    else:
        import rarfile

        with rarfile.RarFile(str(path)) as rf:
            data = rf.read(name)
    return data, sniff_image_mime(name, data)


# ---------------------------------------------------------------- PDF


def extract_pdf(path: Path) -> dict:
    """优先 PyMuPDF（含首页缩略图），不可用时降级 pypdf（仅元数据）。"""
    out: dict = {}
    try:
        try:
            import pymupdf as mupdf
        except ImportError:
            import fitz as mupdf  # 兼容旧版导入名
        doc = mupdf.open(str(path))
        try:
            out["pages"] = doc.page_count
            md = doc.metadata or {}
            out["title"] = (md.get("title") or "").strip()
            out["author"] = (md.get("author") or "").strip()
            if doc.page_count:
                page = doc[0]
                zoom = max(0.05, config.COVER_WIDTH / max(page.rect.width, 1))
                pix = page.get_pixmap(matrix=mupdf.Matrix(zoom, zoom), alpha=False)
                try:
                    out["cover"] = pix.tobytes("jpg")
                except Exception:
                    out["cover"] = pix.tobytes("png")
        finally:
            doc.close()
    except ImportError:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        out["pages"] = len(reader.pages)
        md = reader.metadata or {}
        out["title"] = (getattr(md, "title", "") or "").strip()
        out["author"] = (getattr(md, "author", "") or "").strip()
    return out


# ---------------------------------------------------------------- 封面保存


def save_cover(book_id: int, data: bytes) -> str:
    ext = sniff_image_ext(data)
    fname = f"{book_id}.{ext}"
    (config.COVER_DIR / fname).write_bytes(data)
    return fname


# ---------------------------------------------------------------- 统一处理入口


def process_book(book_id: int) -> bool:
    """提取单本书的元数据与封面并写库。返回是否成功。"""
    row = database.get_book(book_id)
    if row is None:
        return False
    path = config.LIBRARY_ROOT / row["rel_path"]
    if not path.exists():
        database.execute("UPDATE books SET meta_status=2 WHERE id=?", (book_id,))
        return False

    fmt = (row["format"] or "").lower()
    fields: dict = {}
    try:
        if fmt == "epub":
            info = extract_epub(path)
        elif fmt == "cbz":
            info = extract_cbz(path)
        elif fmt == "cbr":
            info = extract_cbr(path)
        elif fmt == "pdf":
            info = extract_pdf(path)
        else:
            info = {}

        title = (info.get("title") or "").strip()
        author = (info.get("author") or "").strip()
        desc = (info.get("description") or "").strip()
        if not title:
            t, a = guess_from_filename(path.name)
            title, author = t, (author or a)
        fields = {
            "title": title or row["rel_path"],
            "author": author,
            "pages": int(info.get("pages") or 0),
            "description": desc,
            "meta_status": 1,
            "updated_at": time.time(),
        }
        cover = info.get("cover")
        if cover:
            fields["cover_file"] = save_cover(book_id, cover)
        log.info("元数据完成: %s", row["rel_path"])
    except Exception as e:
        t, a = guess_from_filename(path.name)
        fields = {
            "title": t or row["rel_path"],
            "author": a,
            "pages": row["pages"] or 0,
            "description": row["description"] or "",
            "meta_status": 2,
            "updated_at": time.time(),
        }
        log.warning("元数据提取失败 %s: %s", row["rel_path"], e)

    old_cover = row["cover_file"]
    new_cover = fields.get("cover_file", old_cover or "")
    if old_cover and new_cover != old_cover:
        try:
            (config.COVER_DIR / old_cover).unlink()
        except OSError:
            pass

    database.execute(
        "UPDATE books SET title=?, author=?, pages=?, description=?, cover_file=?, "
        "meta_status=?, updated_at=? WHERE id=?",
        (
            fields["title"], fields["author"], fields["pages"], fields["description"],
            new_cover, fields["meta_status"], fields["updated_at"], book_id,
        ),
    )
    return fields["meta_status"] == 1


# ---------------------------------------------------------------- 豆瓣简介（可降级）


async def fetch_douban(title: str, author: str) -> Optional[dict]:
    """请求豆瓣搜索建议接口；被限流/无网络/超时一律返回 None，由调用方降级。"""
    if not title:
        return None
    q = f"{title} {author}".strip()
    try:
        import httpx

        async with httpx.AsyncClient(
            timeout=6, headers={"User-Agent": _UA}, follow_redirects=False
        ) as client:
            r = await client.get(
                "https://www.douban.com/j/search_suggest", params={"q": q}
            )
            if r.status_code != 200:
                return None
            data = r.json()
        for c in data.get("cards") or []:
            result = {
                "title": (c.get("title") or "").strip(),
                "rating": None,
                "description": (c.get("subtitle") or c.get("abstract") or "").strip(),
                "douban_id": "",
                "source": "douban",
            }
            rating = c.get("rating") or c.get("value")
            if rating:
                try:
                    result["rating"] = float(str(rating).replace("分", "").strip())
                except (ValueError, TypeError):
                    pass
            m = re.search(r"subject/(\d+)", c.get("url") or "")
            if m:
                result["douban_id"] = m.group(1)
            if result["title"] or result["description"]:
                return result
        return None
    except Exception:
        return None
