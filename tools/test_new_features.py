"""新功能后端测试：暗格权限、统计、笔记导出（UTF-8 安全）。"""
import json
import urllib.request

BASE = "http://127.0.0.1:5000"


def call(method, path, body=None, jar=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if jar:
        req.add_header("Cookie", jar)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            cookie = r.headers.get("Set-Cookie", "")
            tok = cookie.split(";")[0] if cookie else None
            return r.status, json.loads(r.read().decode("utf-8")), tok
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), None


# 登录
_, _, admin = call("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
_, _, user = call("POST", "/api/auth/login", {"username": "reader1", "password": "pass1234"})
print("login:", bool(admin), bool(user))

# 1) admin 把某本书设为私有（选一本 reader1 读过的：三体 id=3）
code, r, _ = call("POST", "/api/books/3/private", {"is_private": True}, admin)
print("set private:", code, r)

# 2) 暗格列表（admin 可见）
code, r, _ = call("GET", "/api/vault", None, admin)
print("vault(admin):", code, [b["title"] for b in r["books"]])

# 3) 普通用户视角：explore 不可见、book 404、file 404、cover 404、search 无、vault 403
code, r, _ = call("GET", "/api/explore?path=%E6%96%87%E5%AD%A6/%E5%B0%8F%E8%AF%B4", None, user)
print("explore(user) books:", [b["title"] for b in r.get("books", [])])
code, r, _ = call("GET", "/api/book/3", None, user)
print("book3(user):", code)
code, r, _ = call("GET", "/api/file/3", None, user)
print("file3(user):", code)
code, r, _ = call("GET", "/api/cover/3", None, user)
print("cover3(user):", code)
code, r, _ = call("GET", "/api/search?q=%E4%B8%89%E4%BD%93", None, user)
print("search 三体(user):", code, r.get("count"))
code, r, _ = call("GET", "/api/vault", None, user)
print("vault(user):", code)

# 4) admin 自己仍可读
code, r, _ = call("GET", "/api/book/3", None, admin)
print("book3(admin):", code, r.get("title"))

# 5) reader1 有 book3 的进度/笔记：recent 与 notes 应过滤
code, r, _ = call("GET", "/api/progress/recent", None, user)
print("recent(user):", code, [b["title"] for b in r.get("books", [])])
code, r, _ = call("PUT", "/api/progress/3", {"position": "{}", "percent": 0.5}, user)
print("progress3(user):", code)

# 6) 统计：心跳 + me + all
code, r, _ = call("POST", "/api/stats/heartbeat", {"seconds": 15}, user)
code, r, _ = call("POST", "/api/stats/heartbeat", {"seconds": 30}, admin)
code, r, _ = call("GET", "/api/stats/me", None, user)
print("stats(user):", r)
code, r, _ = call("GET", "/api/stats/all", None, user)
print("stats(all):", [(u["username"], u["today_seconds"], u["finished"]) for u in r["users"]])

# 7) 笔记导出
code, r, _ = call("POST", "/api/notes/export/save", None, admin)
print("notes export:", code, r)

# 8) 恢复：三体改回公开
code, r, _ = call("POST", "/api/books/3/private", {"is_private": False}, admin)
print("restore public:", code, r)
