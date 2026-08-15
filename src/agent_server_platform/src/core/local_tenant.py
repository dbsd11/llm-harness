# Tenant context singleton — extracts tenantId from WS_SERVER_API_KEY JWT at startup.
#
# ASP is single-tenant per deployment: each instance holds one JWT with one tenantId.
# No signature verification needed — ASP is the token holder, not the verifier.
import base64
import json
import os

from logger import logger

_tenant_id: str = None


def _base64url_decode(s: str) -> bytes:
    rem = len(s) % 4
    if rem > 0:
        s += '=' * (4 - rem)
    return base64.b64decode(s.replace('-', '+').replace('/', '_'))


def init_tenant():
    """Extract tenantId from WS_SERVER_API_KEY JWT. Call once at startup."""
    global _tenant_id
    api_key = os.getenv("WS_SERVER_API_KEY", "")
    if not api_key:
        logger.warning("WS_SERVER_API_KEY not set — tenant_id will be None")
        _tenant_id = None
        return

    parts = api_key.split('.')
    if len(parts) != 3:
        logger.warning(f"WS_SERVER_API_KEY is not a valid JWT (got {len(parts)} parts)")
        _tenant_id = None
        return

    try:
        payload = json.loads(_base64url_decode(parts[1]).decode('utf-8'))
        _tenant_id = payload.get("tenantId")
        if _tenant_id:
            logger.info(f"Tenant context initialized: tenant_id={_tenant_id}")
        else:
            logger.warning("JWT payload missing tenantId claim")
    except Exception as e:
        logger.warning(f"Failed to parse JWT payload: {e}")
        _tenant_id = None


def get_tenant_id() -> str:
    """Return the current deployment's tenant_id (None if not initialized)."""
    return _tenant_id
