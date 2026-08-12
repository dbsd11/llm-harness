"""鉴权模块

提供 JWT API Key 验证、REST API 中间件、WebSocket 握手鉴权功能。
"""
from .jwt_utils import (
    verify_and_parse_api_key,
    extract_token_from_header,
    extract_token_from_ws,
    AuthError,
    TokenExpiredError,
    InvalidTokenError,
    MissingTenantError,
)
from .middleware import auth_middleware, get_jwt_secret
from .ws_auth import authenticate_ws_connection

__all__ = [
    # JWT 工具函数
    "verify_and_parse_api_key",
    "extract_token_from_header",
    "extract_token_from_ws",
    # 异常类
    "AuthError",
    "TokenExpiredError",
    "InvalidTokenError",
    "MissingTenantError",
    # 中间件
    "auth_middleware",
    "get_jwt_secret",
    # WebSocket 鉴权
    "authenticate_ws_connection",
]
