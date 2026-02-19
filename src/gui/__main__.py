"""支持 python -m src.gui 启动方式。"""
import sys
from src.gui.app import main

if __name__ == "__main__":
    sys.exit(main())
