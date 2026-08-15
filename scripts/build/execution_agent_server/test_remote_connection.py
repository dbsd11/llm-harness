#!/usr/bin/env python3
"""Test execution agent connection to remote WebSocket server."""

import os
import sys
import time
import subprocess

# Set environment variables for remote WebSocket server
os.environ['WS_HOST'] = 'agent-socket-server.bdzz.com.cn'
os.environ['WS_PORT'] = '8765'
os.environ['SERVER_ID'] = 'test-agent-1'
os.environ['SERVER_NAME'] = 'Test Agent Server'
os.environ['MAX_QUOTA'] = '4'

print("=" * 60)
print("Testing Execution Agent Connection to Remote WebSocket Server")
print("=" * 60)
print(f"\nTarget: ws://{os.environ['WS_HOST']}:{os.environ['WS_PORT']}")
print(f"Agent ID: {os.environ['SERVER_ID']}")
print(f"Agent Name: {os.environ['SERVER_NAME']}")
print("\nStarting execution agent server...")
print("Press Ctrl+C to stop\n")

try:
    # Start the execution agent server
    # The execution server will connect to the remote WebSocket server as a client
    subprocess.run([sys.executable, '-m', 'execution_server'], cwd=os.path.dirname(__file__))
except KeyboardInterrupt:
    print("\n\nTest stopped by user.")
except Exception as e:
    print(f"\n\nError: {e}")
    sys.exit(1)
