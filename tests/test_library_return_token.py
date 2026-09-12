"""Safe Library-return token (Step 6.2a): unit + web propagation/forgery.

Proves the approved contract:

- the token encodes ONLY canonical validated normal-Library state: the
  filter/sort pairs, a positive page, and the cards/table view; the
  destination is ALWAYS ``reverse('recordings')``;
- invalid/forged/oversized/non-canonical tokens decode to ``None`` and
  the Library falls back to its plain state;
- the token is generated once per normal Library render, added to the
  recording and section links, propagated verbatim through
  section-detail tabs, section summary confirmation/execution redirects
  and section tag redirects, and decoded by the recording-detail
  breadcrumb so the originating page/state is preserved;
- direct section links (no token) still work; search results never
  generate a token (no search-origin support);
- GETs stay strictly read-only.
"""

from __future__ import annotations

import json
import re

import pytest
from django.core import signing
from django.test import Client

from workflow.models import Section, SummaryState, TranscriptSegment
from workflow.services import library_return
from workflow.services.segmentation import save_segmented_version
from workflow.services.web_actions import section_state_fingerprint

from factories import make_summary_version, make_tag, make_transcribed_recording

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("forbid_external_effects")]

TZ = "Europe/Helsinki"


def _make_split_recording(sha="lib-token", count=6):
    rec, transcript, _fixed = make_transcribed_recording(
        [f"segment {i}" for i in range(count)], sha=sha
    )
    result = save_segmented_version(
        rec.pk, transcript.pk, 0, count, [2], ["Topic A", "Topic B"],
        [False, False], timezone_name=TZ,
    )
    sections = list(
        Section.objects.filter(segmented_version_id=result.version_id).order_by("ordinal")
    )
    return rec, transcript, sections


def _signed_payload(payload):
    """Directly sign a payload dict with the SAME salt (for forgery/edge
    tests that must bypass the canonical make_token)."""
    import base64

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return signing.Signer(salt=library_return._SALT).sign(b64)


def _decode_token_str(token: str):
    import base64

    signed = signing.Signer(salt=library_return._SALT).unsign(token)
    raw = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# Token unit contract
# ---------------------------------------------------------------------------


class TestTokenUnit:
    def test_round_trip_all_filter_kinds(self):
        from workflow.query import ListFilters

        filters = ListFilters(
            date_from=None, date_to=None, tags=["family", "work"],
            status="transcribed", summary="current", review=True,
            audio="present", has_summary=True, sort="title_az",
        )
        token = library_return.make_token(filters, 3, "table")
        decoded = library_return.decode_token(token, TZ)
        assert decoded is not None
        assert decoded.page == 3
        assert decoded.view == "table"
        assert decoded.filters.tags == ["family", "work"]
        assert decoded.filters.status == "transcribed"
        assert decoded.filters.summary == "current"
        assert decoded.filters.review is True
        assert decoded.filters.audio == "present"
        assert decoded.filters.has_summary is True
        assert decoded.filters.sort == "title_az"

    def test_default_sort_pairs_omitted_and_rejected(self):
        """Only canonical pairs round-trip: a redundant ``sort=newest``
        pair (the default) decodes to None — the token is server-generated
        state and must never carry noise."""
        from workflow.query import ListFilters

        filters = ListFilters()
        token = library_return.make_token(filters, 1, "cards")
        decoded = library_return.decode_token(token, TZ)
        assert decoded is not None
        assert decoded.filters.sort == "newest"
        assert decoded.page == 1 and decoded.view == "cards"

        bad = _signed_payload({"v": 1, "pairs": [["sort", "newest"]], "page": 1, "view": "cards"})
        assert library_return.decode_token(bad, TZ) is None

    def test_positive_page_bounded_and_view_allowlist(self):
        from workflow.query import ListFilters

        filters = ListFilters()
        for page in (0, -1, "2", None, 2.0, 100_000_001):
            token = _signed_payload({"v": 1, "pairs": [], "page": page, "view": "cards"})
            assert library_return.decode_token(token, TZ) is None, page
        for view in ("grid", "", None, "CARDS", "table/../../"):
            token = _signed_payload({"v": 1, "pairs": [], "page": 1, "view": view})
            assert library_return.decode_token(token, TZ) is None, view

    def test_forged_tampered_and_garbage_tokens_decode_none(self):
        from workflow.query import ListFilters

        token = library_return.make_token(ListFilters(), 1, "cards")
        # Signature tamper.
        flipped = token[:-1] + ("A" if token[-1] != "A" else "B")
        assert library_return.decode_token(flipped, TZ) is None
        # Wrong salt.
        other = signing.Signer(salt="other").sign(token.split(":", 1)[1])
        assert library_return.decode_token(other, TZ) is None
        # Garbage / wrong types / oversized.
        assert library_return.decode_token("garbage", TZ) is None
        assert library_return.decode_token("", TZ) is None
        assert library_return.decode_token(None, TZ) is None
        assert library_return.decode_token(12345, TZ) is None
        assert library_return.decode_token("x" * 9000, TZ) is None
        assert library_return.decode_token(token + token, TZ) is None

    def test_wrong_version_and_non_list_pairs_rejected(self):
        for payload in (
            {"v": 2, "pairs": [], "page": 1, "view": "cards"},
            {"pairs": [], "page": 1, "view": "cards"},
            {"v": 1, "pairs": "nope", "page": 1, "view": "cards"},
            {"v": 1, "pairs": [[1, 2]], "page": 1, "view": "cards"},
            {"v": 1, "pairs": [[5]], "page": 1, "view": "cards"},
            {"v": 1, "pairs": [5], "page": 1, "view": "cards"},
            {"v": 1, "pairs": [["tag"]], "page": 1, "view": "cards"},
            {"v": 1, "pairs": [["tag", "a", "b"]], "page": 1, "view": "cards"},
            {"v": 1, "pairs": [["tag", None]], "page": 1, "view": "cards"},
        ):
            token = _signed_payload(payload)
            assert library_return.decode_token(token, TZ) is None, payload

    def test_search_and_unknown_pairs_rejected(self):
        """The token never encodes search state: a ``q`` pair, a relevance
        sort, an unknown/invalid filter pair, or a DUPLICATE pair decodes
        the WHOLE token to None (fail closed — never a partial/wide filter
        set)."""
        for pairs in (
            [["q", "hello"]],
            [["sort", "relevance"]],
            [["status", "bogus"]],
            [["date", "not-a-date"]],
            [["page", "5"]],
            [["tag", "family"], ["evil", "x"]],
            [["tag", "a"], ["tag", "a"]],  # duplicate (server never emits)
        ):
            token = _signed_payload({"v": 1, "pairs": pairs, "page": 1, "view": "cards"})
            assert library_return.decode_token(token, TZ) is None, pairs

    def test_make_token_dedupes_duplicate_pairs(self):
        """``ListFilters.as_pairs()`` preserves a duplicate ``tag`` value
        from a redundant query string; the SERVER-GENERATED token must
        carry ONE canonical pair, and the decoded round-trip is a single
        canonical tag."""
        from workflow.query import ListFilters

        filters = ListFilters(tags=["family", "family", "work"])
        token = library_return.make_token(filters, 1, "cards")
        decoded = library_return.decode_token(token, TZ)
        assert decoded is not None
        assert decoded.filters.tags == ["family", "work"]
        assert decoded.filters.as_pairs() == [("tag", "family"), ("tag", "work")]

    def test_payload_never_contains_raw_values(self):
        """The signed token is URL-safe base64 + signature — the JSON
        payload is opaque in the URL (never raw filter values)."""
        from workflow.query import ListFilters

        filters = ListFilters(tags=["secret-tag"], sort="title_az")
        token = library_return.make_token(filters, 2, "table")
        assert "secret-tag" not in token
        assert "title_az" not in token
        assert "{" not in token and "}" not in token and '"' not in token
        assert re.fullmatch(r"[A-Za-z0-9_\-:]+", token)

    def test_return_url_always_recordings(self):
        token = library_return.make_token(
            __import__("workflow.query", fromlist=["ListFilters"]).ListFilters(), 1, "cards"
        )
        url = library_return.return_url(token)
        assert url.startswith("/recordings/?lib_return=")
        assert url.count("lib_return=") == 1


# ---------------------------------------------------------------------------
# Web: generation, propagation, forgery fallback
# ---------------------------------------------------------------------------


class TestWebToken:
    @pytest.fixture
    def client(self):
        return Client()

    def _section_link(self, content, rec, section):
        m = re.search(
            rf'href="/recordings/{rec.pk}/sections/{section.pk}/\?lib_return=([^"]+)"',
            content,
        )
        assert m, "section link with token not found"
        return m.group(1)

    def test_library_generates_token_and_section_links_carry_it(self, client):
        rec, transcript, sections = _make_split_recording("web-gen")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        assert self._section_link(content, rec, sections[1]) == token  # ONE token per render
        assert library_return.decode_token(token, TZ) is not None

    def test_library_token_encodes_effective_state(self, client):
        rec, transcript, sections = _make_split_recording("web-state")
        content = client.get(
            "/recordings/?view=table&sort=title_az&status=transcribed"
        ).content.decode()
        token = self._section_link(content, rec, sections[0])
        decoded = library_return.decode_token(token, TZ)
        assert decoded.view == "table"
        assert decoded.filters.sort == "title_az"
        assert decoded.filters.status == "transcribed"

    def test_valid_token_restores_state_on_library(self, client):
        rec, transcript, sections = _make_split_recording("web-restore")
        content = client.get(
            "/recordings/?view=table&sort=title_az"
        ).content.decode()
        token = self._section_link(content, rec, sections[0])
        restored = client.get(f"/recordings/?lib_return={token}").content.decode()
        assert '<table class="recording-table">' in restored  # table view
        assert "Topic A" in restored and "Topic B" in restored  # items present

    def test_invalid_token_falls_back_to_plain_library(self, client):
        rec, transcript, sections = _make_split_recording("web-forge")
        make_tag("Family")
        content = client.get("/recordings/").content.decode()
        assert "Topic A" in content
        # A forged token yields the PLAIN Library (no filters applied) —
        # the token is the sole carrier, so the rest of the raw query
        # string (here a tag filter that would EXCLUDE these untagged
        # sections) is ignored too.
        response = client.get("/recordings/?lib_return=totally-forged&tag=Family")
        assert response.status_code == 200
        assert "Topic A" in response.content.decode()

    def test_valid_token_ignores_query_string_and_search(self, client):
        """A valid token is the SOLE carrier of state: any raw ``q`` or
        forged mode is ignored (no search engine runs)."""
        rec, transcript, sections = _make_split_recording("web-q")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        response = client.get(f"/recordings/?lib_return={token}&q=segment&mode=semantic")
        assert response.status_code == 200
        assert "Topic A" in response.content.decode()
        assert "semantic search result" not in response.content.decode()

    def test_redundant_duplicate_tag_get_does_not_500(self, client):
        """A normal Library GET carrying redundant duplicate tag values is
        not a server error; the returned token carries ONE canonical tag."""
        from workflow.models import Tag, TagAssignment, TagOrigin

        rec, transcript, sections = _make_split_recording("web-dup-tag")
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag,
            origin=TagOrigin.MANUAL, is_active=True,
        )
        response = client.get("/recordings/?tag=Family&tag=Family")
        assert response.status_code == 200
        token = self._section_link(response.content.decode(), rec, sections[0])
        decoded = library_return.decode_token(token, TZ)
        assert decoded is not None
        assert decoded.filters.tags == ["family"]

    def test_raw_view_never_affects_token_present_render(self, client):
        """When ``lib_return`` is present, a raw ``view=`` query parameter
        must NOT affect rendering nor mutate the view cookie: a VALID token
        renders its OWN encoded view, and an INVALID token falls back to
        the cookie/default only."""
        rec, transcript, sections = _make_split_recording("web-view-ignore")
        # A valid TABLE-view token: raw view=cards must NOT override it.
        content = client.get("/recordings/?view=table&sort=title_az").content.decode()
        token = self._section_link(content, rec, sections[0])
        response = client.get(f"/recordings/?lib_return={token}&view=cards")
        assert '<table class="recording-table">' in response.content.decode()
        assert "recording-list" not in response.content.decode()
        # The raw view=cards did NOT refresh the view cookie (the Client
        # jar keeps the earlier explicit table preference; this response
        # carries NO new Set-Cookie for the view).
        assert client.cookies["brain_view_pref"].value == "table"
        assert "brain_view_pref" not in response.cookies

    def test_raw_view_ignored_for_invalid_token(self, client):
        """An INVALID token falls back using the cookie/default ONLY — a
        raw ``view=table`` query parameter is ignored and never mutates
        the cookie."""
        rec, transcript, sections = _make_split_recording("web-view-invalid")
        response = client.get("/recordings/?lib_return=forged&view=table")
        assert response.status_code == 200
        # Plain cards render (no cookie preference set on this client yet).
        assert "recording-list" in response.content.decode()
        assert "recording-table" not in response.content.decode()
        # No view cookie was written.
        assert "brain_view_pref" not in response.cookies

    def test_raw_view_ignored_for_invalid_token_with_cookie(self, client):
        rec, transcript, sections = _make_split_recording("web-view-invalid-cookie")
        client.cookies["brain_view_pref"] = "table"
        response = client.get("/recordings/?lib_return=forged&view=cards")
        assert response.status_code == 200
        # Falls back to the COOKIE (table), ignoring the raw view=cards.
        assert '<table class="recording-table">' in response.content.decode()
        assert "recording-list" not in response.content.decode()

    def test_search_results_never_generate_a_token(self, client):
        """No search-origin support: keyword results never produce a
        library-return token."""
        rec, transcript, sections = _make_split_recording("web-search")
        from workflow.services import search_index as si

        si.rebuild_index()
        content = client.get("/recordings/?q=segment").content.decode()
        assert "lib_return=" not in content

    def test_direct_section_link_works_without_token(self, client):
        rec, transcript, sections = _make_split_recording("web-direct")
        content = client.get(f"/recordings/{rec.pk}/sections/{sections[0].pk}/").content.decode()
        # Breadcrumb falls back to the plain Library.
        assert 'href="/recordings/"' in content
        assert "lib_return=" not in content

    def test_section_detail_propagates_token_to_breadcrumb_tabs_and_forms(self, client):
        rec, transcript, sections = _make_split_recording("web-prop")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        detail = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}"
        ).content.decode()
        # Breadcrumb keeps the originating page/state.
        assert f'href="/recordings/?lib_return={token}"' in detail
        # Variant tabs preserve the token (autoescaped ``&amp;``).
        assert f"?lib_return={token}&amp;language=en" in detail
        assert f"?lib_return={token}&amp;language=default" in detail
        # Section tag forms (modal Done + per-chip confirm/remove) carry it.
        assert (
            f'<form method="post" action="/recordings/{rec.pk}/sections/'
            f'{sections[0].pk}/tags/apply/" class="tag-selection-form">' in detail
        )
        assert f'name="lib_return" value="{token}"' in detail

    def test_recording_title_links_carry_token_cards(self, client):
        """A normal Library render's recording-backed item title link (card
        view) carries the ONE server-generated token — never a plain
        token-free detail link."""
        rec, _t, _s = make_transcribed_recording(["segment 0"], sha="web-rec-card")
        content = client.get("/recordings/").content.decode()
        assert f'href="/recordings/{rec.pk}/?lib_return=' in content
        assert f'href="/recordings/{rec.pk}/"' not in content

    def test_recording_title_links_carry_token_table(self, client):
        """The table view's recording-backed ``col-title`` link carries the
        token too."""
        rec, _t, _s = make_transcribed_recording(["segment 0"], sha="web-rec-table")
        content = client.get("/recordings/?view=table").content.decode()
        assert f'<a class="col-title" href="/recordings/{rec.pk}/?lib_return=' in content
        assert f'<a class="col-title" href="/recordings/{rec.pk}/"' not in content

    def test_section_card_parent_context_link_carries_token(self, client):
        """The card-view section provenance line's parent Recording link
        carries the token (its breadcrumb can return to this Library
        render too)."""
        rec, transcript, sections = _make_split_recording("web-parent-context")
        content = client.get("/recordings/").content.decode()
        assert (
            f'<span class="section-context">Topic · segments 0–1 · in '
            f'<a href="/recordings/{rec.pk}/?lib_return=' in content
        )

    def test_recording_detail_breadcrumb_restores_library_state(self, client):
        """A VALID token on a parent Recording detail renders the
        ``← Library`` breadcrumb with ``library_return.return_url(token)``
        and following that breadcrumb restores the originating filters/
        page/view."""
        rec, transcript, sections = _make_split_recording("web-detail-restore")
        content = client.get("/recordings/?view=table&sort=title_az").content.decode()
        token = self._section_link(content, rec, sections[0])
        detail = client.get(f"/recordings/{rec.pk}/?lib_return={token}").content.decode()
        assert f'href="/recordings/?lib_return={token}"' in detail
        restored = client.get(f"/recordings/?lib_return={token}").content.decode()
        assert '<table class="recording-table">' in restored  # table view restored
        assert "Topic A" in restored and "Topic B" in restored

    def test_recording_detail_invalid_token_plain_breadcrumb_no_echo(self, client):
        """An absent/invalid/forged token on a parent Recording detail
        leaves the plain ``← Library`` breadcrumb and is NEVER echoed."""
        rec, transcript, sections = _make_split_recording("web-detail-bad")
        detail = client.get(
            f"/recordings/{rec.pk}/?lib_return=forged"
        ).content.decode()
        assert 'href="/recordings/"' in detail
        assert "lib_return=" not in detail
        # Absent token is the same plain breadcrumb.
        plain = client.get(f"/recordings/{rec.pk}/").content.decode()
        assert 'href="/recordings/"' in plain
        assert "lib_return=" not in plain

    def test_search_result_recording_links_stay_token_free(self, client):
        """No search-origin support: keyword search result recording-detail
        links are PLAIN (no token ever appears in search output)."""
        rec, _t, _s = make_transcribed_recording(
            ["alpha beta gamma"], sha="web-search-rec"
        )
        from workflow.services import search_index as si

        si.rebuild_index()
        content = client.get("/recordings/?q=alpha").content.decode()
        assert f'href="/recordings/{rec.pk}/"' in content
        assert "lib_return=" not in content

    def test_section_detail_invalid_token_uses_plain_breadcrumb(self, client):
        rec, transcript, sections = _make_split_recording("web-bad-token")
        detail = client.get(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return=forged"
        ).content.decode()
        assert 'href="/recordings/"' in detail
        assert "lib_return=" not in detail

    def test_section_summary_confirmation_and_redirect_preserve_token(self, client, monkeypatch):
        rec, transcript, sections = _make_split_recording("web-summary")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        fingerprint = section_state_fingerprint(rec, sections[0])

        # First POST: the confirmation carries the token as a hidden field.
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summarize/",
            {"language": "default", "mode": "first", "lib_return": token},
        )
        assert response.status_code == 200
        assert f'name="lib_return" value="{token}"' in response.content.decode()

        # Confirmed POST (mocked summarize): the redirect preserves it.
        def fake(config, section, regenerate=False, **kwargs):
            return {"recording_id": rec.pk, "section_id": section.pk,
                    "result": "summarized", "output_language": "en"}

        monkeypatch.setattr("workflow.services.summarize.summarize_section_one", fake)
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summarize/",
            {"confirmed": "1", "language": "default", "mode": "first",
             "fingerprint": fingerprint, "lib_return": token},
        )
        assert response.status_code == 302
        assert response["Location"] == (
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}"
        )

    def test_section_summary_confirmation_cancel_keeps_validated_token(self, client):
        """The confirmation's Back and Cancel links both return to the
        section detail page WITH the already validated token — never a
        raw/unvalidated value — so the breadcrumb keeps the originating
        page/state."""
        rec, transcript, sections = _make_split_recording("web-sum-cancel")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summarize/",
            {"language": "default", "mode": "first", "lib_return": token},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert f'href="/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}"' in content
        # The ``Back to section`` anchor (not the parent recording) keeps
        # the validated token too (and stays clickable while the
        # synchronous request runs via the narrow ``data-confirm-exempt``
        # marker).
        assert (
            f'<a href="/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}" '
            "data-confirm-exempt>Back to section</a>"
        ) in content
        assert f'href="/recordings/{rec.pk}/"' not in content

    def test_section_summary_confirmation_cancel_drops_invalid_token(self, client):
        """An invalid token is never echoed into the Back/Cancel URLs — it
        falls back to the plain section-detail link."""
        rec, transcript, sections = _make_split_recording("web-sum-cancel-bad")
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summarize/",
            {"language": "default", "mode": "first", "lib_return": "forged"},
        )
        assert response.status_code == 200
        content = response.content.decode()
        assert f'href="/recordings/{rec.pk}/sections/{sections[0].pk}/"' in content
        assert "lib_return=" not in content
        assert "Back to recording" not in content
        assert f'href="/recordings/{rec.pk}/"' not in content

    def test_section_summary_invalid_token_is_dropped(self, client, monkeypatch):
        rec, transcript, sections = _make_split_recording("web-sum-bad")
        fingerprint = section_state_fingerprint(rec, sections[0])

        def fake(config, section, regenerate=False, **kwargs):
            return {"recording_id": rec.pk, "section_id": section.pk,
                    "result": "summarized", "output_language": "en"}

        monkeypatch.setattr("workflow.services.summarize.summarize_section_one", fake)
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/summarize/",
            {"confirmed": "1", "language": "default", "mode": "first",
             "fingerprint": fingerprint, "lib_return": "forged"},
        )
        assert response.status_code == 302
        assert response["Location"] == f"/recordings/{rec.pk}/sections/{sections[0].pk}/"

    def test_section_tag_apply_redirect_preserves_token(self, client):
        rec, transcript, sections = _make_split_recording("web-tag-apply")
        make_tag("Family")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        tag = __import__("workflow.models", fromlist=["Tag"]).Tag.objects.get(name="Family")
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"selected_tags": [str(tag.pk)], "lib_return": token},
        )
        assert response.status_code == 302
        assert response["Location"] == (
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}"
        )

    def test_section_tag_remove_redirect_preserves_token(self, client):
        from workflow.models import Tag, TagAssignment, TagOrigin

        rec, transcript, sections = _make_split_recording("web-tag-remove")
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag,
            origin=TagOrigin.MANUAL, is_active=True,
        )
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/remove/",
            {"lib_return": token},
        )
        assert response.status_code == 302
        assert response["Location"] == (
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}"
        )

    def test_section_tag_confirm_redirect_preserves_token(self, client):
        from workflow.models import TagAssignment, TagOrigin

        rec, transcript, sections = _make_split_recording("web-tag-confirm")
        tag = make_tag("Family")
        TagAssignment.objects.create(
            recording=rec, section=sections[0], tag=tag,
            origin=TagOrigin.SUGGESTED, is_active=True,
        )
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/{tag.pk}/confirm/",
            {"lib_return": token},
        )
        assert response.status_code == 302
        assert response["Location"] == (
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/?lib_return={token}"
        )

    def test_section_tag_redirect_drops_forged_token(self, client):
        rec, transcript, sections = _make_split_recording("web-tag-forge")
        response = client.post(
            f"/recordings/{rec.pk}/sections/{sections[0].pk}/tags/apply/",
            {"lib_return": "forged"},
        )
        assert response.status_code == 302
        assert response["Location"] == f"/recordings/{rec.pk}/sections/{sections[0].pk}/"

    def test_get_with_token_remains_read_only(self, client):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        rec, transcript, sections = _make_split_recording("web-readonly")
        content = client.get("/recordings/").content.decode()
        token = self._section_link(content, rec, sections[0])
        with CaptureQueriesContext(connection) as ctx:
            response = client.get(f"/recordings/?lib_return={token}")
        assert response.status_code == 200
        non_select = [
            q for q in ctx.captured_queries
            if not q["sql"].lstrip().upper().startswith("SELECT")
        ]
        assert non_select == []
        # Zero side effects: the fixed + two topic sections are untouched.
        assert Section.objects.filter(segmented_version_id__isnull=False).count() == 2

    def test_token_in_pagination_links(self, client):
        """Normal Library pagination keeps working with a token present:
        the token-driven render is a normal table Library (60 items > 25
        per page) and the token is regenerated once per render."""
        for index in range(30):
            _make_split_recording(f"web-page-{index}")
        content = client.get("/recordings/?view=table&sort=title_az").content.decode()
        m = re.search(
            r'href="/recordings/[0-9a-f-]+/sections/\d+/\?lib_return=([^"]+)"',
            content,
        )
        assert m, "no tokenized section link on page 1"
        token = m.group(1)
        page2 = client.get(f"/recordings/?lib_return={token}&view=table")
        assert page2.status_code == 200
        assert '<table class="recording-table">' in page2.content.decode()