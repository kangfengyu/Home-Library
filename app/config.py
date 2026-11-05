"""全局配置：目录、端口、支持的文件格式。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 书库根目录：可用环境变量 LIBRARY_ROOT 覆盖
LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", str(BASE_DIR / "library"))).resolve()
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
COVER_DIR = DATA_DIR / "covers"
DB_PATH = DATA_DIR / "library.db"

# 局域网图书馆：默认监听所有网卡，允许同一局域网通过 IP:5000 访问
# （Windows 首次运行如弹防火墙提示，请选择“允许访问”；或手动放行 5000 端口）
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5000"))

# 支持的书籍格式（扫描收录 + 上传）
BOOK_EXTS = {".epub", ".pdf", ".cbz", ".cbr", ".mobi", ".azw3"}

# 在线阅读器可读格式（epub.js / PDF.js / zip 解压）；
# mobi/azw3 为 Amazon 私有格式，仅收录（书架展示 / 下载 / 统计），不支持在线阅读
READABLE_FORMATS = {"epub", "pdf", "cbz", "cbr"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# 封面缩略图目标宽度（px）
COVER_WIDTH = 400

# 内容类型映射（压缩包内图片 / 封面）
IMAGE_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
