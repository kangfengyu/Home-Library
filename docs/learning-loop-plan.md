# 学习闭环改造技术方案（书桌 · 复习调度 · 概念库 · AI荐学）

> 定位：把项目从"存储为中心"升级为"以马斯克学习法为灵魂的个人学习工具"。
> 四模块闭环：**AI荐学**（知新：生成课程，可荐书库外的书）→ **书桌**（总览：课程+最近阅读+复习入口）→ **复习调度**（温故：艾宾浩斯+费曼错题）→ **概念库**（沉淀：跨书概念串联）。
> 实施前提：所有现有文件行号以当前 main 分支为准；遵循项目既有约定（`_require_user` 鉴权、`asyncio.to_thread` 包同步 LLM 调用、i18n zh/en 双份、临时实例验证不碰主库）。

---

## 〇、现有挂点速查（已核实）

| 事实 | 位置 |
|---|---|
| tab 结构：recent(1)/library/feynman/speed/notes/questions/me/vault/hidden settings | static/index.html:574-602 |
| 合法 view 白名单（两处，需同步改） | static/index.html:1359、2800 |
| "我的"视图（统计卡+全员统计+家人荐书） | static/index.html:739-757 (`#view-me`) |
| "最近阅读"视图（recentGrid 网格） | static/index.html:634-641 (`#view-recent`) |
| 费曼 finish：LLM 评估产出 `{score,verdict,weak[]}`，weak 每条一句话、≤5条 | app/main.py:696-724 |
| feynman_sessions 表：score/verdict/weak(JSON字符串) | app/database.py:115-126 |
| 速读 Reduce 报告字段：`thesis/insights/concepts[{name,why}]×5/load_bearing/supporting/open/weak/teen/action` | app/speedread.py:308-335 |
| 速读 Map 块摘要（存 speed_chunks.summary，含 concepts[]），label=章节路标 | app/speedread.py:289-305、database.py:160-169 |
| job 完成落库点（概念入库钩子挂这里） | app/speedread.py:483 附近 `update_speed_job(status="done")` |
| LLM 公共层：`llm_cfg()` / `llm_request(cfg, messages, system, max_tokens, timeout)` | app/llm.py:14-81 |
| 容错 JSON 解析 | app/speedread.py:353 `parse_json_tolerant` |
| delete_user 级联删除（新表要追加进去） | app/database.py `delete_user` |
| 划线操作条 #selBar（取消/仅划线/划线并记想法）+ pendingSel 选区 `{kind:'epub',cfi,text}/{kind:'pdf',page,rects,text}` | static/reader.html:210-218、553-589 |
| 划线 anchor JSON（epub cfi / pdf page+rects）与两段式想法输入交互 | static/reader.html:566-581 |
| reader 笔记跳转参数 `?note={anchor}`（书桌"回到原文"复用） | static/reader.html:228 |
| SCHEMA（新表直接追加，启动 executescript 自动建） | app/database.py:18-174 |
| i18n 词典：zh ~949-1080 行 / en ~1083-1160 行 | static/index.html |

---

## 一、数据库变更（app/database.py）

SCHEMA 末尾追加三张表（`CREATE TABLE IF NOT EXISTS`，老库启动自动建，无需补列迁移）：

```sql
-- 1) 复习队列（艾宾浩斯）
CREATE TABLE IF NOT EXISTS review_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'feynman',  -- feynman / concept / manual
    front      TEXT NOT NULL,                    -- 复习问题（费曼weak条目原文 / 概念名）
    back       TEXT DEFAULT '',                  -- 参考答案（费曼=书名+考核日期；概念=why+thesis）
    stage      INTEGER NOT NULL DEFAULT 0,       -- 已连续"记得"次数（阶段索引）
    due_at     REAL NOT NULL DEFAULT 0,          -- 下次到期时间戳（秒）
    reps       INTEGER NOT NULL DEFAULT 0,       -- 总复习次数
    lapses     INTEGER NOT NULL DEFAULT 0,       -- "忘了"次数
    status     TEXT NOT NULL DEFAULT 'active',   -- active / done（毕业）
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_review_user ON review_items(user_id, status, due_at);

-- 2) 概念库（速读报告沉淀）
CREATE TABLE IF NOT EXISTS concepts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    name       TEXT NOT NULL,                    -- 概念名（速读 result.concepts[].name）
    why        TEXT DEFAULT '',                  -- 为什么必须先懂它
    thesis     TEXT DEFAULT '',                  -- 该书核心论旨（展示上下文用）
    job_id     INTEGER DEFAULT 0,                -- 来源速读 job
    created_at REAL DEFAULT 0,
    UNIQUE (user_id, book_id, name)              -- 幂等：rerun 后 upsert 不重复
);
CREATE INDEX IF NOT EXISTS idx_concepts_user ON concepts(user_id, name);

-- 3) 学习课程（AI荐学）
CREATE TABLE IF NOT EXISTS courses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    title      TEXT NOT NULL,
    goal       TEXT DEFAULT '',                  -- 用户原话目标
    plan       TEXT NOT NULL DEFAULT '',         -- JSON: {intro, steps:[...]}
    chat       TEXT DEFAULT '',                  -- JSON: [{role,content},...] 规划对话历史
    step_now   INTEGER NOT NULL DEFAULT 0,       -- 当前进行到第几步（0 起）
    status     TEXT NOT NULL DEFAULT 'active',   -- active / done / archived
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_courses_user ON courses(user_id, updated_at);

-- 4) AI老师问答（阅读中划线追问）
CREATE TABLE IF NOT EXISTS tutor_qa (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    book_id    INTEGER NOT NULL,
    anchor     TEXT DEFAULT '',      -- 与划线同构 JSON：{"type":"epub","cfi":..,"start":..} / {"type":"pdf","page":..,"rects":..}
    chapter    TEXT DEFAULT '',      -- EPUB=当前 TOC 章节标题 / PDF="第 N 页"
    quote      TEXT DEFAULT '',      -- 选中文案（≤500 字入库）
    question   TEXT DEFAULT '',      -- 用户问题（可空 = 请老师直接讲解这段）
    answer     TEXT DEFAULT '',      -- AI 回答（≤4000 字入库）
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tutor_user ON tutor_qa(user_id, created_at);
```

`plan.steps[]` 结构（LLM 产出后校验；**`book_id=0` 表示书库还没有的"外部书"**，靠 title/author 标识，入库后自动/手动关联）：
```json
[{"book_id": 12, "title": "书名", "author": "作者",
  "why": "为什么读它/在课程中的角色",
  "focus": ["重点章节路标（必须来自该书章节列表）"],
  "outcome": "读完这步应达到的状态"}]
```

**delete_user 级联追加**（在现有语句序列里加三行）：
```python
"DELETE FROM review_items WHERE user_id=?",
"DELETE FROM concepts WHERE user_id=?",
"DELETE FROM courses WHERE user_id=?",
"DELETE FROM tutor_qa WHERE user_id=?",
```

---

## 二、复习调度（后端）

### 算法（固定常量，无需配置）
```python
REVIEW_INTERVALS = [1, 2, 4, 7, 15, 30, 60]   # 天
# 入队：  stage=0, due_at=now（立即进入今日待复习）
# 记得：  stage+=1；stage > len(间隔) → status=done（毕业）；否则 due_at=now+间隔[stage-1]天
# 模糊：  stage 不变，due_at=now+1天
# 忘了：  stage=0，due_at=now+10分钟，lapses+=1
```
自评三键（忘了/模糊/记得），**零 token 成本**。

### API（app/main.py 新增）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/review/summary` | `{due, today_done, tomorrow, active}`（书桌顶部卡片数据） |
| GET | `/api/review/due?limit=50` | 到期卡列表（due_at<=now AND active，按 due_at 升序） |
| POST | `/api/review/{id}/grade` | body `{grade: 0忘了/1模糊/2记得}`，按上表更新 |
| POST | `/api/review` | 手动加卡 `{front, back?, book_id?}`（kind=manual） |
| DELETE | `/api/review/{id}` | 删除卡片 |

### 自动入队（改 app/main.py 费曼 finish）
`api_feynman_finish`（main.py:696）在 `finish_feynman_session` 成功后：
```python
added = database.enqueue_weak_reviews(user["id"], s["book_id"], s["book_title"], weak)
# weak 每条 → review_items(kind='feynman', front=条目,
#   back=f"《{book_title}》考核薄弱点 · {date:%m-%d}", due_at=now)
# 去重：同 user + 同 book + front 完全一致且 status=active 的已存在 → 跳过
# 返回 {"ok":.., "score":.., ..., "review_added": added}  ← 前端 finish 后 toast 提示"已加入 N 条复习"
```

### 前端交互
- 书桌"今日复习"卡片：`N 张待复习 · [开始复习]`，N=0 时显示"今日已清空 ✓ 明日 M 张"。
- 复习弹层（全屏 overlay，样式参考现有 modal/费曼对话）：正面显示 `front` → 点击"显示答案" → 露出 `back` + 三按钮（忘了/模糊/记得，支持键盘 1/2/3）。答完一张拉下一张，全部完成显示小结。
- 费曼报告的 weak 列表每条加"重新复习"小按钮（DELETE 旧 active 卡 + 重新入队）。
- 手动加卡入口：复习弹层右上"+"（书桌 v1 可选，优先级低）。

---

## 三、概念库

### 入库（零额外 LLM 调用）
app/speedread.py Reduce 成功处（`update_speed_job(jid, status="done", result=...)` 附近）追加：
```python
database.upsert_concepts(user_id, book_id, jid, result.get("concepts") or [],
                         thesis=result.get("thesis") or "")
# INSERT OR IGNORE 语义（UNIQUE user_id,book_id,name 兜底幂等，rerun 不重复）
```
同时把 `fallback` 降级路径（speedread.py:508-511）同样接上——先验报告的 concepts 也入库（job_id 标记即可，thesis 同步）。

### API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/concepts` | `?book_id=` 过滤；默认返回全部，按 name 排序。`grouped=1` 时按归一化 name（lower/trim）分组：`[{name, items:[{id,book_id,book_title,why}...]}]` 供跨书串联展示 |
| POST | `/api/concepts/{id}/to_review` | 建复习卡：front=`概念名`, back=`why + 《书名》`, kind=concept |
| DELETE | `/api/concepts/{id}` | 删除（同步删掉它生成的 active 复习卡可选，v1 不联动） |
| GET | `/api/books/{book_id}/outline` | 章节大纲：`{chapters:[{label, thesis}], load_bearing:[...]}`。优先取该书最近一次 done 的 speed_job 的 chunks(label+summary解析thesis) + result.load_bearing；无速读 → `{chapters:[], load_bearing:[]}`（同时给 AI荐学和前端"重点章节"跳转用） |

### 前端
书桌"概念库"模块：最近 8 个概念 chips（`概念名 · 书名`）+ "查看全部"打开弹层：搜索框 + 分组列表（同名概念多本书 → 展示"出现在 N 本书"，每条右侧[加入复习][删除]）。点击概念行 → 展开显示 why + 各书论旨。

---

## 四、AI荐学（学习规划）

> 显示名一律"AI荐学"；代码内部命名（data-view / i18n key / 端点路径 `learn`）沿用 `learn` 不变，避免无谓改名。

### 新 tab `learn`（最终 tab 顺序见第五节）
类 chat 页面（布局抄费曼视图）：消息流 + 底部输入框 + 发送按钮。

### 交互流
1. 用户输入目标（例："我想入门投资理财，能看懂财报，不被割韭菜"），可选附深度说明。
2. `POST /api/learn/plan` `{goal}`（≥8 字校验）→ 后端组装目录上下文 + LLM 生成课程（见下）→ 存 `courses`（含 chat 历史）→ 返回完整 course。
3. 前端把 LLM 的课程渲染成**时间线卡片**（步骤序号、书封缩略、why、focus 章节、outcome）。**书库已有的书**正常显示可跳阅读；**外部书**（书库没有）显示书名+作者 + 灰色"待入库"徽标——用户获取并上传书房后自动接上（见下"自动关联"）。
4. 后续轮次：同一页面继续输入（"第 2 步换成更基础的"）→ `POST /api/learn/plan/{course_id}/refine`（带完整 chat 历史重新生成，更新同一 course 的 plan）。也提供"全新目标"按钮开新课程。
5. 课程卡上有 **[加入书桌]**（v1：创建成功即出现在书桌，此按钮即"去书桌看看"；若想降低打扰可做成归档开关——采用前者，简单）。

### 目录上下文组装（token 控制）
```python
books = 全部可见书（private 过滤同 /api/books/pick），按 updated_at DESC 取前 200 本
每本：{"id", "title", "author", "desc": description[:120]}
若该书有 done 的速读 job：追加 "chapters": load_bearing[:6] + 各块 label 列表（去重、≤12 个）、"thesis"
（无速读的书 chapters 为空列表——LLM 对它们不产出 focus）
```

### Planner prompt（全文）
```python
system = (
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
user = "学习目标：" + goal + "\n\n书房书单（含速读分析的章节信息；book_id=0 表示书房还没有）：\n" + json.dumps(catalog, ensure_ascii=False)
```
调用：`await asyncio.to_thread(_llm_request, _llm_cfg(), [...], system=..., max_tokens=2500)`，
解析用 `speedread.parse_json_tolerant`。
**后端后处理（逐步骤）**：
1. `book_id>0`：必须存在于书库——不存在时若该步带 title 则降级为外部步（book_id=0），无 title 则整步丢弃；存在则用 DB 回填 title/author，并把 focus 与该书 outline 的 label 列表做包含匹配过滤（匹配不上的 focus 条目丢弃）。
2. `book_id=0`（外部书）：title 必填（空则丢弃该步），focus 强制 `[]`。
3. 解析失败 → 502 明确报错（复用 retryable_llm_error 判定，不自动重试 v1）。

### API
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/learn/plan` | `{goal}` → 生成课程 |
| POST | `/api/learn/plan/{cid}/refine` | `{goal}`（新一条消息）→ 重建 plan |
| GET | `/api/learn/courses` | 我的有效课程（status!=archived，updated_at DESC） |
| PUT | `/api/learn/courses/{cid}` | `{step_now?}`（推进步骤）/ `{status?}`（done/archived/active） |
| POST | `/api/learn/courses/{cid}/steps/{idx}/link` | `{book_id}`：手动把外部步关联到刚入库的书（校验课程所有权 + 书存在，回填 title/author） |
| DELETE | `/api/learn/courses/{cid}` | 删除课程 |

### 外部书入库后的自动关联
- 扫描入库单点 `metadata.process_book`（metadata.py:287）upsert 成功后调用
  `database.link_pending_steps(book_id, title, author)`：把所有用户课程里 `book_id=0` 且标题归一化相等
  （去空白、全角转半角、casefold）的外部步回填 book_id。**只做精确相等，不做模糊匹配**（个人课程数少，错关联比漏关联更伤）。
- 手动兜底：课程时间线上外部步有 [关联此书] 按钮 → 调 link 端点，从选书器（复用 `makePicker`/`/api/books/pick`）挑书。

### 书桌"我的课程"模块
每门课一张卡：课程名 + 进度（第 x/y 步）+ 当前步骤书名 + **[继续学习]**（`/reader?book={book_id}`，focus 章节有则带 hash 定位可后置）+ 步骤完成勾选（PUT step_now）。当前步是外部书（book_id=0）→ 显示"待入库：《title》· 获取上传后自动接上"，[继续学习] 禁用。步骤书读完（progress≥98%）**不自动推进**，卡片上给小提示"这本书读完了吗？→ 下一本"（v1 手动）。

---

## 五、AI老师（阅读中划线追问）

> 阅读页划线时除了"仅划线 / 划线并记想法"，新增"问AI老师"：带着**书名+作者+当前章节+选中文案+问题**去问 LLM，问答自动收录进书桌。

### 交互（static/reader.html，全部复用现有模式）
1. `#selBar`（210-218 行）加第三个按钮 `#selAsk` "🧑‍🏫 问AI老师"。
2. 复用 `#selThought` 的两段式交互（照 `#selSaveThought` 566-574 行模式）：首次点击 → textarea 显示、placeholder 改为"向AI老师提问…（留空 = 请老师讲解这段）"；再次点击/Enter → 发起提问。
3. 发起后：按钮禁用 + 文案"AI老师思考中…"（LLM 调用同步等待，timeout 60s），成功/失败恢复。
4. 回答展示：新增 `#tutorPanel` 覆盖层（居中卡片，样式随 toast/hlTip 体系）：`《书名》· 章节` 小标 + 引用原文（≤120 字摘录）+ 问题 + 回答（pre-wrap 保留换行），底部 [关闭]；Esc / 点遮罩关闭；toast 提示"已收录到书桌 · AI老师"。
5. anchor 直接复用划线同构 JSON（`doCreateNote` 580-581 行拼法）；chapter 取当前 TOC 章节标题（EPUB）/ `第 N 页`（PDF）；quote 截 500 字。
6. 未登录处理与划线一致（`doCreateNote` 里的 `me` 判断同款提示跳登录）。
7. **不自动创建划线**：问过 ≠ 想划线；想划的用户先点"仅划线"再问（顺序自由）。

### 后端（app/main.py 新增）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tutor/ask` | body `{book_id, anchor, chapter, quote, question}`（quote 必填；question 可空）。调 LLM → 存 tutor_qa → `{id, answer}` |
| GET | `/api/tutor/list?limit=30` | 最近问答（JOIN books 带 book_title，private 过滤同 notes），created_at DESC |
| DELETE | `/api/tutor/{id}` | 删除一条问答（校验 user_id） |

planner prompt（全文，`{title}/{author}/{chapter}` 后端填充）：
```python
system = (
    "你是一位博学、直接的私人读书导师。读者正在读《{title}》" + (f"（{author}）" if author else "") +
    "，读到「{chapter}」时选中了一段文字并向你提问。\n"
    "规则：1) 优先结合本书语境与这段原文作答；2) 可补充必要背景知识，但不要跑题；"
    "3) 不确定就明说，不要编造；4) 直接给答案，不要客套开头结尾；5) 少而精，300字内，可用短列表。"
)
user = (f"选中文本：“{quote[:500]}”\n\n读者的问题："
        + (question if question.strip() else "请讲解这段：它在讲什么、为什么重要、有什么背景或容易误解的地方。"))
```
调用：`await asyncio.to_thread(_llm_request, _llm_cfg(), [...], system=..., max_tokens=800, timeout=60)`。

### 书桌模块（见第六节布局 ⑤）
"AI老师"：最近问答卡 = 引用摘录（40字）+ 问题 + 回答（点击展开/收起）+ **[回到原文]**（`/reader?id={book_id}&note={encodeURIComponent(anchor)}`，复用 reader.html:228 笔记跳转机制）+ [删除]。无数据整节隐藏。

---

## 六、书桌（tab 与视图终态）

### Tab 顺序（唯一改动点 index.html:574-602）
```
书桌(desk) · 书架(library) · AI荐学(learn) · 费曼考核(feynman) · AI速读(speed) · 笔记(notes) · 问题(questions) · 暗格(vault,隐藏) · 设置(settings,隐藏)
```
具体改动：
1. `#view-me` → `id="view-desk"`，`data-view="me"` → `"desk"`，tab 按钮整体**移到第一位**，文案 `tab_me`→`tab_desk`："我的"→"书桌"（en: Desk）。
2. `tab_library` 文案："书库"→"书架"（en: Shelf）。视图内 crumb、`view_library`、`library` 等相关词典同步。
3. 新增 `learn` tab + `#view-learn` section（插在 library 后）。
4. **删除 recent tab 与 `#view-recent`**：recentGrid 网格整体迁入 `#view-desk` 顶部（"最近阅读"小节标题保留），`renderRecent()` 等逻辑不动只换容器 id。
5. 两处白名单数组（1359、2800）：`['recent','feynman',...]` → `['desk','library','learn','feynman','speed','notes','questions','vault','settings']`；`?view=recent|me` 的老链接做兼容映射 → `desk`（读 URL 参数处加一行映射再校验）。
6. `switchView` 内 pageTitle 映射、`k = {library:...}` 那行 toast 映射同步检查。
7. reader.html 顶栏"回书房"逻辑若带 view 参数同步检查（grep `view=` 确认）。

### 书桌内容布局（自上而下）
```
① 今日复习    N 张待复习 · [开始复习]     （+明日 M 张 · 进行中 K 张）
② 我的课程    课程卡 ×N（无课程时隐藏整节）
③ 最近阅读    recentGrid（原样迁入）
④ 概念库      概念 chips ×8 + [查看全部]（无概念时隐藏整节）
⑤ AI老师      最近问答卡 ×N：引用摘录 + 问题 + 回答展开 + [回到原文] + [删除]（无数据隐藏整节）
⑥ 阅读统计    三卡（今日/累计/已读完）+ 全员统计表 + 家人荐书（原 #view-me 内容原样保留）
```
说明：①②④⑤ 模块无数据时整节隐藏，保持新用户页面干净；⑥ 完整保留原"我的"内容。

---

## 七、i18n 词条（zh / en 各补 ~40 条；reader.html 的 STR 词典另加 ~8 条）

新增 key（示例 zh，en 同步翻译）：
```
tab_desk 书桌 / tab_learn AI荐学（AI Learn）
deskTitle 书桌 / reviewToday 今日复习 / reviewDue 张待复习 / reviewStart 开始复习
reviewClear 今日已清空，明日 M 张 / reviewShow 显示答案 / rForgot 忘了 / rFuzzy 模糊
rGotIt 记得 / reviewDone 复习完成：今天 N 张 / reviewAdded 已加入 N 条复习
reReview 重新复习 / myCourses 我的课程 / courseStep 第 x/y 步 / courseNext 下一本
courseGo 继续学习 / courseDone 完成课程 / courseArchive 归档
conceptTitle 概念库 / conceptAll 查看全部 / conceptInBooks 出现在 N 本书
conceptToReview 加入复习 / conceptPh 搜索概念…
learnPh 告诉我你想学什么、解决什么问题、达到什么程度…
learnSend 生成课程 / learnNew 全新目标
learnIntroNoCourse 还没有课程，输入学习目标开始 / learnRefining 正在重新规划…
stepPending 待入库 / stepLink 关联此书 / stepUploadHint 获取上传后自动接上
planTitle 课程 / stepWhy 为什么读它 / stepOutcome 读完你会
tutorTitle AI老师 / tutorJump 回到原文 / tutorEmpty 还没有问过AI老师
view_desk 书桌 / view_learn AI荐学
```
reader.html STR（zh/en 各加）：
```
selAsk 🧑‍🏫 问AI老师 / askPh 向AI老师提问…（留空 = 请老师讲解这段）
askSend 发送给AI老师 / aiThinking AI老师思考中… / tutorSaved 已收录到书桌 · AI老师
```
同步更新：`tab_library`→书架、`view_library`、`library`（如"回到书库"类文案）、`view_recent`/`view_me`/`tab_recent`/`tab_me` 删除。

---

## 八、实施顺序（5 个阶段，每阶段独立可验证、独立 commit）

### Phase 1 — 复习调度 + 书桌骨架
- database.py：review_items 表 + 算法/查询 helpers（due_items/grade_review/enqueue_weak_reviews/summary）
- main.py：5 个 review 端点 + 费曼 finish 自动入队（返回 review_added）
- index.html：me→desk 改名移位、recent 并入 desk、删 recent tab、白名单/兼容映射、今日复习卡 + 复习弹层交互、weak 列表"重新复习"按钮
- 验证：临时实例造 done 费曼会话（手工 UPDATE weak）→ summary/due/grade 全链路；`忘了`10 分钟后再出现；`记得`×7 毕业 done；UI 三键键盘可用
- **回归**：`?view=recent` 老链接跳 desk；tab 切换/标题正常；费曼/速读/笔记功能不回归

### Phase 2 — 概念库
- speedread.py：done 与 fallback 两处 upsert_concepts；rerun 幂等
- main.py：concepts 三个端点 + books/{id}/outline
- index.html：desk 概念模块 + 弹层（分组/搜索/加入复习/删除）
- 验证：速读一本 stub 书 → concepts 表 5 条；重跑 job 不重复；加入复习后 due 列表出现 kind=concept 卡；outline 返回 label+thesis

### Phase 3 — AI荐学
- database.py：courses 表 + helpers + link_pending_steps
- main.py：plan/refine/courses/PUT/link/DELETE 六端点 + 目录组装 + planner prompt + 步骤后处理
- metadata.py：process_book 入库成功后调 link_pending_steps（外部书自动接上）
- index.html：`#view-learn` chat 页 + 课程时间线渲染（外部步"待入库"徽标 + 关联按钮）+ refine 多轮 + 去书桌按钮
- 验证：目标"学会 XX"→ 书库已有的步骤带真实 book_id；出现外部书步骤（book_id=0、有 title/author、focus 空）；伪造不存在的 book_id → 降级为外部步或丢弃；上传同名 stub 书 → 扫描后外部步自动回填 book_id；手动 link 端点生效；refine 改步骤生效；LLM 未配置时报 400 引导设置

### Phase 4 — AI老师
- database.py：tutor_qa 表 + helpers（add/list/delete）
- main.py：ask / list / delete 三端点 + prompt（书名+作者+章节+选中文案+问题）
- reader.html：`#selBar` 加 `#selAsk` 按钮 + 两段式提问 + `#tutorPanel` 回答浮层 + STR 词条
- index.html：书桌 ⑤ AI老师模块（问答卡 + 回到原文 + 删除）
- 验证：EPUB 与 PDF 各问一次（带问题/留空讲解两形态）；断网/未配 LLM → toast 报错不崩；书桌列表出现记录、[回到原文] 正确跳回选中位置（epub cfi 与 pdf page 两种 anchor）；删除生效；未登录提示跳登录；问过不自动产生划线

### Phase 5 — 收尾
- delete_user 级联补四表（建临时用户 + 复习/概念/课程/AI问答数据 → 删号 → 四表无残留）
- i18n en 全量核对 + 移动端 375px 过一遍（复习弹层、课程时间线、概念弹层、AI老师浮层与书桌问答卡）
- 全量回归 + 主服务重启

---

## 九、风险与既定取舍

1. **复习自评不调 LLM**：零 token、秒响应；想要"AI 重判费曼讲得对不对"留给 v2（复用费曼评估 prompt 对单条 weak 追问）。
2. **概念跨书串联用名称精确匹配**（归一化 trim/lower），不做向量/模糊聚类——个人库规模下误合比漏合更伤，v2 再考虑。
3. **planner 单次调用不做多轮 agent**：多轮靠 refine（带 chat 历史整体重生成），实现简单且效果等价。
4. **课程进度手动推进**：自动联动 progress≥98% 推进留给 v2（需要定时任务/进度钩子，收益低）。
5. **章节摘要来源 = 速读数据**：没速读过的书 planner 只见 title/author/desc（focus 自动留空），外部书天然无 focus——想覆盖全库，速读一遍即是。
6. token 成本量级：planner 一次 ≈ 目录 200 本×40 token + 生成 1k ≈ 1 万 token 级；AI老师一次 ≈ 引文 500 字 + 回答 800 字 ≈ 2k token——个人使用均可忽略。
7. **AI老师回答不做流式**（v1 同步等待，timeout 60s 内返回）：urllib 同步调用改流式收益低、复杂度高；体验上用"思考中…"过渡即可，v2 再考虑 SSE。
