"""Small standard-library security primitives for the Step 3 web process."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any, Callable, Mapping


__all__ = (
    "CredentialRecord",
    "LoginThrottle",
    "MIN_NEW_PASSWORD_CHARACTERS",
    "SESSION_COOKIE_NAME",
    "SecurityError",
    "SessionCredentials",
    "SessionPrincipal",
    "SessionStore",
    "ThrottleDecision",
    "build_session_cookie",
    "clear_session_cookie",
    "validate_new_password",
    "verify_credentials",
)

CREDENTIAL_SCHEMA_VERSION = 1
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_SALT_BYTES = 16
SCRYPT_MAXMEM = 64 * 1024 * 1024
MAX_CREDENTIAL_JSON_BYTES = 8 * 1024
MAX_USERNAME_BYTES = 64
MAX_PASSWORD_BYTES = 1024
MIN_NEW_PASSWORD_CHARACTERS = 12
SESSION_COOKIE_NAME = "tinypirelay_session"

_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}\Z")
_DUMMY_SALT = b"TinyPiRelayDummy"
_DUMMY_DIGEST = bytes(SCRYPT_DKLEN)
_MAX_RANDOM_ATTEMPTS = 8


class SecurityError(ValueError):
    """Security input or state is invalid; messages never echo input."""


def _invalid(message: str = "invalid security input") -> SecurityError:
    return SecurityError(message)


def _username(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_USERNAME_BYTES
        or _USERNAME.fullmatch(value) is None
    ):
        raise _invalid("invalid username")
    if len(value.encode("ascii")) > MAX_USERNAME_BYTES:
        raise _invalid("invalid username")
    return value


def _password(value: object, *, allow_empty: bool) -> bytes:
    if not isinstance(value, str) or len(value) > MAX_PASSWORD_BYTES:
        raise _invalid("invalid password")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        raise _invalid("invalid password") from None
    if (not allow_empty and not encoded) or len(encoded) > MAX_PASSWORD_BYTES:
        raise _invalid("invalid password")
    return encoded


def validate_new_password(password: object) -> str:
    """Apply the installation/reset policy without changing stored hashes."""

    _password(password, allow_empty=False)
    if not isinstance(password, str) or len(password) < MIN_NEW_PASSWORD_CHARACTERS:
        raise _invalid(
            f"new password must contain at least {MIN_NEW_PASSWORD_CHARACTERS} characters"
        )
    return password


def _derive(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password,
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=SCRYPT_MAXMEM,
    )


def _random_bytes(factory: Callable[[int], bytes], size: int) -> bytes:
    try:
        value = factory(size)
    except Exception as exc:
        raise _invalid("secure random generation failed") from None
    if type(value) is not bytes or len(value) != size:
        raise _invalid("secure random generation failed")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("invalid credential record")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _invalid("invalid credential record")


def _exact_object(value: object, keys: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid("invalid credential record")
    return value


def _decode_field(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) > 256:
        raise _invalid("invalid credential record")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError):
        raise _invalid("invalid credential record") from None
    if len(decoded) != size:
        raise _invalid("invalid credential record")
    return decoded


@dataclass(frozen=True, repr=False)
class CredentialRecord:
    """One versioned scrypt credential record; hash material is repr-hidden."""

    username: str
    _salt: bytes = field(repr=False)
    _digest: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _username(self.username)
        if type(self._salt) is not bytes or len(self._salt) != SCRYPT_SALT_BYTES:
            raise _invalid("invalid credential record")
        if type(self._digest) is not bytes or len(self._digest) != SCRYPT_DKLEN:
            raise _invalid("invalid credential record")

    def __repr__(self) -> str:
        return (
            "CredentialRecord(schema_version=1, algorithm='scrypt', "
            f"username={self.username!r}, hash=<redacted>)"
        )

    @classmethod
    def create(
        cls,
        username: object,
        password: object,
        *,
        rng: Callable[[int], bytes] = secrets.token_bytes,
    ) -> "CredentialRecord":
        name = _username(username)
        secret = _password(password, allow_empty=False)
        salt = _random_bytes(rng, SCRYPT_SALT_BYTES)
        try:
            digest = _derive(secret, salt)
        except (MemoryError, ValueError):
            raise _invalid("credential hashing failed") from None
        return cls(name, salt, digest)

    @classmethod
    def from_json(cls, text: object) -> "CredentialRecord":
        if not isinstance(text, str) or len(text) > MAX_CREDENTIAL_JSON_BYTES:
            raise _invalid("invalid credential record")
        try:
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_CREDENTIAL_JSON_BYTES:
                raise _invalid("invalid credential record")
            value = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            root = _exact_object(
                value, {"schema_version", "username", "password_hash"}
            )
            if type(root["schema_version"]) is not int or root["schema_version"] != 1:
                raise _invalid("invalid credential record")
            hashed = _exact_object(
                root["password_hash"],
                {
                    "algorithm",
                    "n",
                    "r",
                    "p",
                    "dklen",
                    "salt_b64",
                    "digest_b64",
                },
            )
            expected = {
                "algorithm": "scrypt",
                "n": SCRYPT_N,
                "r": SCRYPT_R,
                "p": SCRYPT_P,
                "dklen": SCRYPT_DKLEN,
            }
            for key, expected_value in expected.items():
                actual = hashed[key]
                if type(actual) is not type(expected_value) or actual != expected_value:
                    raise _invalid("invalid credential record")
            return cls(
                _username(root["username"]),
                _decode_field(hashed["salt_b64"], SCRYPT_SALT_BYTES),
                _decode_field(hashed["digest_b64"], SCRYPT_DKLEN),
            )
        except (
            SecurityError,
            json.JSONDecodeError,
            RecursionError,
            UnicodeError,
            ValueError,
        ):
            raise _invalid("invalid credential record") from None

    def to_json(self) -> str:
        value = {
            "schema_version": CREDENTIAL_SCHEMA_VERSION,
            "username": self.username,
            "password_hash": {
                "algorithm": "scrypt",
                "n": SCRYPT_N,
                "r": SCRYPT_R,
                "p": SCRYPT_P,
                "dklen": SCRYPT_DKLEN,
                "salt_b64": base64.b64encode(self._salt).decode("ascii"),
                "digest_b64": base64.b64encode(self._digest).decode("ascii"),
            },
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


def verify_credentials(
    record: CredentialRecord | None, username: object, password: object
) -> bool:
    """Verify one login, doing the same scrypt work for an unknown username."""

    try:
        name = _username(username)
        secret = _password(password, allow_empty=True)
    except SecurityError:
        return False
    known = isinstance(record, CredentialRecord) and hmac.compare_digest(
        name, record.username
    )
    salt = record._salt if known else _DUMMY_SALT
    expected = record._digest if known else _DUMMY_DIGEST
    try:
        actual = _derive(secret, salt)
    except (MemoryError, ValueError):
        return False
    matched = hmac.compare_digest(actual, expected)
    return bool(known and matched)


class _CheckedClock:
    def __init__(self, clock: Callable[[], float]) -> None:
        if not callable(clock):
            raise _invalid("invalid clock")
        self._clock = clock
        self._last: float | None = None

    def read(self) -> float:
        try:
            value = self._clock()
        except Exception:
            raise _invalid("invalid monotonic clock") from None
        if type(value) not in (int, float) or not math.isfinite(value):
            raise _invalid("invalid monotonic clock")
        result = float(value)
        if self._last is not None and result < self._last:
            raise _invalid("invalid monotonic clock")
        self._last = result
        return result


def _bounded_int(value: object, minimum: int, maximum: int, message: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid(message)
    return value


def _duration(value: object, maximum: float, message: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise _invalid(message)
    result = float(value)
    if not 0 < result <= maximum:
        raise _invalid(message)
    return result


def _deadline(now: float, duration: float) -> float:
    result = now + duration
    if not math.isfinite(result) or result <= now:
        raise _invalid("invalid monotonic clock")
    return result


def _login_key(ip: object, username: object) -> tuple[str, str]:
    if not isinstance(ip, str) or len(ip) > 64:
        raise _invalid("invalid login key")
    try:
        address = str(ipaddress.ip_address(ip))
    except ValueError:
        raise _invalid("invalid login key") from None
    return address, _username(username)


@dataclass(frozen=True)
class ThrottleDecision:
    allowed: bool
    retry_after_seconds: int
    reason: str


@dataclass
class _ThrottleWindow:
    attempts: int
    expires_at: float


class LoginThrottle:
    """Thread-safe fixed-window throttling per pair, IP, and web service."""

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        per_ip_max_attempts: int | None = None,
        global_max_attempts: int = 20,
        window_seconds: float = 60.0,
        max_keys: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_attempts = _bounded_int(
            max_attempts, 1, 1000, "invalid throttle configuration"
        )
        self.per_ip_max_attempts = _bounded_int(
            max_attempts if per_ip_max_attempts is None else per_ip_max_attempts,
            1,
            10_000,
            "invalid throttle configuration",
        )
        self.global_max_attempts = _bounded_int(
            global_max_attempts, 1, 100_000, "invalid throttle configuration"
        )
        self.window_seconds = _duration(
            window_seconds, 86_400, "invalid throttle configuration"
        )
        self.max_keys = _bounded_int(
            max_keys, 1, 100_000, "invalid throttle configuration"
        )
        self._clock = _CheckedClock(clock)
        self._windows: dict[tuple[str, str], _ThrottleWindow] = {}
        self._ip_windows: dict[str, _ThrottleWindow] = {}
        self._global_window: _ThrottleWindow | None = None
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "LoginThrottle(keys=<redacted>, bounded=True)"

    def admit(self, ip: object, username: object) -> ThrottleDecision:
        """Reserve one authentication attempt before password verification."""

        key = _login_key(ip, username)
        address = key[0]
        with self._lock:
            now = self._clock.read()
            self._prune(now)
            pair_window = self._windows.get(key)
            ip_window = self._ip_windows.get(address)
            global_window = self._global_window
            if pair_window is None:
                if len(self._windows) >= self.max_keys:
                    retry = min(item.expires_at for item in self._windows.values()) - now
                    return ThrottleDecision(False, max(1, math.ceil(retry)), "capacity")
            for window, limit, reason in (
                (global_window, self.global_max_attempts, "global_rate_limited"),
                (ip_window, self.per_ip_max_attempts, "ip_rate_limited"),
                (pair_window, self.max_attempts, "rate_limited"),
            ):
                if window is not None and window.attempts >= limit:
                    retry = window.expires_at - now
                    return ThrottleDecision(
                        False, max(1, math.ceil(retry)), reason
                    )
            expires_at = _deadline(now, self.window_seconds)
            if pair_window is None:
                pair_window = _ThrottleWindow(0, expires_at)
                self._windows[key] = pair_window
            if ip_window is None:
                ip_window = _ThrottleWindow(0, expires_at)
                self._ip_windows[address] = ip_window
            if global_window is None:
                global_window = _ThrottleWindow(0, expires_at)
                self._global_window = global_window
            pair_window.attempts += 1
            ip_window.attempts += 1
            global_window.attempts += 1
            return ThrottleDecision(True, 0, "allowed")

    def reset(self, ip: object, username: object) -> None:
        """Forget a key after a successful authentication."""

        key = _login_key(ip, username)
        with self._lock:
            self._windows.pop(key, None)
            self._ip_windows.pop(key[0], None)

    def snapshot(self) -> dict[str, int]:
        """Return counts only; IP addresses and usernames never leave the store."""

        with self._lock:
            self._prune(self._clock.read())
            return {"tracked_keys": len(self._windows), "capacity": self.max_keys}

    def _prune(self, now: float) -> None:
        # ponytail: bounded scan; add a heap only if profiling shows key churn matters.
        expired = [key for key, value in self._windows.items() if now >= value.expires_at]
        for key in expired:
            del self._windows[key]
        expired_ips = [
            key for key, value in self._ip_windows.items() if now >= value.expires_at
        ]
        for key in expired_ips:
            del self._ip_windows[key]
        if self._global_window is not None and now >= self._global_window.expires_at:
            self._global_window = None


@dataclass(frozen=True, repr=False)
class SessionCredentials:
    bearer_token: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def __repr__(self) -> str:
        return "SessionCredentials(bearer_token=<redacted>, csrf_token=<redacted>)"


@dataclass(frozen=True)
class SessionPrincipal:
    username: str


@dataclass(repr=False)
class _Session:
    username: str
    csrf_digest: bytes = field(repr=False)
    created_at: float
    idle_expires_at: float
    absolute_expires_at: float


class SessionStore:
    """Bounded server-side sessions keyed only by SHA-256 bearer digests."""

    def __init__(
        self,
        *,
        max_sessions: int = 128,
        idle_timeout_seconds: float = 900.0,
        absolute_timeout_seconds: float = 86_400.0,
        token_bytes: int = 32,
        csrf_bytes: int = 32,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.max_sessions = _bounded_int(
            max_sessions, 1, 10_000, "invalid session configuration"
        )
        self.idle_timeout_seconds = _duration(
            idle_timeout_seconds, 604_800, "invalid session configuration"
        )
        self.absolute_timeout_seconds = _duration(
            absolute_timeout_seconds, 2_592_000, "invalid session configuration"
        )
        if self.absolute_timeout_seconds < self.idle_timeout_seconds:
            raise _invalid("invalid session configuration")
        self.token_bytes = _bounded_int(
            token_bytes, 16, 64, "invalid session configuration"
        )
        self.csrf_bytes = _bounded_int(
            csrf_bytes, 16, 64, "invalid session configuration"
        )
        if not callable(rng):
            raise _invalid("invalid session configuration")
        self._rng = rng
        self._clock = _CheckedClock(clock)
        self._sessions: OrderedDict[bytes, _Session] = OrderedDict()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "SessionStore(tokens=<hashed>, bounded=True)"

    def create(self, username: object) -> SessionCredentials:
        name = _username(username)
        with self._lock:
            now = self._clock.read()
            self._prune(now)
            credentials, bearer_digest, csrf_digest = self._mint()
            if len(self._sessions) >= self.max_sessions:
                self._sessions.popitem(last=False)
            absolute = _deadline(now, self.absolute_timeout_seconds)
            self._sessions[bearer_digest] = _Session(
                name,
                csrf_digest,
                now,
                min(_deadline(now, self.idle_timeout_seconds), absolute),
                absolute,
            )
            return credentials

    def authenticate(self, bearer_token: object) -> SessionPrincipal | None:
        digest = self._token_digest(bearer_token)
        if digest is None:
            return None
        with self._lock:
            now = self._clock.read()
            session = self._find(digest, now)
            if session is None:
                return None
            self._touch(digest, session, now)
            return SessionPrincipal(session.username)

    def validate_csrf(self, bearer_token: object, csrf_token: object) -> bool:
        bearer_digest = self._token_digest(bearer_token)
        csrf_digest = self._token_digest(csrf_token)
        if bearer_digest is None or csrf_digest is None:
            return False
        with self._lock:
            now = self._clock.read()
            session = self._find(bearer_digest, now)
            if session is None or not hmac.compare_digest(
                session.csrf_digest, csrf_digest
            ):
                return False
            self._touch(bearer_digest, session, now)
            return True

    def rotate(self, bearer_token: object) -> SessionCredentials | None:
        old_digest = self._token_digest(bearer_token)
        if old_digest is None:
            return None
        with self._lock:
            now = self._clock.read()
            session = self._find(old_digest, now)
            if session is None:
                return None
            credentials, new_digest, csrf_digest = self._mint()
            del self._sessions[old_digest]
            self._sessions[new_digest] = _Session(
                session.username,
                csrf_digest,
                session.created_at,
                min(
                    _deadline(now, self.idle_timeout_seconds),
                    session.absolute_expires_at,
                ),
                session.absolute_expires_at,
            )
            return credentials

    def logout(self, bearer_token: object) -> bool:
        digest = self._token_digest(bearer_token)
        if digest is None:
            return False
        with self._lock:
            return self._sessions.pop(digest, None) is not None

    def revoke_all(self) -> None:
        """Invalidate every browser session after a credential change."""
        with self._lock:
            self._sessions.clear()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            self._prune(self._clock.read())
            return {"active_sessions": len(self._sessions), "capacity": self.max_sessions}

    def _mint(self) -> tuple[SessionCredentials, bytes, bytes]:
        for _attempt in range(_MAX_RANDOM_ATTEMPTS):
            bearer = _token(_random_bytes(self._rng, self.token_bytes))
            bearer_digest = hashlib.sha256(bearer.encode("ascii")).digest()
            if bearer_digest in self._sessions:
                continue
            csrf = _token(_random_bytes(self._rng, self.csrf_bytes))
            return (
                SessionCredentials(bearer, csrf),
                bearer_digest,
                hashlib.sha256(csrf.encode("ascii")).digest(),
            )
        raise _invalid("secure random generation failed")

    @staticmethod
    def _token_digest(value: object) -> bytes | None:
        if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
            return None
        return hashlib.sha256(value.encode("ascii")).digest()

    def _find(self, digest: bytes, now: float) -> _Session | None:
        session = self._sessions.get(digest)
        if session is None:
            return None
        if now >= session.idle_expires_at or now >= session.absolute_expires_at:
            del self._sessions[digest]
            return None
        return session

    def _touch(self, digest: bytes, session: _Session, now: float) -> None:
        session.idle_expires_at = min(
            _deadline(now, self.idle_timeout_seconds), session.absolute_expires_at
        )
        self._sessions.move_to_end(digest)

    def _prune(self, now: float) -> None:
        # ponytail: session capacity bounds this scan; no expiry heap until measured.
        expired = [
            digest
            for digest, session in self._sessions.items()
            if now >= session.idle_expires_at or now >= session.absolute_expires_at
        ]
        for digest in expired:
            del self._sessions[digest]


def _token(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _cookie(secure: object, value: str, *, clear: bool) -> str:
    if type(secure) is not bool:
        raise _invalid("invalid cookie configuration")
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE_NAME] = value
    morsel = cookie[SESSION_COOKIE_NAME]
    morsel["httponly"] = True
    morsel["path"] = "/"
    morsel["samesite"] = "Strict"
    if secure:
        morsel["secure"] = True
    if clear:
        morsel["max-age"] = 0
    return cookie.output(header="").strip()


def build_session_cookie(bearer_token: object, *, secure: bool = False) -> str:
    """Construct the sole browser session cookie with conservative flags."""

    if not isinstance(bearer_token, str) or _TOKEN.fullmatch(bearer_token) is None:
        raise _invalid("invalid session token")
    return _cookie(secure, bearer_token, clear=False)


def clear_session_cookie(*, secure: bool = False) -> str:
    """Construct a matching cookie deletion header for logout."""

    return _cookie(secure, "", clear=True)
