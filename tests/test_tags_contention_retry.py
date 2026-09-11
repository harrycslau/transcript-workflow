"""Pre-5B SQLite contention retry policy tests.

Proves (per the approved plan) the small, local retry policy in
``workflow.services.tags`` around the COMPLETE transactional boundary of
the three unlocked public web tag mutations:

1. SQLite BUSY / LOCKED (including an extended code such as
   ``SQLITE_LOCKED_SHAREDCACHE``) are retried;
2. the attempt budget and backoff are bounded; exhausted contention
   re-raises;
3. an unrelated SQLite ``OperationalError`` (non-contention primary
   code) is attempted once and re-raised;
4. a non-SQLite ``django.db.OperationalError`` (or one whose cause is
   not a ``sqlite3.OperationalError`` / has no usable integer code) is
   attempted once and re-raised;
5. each retry runs in a FRESH transaction (the failed attempt is rolled
   back and the next invocation is not left inside a broken
   transaction);
6. a rolled-back attempt fires no sync callback and the final successful
   commit yields exactly one effective callback;
7. all three public mutations route through the policy.

The helper-level tests exercise the private policy semantics directly
(deterministic; no timing dependence). The behavioral tests drive the
real mutation functions through a first-attempt contention failure.
"""

from __future__ import annotations

import sqlite3

import pytest
from django.db import OperationalError
from django.db import connection

from factories import (
    make_tag,
    make_tag_assignment,
    make_transcribed_recording,
)
from workflow.services import tags as tags_service


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _op(code: int, name: str, msg: str = "database is locked") -> OperationalError:
    """A ``django.db.OperationalError`` whose DIRECT cause is a
    ``sqlite3.OperationalError`` carrying ``sqlite_errorcode`` = ``code``
    (a primary or extended code)."""
    try:
        raise sqlite3.OperationalError(msg)
    except sqlite3.OperationalError as orig:  # pragma: no branch
        orig.sqlite_errorcode = code
        orig.sqlite_errorname = name
        try:
            raise OperationalError(msg) from orig
        except OperationalError as exc:
            return exc


def _contention_on_first(real, exc, state):
    """Wrap ``real`` so the first call raises ``exc``, later calls defer."""
    def flaky(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 1:
            raise exc
        return real(*args, **kwargs)

    return flaky


@pytest.fixture
def fast_retry(monkeypatch):
    """Make the policy bounded-but-fast and record sleeps."""
    monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 5)
    sleeps: list[float] = []
    monkeypatch.setattr(tags_service.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


# ---------------------------------------------------------------------------
# 1. BUSY / LOCKED retry, including an extended code
# ---------------------------------------------------------------------------


class TestBusyLockedRetry:
    @pytest.mark.parametrize(
        "code,name",
        [
            (sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
            (sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED"),
            (262, "SQLITE_LOCKED_SHAREDCACHE"),  # 0x106, primary byte 6 (LOCKED)
        ],
    )
    def test_contention_is_retried_then_succeeds(self, fast_retry, code, name):
        calls = {"n": 0}
        sentinel = object()

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _op(code, name)
            return sentinel

        wrapped = tags_service._retry_on_sqlite_contention(flaky)
        assert wrapped() is sentinel
        assert calls["n"] == 2  # one retry

    def test_contention_error_code_matches_primary_byte(self):
        # Extended code 262 -> primary byte 6 -> LOCKED family.
        assert (262 & 0xFF) in tags_service._TAG_RETRY_BUSY_CODES


# ---------------------------------------------------------------------------
# 2. Bounded attempts / backoff; exhausted contention re-raised
# ---------------------------------------------------------------------------


class TestBoundedRetry:
    def test_attempt_budget_bounded_and_exhausted_reraises(self, monkeypatch):
        monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 3)
        sleeps: list[float] = []
        monkeypatch.setattr(tags_service.time, "sleep", lambda s: sleeps.append(s))
        calls = {"n": 0}
        exc = _op(sqlite3.SQLITE_BUSY, "SQLITE_BUSY")

        def always_busy(*a, **k):
            calls["n"] += 1
            raise exc

        wrapped = tags_service._retry_on_sqlite_contention(always_busy)
        with pytest.raises(OperationalError) as err:
            wrapped()
        assert calls["n"] == 3  # exactly the attempt budget
        assert sleeps == [0.02, 0.02]  # one backoff between attempts (bounded)
        assert err.value is exc  # the LAST contention error re-raised

    def test_never_retries_beyond_budget(self, fast_retry):
        calls = {"n": 0}
        exc = _op(sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED")

        def always_locked(*a, **k):
            calls["n"] += 1
            raise exc

        wrapped = tags_service._retry_on_sqlite_contention(always_locked)
        with pytest.raises(OperationalError):
            wrapped()
        # fast_retry sets attempts=5 -> no more than 5 calls.
        assert calls["n"] <= 5


# ---------------------------------------------------------------------------
# 3. Unrelated SQLite OperationalError: one attempt, immediate re-raise
# ---------------------------------------------------------------------------


class TestUnrelatedSqliteError:
    @pytest.mark.parametrize(
        "code,name",
        [
            (1, "SQLITE_ERROR"),  # generic SQL error: not contention
            (2, "SQLITE_INTERNAL"),
            (20, "SQLITE_NOTADB"),  # primary byte 20, not BUSY/LOCKED
        ],
    )
    def test_non_contention_code_attempted_once(self, fast_retry, code, name):
        calls = {"n": 0}
        exc = _op(code, name)

        def flaky(*a, **k):
            calls["n"] += 1
            raise exc

        wrapped = tags_service._retry_on_sqlite_contention(flaky)
        with pytest.raises(OperationalError) as err:
            wrapped()
        assert calls["n"] == 1
        assert err.value is exc


# ---------------------------------------------------------------------------
# 4. Non-SQLite Django OperationalError: one attempt, immediate re-raise
# ---------------------------------------------------------------------------


class TestNonSqliteOperationalError:
    def test_plain_django_op_error_no_cause_attempted_once(self, fast_retry):
        calls = {"n": 0}
        exc = OperationalError("boom")  # no __cause__

        def flaky(*a, **k):
            calls["n"] += 1
            raise exc

        wrapped = tags_service._retry_on_sqlite_contention(flaky)
        with pytest.raises(OperationalError) as err:
            wrapped()
        assert calls["n"] == 1
        assert err.value is exc

    def test_cause_is_not_sqlite_operational_error(self, fast_retry):
        calls = {"n": 0}
        try:
            raise RuntimeError("not sqlite")
        except RuntimeError as cause:  # pragma: no branch
            exc = OperationalError("boom")
            exc.__cause__ = cause

        def flaky(*a, **k):
            calls["n"] += 1
            raise exc

        wrapped = tags_service._retry_on_sqlite_contention(flaky)
        with pytest.raises(OperationalError):
            wrapped()
        assert calls["n"] == 1

    def test_sqlite_cause_without_usable_integer_code(self, fast_retry):
        calls = {"n": 0}
        # A sqlite3.OperationalError with NO sqlite_errorcode attribute.
        try:
            raise sqlite3.OperationalError("no code")
        except sqlite3.OperationalError as cause:  # pragma: no branch
            exc = OperationalError("boom")
            exc.__cause__ = cause

        def flaky(*a, **k):
            calls["n"] += 1
            raise exc

        wrapped = tags_service._retry_on_sqlite_contention(flaky)
        with pytest.raises(OperationalError):
            wrapped()
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 5. Fresh transaction per attempt
# ---------------------------------------------------------------------------


class TestFreshTransactionPerAttempt:
    @pytest.mark.django_db(transaction=True)
    def test_failed_attempt_rolled_back_next_attempt_is_fresh(
        self, monkeypatch, django_capture_on_commit_callbacks
    ):
        rec, _t, _s = make_transcribed_recording(["fresh"], sha="retry-fresh-1")
        tag = make_tag("Fresh")
        real_lock = tags_service._lock_recording
        exc = _op(sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED")
        state = {"n": 0}
        monkeypatch.setattr(
            tags_service, "_lock_recording", _contention_on_first(real_lock, exc, state)
        )
        monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_BACKOFF_SECONDS", 0)

        with django_capture_on_commit_callbacks(execute=True):
            result = tags_service.add_manual_tag(rec, tag)

        assert state["n"] == 2  # one contention + one fresh retry
        # The retried invocation was NOT in a broken transaction: it
        # committed cleanly and exactly one authoritative write exists.
        assert result["created"] is True
        from workflow.models import TagAssignment

        assert TagAssignment.objects.filter(recording=rec, tag=tag).count() == 1
        # No broken transaction leaked out of the retried call.
        assert connection.in_atomic_block is False
        assert connection.needs_rollback is False


# ---------------------------------------------------------------------------
# 6. Rolled-back attempts fire no callback; success commits exactly one
# ---------------------------------------------------------------------------


class TestCallbackContract:
    @pytest.mark.django_db(transaction=True)
    def test_rolled_back_attempt_no_callback_success_commits_one(
        self, monkeypatch
    ):
        from workflow.services import search_sync

        rec, _t, _s = make_transcribed_recording(["cb"], sha="retry-cb-1")
        tag = make_tag("Callback")
        real_schedule = tags_service.schedule_recording_sync
        executed: list[str] = []

        def spy_reconcile(recording_id, **kwargs):
            executed.append(str(recording_id))

        monkeypatch.setattr(search_sync, "reconcile_recording", spy_reconcile)

        state = {"n": 0}
        exc = _op(sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED")

        def flaky_schedule(recording_ids, **kwargs):
            # Register the REAL on_commit callback (so it is subject to
            # rollback semantics), THEN raise contention to fail the
            # first attempt AFTER the authoritative write.
            real_schedule(recording_ids, **kwargs)
            state["n"] += 1
            if state["n"] == 1:
                raise exc

        monkeypatch.setattr(tags_service, "schedule_recording_sync", flaky_schedule)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_BACKOFF_SECONDS", 0)

        tags_service.add_manual_tag(rec, tag)

        assert state["n"] == 2
        # The first (rolled back) attempt's callback was discarded and
        # never executed; the successful commit fired exactly one.
        assert executed == [rec.pk]
        from workflow.models import TagAssignment

        assert TagAssignment.objects.filter(recording=rec, tag=tag).count() == 1


# ---------------------------------------------------------------------------
# 7. All three public mutations route through the policy
# ---------------------------------------------------------------------------


class TestPolicyApplied:
    @pytest.mark.django_db(transaction=True)
    def test_add_confirm_remove_retry_through_policy(self, monkeypatch):
        rec, _t, _s = make_transcribed_recording(["three"], sha="retry-three-1")
        add_tag = make_tag("AddMe")
        confirm_tag = make_tag("ConfirmMe")
        make_tag_assignment(rec, confirm_tag, origin="suggested")
        remove_tag_obj = make_tag("RemoveMe")
        make_tag_assignment(rec, remove_tag_obj, origin="suggested")

        real_lock = tags_service._lock_recording
        # _lock_recording is called twice per function (failed attempt +
        # retry). Fail the ODD-numbered calls (first attempt of each of
        # add / confirm / remove) and let the even-numbered retries run.
        state = {"n": 0}

        def planned_lock(pk):
            state["n"] += 1
            if state["n"] % 2 == 1:
                raise _op(262, "SQLITE_LOCKED_SHAREDCACHE")
            return real_lock(pk)

        monkeypatch.setattr(tags_service, "_lock_recording", planned_lock)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(tags_service, "_TAG_RETRY_BACKOFF_SECONDS", 0)

        # add
        r_add = tags_service.add_manual_tag(rec, add_tag)
        assert r_add["created"] is True
        # confirm
        r_confirm = tags_service.confirm_suggestion(rec, confirm_tag)
        assert r_confirm["already_confirmed"] is False
        # remove
        r_remove = tags_service.remove_tag(rec, remove_tag_obj)
        assert r_remove["removed"] is True

        from workflow.models import TagAssignment, TagOrigin

        assert (
            TagAssignment.objects.get(recording=rec, tag=add_tag).origin
            == TagOrigin.MANUAL
        )
        assert (
            TagAssignment.objects.get(recording=rec, tag=confirm_tag).origin
            == TagOrigin.CONFIRMED
        )
        assert (
            TagAssignment.objects.get(recording=rec, tag=remove_tag_obj).is_active
            is False
        )

    def test_all_mutations_are_wrapped_by_the_retry_policy(self):
        """Structural guard: each web tag mutation's outermost wrapper is
        the retry policy layered ON TOP of ``@transaction.atomic``. A
        plain ``@transaction.atomic`` function's ``__wrapped__`` is the
        raw function (which itself has no ``__wrapped__``); with the
        retry wrapper outermost, ``func.__wrapped__`` is the
        ATOMIC-wrapped function, which in turn wraps the raw function.
        ``create_custom_tag_and_assign`` is wrapped like the other three
        unlocked web tag mutations. The bulk ``apply_tag_selection``
        (Done) is wrapped the same way: retry OUTSIDE one atomic block."""
        for name in (
            "add_manual_tag",
            "confirm_suggestion",
            "remove_tag",
            "create_custom_tag_and_assign",
            "apply_tag_selection",
        ):
            func = getattr(tags_service, name)
            assert getattr(func, "__wrapped__", None) is not None
            # The retry wrapper's target is itself a wrapper (the atomic
            # block), proving the retry sits OUTSIDE transaction.atomic.
            assert (
                getattr(getattr(func, "__wrapped__", None), "__wrapped__", None)
                is not None
            )
            assert func.__name__ == name  # functools.wraps preserved