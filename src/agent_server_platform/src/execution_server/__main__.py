# `python -m execution_server` entry point.
import os
import sys

# Add src/ to sys.path so `core.*` and `execution_server.*` import cleanly
# (mirrors what src/app.py does).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from dotenv import load_dotenv
load_dotenv()

from execution_server.server import main

if __name__ == "__main__":
    main()
