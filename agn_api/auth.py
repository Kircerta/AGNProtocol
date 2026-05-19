from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import jwt
from jwt import InvalidTokenError


@dataclass(frozen=True)
class AuthContext:
    subject: str


class AuthError(Exception):
    pass



def decode_bearer_token(authorization: str | None, *, secret: str, algorithm: str) -> AuthContext:
    if not secret:
        raise AuthError("JWT secret not configured")

    if not authorization:
        raise AuthError("Missing Authorization header")

    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise AuthError("Authorization must use Bearer token")

    token = authorization[len(prefix):].strip()
    if not token:
        raise AuthError("Bearer token is empty")

    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
    except InvalidTokenError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc

    sub = payload.get("sub")
    if not sub:
        raise AuthError("Token missing 'sub'")

    exp = payload.get("exp")
    if exp is not None:
        now = datetime.now(tz=timezone.utc).timestamp()
        if float(exp) < now:
            raise AuthError("Token expired")

    return AuthContext(subject=str(sub))
