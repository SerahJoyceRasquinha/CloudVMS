"""FastAPI dependencies: authentication, permission checks, camera scoping."""
from __future__ import annotations

from typing import Callable

import jwt
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .core.config import get_settings
from .core.errors import AppError, forbidden, not_found
from .core.security import decode_token
from .db import get_db
from .models import Camera, User


def client_ip(request: Request) -> str:
    """The caller's IP. X-Forwarded-For is only believed when the direct peer is a configured
    trusted proxy; otherwise anyone could fake a new IP per request and dodge the login rate limit."""
    peer = request.client.host if request.client else ""
    fwd = request.headers.get("x-forwarded-for")
    if fwd and peer in get_settings().trusted_proxies:
        return fwd.split(",")[-1].strip()  # the address our proxy saw (earlier entries are client-supplied)
    return peer


def _user_from_token(db: Session, token: str, typ: str) -> tuple[User, dict]:
    try:
        data = decode_token(token, typ)
    except jwt.ExpiredSignatureError:
        raise AppError(401, "token_expired", "Your session has expired, please sign in again")
    except jwt.InvalidTokenError:
        raise AppError(401, "unauthorized", "Invalid authentication token")
    user = db.get(User, int(data["sub"]))
    if user is None or not user.is_active or user.token_version != data.get("tv"):
        raise AppError(401, "unauthorized", "Session is no longer valid")
    return user, data


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise AppError(401, "unauthorized", "Sign in required", None)
    user, _ = _user_from_token(db, auth[7:].strip(), "access")
    return user


def require(*perms: str) -> Callable:
    def dep(user: User = Depends(get_current_user)) -> User:
        # an initial / admin-reset password must be replaced before anything else is allowed
        # (/auth/me, /auth/logout and /auth/change-password use get_current_user and stay reachable)
        if user.must_change_password:
            raise AppError(403, "password_change_required", "Choose a new password before continuing")
        missing = [p for p in perms if p not in user.permissions]
        if missing:
            raise forbidden(f"Missing permission: {', '.join(missing)}")
        return user
    return dep


def stream_user(token: str, scope: str, db: Session) -> User:
    """Validate a short-lived stream token passed as a query parameter."""
    user, data = _user_from_token(db, token, "stream")
    if data.get("scp") != scope:
        raise forbidden("Token not valid for this stream")
    return user


def get_camera_for(user: User, db: Session, camera_id: int) -> Camera:
    cam = db.get(Camera, camera_id)
    # 404 (not 403) for cameras outside the user's scope so their existence isn't revealed
    if cam is None or not user.can_see_camera(camera_id):
        raise not_found("Camera")
    return cam


def scoped_camera_ids(user: User) -> list[int] | None:
    return None if user.camera_scope is None else list(user.camera_scope or [])
