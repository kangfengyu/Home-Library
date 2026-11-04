"""启动脚本：python run.py（等效于 python app/main.py）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))

import main  # noqa: E402

if __name__ == "__main__":
    main.run()
