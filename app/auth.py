"""Autenticación con tokens de Amazon Cognito (JWT RS256) y modo de desarrollo local."""
from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import store
from .config import get_settings


@dataclass(frozen=True)
class User:
    id: str
    email: str = ""
    is_admin: bool = False


@lru_cache
def _jwks_client() -> jwt.PyJWKClient:
    s = get_settings()
    url = f"https://cognito-idp.{s.aws_region}.amazonaws.com/{s.cognito_user_pool_id}/.well-known/jwks.json"
    return jwt.PyJWKClient(url, cache_keys=True)


def _decode_cognito(token: str) -> User:
    s = get_settings()
    issuer = f"https://cognito-idp.{s.aws_region}.amazonaws.com/{s.cognito_user_pool_id}"
    key = _jwks_client().get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, options={"verify_aud": False})
    # Los access tokens de Cognito llevan client_id (no aud); los id tokens llevan aud.
    if claims.get("token_use") == "access":
        if claims.get("client_id") != s.cognito_client_id:
            raise jwt.InvalidTokenError("client_id")
    elif claims.get("aud") != s.cognito_client_id:
        raise jwt.InvalidTokenError("aud")
    groups = claims.get("cognito:groups") or []
    return User(id=claims["sub"], email=claims.get("email", ""), is_admin="admin" in groups)


def _decode_dev(token: str) -> User:
    # Bearer dev:<usuario>[:admin]
    parts = token.split(":")
    if len(parts) < 2 or parts[0] != "dev" or not parts[1]:
        raise jwt.InvalidTokenError("dev token")
    return User(id=f"dev-{parts[1]}", email=f"{parts[1]}@example.com", is_admin=parts[-1] == "admin")


# Declarar el esquema Bearer hace que /api/docs muestre el botón "Authorize" para probar con sesión.
_bearer = HTTPBearer(auto_error=False, description="Access token de Cognito (en local: dev:<usuario>)")


def optional_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User | None:
    if creds is None or creds.scheme.lower() != "bearer":
        return None
    token = creds.credentials.strip()
    try:
        user = _decode_dev(token) if get_settings().auth_mode == "dev" else _decode_cognito(token)
    except (jwt.PyJWTError, KeyError) as exc:
        raise HTTPException(401, "Token inválido o expirado.") from exc
    if store.moderation_has("user", user.id):
        raise HTTPException(403, "Esta cuenta está bloqueada.")
    return user


def require_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(401, "Necesitas iniciar sesión.")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Solo administradores.")
    return user
