from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

try:
    import bcrypt
except ImportError as exc:  # pragma: no cover - exercised only on incomplete installs
    bcrypt = None  # type: ignore[assignment]
    _BCRYPT_IMPORT_ERROR: Optional[ImportError] = exc
else:
    _BCRYPT_IMPORT_ERROR = None

log = logging.getLogger(__name__)

VALID_ROLES = {"super_admin", "admin", "publisher", "reviewer", "reader"}
ADMIN_ROLES = {"super_admin", "admin"}
ACCESS_TOKEN_SECONDS = int(os.getenv("COMMONSOURCE_ACCESS_TOKEN_SECONDS", "900"))
REFRESH_TOKEN_SECONDS = int(os.getenv("COMMONSOURCE_REFRESH_TOKEN_SECONDS", str(14 * 24 * 60 * 60)))
PASSWORD_RESET_SECONDS = int(os.getenv("COMMONSOURCE_PASSWORD_RESET_SECONDS", "1800"))
CSRF_TOKEN_SECONDS = int(os.getenv("COMMONSOURCE_CSRF_TOKEN_SECONDS", "3600"))
LOGIN_MAX_FAILURES = int(os.getenv("COMMONSOURCE_LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCKOUT_SECONDS = int(os.getenv("COMMONSOURCE_LOGIN_LOCKOUT_SECONDS", "900"))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_JWT_SECRET_FILE = Path(
    os.getenv("COMMONSOURCE_JWT_SECRET_FILE", PROJECT_ROOT / "data" / "security" / "jwt_secret.key")
)
REQUIRE_ENV_JWT_SECRET = os.getenv("COMMONSOURCE_REQUIRE_JWT_SECRET", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "production",
}
_LOCAL_JWT_SECRET_CACHE: Optional[bytes] = None


class AuthError(RuntimeError):
    def __init__(self, message: str, status_code: int = 401, *, commit: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.commit = commit


class AuthConfigError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def validate_password(password: str) -> Optional[str]:
    password = password or ""
    if len(password) < 10:
        return "Password must be at least 10 characters"
    if not re.search(r"[A-Z]", password):
        return "Password must include an uppercase letter"
    if not re.search(r"[a-z]", password):
        return "Password must include a lowercase letter"
    if not re.search(r"\d", password):
        return "Password must include a number"
    if not re.search(r"[^A-Za-z0-9]", password):
        return "Password must include a symbol"
    return None


def require_bcrypt() -> None:
    if bcrypt is None:
        detail = f": {_BCRYPT_IMPORT_ERROR}" if _BCRYPT_IMPORT_ERROR else ""
        raise AuthConfigError(
            "bcrypt is unavailable in the Python process running CommonSource. "
            "Start the API with the project virtual environment or install bcrypt for that interpreter"
            f"{detail}"
        )


def hash_password(password: str) -> str:
    require_bcrypt()
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")  # type: ignore[union-attr]


def verify_password(password: str, password_hash: str) -> bool:
    require_bcrypt()
    try:
        return bool(bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8")))  # type: ignore[union-attr]
    except ValueError:
        return False


def run_migrations(conn: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row["id"] if isinstance(row, sqlite3.Row) else row[0]
        for row in conn.execute("SELECT id FROM schema_migrations").fetchall()
    }
    newly_applied: list[str] = []
    for path in sorted(migrations_dir.glob("*.sql")):
        migration_id = path.stem
        if migration_id in applied:
            continue
        log.info("[MIGRATION] Applying %s", migration_id)
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
            (migration_id, utc_now()),
        )
        newly_applied.append(migration_id)
    conn.commit()
    return newly_applied


def get_jwt_secret() -> bytes:
    secret = os.getenv("COMMONSOURCE_JWT_SECRET")
    if secret:
        if len(secret) < 32:
            log.warning("[AUTH] COMMONSOURCE_JWT_SECRET should be at least 32 characters")
        return secret.encode("utf-8")
    if REQUIRE_ENV_JWT_SECRET:
        raise AuthConfigError(
            "COMMONSOURCE_JWT_SECRET is required when COMMONSOURCE_REQUIRE_JWT_SECRET is enabled"
        )

    global _LOCAL_JWT_SECRET_CACHE
    if _LOCAL_JWT_SECRET_CACHE:
        return _LOCAL_JWT_SECRET_CACHE

    secret_path = LOCAL_JWT_SECRET_FILE
    if not secret_path.is_absolute():
        secret_path = PROJECT_ROOT / secret_path

    try:
        if secret_path.exists():
            secret = secret_path.read_text(encoding="utf-8").strip()
        else:
            secret = secrets.token_urlsafe(64)
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(secret + "\n", encoding="utf-8")
            try:
                os.chmod(secret_path, 0o600)
            except OSError:
                pass
            log.warning(
                "[AUTH] COMMONSOURCE_JWT_SECRET not set; generated persistent local JWT secret at %s",
                secret_path,
            )
    except OSError as exc:
        raise AuthConfigError(
            "COMMONSOURCE_JWT_SECRET is required because the local JWT secret file "
            f"could not be read or created: {exc}"
        ) from exc

    if len(secret) < 32:
        raise AuthConfigError("JWT secret must be at least 32 characters")
    _LOCAL_JWT_SECRET_CACHE = secret.encode("utf-8")
    return _LOCAL_JWT_SECRET_CACHE


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def create_access_token(user: Dict[str, Any], *, seconds: int = ACCESS_TOKEN_SECONDS) -> str:
    now = int(time.time())
    payload = {
        "sub": user["id"],
        "email": user["email"],
        "role": user["role"],
        "typ": "access",
        "iat": now,
        "exp": now + seconds,
        "jti": uuid.uuid4().hex,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(payload, separators=(',', ':')).encode())}"
    signature = hmac.new(get_jwt_secret(), signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        header_part, payload_part, signature_part = token.split(".")
    except ValueError as exc:
        raise AuthError("Invalid token", 401) from exc

    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    expected = hmac.new(get_jwt_secret(), signing_input, hashlib.sha256).digest()
    try:
        supplied = _b64url_decode(signature_part)
    except Exception as exc:
        raise AuthError("Invalid token", 401) from exc
    if not hmac.compare_digest(expected, supplied):
        raise AuthError("Invalid token signature", 401)

    try:
        header = json.loads(_b64url_decode(header_part))
        payload = json.loads(_b64url_decode(payload_part))
    except Exception as exc:
        raise AuthError("Invalid token payload", 401) from exc
    if header.get("alg") != "HS256" or payload.get("typ") != "access":
        raise AuthError("Invalid token type", 401)
    if int(payload.get("exp", 0)) < int(time.time()):
        raise AuthError("Token expired", 401)
    return payload


def create_csrf_token(user_id: str, *, seconds: int = CSRF_TOKEN_SECONDS) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "typ": "csrf",
        "iat": now,
        "exp": now + seconds,
        "nonce": secrets.token_urlsafe(16),
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(get_jwt_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


def validate_csrf_token(token: str, user_id: str) -> bool:
    try:
        body, signature_part = token.split(".", 1)
        supplied = _b64url_decode(signature_part)
    except Exception:
        return False
    expected = hmac.new(get_jwt_secret(), body.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied):
        return False
    try:
        payload = json.loads(_b64url_decode(body))
    except Exception:
        return False
    return (
        payload.get("typ") == "csrf"
        and payload.get("sub") == user_id
        and int(payload.get("exp", 0)) >= int(time.time())
    )


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def user_to_public(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    data.pop("password_hash", None)
    data["is_active"] = bool(data.get("is_active"))
    if "is_paid" in data:
        data["is_paid"] = bool(data.get("is_paid"))
    return data


def get_user_by_id(conn: sqlite3.Connection, user_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_to_public(row) if row else None


def get_user_by_email(conn: sqlite3.Connection, email: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()


def user_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def create_user(
    conn: sqlite3.Connection,
    *,
    name: str,
    email: str,
    password: str,
    role: str = "reader",
    is_active: bool = True,
) -> Dict[str, Any]:
    clean_name = (name or "").strip()
    clean_email = normalize_email(email)
    if not clean_name:
        raise AuthError("Name is required", 400)
    if not validate_email(clean_email):
        raise AuthError("A valid email is required", 400)
    if get_user_by_email(conn, clean_email):
        raise AuthError("Email is already registered", 409)
    password_error = validate_password(password)
    if password_error:
        raise AuthError(password_error, 400)
    if role not in VALID_ROLES:
        raise AuthError("Invalid role", 400)

    now = utc_now()
    user_id = make_id("user")
    conn.execute(
        """
        INSERT INTO users (id, name, email, password_hash, role, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, clean_name, clean_email, hash_password(password), role, 1 if is_active else 0, now, now),
    )
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_to_public(row)


def authenticate_user(conn: sqlite3.Connection, email: str, password: str) -> Dict[str, Any]:
    row = get_user_by_email(conn, email)
    if not row:
        raise AuthError("Invalid email or password", 401)
    locked_until = row["locked_until"] if "locked_until" in row.keys() else None
    if locked_until and parse_timestamp(locked_until) > datetime.now(timezone.utc):
        raise AuthError("Account is temporarily locked. Try again later.", 423)
    if not verify_password(password or "", row["password_hash"]):
        attempts = int(row["failed_login_attempts"] or 0) + 1 if "failed_login_attempts" in row.keys() else 1
        lock_until = None
        if attempts >= LOGIN_MAX_FAILURES:
            lock_until = (datetime.now(timezone.utc) + timedelta(seconds=LOGIN_LOCKOUT_SECONDS)).isoformat()
        conn.execute(
            "UPDATE users SET failed_login_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
            (attempts, lock_until, utc_now(), row["id"]),
        )
        if lock_until:
            raise AuthError("Account is temporarily locked. Try again later.", 423, commit=True)
        raise AuthError("Invalid email or password", 401, commit=True)
    if not row["is_active"]:
        raise AuthError("Account is inactive", 403)
    conn.execute(
        """
        UPDATE users
        SET failed_login_attempts = 0, locked_until = NULL, last_login_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (utc_now(), utc_now(), row["id"]),
    )
    return user_to_public(row)


def create_refresh_token(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    ip_address: str = "",
    seconds: int = REFRESH_TOKEN_SECONDS,
) -> tuple[str, Dict[str, Any]]:
    raw_token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    row = {
        "id": make_id("rt"),
        "user_id": user_id,
        "token_hash": token_hash(raw_token),
        "expires_at": (now + timedelta(seconds=seconds)).isoformat(),
        "created_at": now.isoformat(),
        "created_ip": ip_address,
    }
    conn.execute(
        """
        INSERT INTO refresh_tokens (id, user_id, token_hash, expires_at, created_at, created_ip)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (row["id"], user_id, row["token_hash"], row["expires_at"], row["created_at"], ip_address),
    )
    return raw_token, row


def issue_token_pair(
    conn: sqlite3.Connection,
    user: Dict[str, Any],
    *,
    ip_address: str = "",
) -> Dict[str, Any]:
    access_token = create_access_token(user)
    refresh_token, refresh_row = create_refresh_token(conn, user["id"], ip_address=ip_address)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_SECONDS,
        "refresh_expires_at": refresh_row["expires_at"],
    }


def refresh_token_pair(
    conn: sqlite3.Connection,
    refresh_token: str,
    *,
    ip_address: str = "",
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    row = conn.execute(
        """
        SELECT
            rt.*,
            u.name,
            u.email,
            u.role,
            u.is_active,
            u.is_paid,
            u.created_at AS user_created_at,
            u.updated_at AS user_updated_at
        FROM refresh_tokens rt
        JOIN users u ON u.id = rt.user_id
        WHERE rt.token_hash = ?
        """,
        (token_hash(refresh_token),),
    ).fetchone()
    if not row:
        raise AuthError("Invalid refresh token", 401)
    if row["revoked_at"]:
        raise AuthError("Refresh token revoked", 401)
    if parse_timestamp(row["expires_at"]) <= datetime.now(timezone.utc):
        raise AuthError("Refresh token expired", 401)
    if not row["is_active"]:
        raise AuthError("Account is inactive", 403)

    now = utc_now()
    conn.execute("UPDATE refresh_tokens SET revoked_at = ? WHERE id = ?", (now, row["id"]))
    user = user_to_public({
        "id": row["user_id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "is_active": row["is_active"],
        "is_paid": row["is_paid"] if "is_paid" in row.keys() else 0,
        "created_at": row["user_created_at"],
        "updated_at": row["user_updated_at"],
    })
    return user, issue_token_pair(conn, user, ip_address=ip_address)


def is_access_token_revoked(conn: sqlite3.Connection, jti: str) -> bool:
    if not jti:
        return True
    row = conn.execute(
        "SELECT expires_at FROM revoked_access_tokens WHERE jti = ?",
        (jti,),
    ).fetchone()
    if not row:
        return False
    if parse_timestamp(row["expires_at"]) <= datetime.now(timezone.utc):
        conn.execute("DELETE FROM revoked_access_tokens WHERE jti = ?", (jti,))
        return False
    return True


def revoke_access_token(conn: sqlite3.Connection, payload: Dict[str, Any]) -> bool:
    jti = payload.get("jti")
    user_id = payload.get("sub")
    exp = payload.get("exp")
    if not jti or not user_id or not exp:
        return False
    expires_at = datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO revoked_access_tokens
          (jti, user_id, expires_at, revoked_at)
        VALUES (?, ?, ?, ?)
        """,
        (jti, user_id, expires_at, utc_now()),
    )
    return True


def revoke_refresh_token(conn: sqlite3.Connection, refresh_token: str) -> bool:
    if not refresh_token:
        return False
    now = utc_now()
    cur = conn.execute(
        "UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE token_hash = ?",
        (now, token_hash(refresh_token)),
    )
    return cur.rowcount > 0


def refresh_token_user_id(conn: sqlite3.Connection, refresh_token: str) -> Optional[str]:
    if not refresh_token:
        return None
    row = conn.execute(
        "SELECT user_id FROM refresh_tokens WHERE token_hash = ?",
        (token_hash(refresh_token),),
    ).fetchone()
    return row["user_id"] if row else None


def create_password_reset_token(
    conn: sqlite3.Connection,
    email: str,
    *,
    ip_address: str = "",
) -> Optional[str]:
    row = get_user_by_email(conn, email)
    if not row or not row["is_active"]:
        return None
    raw_token = secrets.token_urlsafe(36)
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO password_reset_tokens
          (id, user_id, token_hash, expires_at, created_at, created_ip)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("prt"),
            row["id"],
            token_hash(raw_token),
            (now + timedelta(seconds=PASSWORD_RESET_SECONDS)).isoformat(),
            now.isoformat(),
            ip_address,
        ),
    )
    return raw_token


def reset_password(conn: sqlite3.Connection, reset_token: str, new_password: str) -> Dict[str, Any]:
    password_error = validate_password(new_password)
    if password_error:
        raise AuthError(password_error, 400)
    row = conn.execute(
        """
        SELECT
            prt.*,
            u.id AS user_id,
            u.name,
            u.email,
            u.role,
            u.is_active,
            u.is_paid,
            u.created_at AS user_created_at,
            u.updated_at AS user_updated_at
        FROM password_reset_tokens prt
        JOIN users u ON u.id = prt.user_id
        WHERE prt.token_hash = ?
        """,
        (token_hash(reset_token),),
    ).fetchone()
    if not row or row["used_at"]:
        raise AuthError("Invalid reset token", 400)
    if parse_timestamp(row["expires_at"]) <= datetime.now(timezone.utc):
        raise AuthError("Reset token expired", 400)
    if not row["is_active"]:
        raise AuthError("Account is inactive", 403)

    now = utc_now()
    conn.execute(
        """
        UPDATE users
        SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL, updated_at = ?
        WHERE id = ?
        """,
        (hash_password(new_password), now, row["user_id"]),
    )
    conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (now, row["id"]))
    conn.execute("UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?", (now, row["user_id"]))
    return user_to_public({
        "id": row["user_id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "is_active": row["is_active"],
        "is_paid": row["is_paid"] if "is_paid" in row.keys() else 0,
        "created_at": row["user_created_at"],
        "updated_at": now,
    })


def change_password(
    conn: sqlite3.Connection,
    user_id: str,
    current_password: str,
    new_password: str,
) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise AuthError("User not found", 404)
    if not row["is_active"]:
        raise AuthError("Account is inactive", 403)
    if not verify_password(current_password or "", row["password_hash"]):
        raise AuthError("Current password is incorrect", 401)
    password_error = validate_password(new_password)
    if password_error:
        raise AuthError(password_error, 400)
    now = utc_now()
    conn.execute(
        """
        UPDATE users
        SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL, updated_at = ?
        WHERE id = ?
        """,
        (hash_password(new_password), now, user_id),
    )
    conn.execute(
        "UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?) WHERE user_id = ?",
        (now, user_id),
    )
    updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return user_to_public(updated)


def list_users(
    conn: sqlite3.Connection,
    *,
    search: str = "",
    role: str = "",
    active: Optional[bool] = None,
    limit: int = 100,
    offset: int = 0,
    sort: str = "created_desc",
) -> list[Dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if search:
        where.append("(lower(name) LIKE ? OR lower(email) LIKE ?)")
        pattern = f"%{search.strip().lower()}%"
        params.extend([pattern, pattern])
    if role:
        if role not in VALID_ROLES:
            raise AuthError("Invalid role", 400)
        where.append("role = ?")
        params.append(role)
    if active is not None:
        where.append("is_active = ?")
        params.append(1 if active else 0)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    order_sql = {
        "created_desc": "created_at DESC",
        "created_asc": "created_at ASC",
        "email_asc": "email ASC",
        "email_desc": "email DESC",
    }.get(sort, "created_at DESC")
    rows = conn.execute(
        f"""
        SELECT id, name, email, role, is_active, is_paid, created_at, updated_at
        FROM users
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    return [user_to_public(row) for row in rows]


def count_users(
    conn: sqlite3.Connection,
    *,
    search: str = "",
    role: str = "",
    active: Optional[bool] = None,
) -> int:
    where: list[str] = []
    params: list[Any] = []
    if search:
        where.append("(lower(name) LIKE ? OR lower(email) LIKE ?)")
        pattern = f"%{search.strip().lower()}%"
        params.extend([pattern, pattern])
    if role:
        if role not in VALID_ROLES:
            raise AuthError("Invalid role", 400)
        where.append("role = ?")
        params.append(role)
    if active is not None:
        where.append("is_active = ?")
        params.append(1 if active else 0)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return int(conn.execute(f"SELECT COUNT(*) FROM users {where_sql}", params).fetchone()[0])


def update_user(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    name: Optional[str] = None,
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_paid: Optional[bool] = None,
) -> Dict[str, Any]:
    updates: list[str] = []
    params: list[Any] = []
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise AuthError("Name cannot be empty", 400)
        updates.append("name = ?")
        params.append(clean_name)
    if role is not None:
        if role not in VALID_ROLES:
            raise AuthError("Invalid role", 400)
        updates.append("role = ?")
        params.append(role)
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if is_active else 0)
    if is_paid is not None:
        updates.append("is_paid = ?")
        params.append(1 if is_paid else 0)
    if not updates:
        row = conn.execute(
            "SELECT id, name, email, role, is_active, is_paid, created_at, updated_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            raise AuthError("User not found", 404)
        return user_to_public(row)

    updates.append("updated_at = ?")
    params.append(utc_now())
    params.append(user_id)
    cur = conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    if cur.rowcount == 0:
        raise AuthError("User not found", 404)
    row = conn.execute(
        "SELECT id, name, email, role, is_active, is_paid, created_at, updated_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return user_to_public(row)


def role_allows(user_role: str, allowed_roles: Iterable[str]) -> bool:
    return user_role == "super_admin" or user_role in set(allowed_roles)
