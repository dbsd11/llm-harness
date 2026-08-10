"""WebSocket Server 入口点"""
import os
import sys

# 添加 src 到路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dotenv import load_dotenv
load_dotenv()

from websocket_server.server import main

if __name__ == "__main__":
    main()
