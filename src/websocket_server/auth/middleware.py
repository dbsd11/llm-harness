"""aiohttp REST API 鉴权中间件

拦截所有 REST API 请求，验证 JWT API Key 并注入 tenant_id 到请求上下文。
"""
import os
from aiohttp import web

from logger import logger
from .jwt_utils import (
    verify_and_parse_api_key,
    extract_token_from_header,
    AuthError,
    TokenExpiredError,
    InvalidTokenError,
)

# 免鉴权白名单路由
AUTH_WHITELIST = {
    "/api/health",
}

# JWT 密钥（从环境变量读取，提供默认值用于开发）
_jwt_secret: str = None


def get_jwt_secret() -> str:
    """获取 JWT 密钥（懒加载）"""
    global _jwt_secret
    if _jwt_secret is None:
        _jwt_secret = os.getenv("WS_JWT_SECRET", "ws-platform-jwt-secret-2026")
    return _jwt_secret


def reset_jwt_secret():
    """重置 JWT 密钥（用于测试）"""
    global _jwt_secret
    _jwt_secret = None


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """REST API 鉴权中间件

    1. 白名单路由直接放行
    2. 从 Authorization 或 X-API-Key header 提取 token
    3. 验证 JWT 签名和过期时间
    4. 将 tenant_id 注入 request 上下文
    """
    # 白名单放行
    if request.path in AUTH_WHITELIST:
        return await handler(request)

    # 静态文件等非 API 路由放行
    if not request.path.startswith("/api/"):
        return await handler(request)

    # 提取 token
    token = extract_token_from_header(request)
    if not token:
        logger.warning(f"Missing API key for {request.method} {request.path}")
        return web.json_response(
            {"success": False, "error": "Unauthorized: Missing API key"},
            status=401,
        )

    # 验证 JWT
    secret = get_jwt_secret()
    try:
        payload = verify_and_parse_api_key(token, secret)
    except TokenExpiredError as e:
        logger.warning(f"Token expired for {request.method} {request.path}: {e}")
        return web.json_response(
            {"success": False, "error": "Unauthorized: Token expired"},
            status=401,
        )
    except InvalidTokenError as e:
        logger.warning(f"Invalid token for {request.method} {request.path}: {e}")
        return web.json_response(
            {"success": False, "error": "Unauthorized: Invalid token"},
            status=401,
        )
    except AuthError as e:
        logger.warning(f"Auth error for {request.method} {request.path}: {e}")
        return web.json_response(
            {"success": False, "error": f"Unauthorized: {e}"},
            status=401,
        )

    # 注入 tenant_id 到请求上下文
    tenant_id = payload["tenantId"]
    request["tenant_id"] = tenant_id

    # 日志脱敏：只打印 token 前 20 字符
    token_prefix = token[:20] + "..." if len(token) > 20 else token
    logger.debug(f"Auth OK: tenant={tenant_id}, token={token_prefix}")

    return await handler(request)
