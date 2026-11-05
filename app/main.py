"""书房 · 本地电子书/漫画管理与在线阅读系统 —— FastAPI 入口。

运行方式：
    python app/main.py        # 或 python run.py / start.bat
访问：
    http://localhost:5000
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sys
import time
import xml.sax.saxutils as saxutils
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

# 支持 `python app/main.py` 直接运行（把本目录加入 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

config.ensure_dirs()

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import database
import metadata
import scanner
import tagger

COOKIE_NAME = "br_session"

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(config.DATA_DIR / "server.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")
# Windows 控制台默认 GBK，避免打印中文/Unicode 时崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 路径安全


def _resolve_rel(rel: str) -> Path:
    """把库内相对路径解析为绝对路径，严格阻止越权访问（目录穿越）。"""
    root = config.LIBRARY_ROOT
    rel_norm = (rel or "").strip().replace("\\", "/").lstrip("/")
    if any(seg in ("..", "") for seg in rel_norm.split("/") if seg == ".."):
        raise HTTPException(status_code=403, detail="路径越权：禁止访问书库以外的内容")
    p = (root / rel_norm).resolve()
    root_s = os.path.normcase(str(root))
    p_s = os.path.normcase(str(p))
    if p_s != root_s and not p_s.startswith(root_s + os.sep):
        raise HTTPException(status_code=403, detail="路径越权：禁止访问书库以外的内容")
    return p


def _resolve_book(book_id: int, request: Request = None):
    row = database.get_book(book_id)
    if row is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    # 私有书（暗格）：非管理员一律视作不存在（不可见、不可查、不可读）
    if row["is_private"]:
        user = _current_user(request) if request else None
        if user is None or not user["is_admin"]:
            raise HTTPException(status_code=404, detail="书籍不存在")
    path = _resolve_rel(row["rel_path"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件已不存在，请重新扫描书库")
    return row, path


def _book_json(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"] or row["rel_path"],
        "author": row["author"] or "",
        "format": row["format"],
        "size": row["size"] or 0,
        "pages": row["pages"] or 0,
        "rating": row["rating"],
        "description": row["description"] or "",
        "meta_status": row["meta_status"],
        "meta_source": row["meta_source"] or "",
        "path": row["rel_path"],
        "is_private": bool(row["is_private"]),
        "tags": (json.loads(row["tags"]) if row["tags"] else []),
        "cover_url": f"/api/cover/{row['id']}",
        "reader_url": f"/reader?id={row['id']}",
    }


# ---------------------------------------------------------------- 生命周期


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    database.speed_mark_interrupted()     # 上次运行遗留的进行中任务 → error（防僵尸）
    await scanner.start()
    scan_task = asyncio.create_task(scanner.scan_library())
    yield
    scan_task.cancel()
    await scanner.stop()
    database.close()


app = FastAPI(title="书房 · 本地书库", lifespan=lifespan)


# ---------------------------------------------------------------- 页面路由


_NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", response_class=HTMLResponse)
def page_index():
    return FileResponse(STATIC_DIR / "index.html", headers=_NO_CACHE)


@app.get("/reader", response_class=HTMLResponse)
def page_reader():
    return FileResponse(STATIC_DIR / "reader.html", headers=_NO_CACHE)


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------- 数据 API


@app.get("/api/explore")
def api_explore(request: Request, path: str = "", limit: int = 200, offset: int = 0):
    """列出指定目录下的子文件夹与书籍文件。path 为库内相对路径。

    书籍分页（默认 200/批 + total 总数）：几千本平铺在同一目录时
    避免一次性返回/渲染导致卡顿，前端点「加载更多」取下一批。
    """
    viewer = _current_user(request)
    d = _resolve_rel(path)
    if not d.exists():
        raise HTTPException(status_code=404, detail="目录不存在")
    if not d.is_dir():
        raise HTTPException(status_code=400, detail="目标不是目录")

    folders, books = [], []
    try:
        entries = sorted(d.iterdir(), key=lambda e: e.name)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"目录读取失败: {e}")

    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        if entry.is_dir():
            rel = entry.relative_to(config.LIBRARY_ROOT).as_posix()
            folders.append({"name": name, "path": rel})
        elif entry.suffix.lower() in config.BOOK_EXTS:
            rel = entry.relative_to(config.LIBRARY_ROOT).as_posix()
            row = database.get_book_by_path(rel)
            if row is not None and not (row["is_private"] and not (viewer and viewer["is_admin"])):
                books.append(_book_json(row))
    rel = d.relative_to(config.LIBRARY_ROOT).as_posix()
    total = len(books)
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    books = books[offset:offset + limit]
    return {"path": "" if rel == "." else rel,
            "root": str(config.LIBRARY_ROOT), "folders": folders, "books": books,
            "total": total}


@app.get("/api/book/{book_id}")
def api_book(book_id: int, request: Request):
    row, _path = _resolve_book(book_id, request)
    return _book_json(row)


@app.get("/api/cover/{book_id}")
def api_cover(book_id: int, request: Request):
    """封面：有缓存图片返回图片；否则返回服务端生成的 SVG 占位封面。"""
    row = database.get_book(book_id)
    if row is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    if row["is_private"]:
        user = _current_user(request)
        if user is None or not user["is_admin"]:
            raise HTTPException(status_code=404, detail="书籍不存在")
    cover_file = row["cover_file"]
    if cover_file:
        f = config.COVER_DIR / cover_file
        if f.exists():
            return FileResponse(f, media_type=metadata.sniff_image_mime(cover_file),
                                headers={"Cache-Control": "max-age=86400"})
    svg = _placeholder_svg(row["title"] or row["rel_path"], row["format"], book_id)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


@app.get("/api/file/{book_id}")
def api_file(book_id: int, request: Request):
    """返回原始书籍文件（Starlette FileResponse 自带 Range 支持，PDF.js 可断点取页）。"""
    _row, path = _resolve_book(book_id, request)
    return FileResponse(path, filename=path.name)


@app.get("/api/comic/{book_id}/pages")
def api_comic_pages(book_id: int, request: Request):
    row, path = _resolve_book(book_id, request)
    try:
        names = metadata.comic_pages(path, row["format"])
    except Exception as e:
        msg = str(e)
        if "rar" in msg.lower() or row["format"] == "cbr":
            msg = ("CBR 需要系统安装 UnRAR 工具（rarfile 无法调用解压后端）。"
                   "建议将文件转换为 CBZ 格式后使用。")
        return {"count": 0, "error": msg}
    return {"count": len(names)}


@app.get("/api/comic/{book_id}/page/{index}")
def api_comic_page(book_id: int, index: int, request: Request):
    """按需解压单页图片，避免整包载入内存（防 OOM）。"""
    row, path = _resolve_book(book_id, request)
    try:
        data, mime = metadata.comic_page(path, row["format"], index)
    except IndexError:
        raise HTTPException(status_code=404, detail="页码超出范围")
    except Exception as e:
        msg = str(e)
        if row["format"] == "cbr" or "rar" in msg.lower():
            msg = "CBR 需要系统安装 UnRAR 工具，建议转换为 CBZ。"
        raise HTTPException(status_code=500, detail=msg)
    return Response(data, media_type=mime, headers={"Cache-Control": "max-age=86400"})


@app.post("/api/scan")
async def api_scan():
    asyncio.create_task(scanner.scan_library())
    return {"started": True}


@app.delete("/api/books/{book_id}")
def api_delete_book(request: Request, book_id: int):
    """管理员删书：删除书籍原文件 + 封面缓存 + 全部关联数据（不可恢复）。
    荐学课程里指向该书的步骤自动退回「待入库」态。"""
    _require_admin(request)
    row = database.get_book(book_id)
    if row is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    rel_path, cover_file = row["rel_path"], row["cover_file"] or ""
    database.delete_book(book_id)          # 先清记录（此后 rel_path 仅存于本函数局部）
    file_deleted = False
    try:
        p = _resolve_rel(rel_path)
        if p.exists():
            p.unlink()
            file_deleted = True
    except OSError:
        pass
    if cover_file:
        try:
            c = config.COVER_DIR / cover_file
            if c.exists():
                c.unlink()
        except OSError:
            pass
    return {"ok": True, "file_deleted": file_deleted}


_ALLOWED_BOOK_EXT = config.BOOK_EXTS          # 上传与扫描同一份格式白名单（mobi/azw3 仅收录不可读）
# 选书器（费曼/AI速读）只列可在线阅读的书：mobi/azw3 无法提取正文，选了也无法考核/精读
_READABLE_SQL = "(" + ",".join(f"'{f}'" for f in sorted(config.READABLE_FORMATS)) + ")"


def _safe_upload_name(name: str) -> str:
    """取基名并清洗非法字符，防路径穿越。"""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip() or "upload.bin"
    return name


@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    """上传书籍（管理员）：流式落盘并计算 sha256；内容重复返回 409 提示书库已有。"""
    _require_admin(request)
    raw = _safe_upload_name(file.filename)
    ext = os.path.splitext(raw)[1].lower()
    if ext not in _ALLOWED_BOOK_EXT:
        raise HTTPException(status_code=400, detail="仅支持 EPUB / PDF / CBZ / CBR / MOBI / AZW3（后两者仅收录，不可在线阅读）")
    # 1) 先写隐藏临时文件（.part 扩展名不会被扫描收录），边写边算内容 hash
    tmp = config.LIBRARY_ROOT / f".{raw}.{os.getpid()}.part"
    size = 0
    sha = hashlib.sha256()
    try:
        with open(tmp, "wb") as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                size += len(chunk)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    finally:
        await file.close()
    # 2) 内容查重：同 hash 直接拒绝并告知已有书目（临时文件删除）
    dup = database.find_by_hash(sha.hexdigest())
    if dup:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=409, detail={
            "msg": "书库已有此书，无需重复上传",
            "existing": {"id": dup["id"], "title": dup["title"] or "",
                         "author": dup["author"] or "", "path": dup["rel_path"]}})
    # 3) 内容不同：落正式文件名（同名不同内容自动加序号），同目录原子改名
    dest = config.LIBRARY_ROOT / raw
    stem = os.path.splitext(raw)[0]
    i = 1
    while dest.exists():
        dest = config.LIBRARY_ROOT / f"{stem}({i}){ext}"
        i += 1
    os.replace(tmp, dest)
    rel = dest.relative_to(config.LIBRARY_ROOT).as_posix()
    scanner.remember_hash(rel, sha.hexdigest())     # 扫描入库后直填，免重算
    asyncio.create_task(scanner.scan_library())     # 上传完自动扫描入库
    return {"ok": True, "name": dest.name, "size": size}


@app.get("/api/scan/status")
def api_scan_status():
    return dict(scanner.state)


@app.get("/api/search")
def api_search(request: Request, q: str = Query(..., min_length=1)):
    viewer = _current_user(request)
    like = f"%{q.strip()}%"
    rows = database.query_all(
        "SELECT * FROM books WHERE title LIKE ? OR author LIKE ? OR rel_path LIKE ? "
        "ORDER BY title LIMIT 200",
        (like, like, like),
    )
    rows = [r for r in rows if not (r["is_private"] and not (viewer and viewer["is_admin"]))]
    return {"query": q, "count": len(rows), "books": [_book_json(r) for r in rows]}


@app.get("/api/books/pick")
def api_books_pick(request: Request, q: str = Query("", max_length=80)):
    """选书器数据（费曼/AI速读共用）：q 为空返回最近阅读+最近入库，否则返回搜索结果。"""
    user = _require_user(request)

    def _light(r):
        return {"id": r["id"], "title": r["title"] or "", "author": r["author"] or "",
                "format": r["format"] or "", "size": r["size"] or 0}

    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        rows = database.query_all(
            "SELECT id, title, author, format, size, is_private FROM books "
            "WHERE format IN " + _READABLE_SQL +
            " AND (title LIKE ? OR author LIKE ? OR rel_path LIKE ?) "
            "ORDER BY title LIMIT 30", (like, like, like))
        rows = [r for r in rows
                if not (r["is_private"] and not (user and user["is_admin"]))]
        return {"q": q, "books": [_light(r) for r in rows], "recent": [], "latest": []}

    # 默认列表：本人最近阅读（≤8）+ 最近入库（≤12，暗格按权限过滤）
    recent = database.query_all(
        "SELECT b.id, b.title, b.author, b.format, b.size FROM progress p "
        "JOIN books b ON b.id = p.book_id WHERE p.user_id=? "
        "ORDER BY p.updated_at DESC LIMIT 8", (user["id"],))
    latest = database.query_all(
        "SELECT id, title, author, format, size, is_private FROM books "
        "WHERE format IN " + _READABLE_SQL +
        " ORDER BY created_at DESC, id DESC LIMIT 12")
    latest = [r for r in latest if not (r["is_private"] and not user["is_admin"])]
    # 最近入库里去掉与最近阅读重复的（默认列表不重复出现）
    seen_ids = {r["id"] for r in recent}
    latest = [r for r in latest if r["id"] not in seen_ids][:8]
    return {"q": "", "books": [], "recent": [_light(r) for r in recent],
            "latest": [_light(r) for r in latest]}


# ---------------------------------------------------------------- 认证与多账户


def _current_user(request: Request):
    return database.get_session_user(request.cookies.get(COOKIE_NAME))


def _require_user(request: Request):
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _require_admin(request: Request):
    user = _require_user(request)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


@app.post("/api/auth/login")
def api_login(username: str = Body(...), password: str = Body(...)):
    row = database.verify_login(username, password)
    if row is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = database.create_session(row["id"])
    resp = JSONResponse({"id": row["id"], "username": row["username"],
                         "is_admin": bool(row["is_admin"])})
    resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=90 * 24 * 3600)
    return resp


@app.post("/api/auth/logout")
def api_logout(request: Request):
    database.delete_session(request.cookies.get(COOKIE_NAME))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/auth/me")
def api_me(request: Request):
    user = _current_user(request)
    if user is None:
        return {"user": None}
    return {"user": {"id": user["id"], "username": user["username"],
                     "is_admin": bool(user["is_admin"])}}


@app.get("/api/auth/users")
def api_users(request: Request):
    _require_admin(request)
    return {"users": [dict(r) for r in database.list_users()]}


@app.post("/api/auth/users")
def api_add_user(request: Request, username: str = Body(...), password: str = Body(...),
                 is_admin: bool = Body(False)):
    _require_admin(request)
    try:
        uid = database.create_user(username, password, is_admin=1 if is_admin else 0)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": uid, "username": username.strip()}


@app.post("/api/auth/password")
def api_change_password(request: Request, new_password: str = Body(...),
                        old_password: str = Body(""), uid: int = Body(0)):
    """修改密码：本人改需验证旧密码（任何登录用户可用）；
    带 uid 且非本人时仅管理员可重置他人密码（无需旧密码）。"""
    user = _require_user(request)
    if uid and uid != user["id"]:
        _require_admin(request)
        target = database.query_one("SELECT id, username FROM users WHERE id=?", (uid,))
        if target is None:
            raise HTTPException(status_code=404, detail="账户不存在")
    else:
        target = user
        if database.verify_login(user["username"], old_password) is None:
            raise HTTPException(status_code=403, detail="旧密码不正确")
    try:
        database.set_user_password(target["id"], new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.delete("/api/auth/users/{uid}")
def api_delete_user(request: Request, uid: int):
    """管理员删除账户（连带其全部数据）；不能删除自己，避免失去管理入口。"""
    user = _require_admin(request)
    if uid == user["id"]:
        raise HTTPException(status_code=400, detail="不能删除当前登录的账户")
    if database.query_one("SELECT id FROM users WHERE id=?", (uid,)) is None:
        raise HTTPException(status_code=404, detail="账户不存在")
    database.delete_user(uid)
    return {"ok": True}


# ---------------------------------------------------------------- 问题（Q&A）


@app.get("/api/questions")
def api_questions(request: Request):
    user = _require_user(request)
    rows = database.list_questions(user["id"])
    return {"count": len(rows), "questions": [dict(r) for r in rows]}


@app.post("/api/questions")
def api_add_question(request: Request, question: str = Body(...), answer: str = Body("")):
    user = _require_user(request)
    question = (question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")
    return {"id": database.add_question(user["id"], question, (answer or "").strip())}


@app.put("/api/questions/{qid}")
def api_update_question(request: Request, qid: int,
                        question: Optional[str] = Body(None), answer: Optional[str] = Body(None)):
    user = _require_user(request)
    if not database.update_question(user["id"], qid, question, answer):
        raise HTTPException(status_code=404, detail="问题不存在")
    return {"ok": True}


@app.delete("/api/questions/{qid}")
def api_delete_question(request: Request, qid: int):
    user = _require_user(request)
    if not database.delete_question(user["id"], qid):
        raise HTTPException(status_code=404, detail="问题不存在")
    return {"ok": True}


# ---------------------------------------------------------------- LLM 设置（AI 功能前置）

_LLM_SETTING_KEY = "llm"

# 调用逻辑抽到 llm.py（速读流水线复用）；环境变量可覆盖设置页配置
from llm import llm_cfg as _llm_cfg, llm_request as _llm_request  # noqa: E402


@app.get("/api/settings/llm")
def api_get_llm(request: Request):
    _require_admin(request)
    cfg = _llm_cfg()
    key = cfg.pop("api_key")
    cfg["api_key_set"] = bool(key)
    cfg["api_key_hint"] = (key[:3] + "…" + key[-4:]) if len(key) > 8 else ("…" if key else "")
    return cfg


@app.put("/api/settings/llm")
def api_put_llm(request: Request, protocol: str = Body(...), base_url: str = Body(...),
                model: str = Body(...), api_key: str = Body("")):
    _require_admin(request)
    protocol = (protocol or "").strip().lower()
    if protocol not in ("anthropic", "openai"):
        raise HTTPException(status_code=400, detail="协议仅支持 anthropic / openai")
    key = (api_key or "").strip() or _llm_cfg()["api_key"]   # key 留空沿用已保存值
    database.set_setting(_LLM_SETTING_KEY, json.dumps({
        "protocol": protocol, "base_url": (base_url or "").strip(),
        "model": (model or "").strip(), "api_key": key}, ensure_ascii=False))
    return {"ok": True}


@app.post("/api/settings/llm/test")
def api_test_llm(request: Request):
    _require_admin(request)
    cfg = _llm_cfg()
    if not cfg["base_url"] or not cfg["model"]:
        raise HTTPException(status_code=400, detail="请先填写接口地址与模型名")
    try:
        reply = _llm_request(cfg, [{"role": "user", "content": "只回复两个字：OK"}],
                             max_tokens=16, timeout=25)
        return {"ok": True, "reply": (reply or "(空回复)")[:120]}
    except HTTPException as e:
        return {"ok": False, "error": e.detail}


# ---------------------------------------------------------------- 费曼考核（讲书给 AI 小白听）

def _fy_session_json(row) -> dict:
    return {
        "id": row["id"], "book_id": row["book_id"], "lang": row["lang"] or "zh",
        "status": row["status"], "score": row["score"],
        "verdict": row["verdict"] or "",
        "weak": (json.loads(row["weak"]) if row["weak"] else []),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "book_title": row["book_title"] or "", "book_author": row["book_author"] or "",
    }


def _fy_system_prompt(title: str, author: str, desc: str, lang: str) -> str:
    out = "English" if lang == "en" else "中文"
    p = (
        "你是「费曼考核」里的学员小白：聪明好学，但对这本书的内容完全不了解。"
        f"用户正在用费曼技巧给你讲解《{title}》"
        + (f"（作者：{author}）" if author else "") + "。\n"
        "你的任务：扮演小白听讲并不断追问，帮用户暴露自己没讲明白的地方。要求：\n"
        "1. 每次回复先用一两句自己的话复述你的理解（可以是理解错的），再提出 1-2 个追问；"
        "追问要打在要害上——概念之间为什么这样联系、边界情况、反例、跟你已知常识矛盾的地方。\n"
        "2. 听不懂是常态：用户没讲清楚就直说没听懂，指出具体哪里含糊，请他换个说法再讲一遍。\n"
        "3. 语气好奇自然，像真实初学者；不要客套，不要长篇大论，每次回复不超过 120 字。\n"
        "4. 不要替用户总结全书，不要输出清单式长答案，不要跳出小白角色。\n"
        "5. 全程使用" + out + "。"
    )
    if desc:
        p += f"\n（本书简介供你参考，用于提出有质量的问题，不要照念给用户：{desc[:400]}）"
    return p


def _fy_anthropic_fix(messages: list) -> list:
    """anthropic 协议要求首条消息必须是 user；会话以小白的开场白开头时补一个引导轮次。"""
    if messages and messages[0]["role"] == "assistant":
        return [{"role": "user", "content": "（我选好了书，请开始你的开场提问。）"}] + messages
    return messages


@app.get("/api/feynman/books")
def api_feynman_books(request: Request):
    """考核选书下拉框：当前用户可见的全部书（最近更新的在前）。"""
    viewer = _current_user(request)
    rows = database.query_all(
        "SELECT id, title, author, is_private FROM books WHERE format IN " + _READABLE_SQL +
        " ORDER BY updated_at DESC LIMIT 500")
    rows = [r for r in rows if not (r["is_private"] and not (viewer and viewer["is_admin"]))]
    return {"count": len(rows),
            "books": [{"id": r["id"], "title": r["title"] or "", "author": r["author"] or ""}
                      for r in rows]}


@app.get("/api/feynman/sessions")
def api_feynman_sessions(request: Request):
    user = _require_user(request)
    rows = database.feynman_sessions(user["id"])
    return {"count": len(rows), "sessions": [_fy_session_json(r) for r in rows]}


@app.post("/api/feynman/sessions")
async def api_feynman_create(request: Request, book_id: int = Body(...),
                             lang: str = Body("zh")):
    user = _require_user(request)
    row, _path = _resolve_book(book_id, request)   # 404/私有书权限校验
    lang = "en" if (lang or "").lower() == "en" else "zh"
    cfg = _llm_cfg()
    greeting = await asyncio.to_thread(
        _llm_request, cfg,
        [{"role": "user", "content":
          f"（用户刚选了《{row['title']}》这本书，还没开始讲。"
          "请以小白的身份开场：表示好奇并请用户开始讲，一两句话即可。）"}],
        system=_fy_system_prompt(row["title"] or "", row["author"] or "",
                                 row["description"] or "", lang),
        max_tokens=500)
    if not greeting:
        greeting = "你好！我想听听这本书讲了什么，请你开始讲给我听吧。"
    sid = database.create_feynman_session(user["id"], book_id, lang)
    database.add_feynman_message(sid, "assistant", greeting)
    s = database.get_feynman_session(user["id"], sid)
    out = _fy_session_json(s)
    return {"session": out,
            "messages": [{"role": "assistant", "content": greeting}]}


@app.get("/api/feynman/sessions/{sid}")
def api_feynman_detail(request: Request, sid: int):
    user = _require_user(request)
    s = database.get_feynman_session(user["id"], sid)
    if s is None:
        raise HTTPException(status_code=404, detail="考核会话不存在")
    msgs = database.feynman_messages(sid)
    return {"session": _fy_session_json(s),
            "messages": [{"role": m["role"], "content": m["content"]} for m in msgs]}


@app.post("/api/feynman/sessions/{sid}/messages")
async def api_feynman_send(request: Request, sid: int, content: str = Body(..., embed=True)):
    user = _require_user(request)
    s = database.get_feynman_session(user["id"], sid)
    if s is None:
        raise HTTPException(status_code=404, detail="考核会话不存在")
    if s["status"] != "ongoing":
        raise HTTPException(status_code=400, detail="本场考核已结束，不能再继续")
    content = (content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="内容不能为空")
    history = [{"role": m["role"], "content": m["content"]}
               for m in database.feynman_messages(sid)]
    history.append({"role": "user", "content": content})
    msgs = _fy_anthropic_fix(history) if _llm_cfg()["protocol"] == "anthropic" else history
    reply = await asyncio.to_thread(
        _llm_request, _llm_cfg(), msgs,
        system=_fy_system_prompt(s["book_title"] or "", s["book_author"] or "",
                                 s["book_desc"] or "", s["lang"] or "zh"),
        max_tokens=1500)
    if not reply:
        raise HTTPException(status_code=502, detail="LLM 返回了空回复，请重试")
    database.add_feynman_message(sid, "user", content)
    database.add_feynman_message(sid, "assistant", reply)
    return {"reply": reply}


def _parse_feynman_assessment(raw: str) -> tuple:
    """从模型回复里容错提取 {score, verdict, weak[]}；失败则把原文当总评。"""
    score, verdict, weak = None, (raw or "").strip()[:300], []
    try:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                v = data.get("score")
                if isinstance(v, (int, float)):
                    score = max(0, min(100, int(round(v))))
                verdict = str(data.get("verdict") or verdict).strip()[:300]
                w = data.get("weak")
                if isinstance(w, list):
                    weak = [str(x).strip() for x in w if str(x).strip()][:6]
    except Exception:
        pass
    return score, verdict, weak


@app.post("/api/feynman/sessions/{sid}/finish")
async def api_feynman_finish(request: Request, sid: int):
    user = _require_user(request)
    s = database.get_feynman_session(user["id"], sid)
    if s is None:
        raise HTTPException(status_code=404, detail="考核会话不存在")
    if s["status"] == "done":
        raise HTTPException(status_code=400, detail="本场考核已经评估过了")
    history = database.feynman_messages(sid)
    if not any(m["role"] == "user" for m in history):
        raise HTTPException(status_code=400, detail="还没有讲解内容，先讲几句再结束")
    convo = "\n".join(("讲解者" if m["role"] == "user" else "小白") + "：" + m["content"]
                      for m in history)
    system = (
        "你是费曼技巧的评估官。根据下面用户向小白讲解《%s》的完整对话，评估用户对这本书的掌握程度。"
        "只输出一个 JSON 对象，不要输出任何其他文字，格式："
        '{"score": 0到100的整数, "verdict": "一句话总评（指出讲得好的与含糊的）", '
        '"weak": ["没讲明白/理解有偏差的点1", "点2", ...]}'
        "。weak 最多 5 条、每条一句话，来自讲解中含糊、矛盾、答不上追问或明显错误的地方；"
        "若确实没有可写空数组。" % (s["book_title"] or "",)
    )
    raw = await asyncio.to_thread(
        _llm_request, _llm_cfg(),
        [{"role": "user", "content": "对话记录：\n" + convo + "\n\n请输出评估 JSON。"}],
        system=system, max_tokens=2000)
    score, verdict, weak = _parse_feynman_assessment(raw)
    database.finish_feynman_session(user["id"], sid, score, verdict, weak)
    review_added = database.enqueue_weak_reviews(
        user["id"], s["book_id"], s["book_title"] or "", weak) if weak else 0
    return {"ok": True, "score": score, "verdict": verdict, "weak": weak,
            "review_added": review_added}


@app.delete("/api/feynman/sessions/{sid}")
def api_feynman_delete(request: Request, sid: int):
    user = _require_user(request)
    if not database.delete_feynman_session(user["id"], sid):
        raise HTTPException(status_code=404, detail="考核会话不存在")


# ---------------------------------------------------------------- 复习调度（艾宾浩斯）


@app.get("/api/review/summary")
def api_review_summary(request: Request):
    user = _require_user(request)
    return database.review_summary(user["id"])


@app.get("/api/review/due")
def api_review_due(request: Request, limit: int = 50):
    user = _require_user(request)
    limit = max(1, min(100, limit))
    return {"items": database.review_due_items(user["id"], limit)}


@app.post("/api/review")
def api_review_add(request: Request, front: str = Body(...), back: str = Body(""),
                   book_id: int = Body(0)):
    user = _require_user(request)
    front = (front or "").strip()
    if not front:
        raise HTTPException(status_code=400, detail="复习内容不能为空")
    rid = database.add_review_item(user["id"], front, (back or "").strip(), book_id or 0)
    return {"id": rid}


@app.post("/api/review/{rid}/grade")
def api_review_grade(request: Request, rid: int, payload: dict = Body(...)):
    user = _require_user(request)
    try:
        grade = int(payload.get("grade"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="grade 需为 0/1/2")
    out = database.grade_review_item(user["id"], rid, grade)
    if out is None:
        raise HTTPException(status_code=404, detail="复习卡不存在")
    return out


@app.delete("/api/review/{rid}")
def api_review_delete(request: Request, rid: int):
    user = _require_user(request)
    if not database.delete_review_item(user["id"], rid):
        raise HTTPException(status_code=404, detail="复习卡不存在")
    return {"ok": True}


# ---------------------------------------------------------------- 概念库


@app.get("/api/concepts")
def api_concepts(request: Request, book_id: int = 0, grouped: int = 0):
    user = _require_user(request)
    try:        # 旧速读报告的概念回填（幂等；无 speed_jobs 时零成本）
        database.backfill_concepts(user["id"])
    except Exception:
        log.exception("概念回填失败（忽略）")
    rows = database.list_concepts(user["id"], book_id or None)
    if not grouped:
        return {"concepts": [dict(r) for r in rows]}
    groups: dict = {}
    for r in rows:
        groups.setdefault(r["name"].strip().casefold(), []).append(r)
    out = [{"name": g[0]["name"], "items": [dict(x) for x in g]} for g in groups.values()]
    out.sort(key=lambda g: -len(g["items"]))
    return {"concepts": out}


@app.post("/api/concepts/{cid}/to_review")
def api_concept_to_review(request: Request, cid: int):
    user = _require_user(request)
    rid = database.concept_to_review(user["id"], cid)
    if rid is None:
        raise HTTPException(status_code=404, detail="概念不存在")
    return {"id": rid}


@app.delete("/api/concepts/{cid}")
def api_concept_delete(request: Request, cid: int):
    user = _require_user(request)
    if not database.delete_concept(user["id"], cid):
        raise HTTPException(status_code=404, detail="概念不存在")
    return {"ok": True}


@app.get("/api/books/{book_id}/outline")
def api_book_outline(request: Request, book_id: int):
    _require_user(request)
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return database.book_outline(book_id)


# ---------------------------------------------------------------- AI荐学（学习课程）

import speedread as _speedread


def _learn_catalog(user: dict) -> tuple[list, dict]:
    """书库目录（前 200 本 + 速读章节信息）；返回 (catalog, id->entry)。"""
    priv = " AND is_private=0" if not user["is_admin"] else ""
    rows = database.query_all(
        f"SELECT id, title, author, description FROM books WHERE 1=1{priv} "
        "ORDER BY updated_at DESC LIMIT 200") or []
    catalog, by_id = [], {}
    for r in rows:
        ent = {"book_id": r["id"], "title": r["title"] or "",
               "author": r["author"] or "", "desc": (r["description"] or "")[:120],
               "chapters": []}
        ol = database.book_outline(r["id"])
        if ol["chapters"] or ol["load_bearing"]:
            labels = {c["label"] for c in ol["chapters"]}
            ent["chapters"] = sorted(labels)[:12] + [lb for lb in ol["load_bearing"]
                                                     if lb not in labels][:4]
        catalog.append(ent)
        by_id[r["id"]] = ent
    return catalog, by_id


_LEARN_SYSTEM = (
    "你是一位严谨的学习规划师，服务对象是书房主人。用户给出学习目标，"
    "你把目标拆解为一门循序渐进的课程。\n"
    "书籍来源规则：\n"
    "1. 【书单】是书房已有的书，优先使用，引用其 book_id。\n"
    "2. 书单铺不出最优路径时，可以推荐书单外的好书：此时 book_id 填 0，"
    "必须给出 title 和 author，focus 留空数组。\n"
    "3. focus 重点章节只能取该书 chapters 列表里出现过的名称；"
    "chapters 为空的书或外部书一律 focus=[]。\n"
    "4. 步骤 3-6 个为宜，按学习先后排序。\n"
    "只输出一个 JSON 对象，不要 markdown 代码块：\n"
    '{"title":"课程名（12字内）","intro":"课程设计思路，80字内",'
    '"steps":[{"book_id":123,"title":"书名","author":"作者",'
    '"why":"它在课程中的角色，50字内",'
    '"focus":["重点章节"],"outcome":"读完应达到的状态，40字内"}]}'
)


def _postprocess_steps(steps: list, by_id: dict) -> list:
    out = []
    for st in steps:
        if not isinstance(st, dict):
            continue
        bid = st.get("book_id") or 0
        title = (st.get("title") or "").strip()
        if bid and bid in by_id:
            ent = by_id[bid]
            focus = [f for f in (st.get("focus") or []) if f in ent["chapters"]][:4]
            out.append({"book_id": bid, "title": ent["title"], "author": ent["author"],
                        "why": (st.get("why") or "")[:80], "focus": focus,
                        "outcome": (st.get("outcome") or "")[:60]})
        else:
            if not title:
                continue
            out.append({"book_id": 0, "title": title[:80],
                        "author": (st.get("author") or "")[:40],
                        "why": (st.get("why") or "")[:80], "focus": [],
                        "outcome": (st.get("outcome") or "")[:60]})
    return out[:6] or None


async def _gen_plan(user: dict, goal: str, chat: list) -> dict:
    catalog, by_id = _learn_catalog(user)
    user_msg = ("学习目标：" + goal + "\n\n书房书单（含速读分析的章节信息；"
                "book_id=0 表示书房还没有）：\n"
                + json.dumps(catalog, ensure_ascii=False))
    raw = await asyncio.to_thread(
        _llm_request, _llm_cfg(),
        [{"role": m["role"], "content": m["content"]} for m in chat[:-1]] +
        [{"role": "user", "content": user_msg}],
        system=_LEARN_SYSTEM, max_tokens=2500, timeout=120)
    data = _speedread.parse_json_tolerant(raw)
    if not data or not isinstance(data.get("steps"), list):
        raise HTTPException(status_code=502, detail="AI 返回的课程格式异常，请重试")
    steps = _postprocess_steps(data["steps"], by_id)
    if not steps:
        raise HTTPException(status_code=502, detail="AI 未能给出有效课程步骤，请换个说法重试")
    return {"title": (data.get("title") or "课程")[:40],
            "intro": (data.get("intro") or "")[:160], "steps": steps}


@app.post("/api/learn/plan")
async def api_learn_plan(request: Request, payload: dict = Body(...)):
    user = _require_user(request)
    goal = (payload.get("goal") or "").strip()
    if len(goal) < 8:
        raise HTTPException(status_code=400, detail="目标说详细一点（至少 8 个字）")
    chat = [{"role": "user", "content": goal}]
    plan = await _gen_plan(user, goal, chat)
    chat.append({"role": "assistant",
                 "content": plan["title"] + "：" + plan["intro"]})
    cid = database.create_course(user["id"], plan["title"], goal, plan, chat)
    return {"id": cid, "plan": plan}


@app.post("/api/learn/plan/{cid}/refine")
async def api_learn_refine(request: Request, cid: int, payload: dict = Body(...)):
    user = _require_user(request)
    c = database.get_course(user["id"], cid)
    if c is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    goal = (payload.get("goal") or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="请说想怎么调整")
    chat = json.loads(c["chat"]) if c["chat"] else []
    chat.append({"role": "user", "content": goal})
    plan = await _gen_plan(user, goal, chat)
    chat.append({"role": "assistant", "content": plan["title"] + "：" + plan["intro"]})
    database.update_course(user["id"], cid, plan=plan, chat=chat,
                           title=plan["title"], step_now=0)
    return {"id": cid, "plan": plan}


@app.get("/api/learn/courses")
def api_learn_courses(request: Request):
    user = _require_user(request)
    out = []
    for c in database.list_courses(user["id"]):
        item = dict(c)
        item["plan"] = json.loads(c["plan"]) if c["plan"] else {}
        item["chat"] = json.loads(c["chat"]) if c["chat"] else []
        out.append(item)
    return {"courses": out}


@app.put("/api/learn/courses/{cid}")
def api_learn_update(request: Request, cid: int, payload: dict = Body(...)):
    user = _require_user(request)
    if not database.update_course(user["id"], cid,
                                  step_now=payload.get("step_now"),
                                  status=payload.get("status")):
        raise HTTPException(status_code=404, detail="课程不存在")
    return {"ok": True}


@app.post("/api/learn/courses/{cid}/steps/{idx}/link")
def api_learn_link(request: Request, cid: int, idx: int, payload: dict = Body(...)):
    user = _require_user(request)
    try:
        book_id = int(payload.get("book_id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="book_id 不合法")
    c = database.get_course(user["id"], cid)
    if c is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    plan = json.loads(c["plan"]) if c["plan"] else {}
    steps = plan.get("steps") or []
    if not (0 <= idx < len(steps)):
        raise HTTPException(status_code=404, detail="步骤不存在")
    steps[idx].update({"book_id": book_id, "title": book["title"],
                       "author": book["author"] or ""})
    database.update_course(user["id"], cid, plan=plan)
    return {"ok": True}


@app.delete("/api/learn/courses/{cid}")
def api_learn_delete(request: Request, cid: int):
    user = _require_user(request)
    if not database.delete_course(user["id"], cid):
        raise HTTPException(status_code=404, detail="课程不存在")
    return {"ok": True}


# ---------------------------------------------------------------- AI老师（划线追问）

_TUTOR_SYSTEM = (
    "你是一位博学、直接的私人读书导师。读者正在读《{title}》{author_part}，"
    "选中了书中的一段文字并向你提问。\n"
    "规则：1) 优先结合本书语境与这段原文作答；2) 可补充必要背景知识，但不要跑题；"
    "3) 不确定就明说，不要编造；4) 直接给答案，不要客套开头结尾；5) 少而精，300字内，可用短列表。"
)


@app.post("/api/tutor/ask")
async def api_tutor_ask(request: Request, payload: dict = Body(...)):
    user = _require_user(request)
    book_id = int(payload.get("book_id") or 0)
    quote = (payload.get("quote") or "").strip()
    question = (payload.get("question") or "").strip()
    anchor = (payload.get("anchor") or "").strip()
    book = database.get_book(book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    if not quote:
        raise HTTPException(status_code=400, detail="选中文案不能为空")
    system = _TUTOR_SYSTEM.format(
        title=book["title"] or "", author_part=f"（{book['author']}）" if book["author"] else "")
    user_msg = (f"选中文本：“{quote[:500]}”\n\n读者的问题："
                + (question if question else "请讲解这段：它在讲什么、为什么重要、"
                                            "有什么背景或容易误解的地方。"))
    raw = await asyncio.to_thread(_llm_request, _llm_cfg(),
                                  [{"role": "user", "content": user_msg}],
                                  system=system, max_tokens=800, timeout=60)
    if not raw:
        raise HTTPException(status_code=502, detail="AI老师没有返回内容，请重试")
    qid = database.add_tutor_qa(user["id"], book_id, anchor, "",
                                quote, question, raw)
    return {"id": qid, "answer": raw}


@app.get("/api/tutor/list")
def api_tutor_list(request: Request, limit: int = 30):
    user = _require_user(request)
    limit = max(1, min(100, limit))
    return {"items": [dict(r) for r in
                      database.list_tutor_qa(user["id"], limit,
                                             include_private=bool(user["is_admin"]))]}


@app.post("/api/tutor/{qid}/to_review")
def api_tutor_to_review(request: Request, qid: int):
    user = _require_user(request)
    rid = database.tutor_qa_to_review(user["id"], qid)
    if rid is None:
        raise HTTPException(status_code=404, detail="问答不存在")
    return {"id": rid}


@app.delete("/api/tutor/{qid}")
def api_tutor_delete(request: Request, qid: int):
    user = _require_user(request)
    if not database.delete_tutor_qa(user["id"], qid):
        raise HTTPException(status_code=404, detail="问答不存在")
    return {"ok": True}


# ---------------------------------------------------------------- AI 速读（Map-Reduce 流水线）

import speedread  # noqa: E402

_SPEED_RUNNING = ("pending", "extracting", "mapping", "reducing")


def _speed_job_json(row) -> dict:
    result = None
    if row["result"]:
        try:
            result = json.loads(row["result"])
        except Exception:
            result = {"raw": row["result"][:12000]}
    return {"id": row["id"], "book_id": row["book_id"], "lang": row["lang"] or "zh",
            "status": row["status"], "error": row["error"] or "",
            "book_title": row["book_title"] or "", "book_author": row["book_author"] or "",
            "chars": row["chars"] or 0, "chunk_total": row["chunk_total"] or 0,
            "chunk_done": row["chunk_done"] or 0, "chunk_failed": row["chunk_failed"] or 0,
            "fallback": bool(row["fallback"]), "result": result,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


async def _speed_guard(coro, jid: int) -> None:
    try:
        await coro
    except Exception as e:                       # noqa: BLE001
        log.exception("速读 job %s 异常", jid)
        try:
            database.update_speed_job(jid, status="error", error=str(e)[:300])
        except Exception:
            pass


@app.get("/api/speed/estimate")
async def api_speed_estimate(request: Request, book_id: int = Query(...)):
    """开跑前预估：字数 / 块数 / 预计耗时与 token（纯本地计算，零 LLM 成本）。"""
    user = _require_user(request)
    row, path = _resolve_book(book_id, request)
    est = await asyncio.to_thread(speedread.estimate, path, row["format"],
                                  row["size"] or 0, row["mtime"] or 0, book_id)
    est["title"] = row["title"] or ""
    return est


@app.post("/api/speed/start")
async def api_speed_start(request: Request, book_id: int = Body(...),
                          lang: str = Body("zh")):
    """创建速读任务：缓存命中免费复看；不可提取自动转先验降级；否则启动流水线。"""
    user = _require_user(request)
    row, path = _resolve_book(book_id, request)      # 404 / 暗格权限 / 文件缺失
    lang = "en" if (lang or "").lower() == "en" else "zh"
    running = database.speed_running_job(user["id"])
    if running:
        raise HTTPException(status_code=400,
                            detail="已有速读任务在进行中，请等它完成或先取消")
    sig = f"{row['size'] or 0}:{int(row['mtime'] or 0)}"
    cached = database.speed_cached_done_job(book_id, sig)
    if cached:
        return {"job": _speed_job_json(cached), "cached": True}
    title, author = row["title"] or "", row["author"] or ""
    try:
        sections = await asyncio.to_thread(
            speedread.get_sections_cached, book_id, sig, path, row["format"])
        chunks = speedread.build_chunks(sections)
        if len(chunks) > speedread.MAX_CHUNKS:
            raise speedread.NotExtractable(
                f"书籍过长（约 {len(chunks)} 块，超过单次速读上限 {speedread.MAX_CHUNKS} 块）")
    except speedread.NotExtractable as e:
        # 不可提取（扫描版/DRM/漫画/超大）：仍建任务，直接走先验降级出口
        jid = database.create_speed_job(user["id"], book_id, lang, title, author,
                                        sig, 0, 0, [])
        database.update_speed_job(jid, status="extracting", error=str(e))
        asyncio.create_task(_speed_guard(speedread.run_fallback(jid), jid))
        job = database.get_speed_job(user["id"], jid)
        return {"job": _speed_job_json(job), "fallback_auto": True}
    chars = sum(len(c[2]) for c in chunks)
    jid = database.create_speed_job(user["id"], book_id, lang, title, author, sig,
                                    chars, len(chunks), [(i, lab) for i, lab, _ in chunks])
    speedread.remember_chunks(jid, chunks)
    asyncio.create_task(_speed_guard(speedread.run_job(jid), jid))
    return {"job": _speed_job_json(database.get_speed_job(user["id"], jid))}


@app.get("/api/speed/jobs")
def api_speed_jobs(request: Request):
    user = _require_user(request)
    rows = database.speed_jobs(user["id"])
    return {"count": len(rows), "jobs": [_speed_job_json(r) for r in rows]}


@app.get("/api/speed/jobs/{jid}")
def api_speed_detail(request: Request, jid: int):
    user = _require_user(request)
    job = database.get_speed_job(user["id"], jid)
    if job is None:
        raise HTTPException(status_code=404, detail="速读任务不存在")
    chunks = database.speed_chunks(jid)
    return {"job": _speed_job_json(job),
            "chunks": [{"idx": c["idx"], "label": c["label"], "status": c["status"]}
                       for c in chunks]}


@app.post("/api/speed/jobs/{jid}/cancel")
def api_speed_cancel(request: Request, jid: int):
    user = _require_user(request)
    job = database.get_speed_job(user["id"], jid)
    if job is None:
        raise HTTPException(status_code=404, detail="速读任务不存在")
    if job["status"] not in _SPEED_RUNNING:
        raise HTTPException(status_code=400, detail="任务不在进行中，无需取消")
    database.request_speed_cancel(jid)
    return {"ok": True}


@app.post("/api/speed/jobs/{jid}/rerun")
async def api_speed_rerun(request: Request, jid: int):
    """断点续跑：保留已 ok 的块（不重复计费），只重跑失败/未完成的部分。"""
    user = _require_user(request)
    job = database.get_speed_job(user["id"], jid)
    if job is None:
        raise HTTPException(status_code=404, detail="速读任务不存在")
    if job["status"] in _SPEED_RUNNING:
        raise HTTPException(status_code=400, detail="任务正在运行")
    running = database.speed_running_job(user["id"])
    if running and running["id"] != jid:
        raise HTTPException(status_code=400, detail="已有其他速读任务在进行中")
    row, path = _resolve_book(job["book_id"], request)
    sig = f"{row['size'] or 0}:{int(row['mtime'] or 0)}"
    if sig != job["book_sig"]:
        raise HTTPException(status_code=400, detail="书籍文件已更新，请新建一次速读")
    sections = await asyncio.to_thread(
        speedread.get_sections_cached, job["book_id"], sig, path, row["format"])
    chunks = speedread.build_chunks(sections)
    speedread.remember_chunks(jid, chunks)
    database.reset_speed_chunks_for_rerun(jid)
    database.clear_speed_cancel(jid)
    database.update_speed_job(jid, status="pending", error="")
    asyncio.create_task(_speed_guard(speedread.run_job(jid), jid))
    return {"ok": True}


@app.post("/api/speed/jobs/{jid}/fallback")
async def api_speed_fallback(request: Request, jid: int):
    """手动触发先验降级：不读原文，仅凭模型既有知识出报告（带免责标注）。"""
    user = _require_user(request)
    job = database.get_speed_job(user["id"], jid)
    if job is None:
        raise HTTPException(status_code=404, detail="速读任务不存在")
    if job["status"] in _SPEED_RUNNING:
        raise HTTPException(status_code=400, detail="任务正在运行，请先取消")
    asyncio.create_task(_speed_guard(speedread.run_fallback(jid), jid))
    return {"ok": True}


@app.delete("/api/speed/jobs/{jid}")
def api_speed_delete(request: Request, jid: int):
    user = _require_user(request)
    job = database.get_speed_job(user["id"], jid)
    if job is None:
        raise HTTPException(status_code=404, detail="速读任务不存在")
    if job["status"] in _SPEED_RUNNING:
        raise HTTPException(status_code=400, detail="任务进行中，请先取消再删除")
    if not database.delete_speed_job(user["id"], jid):
        raise HTTPException(status_code=404, detail="速读任务不存在")
    speedread.forget_chunks(jid)
    return {"ok": True}


# ---------------------------------------------------------------- 阅读进度


@app.put("/api/progress/{book_id}")
def api_save_progress(book_id: int, request: Request,
                      position: str = Body(""), percent: float = Body(0)):
    user = _require_user(request)
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    pct = max(0.0, min(1.0, percent or 0))
    database.upsert_progress(user["id"], book_id, position or "", pct)
    if pct >= 0.98:                      # 已读完判定：进度达到 98%
        database.mark_finished(user["id"], book_id)
    return {"ok": True}


@app.get("/api/progress/recent")
def api_recent(request: Request, limit: int = 10):
    user = _require_user(request)
    limit = max(1, min(50, limit))
    out = []
    for r in database.recent_progress(user["id"], limit,
                                      include_private=bool(user["is_admin"])):
        item = _book_json(r)
        item.update({
            "read_position": r["read_position"] or "",
            "read_percent": round((r["read_percent"] or 0) * 100, 1),
            "read_at": r["read_at"],
        })
        out.append(item)
    return {"books": out}


@app.get("/api/progress/{book_id}")
def api_get_progress(book_id: int, request: Request):
    user = _require_user(request)
    row = database.get_progress(user["id"], book_id)
    if row is None:
        return {"position": "", "percent": 0}
    return {"position": row["position"] or "", "percent": row["percent"] or 0}


# ---------------------------------------------------------------- 笔记（划线与想法）


def _note_json(row, with_book: bool = False) -> dict:
    out = {
        "id": row["id"], "kind": row["kind"], "content": row["content"],
        "anchor": row["anchor"] or "", "parent_id": row["parent_id"],
        "book_id": row["book_id"], "created_at": row["created_at"],
    }
    if with_book:
        out["book_title"] = row["book_title"]
        out["book_author"] = row["book_author"]
        out["book_format"] = row["book_format"]
        out["cover_file"] = row["cover_file"]
    return out


def _build_note_tree(rows) -> list:
    """把划线与其想法组装成树。"""
    by_id = {}
    highlights = []
    for r in rows:
        item = _note_json(r)
        item["thoughts"] = []
        by_id[item["id"]] = item
        if item["kind"] == "highlight":
            highlights.append(item)
    for r in rows:
        if r["kind"] == "thought" and r["parent_id"] in by_id:
            by_id[r["parent_id"]]["thoughts"].append(_note_json(r))
    return highlights


@app.post("/api/notes")
def api_add_note(request: Request, book_id: int = Body(...), kind: str = Body(...),
                 content: str = Body(""), anchor: str = Body(""),
                 parent_id: Optional[int] = Body(None)):
    user = _require_user(request)
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    if kind not in ("highlight", "thought"):
        raise HTTPException(status_code=400, detail="kind 需为 highlight 或 thought")
    if kind == "highlight":
        if not (content or "").strip():
            raise HTTPException(status_code=400, detail="划线内容不能为空")
        parent_id = None
    else:
        if not (content or "").strip():
            raise HTTPException(status_code=400, detail="想法内容不能为空")
        parent = database.query_one(
            "SELECT id FROM notes WHERE id=? AND user_id=? AND kind='highlight'",
            (parent_id, user["id"]),
        )
        if parent is None:
            raise HTTPException(status_code=400, detail="想法必须挂在已有的划线下")
    nid = database.add_note(user["id"], book_id, kind, content.strip(),
                            anchor or "", parent_id)
    return {"id": nid}


@app.get("/api/notes")
def api_notes_for_book(request: Request, book_id: int):
    user = _require_user(request)
    rows = database.notes_rows_for_book(user["id"], book_id)
    return {"highlights": _build_note_tree(rows)}


@app.get("/api/notes/all")
def api_notes_all(request: Request):
    user = _require_user(request)
    rows = database.notes_rows_all(user["id"], include_private=bool(user["is_admin"]))
    highlights = _build_note_tree(rows)
    by_id = {r["id"]: r for r in rows}
    for h in highlights:
        src = by_id[h["id"]]
        h["book_title"] = src["book_title"]
        h["book_author"] = src["book_author"]
        h["book_format"] = src["book_format"]
        h["cover_url"] = f"/api/cover/{h['book_id']}"
    return {"highlights": highlights}


@app.delete("/api/notes/{note_id}")
def api_delete_note(note_id: int, request: Request):
    user = _require_user(request)
    n = database.delete_note(user["id"], note_id)
    if not n:
        raise HTTPException(status_code=404, detail="笔记不存在")
    return {"deleted": n}


# ---------------------------------------------------------------- 家人荐书


@app.get("/api/users/names")
def api_user_names(request: Request):
    """登录用户可获取全部账户名单（荐书选择推荐对象用）。"""
    _require_user(request)
    return {"users": [{"id": u["id"], "username": u["username"]}
                      for u in database.list_users()]}


@app.post("/api/recommendations")
def api_add_recommendation(request: Request, book_id: int = Body(..., embed=True),
                           message: str = Body("", embed=True),
                           to_users: list = Body(default_factory=list, embed=True)):
    """推荐书籍；to_users 为目标账户 id 列表（可多选，须为其他账户）。"""
    user = _require_user(request)
    if database.get_book(book_id) is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    valid_ids = {u["id"] for u in database.list_users()}
    targets = sorted({int(t) for t in (to_users or []) if int(t) in valid_ids and int(t) != user["id"]})
    if not targets:
        raise HTTPException(status_code=400, detail="请至少选择一位推荐对象")
    rid = database.add_recommendation(user["id"], book_id, (message or "").strip()[:300], targets)
    return {"id": rid, "to_users": targets}


@app.get("/api/recommendations")
def api_recommendations(request: Request):
    """荐书列表：received=推荐给我的，sent=我发出的。私有书对非管理员隐藏。"""
    user = _require_user(request)
    is_admin = bool(user["is_admin"])
    received, sent = [], []
    for r in database.recommendations_rows():
        if r["book_private"] and not is_admin and r["from_user"] != user["id"]:
            continue
        recipients = database.recommendation_recipients(r["id"])
        item = {
            "id": r["id"], "book_id": r["book_id"], "message": r["message"] or "",
            "from_name": r["from_name"], "to_names": r["to_names"] or "",
            "mine": r["from_user"] == user["id"], "to_me": user["id"] in recipients,
            "created_at": r["created_at"], "title": r["book_title"],
            "author": r["book_author"] or "", "format": r["book_format"],
            "cover_url": f"/api/cover/{r['book_id']}",
            "reader_url": f"/reader?id={r['book_id']}",
        }
        if item["mine"]:
            sent.append(item)
        elif item["to_me"]:
            received.append(item)
    return {"received": received, "sent": sent}


@app.delete("/api/recommendations/{rid}")
def api_delete_recommendation(rid: int, request: Request):
    """仅推荐人本人或管理员可删除（先校验权限，再删除）。"""
    user = _require_user(request)
    row = database.query_one("SELECT * FROM recommendations WHERE id=?", (rid,))
    if row is None:
        raise HTTPException(status_code=404, detail="荐书记录不存在")
    if row["from_user"] != user["id"] and not user["is_admin"]:
        raise HTTPException(status_code=403, detail="只能删除自己发出的推荐")
    database.delete_recommendation(rid)
    return {"deleted": 1}


# ---------------------------------------------------------------- 暗格（私有书）


@app.post("/api/books/{book_id}/private")
def api_set_private(book_id: int, request: Request, is_private: bool = Body(..., embed=True)):
    """admin 将书籍设为/取消私有（暗格）。"""
    _require_admin(request)
    row = database.get_book(book_id)
    if row is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    database.execute("UPDATE books SET is_private=? WHERE id=?",
                     (1 if is_private else 0, book_id))
    return {"id": book_id, "is_private": is_private}


@app.get("/api/vault")
def api_vault(request: Request):
    """admin 暗格：私有书集合。"""
    _require_admin(request)
    return {"books": [_book_json(r) for r in database.private_books()]}


# ---------------------------------------------------------------- 阅读时长统计


@app.post("/api/stats/heartbeat")
def api_heartbeat(request: Request, seconds: float = Body(..., embed=True)):
    """阅读器每 15 秒（页面可见时）上报一次阅读时长。"""
    user = _require_user(request)
    database.add_reading_seconds(user["id"], int(seconds))
    return {"ok": True}


@app.get("/api/stats/me")
def api_stats_me(request: Request):
    user = _require_user(request)
    return database.reading_stats_user(user["id"])


@app.get("/api/stats/all")
def api_stats_all(request: Request):
    """所有账户的阅读统计（账户间互相可见）。"""
    _require_user(request)
    out = []
    for r in database.reading_stats_all():
        out.append({"username": r["username"], "is_admin": bool(r["is_admin"]),
                    "today_seconds": r["today_seconds"], "total_seconds": r["total_seconds"],
                    "finished": r["finished"]})
    return {"users": out}


# ---------------------------------------------------------------- 笔记导出


@app.get("/api/node/info")
def api_node_info(request: Request):
    """本机节点信息（左侧栏局域网节点卡）：主机名 / 内网 IP / 在线状态。"""
    import socket

    _require_user(request)
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        hostname = ""
    return {"hostname": hostname, "ips": ips,
            "online": True, "version": "1.0"}


@app.get("/api/tags")
def api_tags(request: Request):
    """全部标签及数量（按出现次数降序）；非管理员不统计私有书。"""
    viewer = _current_user(request)
    rows = database.query_all("SELECT tags FROM books WHERE COALESCE(is_private,0)=0")
    if viewer and viewer["is_admin"]:
        rows = database.query_all("SELECT tags FROM books")
    counts = {}
    for r in rows:
        if not r["tags"]:
            continue
        try:
            for t in json.loads(r["tags"]):
                counts[t] = counts.get(t, 0) + 1
        except (ValueError, TypeError):
            pass
    return {"tags": sorted(counts.items(), key=lambda kv: -kv[1])}


@app.get("/api/books")
def api_books_by_tag(request: Request, tag: str = Query(..., min_length=1),
                     limit: int = 200, offset: int = 0):
    """按标签列出书籍集合（跨目录）；与目录浏览同样分页（大标签几千本不卡）。"""
    viewer = _current_user(request)
    like = f'%"{tag.strip()}"%'
    private_sql = "" if (viewer and viewer["is_admin"]) else " AND NOT IFNULL(is_private,0)"
    total = database.query_one(
        "SELECT COUNT(*) AS c FROM books WHERE tags LIKE ?" + private_sql, (like,))["c"]
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    rows = database.query_all(
        "SELECT * FROM books WHERE tags LIKE ?" + private_sql +
        " ORDER BY title LIMIT ? OFFSET ?", (like, limit, offset))
    return {"tag": tag.strip(), "count": len(rows), "total": total,
            "books": [_book_json(r) for r in rows]}


@app.post("/api/notes/export/save")
def api_notes_export(request: Request):
    """把当前用户的划线与想法导出为 Markdown，写入服务器磁盘。"""
    user = _require_user(request)
    rows = database.notes_rows_all(user["id"], include_private=bool(user["is_admin"]))

    # 按书分组（保持划线倒序），想法挂到各自划线下
    thoughts_by_parent = {}
    for r in rows:
        if r["kind"] == "thought":
            thoughts_by_parent.setdefault(r["parent_id"], []).append(r)
    books_order, by_book = [], {}
    for r in rows:
        if r["kind"] != "highlight":
            continue
        if r["book_id"] not in by_book:
            books_order.append(r["book_id"])
            by_book[r["book_id"]] = []
        by_book[r["book_id"]].append(r)

    lines = [f"# 书房笔记 · {user['username']}", "",
             f"> 导出时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    n_h = n_t = 0
    for bid in books_order:
        hs = by_book[bid]
        lines.append(f"## 《{hs[0]['book_title']}》 {hs[0]['book_author'] or ''}".rstrip())
        lines.append("")
        for h in hs:
            hd = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["created_at"]))
            lines.append(f"> {h['content'].replace(chr(10), ' ')}")
            lines.append("")
            lines.append(f"*划线于 {hd}*")
            n_h += 1
            for t in thoughts_by_parent.get(h["id"], []):
                td = time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created_at"]))
                lines.append(f"- 💬 {t['content']} *({td})*")
                n_t += 1
            lines.append("")
        lines.append("")
    if not n_h:
        lines.append("（暂无笔记）")

    export_dir = config.DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"notes-{user['username']}-{time.strftime('%Y%m%d-%H%M%S')}.md"
    path.write_text(chr(10).join(lines), encoding="utf-8")
    return {"path": str(path), "highlights": n_h, "thoughts": n_t}


# ---------------------------------------------------------------- 数据备份（迁移用）


def _build_backup_bytes() -> bytes:
    """打包数据库 + 封面缓存（含说明文件）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "说明.txt",
            "书房数据备份\n"
            "===========\n"
            "内容：\n"
            "  - library.db ：SQLite 数据库（全部账户、阅读进度、划线笔记、书目元数据）\n"
            "  - covers/    ：封面缓存图片\n"
            "\n"
            "恢复方法：\n"
            "  1. 停止书房服务\n"
            "  2. 解压本压缩包，覆盖到数据目录（Docker 部署为宿主机 data/ 目录；本地运行为项目 data/ 目录）\n"
            "  3. 书籍原文件在书库目录（Docker 为 library/，本地为项目 library/），需另行整体拷贝\n"
            "  4. 重新启动服务即可\n",
        )
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        zf.write(str(config.DB_PATH), "library.db")
        for f in config.COVER_DIR.glob("*"):
            if f.is_file():
                zf.write(str(f), f"covers/{f.name}")
    return buf.getvalue()


@app.get("/api/export")
@app.get("/api/export/{fname}")
def api_export(request: Request, fname: str = "bookroom-backup.zip"):
    """管理员下载备份 zip（浏览器直连方式）。"""
    _require_admin(request)
    if not fname.endswith(".zip"):
        fname = "bookroom-backup.zip"
    return Response(
        _build_backup_bytes(), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.post("/api/export/save")
def api_export_save(request: Request):
    """管理员在服务器本地磁盘生成备份文件（绕开浏览器下载，文件名绝对可靠）。"""
    _require_admin(request)
    export_dir = config.DATA_DIR / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = export_dir / f"bookroom-backup-{stamp}.zip"
    path.write_bytes(_build_backup_bytes())
    return {"path": str(path), "size": path.stat().st_size}


# ---------------------------------------------------------------- SVG 占位封面

_PALETTES = [
    ("#1e293b", "#4f46e5"), ("#0f766e", "#0ea5e9"), ("#3b0764", "#9333ea"),
    ("#7c2d12", "#f59e0b"), ("#14532d", "#22c55e"), ("#7f1d1d", "#ef4444"),
    ("#0c4a6e", "#38bdf8"), ("#4a044e", "#d946ef"),
]


def _placeholder_svg(title: str, fmt: str, seed: int) -> str:
    c1, c2 = _PALETTES[seed % len(_PALETTES)]
    first = saxutils.escape((title.strip()[:1] or "书").upper())
    # 标题按 10 字/行折行，最多 3 行
    t = title.strip()
    lines = [t[i:i + 10] for i in range(0, min(len(t), 30), 10)]
    tspans = "".join(
        f'<tspan x="200" dy="{34 if i else 0}">{saxutils.escape(ln)}</tspan>'
        for i, ln in enumerate(lines)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600" '
        'viewBox="0 0 400 600">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/>'
        '</linearGradient></defs>'
        f'<rect width="400" height="600" fill="url(#g)"/>'
        '<circle cx="330" cy="70" r="150" fill="rgba(255,255,255,.08)"/>'
        '<circle cx="40" cy="560" r="110" fill="rgba(0,0,0,.18)"/>'
        f'<text x="200" y="230" font-size="150" font-family="Georgia, serif" '
        f'fill="rgba(255,255,255,.92)" text-anchor="middle" font-weight="bold">{first}</text>'
        f'<text x="200" y="380" font-size="30" font-family="sans-serif" '
        f'fill="rgba(255,255,255,.95)" text-anchor="middle">{tspans}</text>'
        f'<text x="200" y="520" font-size="22" font-family="monospace" letter-spacing="4" '
        f'fill="rgba(255,255,255,.55)" text-anchor="middle">{saxutils.escape((fmt or "?").upper())}</text>'
        '</svg>'
    )


def run() -> None:
    import uvicorn

    log.info("书房启动: http://%s:%s  书库=%s", config.HOST, config.PORT, config.LIBRARY_ROOT)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    run()
