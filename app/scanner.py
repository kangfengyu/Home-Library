"""书库扫描：递归发现书籍文件 → 入库/比对 → 后台元数据队列（异步 worker）+ hash 回填。"""
import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import config
import database
import metadata
import tagger

log = logging.getLogger("scanner")

state = {
    "scanning": False,
    "last_scan": 0.0,
    "total": 0,
    "added": 0,
    "updated": 0,
    "removed": 0,
    "pending": 0,
    "error": "",
    "hashing": False,
    "hash_pending": 0,
}

_queue: Optional[asyncio.Queue] = None
_loop: Optional[asyncio.AbstractEventLoop] = None
_worker_task: Optional[asyncio.Task] = None
_enqueued: set = set()
_rescan: bool = False          # 扫描进行中又收到触发（如连续上传）→ 本轮结束后自动补扫一轮

# 上传时已算出的 hash（rel_path -> sha256），扫描入库 'new' 时直接取用，避免重算大文件
_pending_hashes: dict = {}

# 豆瓣补抓节流（大书库防限流/封 IP）：最小请求间隔 + 连续失败熔断
_DOUBAN_MIN_INTERVAL = 1.5      # 秒
_douban_last = 0.0              # 上次请求的 monotonic 时刻
_douban_fails = 0               # 连续请求失败计数
_douban_pause_until = 0.0       # 熔断截止时刻（monotonic）


async def start() -> None:
    """创建元数据后台 worker（须在事件循环内调用）。"""
    global _queue, _loop, _worker_task
    _loop = asyncio.get_running_loop()
    _queue = asyncio.Queue()
    _worker_task = asyncio.create_task(_worker_loop())


async def stop() -> None:
    global _worker_task
    if _worker_task is not None:
        _worker_task.cancel()
        try:
            await _worker_task
        except (asyncio.CancelledError, Exception):
            pass
        _worker_task = None


def enqueue(book_id: int) -> None:
    """线程安全地把书加入元数据队列（去重）。"""
    if _queue is None or _loop is None or book_id in _enqueued:
        return
    _enqueued.add(book_id)
    try:
        _loop.call_soon_threadsafe(_queue.put_nowait, book_id)
    except RuntimeError:
        _enqueued.discard(book_id)


async def _worker_loop() -> None:
    while True:
        book_id = await _queue.get()
        state["pending"] = _queue.qsize()
        try:
            await asyncio.to_thread(_process_safe, book_id)
            await _enrich_douban(book_id)
        finally:
            _enqueued.discard(book_id)
            state["pending"] = _queue.qsize()
            _queue.task_done()


async def _enrich_douban(book_id: int) -> None:
    """元数据提取成功后，异步补抓豆瓣评分/简介；失败保持现状（优雅降级）。

    大书库防护：请求间隔 ≥1.5s（几千本摊开慢速补抓，不触发豆瓣反爬）；
    连续 3 次请求失败（限流/断网）熔断 5 分钟，期间跳过补抓留待下次扫描。
    """
    global _douban_last, _douban_fails, _douban_pause_until
    try:
        now = time.monotonic()
        if now < _douban_pause_until:
            return                                    # 熔断冷却中
        wait = _douban_last + _DOUBAN_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _douban_last = time.monotonic()

        row = database.get_book(book_id)
        if row is None or row["meta_status"] != 1 or row["meta_source"]:
            return
        info, blocked = await metadata.fetch_douban(row["title"], row["author"])
        if blocked:
            _douban_fails += 1
            if _douban_fails >= 3:
                _douban_pause_until = time.monotonic() + 300   # 连续 3 次失败 → 冷却 5 分钟
                _douban_fails = 0
                log.warning("豆瓣连续请求失败，暂停补抓 5 分钟（%s 待下次重试）", row["rel_path"])
            return                                    # 不标记 meta_source，下次扫描重试
        _douban_fails = 0
        if info:
            desc = info.get("description") or ""
            database.execute(
                "UPDATE books SET rating=?, description=?, meta_source=?, updated_at=? "
                "WHERE id=?",
                (
                    info.get("rating"),
                    desc if len(desc) > len(row["description"] or "") else row["description"],
                    info.get("source") or "douban",
                    time.time(),
                    book_id,
                ),
            )
            log.info("豆瓣信息已写入: %s (rating=%s)", row["rel_path"], info.get("rating"))
        else:
            # 标记“已尝试豆瓣但无结果”，避免重复请求
            database.execute(
                "UPDATE books SET meta_source='local', updated_at=? WHERE id=?",
                (time.time(), book_id),
            )
    except Exception as e:
        log.warning("豆瓣补抓异常 id=%s: %s", book_id, e)


def _process_safe(book_id: int) -> None:
    try:
        metadata.process_book(book_id)
    except Exception as e:
        log.warning("元数据任务异常 id=%s: %s", book_id, e)
    try:
        tagger.apply_tags(book_id)   # 元数据就绪后自动打标
    except Exception as e:
        log.warning("打标异常 id=%s: %s", book_id, e)


# ---------------------------------------------------------------- hash 回填

def remember_hash(rel_path: str, h: str) -> None:
    """登记上传时已算出的内容 hash，待扫描入库后直填（只保留最近 64 条防膨胀）。"""
    if len(_pending_hashes) > 64:
        _pending_hashes.clear()
    _pending_hashes[rel_path] = h


def _sha256_file(path: Path) -> str:
    """流式计算文件 sha256（1MiB 分块），文件不可读抛 OSError。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


async def hash_backfill() -> None:
    """为 content_hash 为空的书异步补算 sha256（串行 + 防重入，不阻塞事件循环）。

    防重入期间新增的书（另一轮扫描入库）由 while 重查兜住；本次失败的
    （文件不可读等）排除后继续，留待下次扫描重试，保证循环必然收敛。
    """
    if state["hashing"]:
        return
    state["hashing"] = True
    failed: set = set()
    try:
        while True:
            rows = database.query_all(
                "SELECT id, rel_path FROM books "
                "WHERE (content_hash IS NULL OR content_hash='') "
                f"AND id NOT IN ({','.join('?' * len(failed)) or '0'})",
                tuple(failed))
            if not rows:
                break
            state["hash_pending"] = len(rows)
            for r in rows:
                path = config.LIBRARY_ROOT / r["rel_path"]
                try:
                    h = await asyncio.to_thread(_sha256_file, path)
                    database.set_book_hash(r["id"], h)
                except OSError as e:
                    failed.add(r["id"])
                    log.warning("hash 计算失败 %s: %s", r["rel_path"], e)
                finally:
                    state["hash_pending"] -= 1
            log.info("hash 回填完成: %s 本", len(rows))
        if failed:
            log.warning("hash 回填 %s 本失败，下次扫描自动重试", len(failed))
    except Exception as e:
        log.warning("hash 回填异常: %s", e)
    finally:
        state["hashing"] = False
        state["hash_pending"] = 0


async def scan_library() -> dict:
    """全量扫描书库（异步入口），返回统计结果。

    已在扫描中时标记补扫（连续上传/批量入库场景不丢触发），本轮结束自动再来一轮。
    """
    global _rescan
    if state["scanning"]:
        _rescan = True
        return {"started": False, "msg": "扫描已在进行中"}
    state.update(scanning=True, added=0, updated=0, removed=0, error="")
    try:
        result = await asyncio.to_thread(_scan_sync)
        state.update(
            last_scan=time.time(), total=result["total"],
            added=result["added"], updated=result["updated"], removed=result["removed"],
        )
        log.info("扫描完成: 共 %s 本, 新增 %s, 更新 %s, 移除 %s",
                 result["total"], result["added"], result["updated"], result["removed"])
        # 补齐仍缺元数据的书（首次启动 / 上次中断遗留）
        for r in database.query_all("SELECT id FROM books WHERE meta_status != 1"):
            enqueue(r["id"])
        # 为旧数据补打标签（tags 为 NULL 的书）
        try:
            for r in database.query_all("SELECT id FROM books WHERE tags IS NULL"):
                tagger.apply_tags(r["id"])
        except Exception as e:
            log.warning("批量打标异常: %s", e)
        # 异步回填内容 hash（存量书 / 文件变更过的书），供上传查重
        asyncio.create_task(hash_backfill())
        return result
    except Exception as e:
        state["error"] = str(e)
        log.exception("扫描失败")
        return {"error": str(e)}
    finally:
        state["scanning"] = False
        # 服务关停（task 被 cancel）时不排补扫，避免关停瞬间重启扫描
        if _rescan and not asyncio.current_task().cancelling():
            _rescan = False
            asyncio.create_task(scan_library())
        else:
            _rescan = False


def _scan_sync() -> dict:
    root = config.LIBRARY_ROOT
    seen: dict = {}
    added = 0
    updated = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 跳过隐藏目录与数据目录
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() != "data"]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in config.BOOK_EXTS:
                continue
            full = Path(dirpath) / fn
            try:
                st = full.stat()
            except OSError:
                continue
            rel = full.relative_to(root).as_posix()
            try:
                bid, status = database.upsert_book(rel, st.st_size, st.st_mtime, ext.lstrip("."))
            except Exception as e:
                log.warning("入库失败 %s: %s", rel, e)
                continue
            seen[rel] = bid
            if status == "new":
                added += 1
                enqueue(bid)
                h = _pending_hashes.pop(rel, None)   # 上传时已算出的 hash 直填，免重算
                if h:
                    try:
                        database.set_book_hash(bid, h)
                    except Exception as e:
                        log.warning("直填 hash 失败 %s: %s", rel, e)
            elif status == "changed":
                updated += 1
                enqueue(bid)

    removed = 0
    for r in database.query_all("SELECT id, rel_path FROM books"):
        if r["rel_path"] not in seen:
            database.execute("DELETE FROM books WHERE id=?", (r["id"],))
            removed += 1

    total_row = database.query_one("SELECT COUNT(*) AS c FROM books")
    return {"total": total_row["c"], "added": added, "updated": updated, "removed": removed}
