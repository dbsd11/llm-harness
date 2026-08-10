"""Isolate httpx vs raw-socket on _global_loop_. Test A: httpx only, fresh server.
Test B: httpx with Connection: close. Test C: inspect protocol self.loop."""
import sys, os, time, asyncio, threading, socket
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
import uvicorn, httpx
from common.utils.global_loop_util import get_global_loop, _global_loop_

app = FastAPI()
_calls = 0
@app.get("/test")
def test():
    global _calls
    _calls += 1
    return {"ok": True}

def raw_get(port, path):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
    chunks = []
    while True:
        d = s.recv(4096)
        if not d: break
        chunks.append(d)
    s.close()
    return b"".join(chunks)

def run_server(port):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    fut = asyncio.run_coroutine_threadsafe(server.serve(), get_global_loop())
    deadline = time.time() + 8
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, fut

# --- Test A: httpx only, fresh server ---
print("=== TEST A: httpx only (fresh server) ===")
srv, fut = run_server(8094)
_calls = 0
try:
    r = httpx.get("http://127.0.0.1:8094/test", timeout=5)
    print(f"httpx -> {r.status_code} body={r.text!r} handler_calls={_calls}")
except Exception as e:
    print("httpx error:", type(e).__name__, e, "handler_calls=", _calls)
srv.should_exit = True; time.sleep(0.4)

# --- Test B: httpx with Connection: close ---
print("=== TEST B: httpx with Connection: close ===")
srv, fut = run_server(8095)
_calls = 0
try:
    r = httpx.get("http://127.0.0.1:8095/test", timeout=5, headers={"Connection": "close"})
    print(f"httpx(close) -> {r.status_code} body={r.text!r} handler_calls={_calls}")
except Exception as e:
    print("httpx error:", type(e).__name__, e, "handler_calls=", _calls)
srv.should_exit = True; time.sleep(0.4)

# --- Test C: raw socket only ---
print("=== TEST C: raw socket only (control) ===")
srv, fut = run_server(8096)
_calls = 0
raw = raw_get(8096, "/test")
first_line = raw.split(b"\r\n",1)[0].decode()
print(f"raw -> {first_line} handler_calls={_calls}")
srv.should_exit = True; time.sleep(0.4)
