from __future__ import annotations

import json
import math
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tinypirelay.web_security import (
    CredentialRecord,
    LoginThrottle,
    SecurityError,
    SessionStore,
    build_session_cookie,
    clear_session_cookie,
    verify_credentials,
)


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class SequenceRng:
    def __init__(self) -> None:
        self.value = 0
        self.lock = threading.Lock()

    def __call__(self, size: int) -> bytes:
        with self.lock:
            self.value += 1
            value = self.value
        return bytes([value]) * size


class CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.password = "correct horse battery staple"
        cls.record = CredentialRecord.create(
            "operator", cls.password, rng=lambda size: bytes(range(size))
        )

    def test_scrypt_record_round_trips_without_plaintext_or_repr_disclosure(self) -> None:
        encoded = self.record.to_json()
        value = json.loads(encoded)
        restored = CredentialRecord.from_json(encoded)

        self.assertEqual(1, value["schema_version"])
        self.assertEqual("scrypt", value["password_hash"]["algorithm"])
        self.assertNotIn(self.password, encoded)
        self.assertNotIn(value["password_hash"]["salt_b64"], repr(self.record))
        self.assertNotIn(value["password_hash"]["digest_b64"], repr(self.record))
        self.assertTrue(verify_credentials(restored, "operator", self.password))
        self.assertFalse(verify_credentials(restored, "operator", "wrong"))

    def test_unknown_username_performs_dummy_scrypt_and_never_authenticates(self) -> None:
        record = CredentialRecord("operator", b"s" * 16, b"d" * 32)
        with mock.patch(
            "tinypirelay.web_security.hashlib.scrypt", return_value=b"d" * 32
        ) as derive:
            self.assertTrue(verify_credentials(record, "operator", "password"))
            self.assertFalse(verify_credentials(record, "unknown", "password"))
            self.assertFalse(verify_credentials(None, "unknown", "password"))

        self.assertEqual(3, derive.call_count)
        self.assertEqual(b"s" * 16, derive.call_args_list[0].kwargs["salt"])
        self.assertNotEqual(
            derive.call_args_list[0].kwargs["salt"],
            derive.call_args_list[1].kwargs["salt"],
        )

    def test_credential_parser_rejects_semantic_drift_and_hostile_json(self) -> None:
        good = json.loads(self.record.to_json())
        cases = []

        duplicate = self.record.to_json().replace(
            '"schema_version":1', '"schema_version":1,"schema_version":1', 1
        )
        cases.extend(
            (
                duplicate,
                '{"schema_version":NaN}',
                '{"schema_version":' + ("9" * 4301) + "}",
                "x" * 8193,
            )
        )

        for mutation in (
            lambda value: value.__setitem__("schema_version", True),
            lambda value: value.__setitem__("extra", 1),
            lambda value: value["password_hash"].__setitem__("n", 2**20),
            lambda value: value["password_hash"].__setitem__("salt_b64", "not base64"),
        ):
            value = json.loads(json.dumps(good))
            mutation(value)
            cases.append(json.dumps(value))

        for text in cases:
            with self.subTest(text_length=len(text)):
                with self.assertRaisesRegex(SecurityError, "credential record") as raised:
                    CredentialRecord.from_json(text)
                self.assertNotIn(text, str(raised.exception))

    def test_oversized_and_malformed_login_inputs_fail_closed(self) -> None:
        self.assertFalse(verify_credentials(self.record, "bad user", self.password))
        self.assertFalse(
            verify_credentials(self.record, "operator", "secret" * 1000)
        )
        self.assertFalse(verify_credentials(self.record, "operator", "\ud800"))
        with self.assertRaises(SecurityError) as raised:
            CredentialRecord.create("operator", "secret" * 1000)
        self.assertNotIn("secret", str(raised.exception))


class LoginThrottleTests(unittest.TestCase):
    def test_per_pair_fixed_windows_reset_after_success_or_finite_expiry(self) -> None:
        clock = Clock()
        throttle = LoginThrottle(
            max_attempts=2,
            per_ip_max_attempts=3,
            global_max_attempts=10,
            window_seconds=10,
            max_keys=4,
            clock=clock,
        )

        self.assertTrue(throttle.admit("192.0.2.1", "operator").allowed)
        self.assertTrue(throttle.admit("192.0.2.1", "operator").allowed)
        blocked = throttle.admit("192.0.2.1", "operator")
        self.assertFalse(blocked.allowed)
        self.assertEqual(10, blocked.retry_after_seconds)
        self.assertTrue(throttle.admit("192.0.2.2", "operator").allowed)
        self.assertTrue(throttle.admit("192.0.2.1", "other").allowed)

        throttle.reset("192.0.2.1", "operator")
        self.assertTrue(throttle.admit("192.0.2.1", "operator").allowed)

    def test_rotating_usernames_cannot_bypass_ip_or_global_limits(self) -> None:
        clock = Clock()
        throttle = LoginThrottle(
            max_attempts=5,
            per_ip_max_attempts=3,
            global_max_attempts=5,
            window_seconds=10,
            max_keys=20,
            clock=clock,
        )

        for username in ("one", "two", "three"):
            self.assertTrue(throttle.admit("192.0.2.1", username).allowed)
        blocked_ip = throttle.admit("192.0.2.1", "four")
        self.assertFalse(blocked_ip.allowed)
        self.assertEqual("ip_rate_limited", blocked_ip.reason)

        self.assertTrue(throttle.admit("192.0.2.2", "five").allowed)
        self.assertTrue(throttle.admit("192.0.2.3", "six").allowed)
        blocked_global = throttle.admit("192.0.2.4", "seven")
        self.assertFalse(blocked_global.allowed)
        self.assertEqual("global_rate_limited", blocked_global.reason)

        clock.value = 10
        self.assertTrue(throttle.admit("192.0.2.1", "eight").allowed)
        clock.value = 10
        self.assertTrue(throttle.admit("192.0.2.1", "operator").allowed)

    def test_key_capacity_fails_closed_and_exposes_counts_only(self) -> None:
        clock = Clock()
        throttle = LoginThrottle(max_attempts=1, window_seconds=5, max_keys=2, clock=clock)
        throttle.admit("192.0.2.1", "one")
        throttle.admit("192.0.2.2", "two")

        saturated = throttle.admit("192.0.2.3", "three")
        self.assertFalse(saturated.allowed)
        self.assertEqual("capacity", saturated.reason)
        self.assertEqual({"tracked_keys": 2, "capacity": 2}, throttle.snapshot())
        self.assertNotIn("192.0.2", repr(throttle))

        clock.value = 5
        self.assertTrue(throttle.admit("192.0.2.3", "three").allowed)
        self.assertEqual(1, throttle.snapshot()["tracked_keys"])

    def test_concurrent_attempts_cannot_bypass_the_limit(self) -> None:
        throttle = LoginThrottle(max_attempts=5, window_seconds=60, max_keys=8)
        decisions = []
        lock = threading.Lock()

        def attempt() -> None:
            decision = throttle.admit("2001:db8::1", "operator")
            with lock:
                decisions.append(decision.allowed)

        threads = [threading.Thread(target=attempt) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(5, decisions.count(True))
        self.assertEqual(35, decisions.count(False))

    def test_invalid_or_reversing_clock_and_invalid_keys_fail_closed(self) -> None:
        clock = Clock()
        throttle = LoginThrottle(clock=clock)
        throttle.admit("192.0.2.1", "operator")
        clock.value = -1
        with self.assertRaisesRegex(SecurityError, "clock"):
            throttle.admit("192.0.2.1", "operator")
        with self.assertRaises(SecurityError):
            LoginThrottle(window_seconds=math.inf)
        with self.assertRaises(SecurityError):
            throttle.admit("not-an-ip", "operator")


class SessionStoreTests(unittest.TestCase):
    def make_store(self, **kwargs: object) -> tuple[Clock, SessionStore]:
        clock = Clock()
        values = {
            "max_sessions": 3,
            "idle_timeout_seconds": 5,
            "absolute_timeout_seconds": 12,
            "clock": clock,
            "rng": SequenceRng(),
        }
        values.update(kwargs)
        return clock, SessionStore(**values)

    def test_tokens_are_server_side_hashed_and_csrf_is_per_session(self) -> None:
        _clock, store = self.make_store()
        credentials = store.create("operator")

        self.assertEqual("operator", store.authenticate(credentials.bearer_token).username)
        self.assertTrue(
            store.validate_csrf(credentials.bearer_token, credentials.csrf_token)
        )
        self.assertFalse(store.validate_csrf(credentials.bearer_token, "x" * 43))
        self.assertNotIn(credentials.bearer_token, repr(store._sessions))
        self.assertNotIn(credentials.csrf_token, repr(store._sessions))
        self.assertNotIn(credentials.bearer_token, repr(credentials))
        self.assertEqual({"active_sessions": 1, "capacity": 3}, store.snapshot())

    def test_invalid_csrf_does_not_extend_idle_expiry(self) -> None:
        clock, store = self.make_store()
        credentials = store.create("operator")
        clock.value = 4
        self.assertFalse(store.validate_csrf(credentials.bearer_token, "x" * 43))
        clock.value = 5
        self.assertIsNone(store.authenticate(credentials.bearer_token))

    def test_idle_touch_never_extends_absolute_expiry(self) -> None:
        clock, store = self.make_store()
        credentials = store.create("operator")
        for now in (4, 8, 11):
            clock.value = now
            self.assertIsNotNone(store.authenticate(credentials.bearer_token))
        clock.value = 12
        self.assertIsNone(store.authenticate(credentials.bearer_token))

    def test_rotation_invalidates_old_tokens_and_preserves_absolute_deadline(self) -> None:
        clock, store = self.make_store()
        old = store.create("operator")
        clock.value = 4
        new = store.rotate(old.bearer_token)
        self.assertIsNotNone(new)
        assert new is not None
        self.assertIsNone(store.authenticate(old.bearer_token))
        self.assertFalse(store.validate_csrf(new.bearer_token, old.csrf_token))
        self.assertTrue(store.validate_csrf(new.bearer_token, new.csrf_token))
        clock.value = 12
        self.assertIsNone(store.authenticate(new.bearer_token))

    def test_lru_eviction_and_logout_bound_session_state(self) -> None:
        clock, store = self.make_store(max_sessions=2)
        first = store.create("one")
        clock.value = 1
        second = store.create("two")
        clock.value = 2
        self.assertIsNotNone(store.authenticate(first.bearer_token))
        third = store.create("three")

        self.assertIsNone(store.authenticate(second.bearer_token))
        self.assertIsNotNone(store.authenticate(first.bearer_token))
        self.assertTrue(store.logout(third.bearer_token))
        self.assertFalse(store.logout(third.bearer_token))
        self.assertIsNone(store.authenticate(third.bearer_token))
        self.assertLessEqual(store.snapshot()["active_sessions"], 2)

    def test_random_collision_is_bounded_and_preserves_existing_session(self) -> None:
        store = SessionStore(
            max_sessions=2,
            idle_timeout_seconds=5,
            absolute_timeout_seconds=10,
            clock=Clock(),
            rng=lambda size: b"z" * size,
        )
        first = store.create("one")
        with self.assertRaisesRegex(SecurityError, "random generation"):
            store.create("two")
        self.assertIsNotNone(store.authenticate(first.bearer_token))
        self.assertEqual(1, store.snapshot()["active_sessions"])

    def test_malformed_tokens_and_nonfinite_configuration_fail_closed(self) -> None:
        _clock, store = self.make_store()
        self.assertIsNone(store.authenticate("x" * 10_000))
        self.assertFalse(store.validate_csrf("bad", "bad"))
        self.assertFalse(store.logout("bad"))
        with self.assertRaises(SecurityError):
            SessionStore(idle_timeout_seconds=math.nan)


class CookieTests(unittest.TestCase):
    def test_cookie_flags_are_conservative_and_secure_is_optional(self) -> None:
        token = "a" * 43
        plain = build_session_cookie(token)
        secure = build_session_cookie(token, secure=True)

        for header in (plain, secure):
            self.assertIn(f"tinypirelay_session={token}", header)
            self.assertIn("HttpOnly", header)
            self.assertIn("Path=/", header)
            self.assertIn("SameSite=Strict", header)
        self.assertNotIn("; Secure", plain)
        self.assertIn("; Secure", secure)

    def test_logout_cookie_matches_scope_and_hostile_values_are_rejected(self) -> None:
        cleared = clear_session_cookie(secure=True)
        self.assertIn("tinypirelay_session=", cleared)
        self.assertIn("Max-Age=0", cleared)
        self.assertIn("HttpOnly", cleared)
        self.assertIn("Path=/", cleared)
        self.assertIn("SameSite=Strict", cleared)
        self.assertIn("Secure", cleared)

        secret = "valid-looking\r\nSet-Cookie: stolen=1"
        with self.assertRaises(SecurityError) as raised:
            build_session_cookie(secret)
        self.assertNotIn(secret, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
