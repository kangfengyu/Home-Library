"""SQLite 持久层：单连接 + 读写锁 + WAL，跨线程安全（配合 asyncio.to_thread 使用）。"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from typing import Any, Optional

import config

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path     TEXT UNIQUE NOT NULL,
    title        TEXT DEFAULT '',
    author       TEXT DEFAULT '',
    format       TEXT DEFAULT '',
    size         INTEGER DEFAULT 0,
    mtime        REAL DEFAULT 0,
    pages        INTEGER DEFAULT 0,
    cover_file   TEXT DEFAULT '',
    rating       REAL,
    description  TEXT DEFAULT '',
    meta_source  TEXT DEFAULT '',
    meta_status  INTEGER DEFAULT 0,
    is_private   INTEGER DEFAULT 0,
    tags         TEXT,
    content_hash TEXT,             -- 文件内容 sha256（异步回填），用于上传查重
    created_at   REAL DEFAULT 0,
    updated_at   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_books_relpath ON books(rel_path);
CREATE INDEX IF NOT EXISTS idx_books_title   ON books(title);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER DEFAULT 0,
    created_at    REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS progress (
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    position   TEXT DEFAULT '',
    percent    REAL DEFAULT 0,
    updated_at REAL DEFAULT 0,
    PRIMARY KEY (user_id, book_id)
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'highlight',
    content    TEXT DEFAULT '',
    anchor     TEXT DEFAULT '',
    parent_id  INTEGER,
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notes_user  ON notes(user_id);
CREATE INDEX IF NOT EXISTS idx_notes_book  ON notes(user_id, book_id);

CREATE TABLE IF NOT EXISTS reading_stats (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    seconds INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user  INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    message    TEXT DEFAULT '',
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reco_from ON recommendations(from_user);

CREATE TABLE IF NOT EXISTS recommendation_recipients (
    rid     INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    PRIMARY KEY (rid, to_user)
);

CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL DEFAULT '',
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_questions_user ON questions(user_id, updated_at);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feynman_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    lang       TEXT DEFAULT 'zh',
    status     TEXT NOT NULL DEFAULT 'ongoing',   -- ongoing / done
    score      INTEGER,                           -- 掌握度 0-100
    verdict    TEXT DEFAULT '',                   -- 一句话总评
    weak       TEXT DEFAULT '',                   -- JSON 数组：没懂的地方
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feynman_user ON feynman_sessions(user_id, updated_at);

CREATE TABLE IF NOT EXISTS feynman_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role       TEXT NOT NULL,                     -- user / assistant
    content    TEXT NOT NULL,
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_feynman_msg ON feynman_messages(session_id, id);

CREATE TABLE IF NOT EXISTS speed_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    book_id     INTEGER NOT NULL,
    lang        TEXT DEFAULT 'zh',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending/extracting/mapping/reducing/done/error/canceled
    error       TEXT DEFAULT '',
    book_sig    TEXT DEFAULT '',                  -- "size:mtime" 版本签名（结果缓存键）
    book_title  TEXT DEFAULT '',
    book_author TEXT DEFAULT '',
    chars       INTEGER DEFAULT 0,                -- 全书提取字符数
    chunk_total INTEGER DEFAULT 0,
    chunk_done  INTEGER DEFAULT 0,                -- Map 成功块数
    chunk_failed INTEGER DEFAULT 0,
    fallback    INTEGER DEFAULT 0,                -- 1 = 先验降级结果（未经原文校验）
    result      TEXT DEFAULT '',                  -- Reduce 输出 JSON
    created_at  REAL DEFAULT 0,
    updated_at  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_speed_user ON speed_jobs(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_speed_book ON speed_jobs(book_id, book_sig, status);

CREATE TABLE IF NOT EXISTS speed_chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL,
    idx     INTEGER NOT NULL,                     -- 块序号（决定 Reduce 拼接顺序）
    label   TEXT DEFAULT '',                      -- 章节路标（如「第3章 · 黑暗森林」）
    status  TEXT NOT NULL DEFAULT 'pending',      -- pending/ok/failed
    summary TEXT DEFAULT '',                      -- Map 产出的结构化 JSON 摘要
    error   TEXT DEFAULT '',
    UNIQUE (job_id, idx)
);

CREATE TABLE IF NOT EXISTS speed_job_cancel (
    job_id INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS review_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'feynman',  -- feynman / concept / manual
    front      TEXT NOT NULL,                    -- 复习问题（费曼weak条目原文 / 概念名）
    back       TEXT DEFAULT '',                  -- 参考答案
    stage      INTEGER NOT NULL DEFAULT 0,       -- 已连续"记得"次数（阶段索引）
    due_at     REAL NOT NULL DEFAULT 0,          -- 下次到期时间戳（秒）
    reps       INTEGER NOT NULL DEFAULT 0,       -- 总复习次数
    lapses     INTEGER NOT NULL DEFAULT 0,       -- "忘了"次数
    status     TEXT NOT NULL DEFAULT 'active',   -- active / done（毕业）
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_review_user ON review_items(user_id, status, due_at);

CREATE TABLE IF NOT EXISTS concepts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,
    why        TEXT DEFAULT '',
    thesis     TEXT DEFAULT '',
    job_id     INTEGER DEFAULT 0,
    created_at REAL DEFAULT 0,
    UNIQUE (user_id, book_id, name)
);
CREATE INDEX IF NOT EXISTS idx_concepts_user ON concepts(user_id, name);

CREATE TABLE IF NOT EXISTS courses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    title      TEXT NOT NULL,
    goal       TEXT DEFAULT '',
    plan       TEXT NOT NULL DEFAULT '',         -- JSON: {title?, intro, steps:[...]}
    chat       TEXT DEFAULT '',                  -- JSON: [{role,content},...] 规划对话历史
    step_now   INTEGER NOT NULL DEFAULT 0,
    status     TEXT NOT NULL DEFAULT 'active',   -- active / done / archived
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_courses_user ON courses(user_id, updated_at);

CREATE TABLE IF NOT EXISTS tutor_qa (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    anchor     TEXT DEFAULT '',      -- 与划线同构 JSON：{"type":"epub","cfi":..,"start":..} / {"type":"pdf","page":..,"rects":..}
    chapter    TEXT DEFAULT '',
    quote      TEXT DEFAULT '',      -- 选中文案（≤500 字入库）
    question   TEXT DEFAULT '',
    answer     TEXT DEFAULT '',      -- AI 回答（≤4000 字入库）
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tutor_user ON tutor_qa(user_id, created_at);
"""


def init_db() -> None:
    global _conn
    config.ensure_dirs()
    _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.executescript(SCHEMA)
    _conn.commit()
    _migrate()
    _bootstrap_admin()


def _migrate() -> None:
    """为已存在的旧表补列（CREATE TABLE IF NOT EXISTS 不会更新老表结构）。"""
    with _lock:
        book_cols = {r[1] for r in _check().execute("PRAGMA table_info(books)").fetchall()}
        if "is_private" not in book_cols:
            _check().execute("ALTER TABLE books ADD COLUMN is_private INTEGER DEFAULT 0")
        prog_cols = {r[1] for r in _check().execute("PRAGMA table_info(progress)").fetchall()}
        if "finished" not in prog_cols:
            _check().execute("ALTER TABLE progress ADD COLUMN finished INTEGER DEFAULT 0")
        if "tags" not in book_cols:
            _check().execute("ALTER TABLE books ADD COLUMN tags TEXT")
        if "content_hash" not in book_cols:
            _check().execute("ALTER TABLE books ADD COLUMN content_hash TEXT")
        # 索引须在列就绪后建（老库列由上面的 ALTER 补、新库建表自带），不能放 SCHEMA：
        # 老库 executescript 时 books 表无此列会直接启动失败
        _check().execute(
            "CREATE INDEX IF NOT EXISTS idx_books_hash ON books(content_hash)")
        # 旧荐书数据（无收件人）补默认收件人 = 全部其他账户（历史行为）
        orphan = _check().execute(
            "SELECT id, from_user FROM recommendations r WHERE NOT EXISTS "
            "(SELECT 1 FROM recommendation_recipients rr WHERE rr.rid = r.id)").fetchall()
        if orphan:
            all_users = [r["id"] for r in _check().execute("SELECT id FROM users").fetchall()]
            for row in orphan:
                for uid in all_users:
                    if uid != row["from_user"]:
                        _check().execute(
                            "INSERT OR IGNORE INTO recommendation_recipients(rid,to_user) VALUES(?,?)",
                            (row["id"], uid))
        _conn.commit()


def _bootstrap_admin() -> None:
    """首次启动时创建默认管理员账户。"""
    row = query_one("SELECT COUNT(*) AS c FROM users")
    if row and row["c"] == 0:
        pwd = os.environ.get("ADMIN_PASSWORD", "admin123")
        create_user("admin", pwd, is_admin=1)
        print(f"[init] 已创建默认管理员账户: admin / {pwd}（请尽快修改）", flush=True)


# ---------------------------------------------------------------- 密码与会话


def hash_password(pw: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return f"{salt}${digest}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()
    return hmac.compare_digest(calc, digest)


def create_user(username: str, password: str, is_admin: int = 0) -> int:
    username = (username or "").strip()
    if not (1 <= len(username) <= 32):
        raise ValueError("用户名长度需为 1-32 个字符")
    if len(password or "") < 4:
        raise ValueError("密码至少 4 位")
    with _lock:
        exists = _check().execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        if exists:
            raise ValueError(f"用户名已存在: {username}")
        cur = _conn.execute(
            "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?,?,?,?)",
            (username, hash_password(password), 1 if is_admin else 0, time.time()),
        )
        _conn.commit()
        return cur.lastrowid


def verify_login(username: str, password: str) -> Optional[sqlite3.Row]:
    with _lock:
        row = _check().execute(
            "SELECT * FROM users WHERE username=?", ((username or "").strip(),)
        ).fetchone()
    if row is None or not verify_password(password or "", row["password_hash"]):
        return None
    return row


def set_user_password(uid: int, password: str) -> None:
    if len(password or "") < 4:
        raise ValueError("密码至少 4 位")
    with _lock:
        _check().execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(password), uid))
        _conn.commit()


def delete_user(uid: int) -> None:
    """删除账户并级联清理其全部数据（进度/笔记/费曼/速读/问题/推荐）。"""
    stmts = [
        # 先删子表（按父表会话/任务关联），再删主表
        "DELETE FROM feynman_messages WHERE session_id IN "
        "(SELECT id FROM feynman_sessions WHERE user_id=?)",
        "DELETE FROM feynman_sessions WHERE user_id=?",
        "DELETE FROM speed_chunks WHERE job_id IN "
        "(SELECT id FROM speed_jobs WHERE user_id=?)",
        "DELETE FROM speed_job_cancel WHERE job_id IN "
        "(SELECT id FROM speed_jobs WHERE user_id=?)",
        "DELETE FROM speed_jobs WHERE user_id=?",
        "DELETE FROM recommendation_recipients WHERE rid IN "
        "(SELECT id FROM recommendations WHERE from_user=?) OR to_user=?",
        "DELETE FROM recommendations WHERE from_user=?",
        "DELETE FROM questions WHERE user_id=?",
        "DELETE FROM review_items WHERE user_id=?",
        "DELETE FROM concepts WHERE user_id=?",
        "DELETE FROM courses WHERE user_id=?",
        "DELETE FROM tutor_qa WHERE user_id=?",
        "DELETE FROM notes WHERE user_id=?",
        "DELETE FROM reading_stats WHERE user_id=?",
        "DELETE FROM progress WHERE user_id=?",
        "DELETE FROM sessions WHERE user_id=?",
        "DELETE FROM users WHERE id=?",
    ]
    with _lock:
        for s in stmts:
            _conn.execute(s, (uid,) * s.count("?"))
        _conn.commit()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    execute("INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
            (token, user_id, time.time()))
    return token


def get_session_user(token: Optional[str]) -> Optional[sqlite3.Row]:
    if not token:
        return None
    return query_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?", (token,)
    )


def delete_session(token: Optional[str]) -> None:
    if token:
        execute("DELETE FROM sessions WHERE token=?", (token,))


def list_users() -> list:
    return query_all("SELECT id, username, is_admin, created_at FROM users ORDER BY id")


# ---------------------------------------------------------------- 问题（Q&A）


def add_question(user_id: int, question: str, answer: str = "") -> int:
    now = time.time()
    with _lock:
        cur = _check().execute(
            "INSERT INTO questions(user_id, question, answer, created_at, updated_at) "
            "VALUES(?,?,?,?,?)", (user_id, question, answer, now, now))
        _conn.commit()
        return cur.lastrowid


def list_questions(user_id: int) -> list:
    return query_all("SELECT * FROM questions WHERE user_id=? ORDER BY updated_at DESC", (user_id,))


def update_question(user_id: int, qid: int, question: Optional[str] = None,
                    answer: Optional[str] = None) -> bool:
    row = query_one("SELECT * FROM questions WHERE id=? AND user_id=?", (qid, user_id))
    if row is None:
        return False
    q = row["question"] if question is None else question
    a = row["answer"] if answer is None else answer
    execute("UPDATE questions SET question=?, answer=?, updated_at=? WHERE id=?",
            (q, a, time.time(), qid))
    return True


def delete_question(user_id: int, qid: int) -> bool:
    row = query_one("SELECT id FROM questions WHERE id=? AND user_id=?", (qid, user_id))
    if row is None:
        return False
    execute("DELETE FROM questions WHERE id=?", (qid,))
    return True


# ---------------------------------------------------------------- 系统设置（LLM 等）


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, time.time()),
    )


# ---------------------------------------------------------------- 费曼考核（讲书给 AI 小白听）


def create_feynman_session(user_id: int, book_id: int, lang: str = "zh") -> int:
    now = time.time()
    with _lock:
        cur = _check().execute(
            "INSERT INTO feynman_sessions(user_id, book_id, lang, created_at, updated_at) "
            "VALUES(?,?,?,?,?)", (user_id, book_id, lang, now, now))
        _conn.commit()
        return cur.lastrowid


def add_feynman_message(session_id: int, role: str, content: str) -> None:
    now = time.time()
    with _lock:
        _check().execute(
            "INSERT INTO feynman_messages(session_id, role, content, created_at) VALUES(?,?,?,?)",
            (session_id, role, content, now))
        _check().execute("UPDATE feynman_sessions SET updated_at=? WHERE id=?", (now, session_id))
        _conn.commit()


def feynman_messages(session_id: int) -> list:
    return query_all("SELECT * FROM feynman_messages WHERE session_id=? ORDER BY id", (session_id,))


def feynman_sessions(user_id: int) -> list:
    return query_all(
        "SELECT f.*, b.title AS book_title, b.author AS book_author, b.format AS book_format "
        "FROM feynman_sessions f JOIN books b ON b.id = f.book_id "
        "WHERE f.user_id=? ORDER BY f.updated_at DESC", (user_id,))


def get_feynman_session(user_id: int, sid: int):
    return query_one(
        "SELECT f.*, b.title AS book_title, b.author AS book_author, "
        "b.description AS book_desc FROM feynman_sessions f "
        "JOIN books b ON b.id = f.book_id WHERE f.id=? AND f.user_id=?", (sid, user_id))


def finish_feynman_session(user_id: int, sid: int, score: Optional[int],
                           verdict: str, weak: list) -> bool:
    row = query_one("SELECT id FROM feynman_sessions WHERE id=? AND user_id=?", (sid, user_id))
    if row is None:
        return False
    execute(
        "UPDATE feynman_sessions SET status='done', score=?, verdict=?, weak=?, updated_at=? "
        "WHERE id=?",
        (score, verdict, json.dumps(weak, ensure_ascii=False), time.time(), sid))
    return True


def delete_feynman_session(user_id: int, sid: int) -> bool:
    row = query_one("SELECT id FROM feynman_sessions WHERE id=? AND user_id=?", (sid, user_id))
    if row is None:
        return False
    with _lock:
        _check().execute("DELETE FROM feynman_messages WHERE session_id=?", (sid,))
        _check().execute("DELETE FROM feynman_sessions WHERE session_id=?", (sid,))
        _conn.commit()
    return True


# ---------------------------------------------------------------- 复习调度（艾宾浩斯）


REVIEW_INTERVALS = [1, 2, 4, 7, 15, 30, 60]   # 天：stage-1 索引
GRADE_FORGOT, GRADE_FUZZY, GRADE_GOT = 0, 1, 2


def enqueue_weak_reviews(user_id: int, book_id: int, book_title: str, weak: list) -> int:
    """费曼考核结束：把没讲明白的点入队（同 user+book+front 且 active 的跳过）。"""
    now = time.time()
    added = 0
    with _lock:
        for w in weak:
            front = (w or "").strip()
            if not front:
                continue
            dup = _check().execute(
                "SELECT 1 FROM review_items WHERE user_id=? AND book_id=? AND front=? "
                "AND status='active'", (user_id, book_id, front)).fetchone()
            if dup:
                continue
            _check().execute(
                "INSERT INTO review_items(user_id, book_id, kind, front, back, due_at, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (user_id, book_id, "feynman", front,
                 f"《{book_title}》考核薄弱点 · {time.strftime('%m-%d', time.localtime(now))}",
                 now, now, now))
            added += 1
        _conn.commit()
    return added


def add_review_item(user_id: int, front: str, back: str = "",
                    book_id: int = 0, kind: str = "manual") -> int:
    now = time.time()
    with _lock:
        cur = _check().execute(
            "INSERT INTO review_items(user_id, book_id, kind, front, back, due_at, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, book_id or 0, kind, front, back or "", now, now, now))
        _conn.commit()
        return cur.lastrowid


def review_due_items(user_id: int, limit: int = 50) -> list:
    return query_all(
        "SELECT r.*, b.title AS book_title FROM review_items r "
        "LEFT JOIN books b ON b.id = r.book_id "
        "WHERE r.user_id=? AND r.status='active' AND r.due_at<=? "
        "ORDER BY r.due_at ASC LIMIT ?",
        (user_id, time.time(), limit))


def grade_review_item(user_id: int, rid: int, grade: int) -> Optional[dict]:
    if grade not in (GRADE_FORGOT, GRADE_FUZZY, GRADE_GOT):
        return None
    row = query_one("SELECT * FROM review_items WHERE id=? AND user_id=?", (rid, user_id))
    if row is None:
        return None
    now = time.time()
    stage, status, due = row["stage"], row["status"], row["due_at"]
    reps, lapses = row["reps"], row["lapses"]
    if grade == GRADE_GOT:
        stage += 1
        if stage > len(REVIEW_INTERVALS):
            status = "done"
            due = now
        else:
            due = now + REVIEW_INTERVALS[stage - 1] * 86400
    elif grade == GRADE_FUZZY:
        due = now + 86400
    else:                                     # 忘了：打回原点，10 分钟后再来
        stage, due = 0, now + 600
        lapses += 1
    reps += 1
    with _lock:
        _check().execute(
            "UPDATE review_items SET stage=?, due_at=?, reps=?, lapses=?, status=?, "
            "updated_at=? WHERE id=?",
            (stage, due, reps, lapses, status, now, rid))
        _conn.commit()
    return {"id": rid, "stage": stage, "status": status, "due_at": due}


def review_summary(user_id: int) -> dict:
    now = time.time()
    day_end = now + 86400
    due = query_one("SELECT COUNT(*) AS c FROM review_items WHERE user_id=? AND "
                    "status='active' AND due_at<=?", (user_id, now))["c"]
    tomorrow = query_one("SELECT COUNT(*) AS c FROM review_items WHERE user_id=? AND "
                         "status='active' AND due_at>? AND due_at<=?",
                         (user_id, now, day_end))["c"]
    active = query_one("SELECT COUNT(*) AS c FROM review_items WHERE user_id=? AND "
                       "status='active'", (user_id,))["c"]
    done_today = query_one("SELECT COUNT(*) AS c FROM review_items WHERE user_id=? AND "
                           "status='active' AND updated_at>=? AND reps>?",
                           (user_id, now - 86400, 0))["c"]
    return {"due": due, "tomorrow": tomorrow, "active": active, "today_done": done_today}


def delete_review_item(user_id: int, rid: int) -> bool:
    row = query_one("SELECT id FROM review_items WHERE id=? AND user_id=?", (rid, user_id))
    if row is None:
        return False
    execute("DELETE FROM review_items WHERE id=?", (rid,))
    return True


# ---------------------------------------------------------------- 概念库


def upsert_concepts(user_id: int, book_id: int, job_id: int,
                    concepts: list, thesis: str = "") -> int:
    """速读报告 done 时把 concepts[{name,why}] 入库（UNIQUE 幂等，rerun 不重复）。"""
    now = time.time()
    added = 0
    with _lock:
        for c in concepts:
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if not name:
                continue
            cur = _check().execute(
                "INSERT OR IGNORE INTO concepts(user_id, book_id, name, why, thesis, "
                "job_id, created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, book_id, name[:80], (c.get("why") or "").strip()[:200],
                 (thesis or "")[:200], job_id, now))
            added += cur.rowcount
        _conn.commit()
    return added


def list_concepts(user_id: int, book_id: Optional[int] = None) -> list:
    sql = ("SELECT c.*, b.title AS book_title FROM concepts c "
           "LEFT JOIN books b ON b.id = c.book_id WHERE c.user_id=?")
    args: tuple = (user_id,)
    if book_id:
        sql += " AND c.book_id=?"
        args = (user_id, book_id)
    sql += " ORDER BY c.name, c.created_at DESC"
    return query_all(sql, args)


def concept_to_review(user_id: int, cid: int) -> Optional[int]:
    row = query_one("SELECT c.*, b.title AS book_title FROM concepts c "
                    "LEFT JOIN books b ON b.id = c.book_id "
                    "WHERE c.id=? AND c.user_id=?", (cid, user_id))
    if row is None:
        return None
    back = (row["why"] or "")
    if row["book_title"]:
        back = f"{back}（{row['thesis']}）" if row["thesis"] else back
        back = f"《{row['book_title']}》：{back}"
    return add_review_item(user_id, row["name"], back, row["book_id"] or 0, kind="concept")


def delete_concept(user_id: int, cid: int) -> bool:
    row = query_one("SELECT id FROM concepts WHERE id=? AND user_id=?", (cid, user_id))
    if row is None:
        return False
    execute("DELETE FROM concepts WHERE id=?", (cid,))
    return True


def book_outline(book_id: int) -> dict:
    """章节大纲：优先取该书最近一次 done 的速读 job（chunks label + result.load_bearing）。"""
    job = query_one("SELECT id, result FROM speed_jobs WHERE book_id=? AND status='done' "
                    "ORDER BY updated_at DESC LIMIT 1", (book_id,))
    if job is None:
        return {"chapters": [], "load_bearing": []}
    chapters = []
    for r in query_all("SELECT label, summary FROM speed_chunks WHERE job_id=? "
                       "AND status='ok' ORDER BY idx", (job["id"],)):
        if not r["label"]:
            continue
        thesis = ""
        try:
            data = json.loads(r["summary"])
            thesis = (data.get("thesis") or "")[:120]
        except Exception:
            pass
        chapters.append({"label": r["label"], "thesis": thesis})
    load_bearing: list = []
    try:
        data = json.loads(job["result"]) if job["result"] else {}
        load_bearing = [str(x) for x in (data.get("load_bearing") or [])][:6]
    except Exception:
        pass
    return {"chapters": chapters, "load_bearing": load_bearing}


# ---------------------------------------------------------------- AI 速读（Map-Reduce 流水线）


def create_speed_job(user_id: int, book_id: int, lang: str, title: str, author: str,
                     sig: str, chars: int, chunk_total: int, chunks: list) -> int:
    """chunks = [(idx, label)]。建 job + 全部块记录，状态 pending。"""
    now = time.time()
    with _lock:
        cur = _check().execute(
            "INSERT INTO speed_jobs(user_id, book_id, lang, status, book_sig, book_title, "
            "book_author, chars, chunk_total, chunk_done, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,0,?,?)",
            (user_id, book_id, lang, "pending", sig, title, author, chars, chunk_total, now, now))
        jid = cur.lastrowid
        _check().executemany(
            "INSERT OR REPLACE INTO speed_chunks(job_id, idx, label, status) VALUES(?,?,?,'pending')",
            [(jid, i, lab) for i, lab in chunks])
        _conn.commit()
        return jid


def get_speed_job(user_id: int, jid: int):
    return query_one("SELECT * FROM speed_jobs WHERE id=? AND user_id=?", (jid, user_id))


def speed_jobs(user_id: int, limit: int = 50) -> list:
    return query_all(
        "SELECT * FROM speed_jobs WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit))


def update_speed_job(jid: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k}=?")
        vals.append(v)
    vals.append(jid)
    execute(f"UPDATE speed_jobs SET {', '.join(cols)} WHERE id=?", tuple(vals))


def speed_chunks(job_id: int) -> list:
    return query_all("SELECT * FROM speed_chunks WHERE job_id=? ORDER BY idx", (job_id,))


def set_speed_chunk(job_id: int, idx: int, status: str, summary: str = "", error: str = "") -> None:
    execute("UPDATE speed_chunks SET status=?, summary=?, error=? WHERE job_id=? AND idx=?",
            (status, summary, error, job_id, idx))


def _bump_chunk_counts(job_id: int) -> None:
    """按块表实际状态回写 job 的 chunk_done / chunk_failed 计数。"""
    row = query_one(
        "SELECT SUM(status='ok') AS okc, SUM(status='failed') AS failc "
        "FROM speed_chunks WHERE job_id=?", (job_id,))
    update_speed_job(job_id,
                     chunk_done=(row["okc"] or 0) if row else 0,
                     chunk_failed=(row["failc"] or 0) if row else 0)


def reset_speed_chunks_for_rerun(job_id: int) -> None:
    """重跑：只重置未完成/失败的块（ok 的块保留 → 断点免费续跑）。"""
    execute("UPDATE speed_chunks SET status='pending', summary='', error='' "
            "WHERE job_id=? AND status!='ok'", (job_id,))


def speed_running_job(user_id: int):
    """该用户是否有进行中的任务（单用户同时只跑一个）。"""
    return query_one(
        "SELECT * FROM speed_jobs WHERE user_id=? AND status IN "
        "('pending','extracting','mapping','reducing') ORDER BY id DESC LIMIT 1", (user_id,))


def speed_cached_done_job(book_id: int, sig: str):
    """同书同版本已完成的速读结果（缓存命中，第二次免费）。"""
    return query_one(
        "SELECT * FROM speed_jobs WHERE book_id=? AND book_sig=? AND status='done' "
        "ORDER BY id DESC LIMIT 1", (book_id, sig))


def request_speed_cancel(job_id: int) -> None:
    execute("INSERT OR REPLACE INTO speed_job_cancel(job_id) VALUES(?)", (job_id,))


def speed_cancel_requested(job_id: int) -> bool:
    return query_one("SELECT 1 AS x FROM speed_job_cancel WHERE job_id=?", (job_id,)) is not None


def clear_speed_cancel(job_id: int) -> None:
    execute("DELETE FROM speed_job_cancel WHERE job_id=?", (job_id,))


def delete_speed_job(user_id: int, jid: int) -> bool:
    row = query_one("SELECT id FROM speed_jobs WHERE id=? AND user_id=?", (jid, user_id))
    if row is None:
        return False
    execute("DELETE FROM speed_chunks WHERE job_id=?", (jid,))
    execute("DELETE FROM speed_job_cancel WHERE job_id=?", (jid,))
    execute("DELETE FROM speed_jobs WHERE id=?", (jid,))
    return True


def speed_mark_interrupted() -> None:
    """服务启动时把上次遗留的进行中任务标记为 error（防僵尸任务永远转圈）。"""
    execute(
        "UPDATE speed_jobs SET status='error', error='服务重启导致任务中断，请重新运行', "
        "updated_at=? WHERE status IN ('pending','extracting','mapping','reducing')", (time.time(),))


# ---------------------------------------------------------------- 阅读进度


def upsert_progress(user_id: int, book_id: int, position: str, percent: float) -> None:
    now = time.time()
    execute(
        "INSERT INTO progress(user_id, book_id, position, percent, updated_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id, book_id) DO UPDATE SET position=excluded.position, "
        "percent=excluded.percent, updated_at=excluded.updated_at",
        (user_id, book_id, position, percent, now),
    )


def mark_finished(user_id: int, book_id: int) -> None:
    execute("UPDATE progress SET finished=1 WHERE user_id=? AND book_id=?", (user_id, book_id))


def get_progress(user_id: int, book_id: int):
    return query_one("SELECT * FROM progress WHERE user_id=? AND book_id=?", (user_id, book_id))


def recent_progress(user_id: int, limit: int = 10, include_private: bool = False) -> list:
    sql = (
        "SELECT b.*, p.position AS read_position, p.percent AS read_percent, "
        "p.updated_at AS read_at FROM progress p JOIN books b ON b.id = p.book_id "
        "WHERE p.user_id=? "
        + ("" if include_private else "AND COALESCE(b.is_private,0)=0 ")
        + "ORDER BY p.updated_at DESC LIMIT ?"
    )
    return query_all(sql, (user_id, limit))


# ---------------------------------------------------------------- 阅读时长统计


def add_reading_seconds(user_id: int, seconds: int) -> None:
    day = time.strftime("%Y-%m-%d")
    seconds = max(0, min(int(seconds), 120))  # 单次心跳上限，防异常放大
    execute(
        "INSERT INTO reading_stats(user_id, day, seconds) VALUES(?,?,?) "
        "ON CONFLICT(user_id, day) DO UPDATE SET seconds = seconds + excluded.seconds",
        (user_id, day, seconds),
    )


def reading_stats_user(user_id: int) -> dict:
    today = time.strftime("%Y-%m-%d")
    total = query_one("SELECT COALESCE(SUM(seconds),0) s FROM reading_stats WHERE user_id=?", (user_id,))
    today_row = query_one("SELECT seconds FROM reading_stats WHERE user_id=? AND day=?", (user_id, today))
    fin = query_one("SELECT COUNT(*) c FROM progress WHERE user_id=? AND finished=1", (user_id,))
    return {"today_seconds": today_row["seconds"] if today_row else 0,
            "total_seconds": total["s"], "finished": fin["c"]}


def reading_stats_all() -> list:
    today = time.strftime("%Y-%m-%d")
    return query_all(
        "SELECT u.id, u.username, u.is_admin, "
        " COALESCE((SELECT seconds FROM reading_stats r WHERE r.user_id=u.id AND r.day=?),0) AS today_seconds, "
        " COALESCE((SELECT SUM(seconds) FROM reading_stats r WHERE r.user_id=u.id),0) AS total_seconds, "
        " (SELECT COUNT(*) FROM progress p WHERE p.user_id=u.id AND p.finished=1) AS finished "
        "FROM users u ORDER BY total_seconds DESC",
        (today,),
    )


# ---------------------------------------------------------------- 笔记（划线与想法）


def add_note(user_id: int, book_id: int, kind: str, content: str,
             anchor: str = "", parent_id: Optional[int] = None) -> int:
    with _lock:
        cur = _check().execute(
            "INSERT INTO notes(user_id, book_id, kind, content, anchor, parent_id, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, book_id, kind, content, anchor, parent_id, time.time()),
        )
        _conn.commit()
        return cur.lastrowid


def notes_rows_for_book(user_id: int, book_id: int) -> list:
    return query_all(
        "SELECT * FROM notes WHERE user_id=? AND book_id=? ORDER BY id", (user_id, book_id)
    )


def notes_rows_all(user_id: int, include_private: bool = False) -> list:
    sql = (
        "SELECT n.*, b.title AS book_title, b.author AS book_author, b.format AS book_format, "
        "b.cover_file AS cover_file FROM notes n JOIN books b ON b.id = n.book_id "
        "WHERE n.user_id=? "
        + ("" if include_private else "AND COALESCE(b.is_private,0)=0 ")
        + "ORDER BY n.id DESC"
    )
    return query_all(sql, (user_id,))


def private_books() -> list:
    return query_all("SELECT * FROM books WHERE COALESCE(is_private,0)=1 ORDER BY title")


# ---------------------------------------------------------------- 家人荐书


def add_recommendation(from_user: int, book_id: int, message: str, to_users: list) -> int:
    with _lock:
        cur = _check().execute(
            "INSERT INTO recommendations(from_user, book_id, message, created_at) VALUES(?,?,?,?)",
            (from_user, book_id, message or "", time.time()),
        )
        rid = cur.lastrowid
        for uid in set(to_users):
            _check().execute(
                "INSERT OR IGNORE INTO recommendation_recipients(rid, to_user) VALUES(?,?)",
                (rid, uid),
            )
        _conn.commit()
        return rid


def recommendations_rows() -> list:
    return query_all(
        "SELECT r.*, b.title AS book_title, b.author AS book_author, b.format AS book_format, "
        "COALESCE(b.is_private,0) AS book_private, u.username AS from_name, "
        "(SELECT GROUP_CONCAT(u2.username, ', ') FROM recommendation_recipients rr "
        "  JOIN users u2 ON u2.id = rr.to_user WHERE rr.rid = r.id) AS to_names "
        "FROM recommendations r JOIN books b ON b.id = r.book_id "
        "JOIN users u ON u.id = r.from_user ORDER BY r.id DESC"
    )


def recommendation_recipients(rid: int) -> list:
    return [r["to_user"] for r in query_all(
        "SELECT to_user FROM recommendation_recipients WHERE rid=?", (rid,))]


def delete_recommendation(rid: int) -> None:
    with _lock:
        _check().execute("DELETE FROM recommendation_recipients WHERE rid=?", (rid,))
        _check().execute("DELETE FROM recommendations WHERE id=?", (rid,))
        _conn.commit()


def delete_note(user_id: int, note_id: int) -> int:
    """删除划线（连带其想法）或单条想法，返回删除条数；仅限本人数据。"""
    row = query_one("SELECT id, kind FROM notes WHERE id=? AND user_id=?", (note_id, user_id))
    if row is None:
        return 0
    with _lock:
        if row["kind"] == "highlight":
            cur = _check().execute(
                "DELETE FROM notes WHERE id=? OR parent_id=?", (note_id, note_id)
            )
        else:
            cur = _check().execute("DELETE FROM notes WHERE id=? AND user_id=?", (note_id, user_id))
        _conn.commit()
        return cur.rowcount


def _check() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("数据库尚未初始化（init_db）")
    return _conn


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        _check().execute(sql, params)
        _conn.commit()


def query_all(sql: str, params: tuple = ()) -> list:
    with _lock:
        return _check().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> Optional[Any]:
    with _lock:
        return _check().execute(sql, params).fetchone()


def upsert_book(rel_path: str, size: int, mtime: float, fmt: str) -> tuple:
    """按相对路径入库；返回 (id, 状态)，状态为 'new' / 'changed' / 'same'。"""
    with _lock:
        row = _check().execute(
            "SELECT id, size, mtime FROM books WHERE rel_path=?", (rel_path,)
        ).fetchone()
        now = time.time()
        if row is None:
            cur = _conn.execute(
                "INSERT INTO books(rel_path, format, size, mtime, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (rel_path, fmt, size, mtime, now, now),
            )
            _conn.commit()
            return cur.lastrowid, "new"
        if row["size"] != size or abs((row["mtime"] or 0) - mtime) > 1e-6:
            _conn.execute(
                "UPDATE books SET size=?, mtime=?, meta_status=0, content_hash=NULL, "
                "updated_at=? WHERE id=?",
                (size, mtime, now, row["id"]),
            )
            _conn.commit()
            return row["id"], "changed"
        return row["id"], "same"


def set_book_hash(book_id: int, h: str) -> None:
    """写入内容 hash（不清 updated_at，hash 属于后台补全信息）。"""
    with _lock:
        _check().execute("UPDATE books SET content_hash=? WHERE id=?", (h, book_id))
        _conn.commit()


def find_by_hash(h: str):
    """按内容 hash 查重：返回第一条匹配（id/title/author/rel_path），无则 None。"""
    return query_one(
        "SELECT id, title, author, rel_path FROM books WHERE content_hash=? LIMIT 1", (h,))


def get_book(book_id: int):
    return query_one("SELECT * FROM books WHERE id=?", (book_id,))


def get_book_by_path(rel_path: str):
    return query_one("SELECT * FROM books WHERE rel_path=?", (rel_path,))


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
