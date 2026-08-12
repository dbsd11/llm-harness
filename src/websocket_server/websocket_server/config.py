"""WebSocket Server 配置加载

单端口设计：WS_HOST/WS_PORT (默认 8765) 同时服务 WebSocket 和 REST API。
"""
import os
from dotenv import load_dotenv

def load_config():
    """加载配置"""
    load_dotenv()

    return {
        "host": os.getenv("WS_HOST", "0.0.0.0"),
        "port": int(os.getenv("WS_PORT", "8765")),
        "heartbeat_interval": int(os.getenv("HEARTBEAT_INTERVAL", "5")),
        "heartbeat_timeout": int(os.getenv("HEARTBEAT_TIMEOUT", "15")),
        # 启用 TLS(wss/https)：证书+私钥路径，同时设置后端口切换为加密（与明文互斥）
        "ssl_cert": os.getenv("WS_SSL_CERT"),
        "ssl_key": os.getenv("WS_SSL_KEY"),
        # JWT API Key 签名密钥（用于多租户鉴权）
        "jwt_secret": os.getenv("WS_JWT_SECRET", "ws-platform-jwt-secret-2026"),
        # 租户隔离严格模式（false=兼容模式允许 NULL tenant_id，true=严格模式）
        "tenant_strict": os.getenv("WS_TENANT_STRICT", "false").lower() == "true",
    }
