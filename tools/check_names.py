"""校验样例书库文件名（避免 shell 传参中文转码问题）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import config  # noqa: E402

root = config.LIBRARY_ROOT
expected = [
    "文学/小说/三体 - 刘慈欣.epub",
    "文学/小说/百年孤独 - 加西亚·马尔克斯.epub",
    "技术/Python/流畅的Python - Luciano Ramalho.epub",
    "技术/架构/系统设计入门.pdf",
    "文学/散文/浮生六记.pdf",
    "漫画/国漫/星海镖师 第1卷.cbz",
    "漫画/日漫/海街日记 01.cbz",
]
files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
miss = 0
for e in expected:
    ok = e in files
    miss += (not ok)
    print(("OK  " if ok else "MISS") + " | " + e)
print("实际文件数:", len(files), "| 缺失:", miss)
sys.exit(1 if miss else 0)
