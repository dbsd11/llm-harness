#!/usr/bin/env python3
"""Docker HEALTHCHECK: probe /api/health, exit 1 on failure."""
import os
import ssl
import sys
import urllib.request

port = os.getenv("WS_PORT", "8765")
scheme = "https" if os.getenv("WS_SSL_CERT") else "http"
url = f"{scheme}://127.0.0.1:{port}/api/health"

ctx = None
if scheme == "https":
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

try:
    req = urllib.request.Request(url, method="GET")
    resp = urllib.request.urlopen(req, timeout=5, context=ctx)
    if resp.getcode() == 200:
        sys.exit(0)
    print(f"Unhealthy: HTTP {resp.getcode()}")
    sys.exit(1)
except Exception as e:
    print(f"Unhealthy: {e}")
    sys.exit(1)
