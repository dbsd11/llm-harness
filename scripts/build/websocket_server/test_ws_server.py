#!/usr/bin/env python3
"""测试 WebSocket Server (aiohttp client)

手动冒烟测试：连接 -> 注册 -> 收 ACK -> 心跳 -> 保持连接。
默认连本地 (127.0.0.1:8765)，可用 WS_URL 环境变量覆盖。
"""
import asyncio
import json
import os

import aiohttp

WS_URL = os.getenv("WS_URL", "ws://127.0.0.1:8765")


async def test_connection():
    """测试基本连接"""
    print(f"正在连接到 WebSocket Server: {WS_URL}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL) as ws:
                print("✓ 连接成功")

                # 发送注册请求
                register_frame = {
                    "type": "register",
                    "payload": {
                        "server_id": "test-server-1",
                        "name": "Test Server",
                        "quota": 4
                    }
                }
                await ws.send_str(json.dumps(register_frame))
                print("✓ 发送注册请求")

                # 等待响应
                msg = await ws.receive(timeout=5)
                print(f"✓ 收到响应: {msg.data}")

                # 发送心跳
                heartbeat_frame = {
                    "type": "heartbeat",
                    "payload": {
                        "server_id": "test-server-1",
                        "status": "idle"
                    }
                }
                await ws.send_str(json.dumps(heartbeat_frame))
                print("✓ 发送心跳")

                # 保持连接 10 秒
                print("保持连接 10 秒...")
                await asyncio.sleep(10)

                print("✓ 测试完成")

    except Exception as e:
        print(f"✗ 测试失败: {e}")


if __name__ == "__main__":
    asyncio.run(test_connection())
