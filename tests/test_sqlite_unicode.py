"""Unicode-aware SQLite collation infrastructure (Step 5A.1 corrective).

Proves per-connection registration is idempotent, survives connection
reopen, works across a second connection alias, raises a stable error on
registration failure, and actually orders rows database-side using
NFC + casefold (ascending and descending, with PK tie-break) rather than
ASCII ``LOWER()``.
"""

from __future__ import annotations

import unicodedata

import pytest
from django.db import connection

from workflow import sqlite_unicode
from workflow.sqlite_unicode import ensure_registered, folded_title_expression

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def test_ensure_registered_opens_and_is_idempotent():
    assert connection.vendor == "sqlite"
    ensure_registered()  # opens the connection if needed, registers
    ensure_registered()  # repeated registration is a safe no-op
    assert connection.connection is not None


def test_comparator_is_nfc_casefold():
    assert sqlite_unicode._collate("Äiti", "äiti") == 0
    assert sqlite_unicode._collate("Åland", "åland") == 0
    assert sqlite_unicode._collate("Örebro", "örebro") == 0
    assert sqlite_unicode._collate("örebro", "o\u0308rebro") == 0  # composed vs decomposed
    assert sqlite_unicode._collate("a", "b") < 0
    assert sqlite_unicode._collate("B", "a") > 0


def _assert_sanitized(exc_info, sentinel):
    """The fixed stable message is shown, and neither the message nor the
    formatted traceback leaks raw details."""
    message = str(exc_info.value)
    assert message == sqlite_unicode._UNAVAILABLE_MESSAGE
    assert sqlite_unicode.COLLATION_NAME in message
    assert "could not be registered" in message
    import traceback

    formatted = "".join(traceback.format_exception(exc_info.value))
    assert sentinel not in message
    assert sentinel not in formatted


def test_ensure_registered_fails_stably_on_registration_error(monkeypatch):
    class Raw:
        def create_collation(self, name, func):
            raise RuntimeError("boom sentinel collation in use")

    class FakeConn:
        vendor = "sqlite"

        def __init__(self):
            self.connection = Raw()

        def ensure_connection(self):
            return None

    class FakeHandler:
        def __getitem__(self, using):
            assert using == "default"
            return FakeConn()

    monkeypatch.setattr(sqlite_unicode, "connections", FakeHandler())
    with pytest.raises(RuntimeError) as exc_info:
        ensure_registered()
    _assert_sanitized(exc_info, "boom")


def test_signal_time_registration_failure_is_sanitized(monkeypatch):
    """A failure raised by _register DURING ensure_connection (through the
    connection_created signal) must also surface as the fixed stable
    message, never the raw exception."""
    from django.db.backends.signals import connection_created

    class Raw:
        def create_collation(self, name, func):
            raise RuntimeError("sentinel secret path /Users/secret/db.sqlite3")

    class FakeConn:
        vendor = "sqlite"

        def __init__(self):
            self.connection = Raw()

        def ensure_connection(self):
            # Django fires connection_created during connect(); the real
            # _register receiver runs here and must sanitize its failure.
            connection_created.send(sender=type(self), connection=self)

    class FakeHandler:
        def __getitem__(self, using):
            assert using == "default"
            return FakeConn()

    monkeypatch.setattr(sqlite_unicode, "connections", FakeHandler())
    with pytest.raises(RuntimeError) as exc_info:
        ensure_registered()
    _assert_sanitized(exc_info, "sentinel")


def test_reconnect_re_registers():
    connection.close()
    ensure_registered()  # reopens and registers on the new raw connection
    ensure_registered()  # still idempotent after reopen
    connection.close()
    ensure_registered()  # no stale "already registered" state survives close
    # The collation is actually usable after the reopen.
    from django.db.models import Value

    from workflow.models import Recording

    qs = Recording.objects.annotate(v=Value("äiti")).order_by(
        folded_title_expression("v")
    )
    assert list(qs) == []  # executes without a collation error


def test_second_connection_registers_independently(tmp_path):
    """A second, independent SQLite connection gets the collation too.

    Django caches ``DATABASES`` at startup, so adding a runtime alias is
    not practical; an independent raw sqlite3 connection exercises the
    same per-connection ``_register`` path the signal uses.
    """
    import sqlite3

    raw = sqlite3.connect(str(tmp_path / "second.sqlite3"))

    class FakeDjangoConn:
        vendor = "sqlite"

        def __init__(self):
            self.connection = raw

    sqlite_unicode._register(FakeDjangoConn())
    cursor = raw.execute(
        "SELECT column1 FROM (VALUES ('Äiti'), ('äiti'), ('Åland'))"
        " ORDER BY column1 COLLATE unicode_fold"
    )
    assert {row[0] for row in cursor.fetchall()} == {"Äiti", "äiti", "Åland"}
    raw.close()


def test_folded_expression_renders_collate():
    from django.db.models import Value

    from workflow.models import Recording

    qs = Recording.objects.annotate(t=Value("x")).order_by(
        folded_title_expression("t")
    )
    sql = str(qs.query)
    assert "COLLATE" in sql
    assert sqlite_unicode.COLLATION_NAME in sql


def test_collation_orders_database_side():
    """Real ordering assertion: rows with values where raw ASCII ordering
    differs from NFC+casefold ordering, ascending AND descending, with PK
    tie-break for casefold-equivalent strings."""
    from django.db.models import Case, Value, When

    from workflow.models import ProcessingStatus, Recording, SummaryState

    values = {
        "row-1": "z-B",
        "row-2": "Äiti",
        "row-3": "äiti",
        "row-4": "Åland",
        "row-5": "åland",
        "row-6": "Örebro",
    }
    for sha in values:
        Recording.objects.create(
            sha256=sha,
            duration_seconds=60.0,
            processing_status=ProcessingStatus.TRANSCRIBED,
            summary_status=SummaryState.MISSING,
        )
    annotated = Recording.objects.annotate(
        val=Case(
            *[When(sha256=sha, then=Value(value)) for sha, value in values.items()],
            default=Value(""),
        )
    ).filter(sha256__in=values)

    pairs = list(annotated.values_list("pk", "val"))
    pk_by_sha = dict(Recording.objects.filter(sha256__in=values).values_list("sha256", "pk"))

    # Ascending: fold(value) then PK.
    expected_asc = [pk for pk, val in sorted(pairs, key=lambda p: (_fold(p[1]), p[0]))]
    got_asc = list(
        annotated.order_by(folded_title_expression("val").asc(), "pk").values_list(
            "pk", flat=True
        )
    )
    assert got_asc == expected_asc

    # Descending: fold(value) DESC but PK still ASCENDING within ties.
    by_fold: dict[str, list] = {}
    for pk, val in pairs:
        by_fold.setdefault(_fold(val), []).append(pk)
    expected_desc = []
    for folded in sorted(by_fold, reverse=True):
        expected_desc.extend(sorted(by_fold[folded]))
    got_desc = list(
        annotated.order_by(folded_title_expression("val").desc(), "pk").values_list(
            "pk", flat=True
        )
    )
    assert got_desc == expected_desc

    # Explicit tie-break check: the äiti pair and åland pair are
    # casefold-equivalent, sort adjacently, and are PK-ordered.
    assert _fold(values["row-2"]) == _fold(values["row-3"]) == "äiti"
    assert _fold(values["row-4"]) == _fold(values["row-5"]) == "åland"
    order = {pk: index for index, pk in enumerate(got_asc)}

    def _adjacent_pk_ordered(sha_a: str, sha_b: str) -> None:
        pk_a, pk_b = pk_by_sha[sha_a], pk_by_sha[sha_b]
        assert abs(order[pk_a] - order[pk_b]) == 1, "tied titles must be adjacent"
        first = min(pk_a, pk_b)
        assert order[first] < order[max(pk_a, pk_b)], "tie must be PK-ordered"

    _adjacent_pk_ordered("row-2", "row-3")
    _adjacent_pk_ordered("row-4", "row-5")

# ---------------------------------------------------------------------------
# brain_fold (Step 5A.4.1): fold contract, isolation, failure semantics
# ---------------------------------------------------------------------------


def test_fold_text_contract():
    assert sqlite_unicode.fold_text("Äiti") == "äiti"
    assert sqlite_unicode.fold_text("A\u0308iti") == "äiti"  # NFC composes
    assert sqlite_unicode.fold_text(None) == ""
    assert sqlite_unicode.fold_text("") == ""
    # position-preserving for the practical scripts (no case expansion)
    assert len(sqlite_unicode.fold_text("KÄYTTÖ 中文")) == len("KÄYTTÖ 中文")


def test_brain_fold_available_in_sql_and_matches_python_contract():
    ensure_registered()  # opens the connection; the signal registers both
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT brain_fold(%s) = brain_fold(%s), brain_fold(%s)",
            ("Äiti", "äiti", "ÄITI"),
        )
        equal, folded = cursor.fetchone()
    assert equal == 1
    assert folded == sqlite_unicode.fold_text("ÄITI") == "äiti"


def test_fold_registration_failure_cannot_break_the_collation():
    """_register swallows a brain_fold failure: the collation still
    registers, unrelated ORM/page work is unaffected, nothing raises."""
    import sqlite3

    raw = sqlite3.connect(":memory:")

    class Inner:
        def create_collation(self, name, func):
            raw.create_collation(name, func)

        def create_function(self, *args, **kwargs):
            raise RuntimeError("boom-secret-sentinel create_function")

    class FakeConn:
        vendor = "sqlite"
        connection = Inner()

    sqlite_unicode._register(FakeConn())  # must NOT raise

    with raw:
        cursor = raw.execute("SELECT 'B' = 'b' COLLATE unicode_fold")
        assert cursor.fetchone()[0] == 1
        try:
            raw.execute("SELECT brain_fold('X')")
        except sqlite3.OperationalError:
            pass  # the function is simply absent; surfaced only at use
        else:
            raise AssertionError("brain_fold must not exist after failed registration")
    raw.close()


def test_ensure_fold_function_fails_stably_and_sanitize(monkeypatch):
    class Inner:
        def create_collation(self, name, func):
            return None

        def create_function(self, *args, **kwargs):
            raise RuntimeError("secret /Users/x/path boom")

    class FakeConn:
        vendor = "sqlite"
        connection = Inner()

        def ensure_connection(self):
            return None

    class FakeHandler:
        def __getitem__(self, using):
            assert using == "default"
            return FakeConn()

    monkeypatch.setattr(sqlite_unicode, "connections", FakeHandler())
    with pytest.raises(RuntimeError) as exc_info:
        sqlite_unicode.ensure_fold_function()
    assert str(exc_info.value) == sqlite_unicode._FOLD_UNAVAILABLE_MESSAGE
    assert "secret" not in str(exc_info.value)


def test_ensure_fold_function_keeps_collation_failure_message(monkeypatch):
    """A collation failure surfaced through ``ensure_connection`` (the
    signal path) keeps its OWN stable message — never relabeled as a
    fold failure."""
    from django.db.backends.signals import connection_created

    class Inner:
        def create_collation(self, name, func):
            raise RuntimeError("collation secret boom")

        def create_function(self, *args, **kwargs):
            return None

    class FakeConn:
        vendor = "sqlite"
        connection = Inner()

        def ensure_connection(self):
            connection_created.send(sender=type(self), connection=self)

    class FakeHandler:
        def __getitem__(self, using):
            return FakeConn()

    monkeypatch.setattr(sqlite_unicode, "connections", FakeHandler())
    with pytest.raises(RuntimeError) as exc_info:
        sqlite_unicode.ensure_fold_function()
    assert str(exc_info.value) == sqlite_unicode._UNAVAILABLE_MESSAGE


def test_fold_function_idempotent_and_survives_reconnect():
    from django.db import connection

    connection.close()
    sqlite_unicode.ensure_fold_function()  # first connection in the process
    sqlite_unicode.ensure_fold_function()  # idempotent
    connection.close()
    sqlite_unicode.ensure_fold_function()  # new raw connection re-registers
    with connection.cursor() as cursor:
        cursor.execute("SELECT brain_fold('ÄITI')")
        assert cursor.fetchone()[0] == "äiti"
