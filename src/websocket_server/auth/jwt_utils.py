"""JWT API Key 验证与解析模块

零依赖实现 HS256 签名验证，从 API Key 中提取 tenant_id。
"""
import hmac
import hashlib
import base64
import json
import time
from typing import Optional


class AuthError(Exception):
    """鉴权错误基类"""
    pass


class TokenExpiredError(AuthError):
    """Token 已过期"""
    pass


class InvalidTokenError(AuthError):
    """Token 格式错误或签名无效"""
    pass


class MissingTenantError(AuthError):
    """Token 中缺少 tenantId"""
    pass


def base64url_decode(input_str: str) -> bytes:
    """Base64URL 解码，自动补齐 padding"""
    rem = len(input_str) % 4
    if rem > 0:
        input_str += '=' * (4 - rem)
    input_str = input_str.replace('-', '+').replace('_', '/')
    return base64.b64decode(input_str)


def base64url_encode(input_bytes: bytes) -> str:
    """Base64URL 编码，去除 padding"""
    encoded = base64.b64encode(input_bytes).decode('utf-8')
    return encoded.replace('+', '-').replace('/', '_').rstrip('=')


def verify_and_parse_api_key(api_key: str, secret: str) -> dict:
    """验证 JWT 签名并解析 payload

    Args:
        api_key: JWT 格式的 API Key（三段式：header.payload.signature）
        secret: HMAC-SHA256 签名密钥

    Returns:
        解析后的 payload dict，包含 tenantId, iat, exp 等字段

    Raises:
        InvalidTokenError: Token 格式错误或签名无效
        TokenExpiredError: Token 已过期
        MissingTenantError: Token 中缺少 tenantId
    """
    if not api_key:
        raise InvalidTokenError("Empty API key")

    parts = api_key.split('.')
    if len(parts) != 3:
        raise InvalidTokenError("Invalid token format: expected 3 parts")

    header_b64, payload_b64, sig_b64 = parts

    # 计算期望签名
    sig_input = f"{header_b64}.{payload_b64}".encode('utf-8')
    expected_sig = hmac.new(
        secret.encode('utf-8'),
        sig_input,
        hashlib.sha256
    ).digest()
    expected_sig_b64 = base64url_encode(expected_sig)

    # 恒定时间比较防时序攻击
    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        raise InvalidTokenError("Invalid signature")

    # 解码 Payload
    try:
        payload_json = base64url_decode(payload_b64).decode('utf-8')
        payload = json.loads(payload_json)
    except Exception as e:
        raise InvalidTokenError(f"Failed to decode payload: {e}")

    # 校验过期时间
    now = int(time.time())
    exp = payload.get("exp")
    if exp and now > exp:
        raise TokenExpiredError(f"Token expired at {exp}, current time is {now}")

    # 校验 tenantId
    tenant_id = payload.get("tenantId")
    if not tenant_id:
        raise MissingTenantError("Missing tenantId in token payload")

    return payload


def extract_token_from_header(request) -> Optional[str]:
    """从 HTTP 请求头中提取 API Key

    支持以下格式：
    - Authorization: Bearer <token>
    - X-API-Key: <token>

    Args:
        request: aiohttp Request 对象

    Returns:
        提取到的 token 字符串，如果不存在返回 None
    """
    # 优先检查 Authorization header
    auth_header = request.headers.get("Authorization")
    if auth_header:
        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()

    # 备用 X-API-Key header
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        return api_key_header.strip()

    return None


def extract_token_from_ws(request) -> Optional[str]:
    """从 WebSocket 握手请求中提取 API Key

    支持以下方式（按优先级）：
    1. URL 查询参数：?api_key=<token>
    2. Authorization: Bearer <token>
    3. Sec-WebSocket-Protocol: api-key, <token>

    Args:
        request: aiohttp Request 对象

    Returns:
        提取到的 token 字符串，如果不存在返回 None
    """
    # 1. URL 查询参数
    token = request.query.get("api_key")
    if token:
        return token.strip()

    # 2. Authorization header
    token = extract_token_from_header(request)
    if token:
        return token

    # 3. Sec-WebSocket-Protocol
    protocols = request.headers.get("Sec-WebSocket-Protocol", "")
    if protocols:
        for proto in protocols.split(","):
            proto = proto.strip()
            if proto.startswith("api-key."):
                return proto[8:].strip()
            elif proto == "api-key":
                continue
            elif "." in proto:
                # 格式: api-key.<token>
                parts = proto.split(".", 1)
                if parts[0] == "api-key" and len(parts) > 1:
                    return parts[1].strip()

    return None
