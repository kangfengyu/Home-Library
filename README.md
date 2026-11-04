# 📚 书房 · 本地电子书/漫画管理与在线阅读系统

轻量级、高性能的本地书库：FastAPI + SQLite 后端，Tailwind + 原生 JS 前端。
支持多账户、阅读进度记忆、划线笔记，Docker 一键部署。

## 功能

- **书库浏览**：文件夹递归浏览（EPUB / PDF / CBZ / CBR）、封面异步提取、豆瓣评分简介（失败优雅降级）、全文搜索
- **最近阅读**：按时间倒序展示最近 10 本，书架风格首页，一键续读到上次位置
- **在线阅读**：
  - EPUB：epub.js 渲染，目录跳转、字体调节、划线高亮
  - PDF：PDF.js 渲染（CJK CMap 完整支持），懒加载、缩放
  - 漫画：按需解压逐页显示（防 OOM）
  - 全格式统一：单页/双页、左右翻页/上下滚动、日间/夜间模式、全屏
- **笔记**：阅读中划线原文（EPUB 文本批注 / PDF 选区矩形），可附想法；笔记页集中管理、点击跳回原文
- **多账户**：管理员添加账户；阅读进度与笔记按账户隔离

## 快速开始（本地）

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux: .venv/bin/pip ...
python tools/make_samples.py   # 可选：生成演示样例书库
start.bat                      # 或 python run.py / python app/main.py
```

打开 <http://localhost:5000>，默认管理员：`admin / admin123`（可用环境变量 `ADMIN_PASSWORD` 覆盖，登录后请尽快修改）。

## Docker 部署

```bash
docker compose up -d        # 构建并启动，访问 http://localhost:5000
```

| 卷 | 内容 | 迁移 |
|---|---|---|
| `./library` | 书籍原文件 | 整目录拷贝 |
| `./data` | SQLite 库 + 封面缓存 + 日志 | 整目录拷贝，或管理员账户里点「导出数据备份」（zip） |

换设备：拷走 `library/` 与 `data/` 两个目录即可完整迁移（账户、进度、笔记全部保留）。
CBR 支持通过容器内 `bsdtar`（libarchive-tools）实现。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `LIBRARY_ROOT` | `./library`（容器内 `/library`） | 书库根目录 |
| `DATA_DIR` | `./data`（容器内 `/data`） | 数据目录 |
| `PORT` / `HOST` | `5000` / `127.0.0.1`（容器内 `0.0.0.0`） | 监听地址 |
| `ADMIN_PASSWORD` | `admin123` | 首次启动 admin 初始密码 |
| `LLM_PROTOCOL` | 设置页值 | `anthropic` / `openai`（**优先级高于设置页**） |
| `LLM_BASE_URL` | 设置页值 | 例 `https://api.minimax.cn/anthropic` |
| `LLM_MODEL` | 设置页值 | 例 `MiniMax-M3`（模型名在厂商目录里，URL 推不出） |
| `LLM_API_KEY` | 设置页值 | 覆盖设置页的 key（Docker / 测试实例用） |

## 架构

- `app/main.py` — 全部路由：浏览/详情/封面/文件/漫画分页/搜索/扫描 + 认证/账户/进度/笔记/导出 + 问题 Q&A + 费曼考核 + AI 速读
- `app/scanner.py` — 递归扫描 + 后台元数据队列（asyncio worker）+ 豆瓣补抓
- `app/metadata.py` — EPUB(zip+xml)/CBZ(zip)/CBR(rarfile+bsdtar)/PDF(PyMuPDF→pypdf 降级) 解析
- `app/database.py` — SQLite WAL + 线程锁；books/users/sessions/progress/notes/questions/settings/feynman_*/speed_* 表；PBKDF2 密码
- `app/llm.py` — LLM 调用公共层：anthropic / openai 协议适配 + 配置读取（设置页存 settings 表，环境变量可覆盖）
- `app/speedread.py` — AI 速读 Map-Reduce 流水线：提取 → 分块 → 并行 Map → 聚合 Reduce
- `static/index.html` — 九视图（最近阅读/AI 速读/费曼考核/书库/笔记/问题/我的/暗格/设置）+ 云朵浮层 + 中英双语
- `static/reader.html` — 三格式阅读器：统一 单/双页 + 上下/左右 模式、续读、划线与想法

## AI 功能

两个基于 LLM 的学习功能，共用设置页的同一份 LLM 配置：

- **费曼考核** — 选一本书，AI 扮演小白听你讲书、不断追问，结束后生成掌握度评分与「没懂的地方」清单
- **AI 速读** — Map-Reduce 流水线分块并行精读整本书，聚合出四组问题的深度报告（核心论旨 / 承重章节 / 批评视角 / 三句话讲透 + 行动点）

## 安全

- 书库路径越权防护（规范化路径校验，防目录穿越）
- 漫画页逐页按需解压；PDF.js Range 流式取页
- 密码 PBKDF2-SHA256（10 万轮）；会话 HttpOnly Cookie；笔记/进度严格按 user_id 隔离
- 「暗格」私有书对非管理员一律返回 404（不泄露存在性）

## 文档

- **[docs/HANDOVER.md](docs/HANDOVER.md)** — 交接文档：环境约束、架构、功能清单、待办、接手检查清单
- **[docs/AI_SPEEDREAD_DESIGN.md](docs/AI_SPEEDREAD_DESIGN.md)** — AI 速读方案与决策记录（为什么不用整本直读、先验知识的两处正确用法）

