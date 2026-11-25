from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from getpass import getpass
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import get_settings
from backend.database import (
    AuthLoginThrottle,
    AuthSession,
    SessionLocal,
    UserAccount,
    get_session,
)


ACCESS_COOKIE = "aerosense_access"
REFRESH_COOKIE = "aerosense_refresh"
CSRF_COOKIE = "aerosense_csrf"
ACCESS_LIFETIME = timedelta(minutes=30)
REFRESH_LIFETIME = timedelta(days=7)
THROTTLE_WINDOW = timedelta(minutes=15)
THROTTLE_LOCK = timedelta(minutes=15)
MAX_LOGIN_FAILURES = 5
MAX_SIGNUPS_PER_WINDOW = 5
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
ROLES = {"viewer", "analyst", "admin"}

auth_router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class RegistrationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(
        min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH
    )


def _auth_secret() -> bytes:
    secret = get_settings().auth_secret_key
    if secret is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication is not configured; set AUTH_SECRET_KEY.",
        )
    return secret.encode("utf-8")


def _normalise_email(email: str) -> str:
    value = email.strip().lower()
    if "@" not in value or any(character.isspace() for character in value):
        raise ValueError("A valid email address is required.")
    return value


def hash_password(password: str) -> str:
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise ValueError(
            f"Password must contain {PASSWORD_MIN_LENGTH} to {PASSWORD_MAX_LENGTH} characters."
        )
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**15,
        r=8,
        p=1,
        dklen=64,
        maxmem=64 * 1024 * 1024,
    )
    return "scrypt$32768$8$1${}${}".format(
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_value, digest_value = stored_hash.split("$")
        if algorithm != "scrypt" or (int(n), int(r), int(p)) != (2**15, 8, 1):
            return False
        expected = base64.urlsafe_b64decode(digest_value)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.urlsafe_b64decode(salt_value),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
    except (ValueError, TypeError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(actual, expected)


def _token_digest(token: str) -> str:
    return hmac.new(_auth_secret(), token.encode("utf-8"), hashlib.sha256).hexdigest()


def _new_token(session_id: str) -> str:
    return f"{session_id}.{secrets.token_urlsafe(32)}"


def _split_token(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    session_id, separator, secret = value.partition(".")
    if not separator or not secret:
        return None
    try:
        return str(uuid.UUID(session_id)), secret
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Authentication required.")


def _set_session_cookies(response: Response, access: str, refresh: str) -> None:
    settings = get_settings()
    response.set_cookie(
        ACCESS_COOKIE,
        access,
        max_age=int(ACCESS_LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/api/v1",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh,
        max_age=int(REFRESH_LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/api/v1/auth",
    )


def _set_csrf_cookie(response: Response, token: str | None = None) -> str:
    settings = get_settings()
    token = token or secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=int(REFRESH_LIFETIME.total_seconds()),
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )
    return token


def require_csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get("X-CSRF-Token", "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


def get_current_user(
    request: Request,
    session: Session = Depends(get_session),
) -> UserAccount:
    token_parts = _split_token(request.cookies.get(ACCESS_COOKIE))
    if token_parts is None:
        raise _unauthorized()
    session_id, token = token_parts
    auth_session = session.get(AuthSession, session_id)
    now = datetime.now(timezone.utc)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or _as_utc(auth_session.access_expires_at) <= now
        or not hmac.compare_digest(
            auth_session.access_token_hash, _token_digest(token)
        )
    ):
        raise _unauthorized()
    request.state.auth_session_id = auth_session.id
    user = session.get(UserAccount, auth_session.user_id)
    if user is None or not user.is_active:
        raise _unauthorized()
    return user


def require_roles(*roles: str) -> Callable:
    allowed = frozenset(roles)

    def check_role(user: UserAccount = Depends(get_current_user)) -> UserAccount:
        if user.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient role.")
        return user

    return check_role


def _throttle_row(session: Session, bucket_key: str, now: datetime) -> AuthLoginThrottle:
    dialect = session.get_bind().dialect.name
    values = {
        "bucket_key": bucket_key,
        "window_started_at": now,
        "failures": 0,
    }
    if dialect == "postgresql":
        statement = postgresql_insert(AuthLoginThrottle).values(**values)
        session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[AuthLoginThrottle.bucket_key]
            )
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(AuthLoginThrottle).values(**values)
        session.execute(
            statement.on_conflict_do_nothing(
                index_elements=[AuthLoginThrottle.bucket_key]
            )
        )
    else:
        if session.get(AuthLoginThrottle, bucket_key) is None:
            session.add(AuthLoginThrottle(**values))
            session.flush()
    row = session.get(AuthLoginThrottle, bucket_key, with_for_update=True)
    if row is None:
        raise RuntimeError("Login throttle row was not created.")
    if now - _as_utc(row.window_started_at) >= THROTTLE_WINDOW or (
        row.locked_until is not None and _as_utc(row.locked_until) <= now
    ):
        row.window_started_at = now
        row.failures = 0
        row.locked_until = None
    return row


def _user_payload(user: UserAccount) -> dict[str, int | str]:
    return {"id": user.id, "email": user.email, "role": user.role}


def _cleanup_expired_auth_data(session: Session, now: datetime) -> None:
    session.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    session.execute(
        delete(AuthLoginThrottle).where(
            AuthLoginThrottle.window_started_at < now - timedelta(days=1),
            or_(
                AuthLoginThrottle.locked_until.is_(None),
                AuthLoginThrottle.locked_until <= now,
            ),
        )
    )


def _create_authenticated_session(
    user: UserAccount, session: Session, response: Response, now: datetime
) -> dict[str, int | str]:
    session_id = str(uuid.uuid4())
    access = _new_token(session_id)
    refresh = _new_token(session_id)
    session.add(
        AuthSession(
            id=session_id,
            user_id=user.id,
            access_token_hash=_token_digest(access.split(".", 1)[1]),
            refresh_token_hash=_token_digest(refresh.split(".", 1)[1]),
            access_expires_at=now + ACCESS_LIFETIME,
            expires_at=now + REFRESH_LIFETIME,
        )
    )
    session.commit()
    _set_session_cookies(response, access, refresh)
    return {**_user_payload(user), "csrf_token": _set_csrf_cookie(response)}


@auth_router.get("/csrf")
def issue_csrf_token(response: Response) -> dict[str, str]:
    return {"csrf_token": _set_csrf_cookie(response)}


@auth_router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    _: None = Depends(require_csrf),
) -> dict[str, int | str]:
    secret = _auth_secret()
    try:
        email = _normalise_email(payload.email)
    except ValueError:
        email = payload.email.strip().lower()
    client_ip = request.client.host if request.client else "unknown"
    bucket_key = hmac.new(
        secret,
        f"{client_ip}\0{email}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    now = datetime.now(timezone.utc)
    _cleanup_expired_auth_data(session, now)
    throttle = _throttle_row(session, bucket_key, now)
    if throttle.locked_until is not None and _as_utc(throttle.locked_until) > now:
        retry_after = max(1, int((_as_utc(throttle.locked_until) - now).total_seconds()))
        session.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    user = session.scalar(select(UserAccount).where(UserAccount.email == email))
    stored_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    valid_password = verify_password(payload.password, stored_hash)
    if user is None or not user.is_active or not valid_password:
        throttle.failures += 1
        if throttle.failures >= MAX_LOGIN_FAILURES:
            throttle.locked_until = now + THROTTLE_LOCK
        session.commit()
        if throttle.locked_until is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again later.",
                headers={"Retry-After": str(int(THROTTLE_LOCK.total_seconds()))},
            )
        raise _unauthorized()

    throttle.failures = 0
    throttle.locked_until = None
    return _create_authenticated_session(user, session, response, now)


@auth_router.post("/register", status_code=201)
def register(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    _: None = Depends(require_csrf),
) -> dict[str, int | str]:
    secret = _auth_secret()
    try:
        email = _normalise_email(payload.email)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    client_ip = request.client.host if request.client else "unknown"
    bucket_key = hmac.new(
        secret,
        f"signup\0{client_ip}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    now = datetime.now(timezone.utc)
    _cleanup_expired_auth_data(session, now)
    throttle = _throttle_row(session, bucket_key, now)
    if throttle.locked_until is not None and _as_utc(throttle.locked_until) > now:
        retry_after = max(1, int((_as_utc(throttle.locked_until) - now).total_seconds()))
        session.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many account creation attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if throttle.failures >= MAX_SIGNUPS_PER_WINDOW:
        throttle.locked_until = now + THROTTLE_LOCK
        session.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many account creation attempts. Try again later.",
            headers={"Retry-After": str(int(THROTTLE_LOCK.total_seconds()))},
        )

    throttle.failures += 1
    if session.scalar(select(UserAccount.id).where(UserAccount.email == email)):
        session.commit()
        raise HTTPException(status_code=409, detail="An account with that email already exists.")

    user = UserAccount(
        email=email,
        password_hash=hash_password(payload.password),
        role="viewer",
        is_active=True,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Unable to create this account.") from error
    return _create_authenticated_session(user, session, response, now)


@auth_router.post("/refresh")
def refresh_session(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    _: None = Depends(require_csrf),
) -> dict[str, int | str]:
    token_parts = _split_token(request.cookies.get(REFRESH_COOKIE))
    if token_parts is None:
        raise _unauthorized()
    session_id, token = token_parts
    auth_session = session.get(AuthSession, session_id, with_for_update=True)
    now = datetime.now(timezone.utc)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or _as_utc(auth_session.expires_at) <= now
    ):
        raise _unauthorized()
    token_hash = _token_digest(token)
    if not hmac.compare_digest(auth_session.refresh_token_hash, token_hash):
        auth_session.revoked_at = now
        session.commit()
        raise _unauthorized()
    user = session.get(UserAccount, auth_session.user_id)
    if user is None or not user.is_active:
        auth_session.revoked_at = now
        session.commit()
        raise _unauthorized()

    access = _new_token(session_id)
    refresh = _new_token(session_id)
    auth_session.access_token_hash = _token_digest(access.split(".", 1)[1])
    auth_session.refresh_token_hash = _token_digest(refresh.split(".", 1)[1])
    auth_session.access_expires_at = now + ACCESS_LIFETIME
    auth_session.expires_at = now + REFRESH_LIFETIME
    session.commit()
    _set_session_cookies(response, access, refresh)
    return _user_payload(user)


@auth_router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    _: None = Depends(require_csrf),
) -> dict[str, str]:
    for cookie_name in (REFRESH_COOKIE, ACCESS_COOKIE):
        token_parts = _split_token(request.cookies.get(cookie_name))
        if token_parts is None:
            continue
        auth_session = session.get(AuthSession, token_parts[0])
        if auth_session is None or auth_session.revoked_at is not None:
            continue
        stored_hash = (
            auth_session.refresh_token_hash
            if cookie_name == REFRESH_COOKIE
            else auth_session.access_token_hash
        )
        if hmac.compare_digest(stored_hash, _token_digest(token_parts[1])):
            auth_session.revoked_at = datetime.now(timezone.utc)
            session.commit()
            break
    response.delete_cookie(ACCESS_COOKIE, path="/api/v1")
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    return {"status": "logged_out"}


@auth_router.get("/me")
def me(user: UserAccount = Depends(get_current_user)) -> dict[str, int | str]:
    return _user_payload(user)


def create_account(email: str, password: str, role: str, *, bootstrap: bool = False) -> None:
    normalized_email = _normalise_email(email)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(sorted(ROLES))}.")
    with SessionLocal.begin() as session:
        if bootstrap and session.scalar(select(UserAccount.id).limit(1)) is not None:
            raise ValueError("Bootstrap is only allowed before the first account exists.")
        if session.scalar(
            select(UserAccount.id).where(UserAccount.email == normalized_email)
        ) is not None:
            raise ValueError("An account with that email already exists.")
        session.add(
            UserAccount(
                email=normalized_email,
                password_hash=hash_password(password),
                role=role,
                is_active=True,
            )
        )


def reset_account_password(email: str, password: str) -> None:
    normalized_email = _normalise_email(email)
    password_hash = hash_password(password)
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        user = session.scalar(select(UserAccount).where(UserAccount.email == normalized_email))
        if user is None:
            raise ValueError("No account exists with that email.")
        user.password_hash = password_hash
        sessions = session.scalars(
            select(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
        for auth_session in sessions:
            auth_session.revoked_at = now


_DUMMY_PASSWORD_HASH = hash_password("aerosense-invalid-user-password")


def _run_cli() -> None:
    parser = argparse.ArgumentParser(description="Manage AeroSense API accounts.")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap-admin")
    bootstrap.set_defaults(role="admin", bootstrap=True)
    create = commands.add_parser("create-user")
    create.add_argument("--role", choices=sorted(ROLES), default="viewer")
    create.set_defaults(bootstrap=False)
    reset = commands.add_parser("reset-password")
    reset.add_argument("--email", required=True)
    args = parser.parse_args()

    if get_settings().auth_secret_key is None:
        parser.error("Set AUTH_SECRET_KEY before provisioning an account.")
    email = args.email if args.command == "reset-password" else input("Email: ").strip()
    password = getpass("Password (12+ characters): ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match.")
    try:
        if args.command == "reset-password":
            reset_account_password(email, password)
        else:
            create_account(email, password, args.role, bootstrap=args.bootstrap)
    except ValueError as error:
        parser.error(str(error))
    if args.command == "reset-password":
        print(f"Reset password for {email.strip().lower()} and revoked its sessions.")
    else:
        print(f"Created {args.role} account for {email.strip().lower()}.")


if __name__ == "__main__":
    _run_cli()
