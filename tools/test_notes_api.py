"""笔记/进度 API 的 UTF-8 安全测试（绕开 shell 中文传参问题）。"""
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
        with urllib.request.urlopen(req, timeout=10) as r:
            set_cookie = r.headers.get("Set-Cookie", "")
            token = set_cookie.split(";")[0] if set_cookie else None
            return r.status, json.loads(r.read().decode("utf-8")), token
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8")), None


# 登录两个账户
_, _, admin_tok = call("POST", "/api/auth/login",
                       {"username": "admin", "password": "admin123"})
_, _, user_tok = call("POST", "/api/auth/login",
                      {"username": "reader1", "password": "pass1234"})
admin_jar, user_jar = admin_tok, user_tok
print("login:", admin_jar is not None, user_jar is not None)

# user 添加划线 + 想法
code, r, _ = call("POST", "/api/notes",
                  {"book_id": 3, "kind": "highlight",
                   "content": "山川湖海，昼夜星辰，皆入文中。",
                   "anchor": "epubcfi(/6/4!/4/2,/1:0,/1:120)"}, user_jar)
print("highlight:", code, r)
hid = r.get("id")

code, r, _ = call("POST", "/api/notes",
                  {"book_id": 3, "kind": "thought",
                   "content": "开篇很有画面感", "parent_id": hid}, user_jar)
print("thought:", code, r)

code, r, _ = call("GET", "/api/notes?book_id=3", None, user_jar)
print("user notes book3:", json.dumps(r, ensure_ascii=False)[:400])

code, r, _ = call("GET", "/api/notes/all", None, user_jar)
print("user notes all count:", len(r["highlights"]))

code, r, _ = call("GET", "/api/notes/all", None, admin_jar)
print("admin notes all count (隔离应为0):", len(r["highlights"]))

code, r, _ = call("GET", "/api/progress/3", None, user_jar)
print("user progress book3:", r)

code, r, _ = call("GET", "/api/progress/3", None, admin_jar)
print("admin progress book3 (隔离应为空):", r)

# 删除划线（连带想法）
if hid:
    code, r, _ = call("DELETE", f"/api/notes/{hid}", None, user_jar)
    print("delete:", code, r)
    code, r, _ = call("GET", "/api/notes?book_id=3", None, user_jar)
    print("after delete count:", len(r["highlights"]))
