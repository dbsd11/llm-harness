"""WebSocket 握手鉴权模块

在 WebSocket 连接建立时验证 API Key 并提取 tenant_id。
"""
from typing import Optional

from aiohttp import web

from logger import logger
from .jwt_utils import (
    verify_and_parse_api_key,
    extract_token_from_ws,
    AuthError,
    TokenExpiredError,
    InvalidTokenError,
)
from .middleware import get_jwt_secret


async def authenticate_ws_connection(request: web.Request) -> Optional[str]:
    """验证 WebSocket 连接的 API Key

    从 URL 参数、Authorization header 或 Sec-WebSocket-Protocol 中提取 token，
    验证签名和过期时间后返回 tenant_id。

    Args:
        request: aiohttp Request 对象

    Returns:
        验证成功返回 tenant_id，失败返回 None
    """
    # 提取 token
    token = extract_token_from_ws(request)
    if not token:
        logger.warning(f"WS connection missing API key from {request.remote}")
        return None

    # 验证 JWT
    secret = get_jwt_secret()
    try:
        payload = verify_and_parse_api_key(token, secret)
    except TokenExpiredError as e:
        logger.warning(f"WS token expired from {request.remote}: {e}")
        return None
    except InvalidTokenError as e:
        logger.warning(f"WS invalid token from {request.remote}: {e}")
        return None
    except AuthError as e:
        logger.warning(f"WS auth error from {request.remote}: {e}")
        return None

    tenant_id = payload["tenantId"]

    # 日志脱敏
    token_prefix = token[:20] + "..." if len(token) > 20 else token
    logger.info(f"WS auth OK: tenant={tenant_id}, token={token_prefix}")

    return tenant_id
