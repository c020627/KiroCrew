"""Tests for the session-pulse survey's Aperture backend proxy (feedback.py).

Scoped to ``/api/feedback/identity`` — the cross-instance identity hash added
to dedupe the survey across a real owner's multiple browsers/machines (see
``SessionPulseSurveyCard.tsx``'s ``resolvedIdentity``). The Aperture-facing
submit/eligible handlers have no existing test coverage in this repo; this
file does not attempt to backfill that, deliberately narrow to the new
endpoint and its pure hashing helper.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import feedback


class _FakeResp:
    """Stand-in for the aiohttp response returned by ``session.post``/``.get``.

    Doubles as its own async context manager, matching how ``feedback.py`` uses
    the response (``async with session.post(...) as resp:``).
    """

    def __init__(self, status: int, *, text_body: str = "", json_body: object = None) -> None:
        self.status = status
        self._text = text_body
        self._json = json_body

    async def text(self) -> str:
        return self._text

    async def json(self) -> object:
        return self._json

    async def __aenter__(self) -> "_FakeResp":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FakeSession:
    """Stand-in for ``aiohttp.ClientSession`` — an async context manager whose
    ``post``/``get`` return a preset :class:`_FakeResp`, or raise to exercise the
    handlers' network-failure branches. Accepts and ignores the ``json=`` /
    ``headers=`` / ``timeout=`` kwargs the real calls pass.
    """

    def __init__(self, resp: _FakeResp | None = None, *, raise_on_call: bool = False) -> None:
        self._resp = resp
        self._raise = raise_on_call

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    def post(self, *_a: object, **_kw: object) -> _FakeResp:
        if self._raise:
            raise RuntimeError("simulated network failure")
        assert self._resp is not None
        return self._resp

    def get(self, *_a: object, **_kw: object) -> _FakeResp:
        if self._raise:
            raise RuntimeError("simulated network failure")
        assert self._resp is not None
        return self._resp


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
    resp: _FakeResp | None = None,
    *,
    raise_on_call: bool = False,
) -> None:
    """Patch the ``aiohttp.ClientSession`` the feedback module resolves so an
    outbound call yields *resp* (or raises)."""
    monkeypatch.setattr(
        feedback.aiohttp,
        "ClientSession",
        lambda *_a, **_kw: _FakeSession(resp, raise_on_call=raise_on_call),
    )


def _submit_req(body: object) -> web.Request:
    """A mocked POST whose ``.json()`` resolves to *body*."""
    req = make_mocked_request("POST", "/api/feedback/submit")
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _submit_req_bad_json() -> web.Request:
    """A mocked POST whose ``.json()`` raises, as on a malformed body."""
    req = make_mocked_request("POST", "/api/feedback/submit")
    req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    return req


class TestHashedSurveyIdentity:
    """Unit coverage for the pure ``_hashed_survey_identity`` helper."""

    def test_sentinel_subjects_return_none(self) -> None:
        # Both unconfigured-install sentinels (mirroring
        # kiro_prerequisite.py's _LOCAL_DASHBOARD_OWNER_SUBJECTS) have no
        # real owner identity to hash.
        assert feedback._hashed_survey_identity("local-app") is None
        assert feedback._hashed_survey_identity("local-startup") is None

    def test_empty_subject_returns_none(self) -> None:
        assert feedback._hashed_survey_identity("") is None

    def test_real_owner_returns_deterministic_hex_digest(self) -> None:
        first = feedback._hashed_survey_identity("U01234ABCDE")
        second = feedback._hashed_survey_identity("U01234ABCDE")
        assert first is not None
        # Same owner -> same value every call. This is the whole point: it's
        # what lets the SAME person get the SAME identity across every
        # browser/machine that authenticates to this install.
        assert first == second
        assert len(first) == 64  # SHA-256 hex digest length
        int(first, 16)  # raises ValueError if this isn't valid hex

    def test_different_owners_get_different_hashes(self) -> None:
        a = feedback._hashed_survey_identity("U01234ABCDE")
        b = feedback._hashed_survey_identity("U99999ZZZZZ")
        assert a != b

    def test_hash_never_contains_the_raw_id(self) -> None:
        raw = "U01234ABCDE"
        hashed = feedback._hashed_survey_identity(raw)
        assert hashed is not None
        assert raw not in hashed


class TestApiFeedbackIdentity:
    """Route-level coverage: the handler reads ``request['user']`` (set by the
    standard auth middleware on every route, same source ``api_auth_me`` in
    auth_refresh.py reads) and returns the hash, or null, as JSON."""

    @pytest.mark.asyncio
    async def test_real_owner_returns_its_hash(self) -> None:
        req = make_mocked_request("GET", "/api/feedback/identity")
        req["user"] = "U01234ABCDE"

        resp = await feedback.api_feedback_identity(req)

        assert resp.status == 200
        body = json.loads(resp.body)
        assert body == {"identityHash": feedback._hashed_survey_identity("U01234ABCDE")}

    @pytest.mark.asyncio
    async def test_unconfigured_install_returns_null(self) -> None:
        req = make_mocked_request("GET", "/api/feedback/identity")
        req["user"] = "local-app"

        resp = await feedback.api_feedback_identity(req)

        assert resp.status == 200
        assert json.loads(resp.body) == {"identityHash": None}

    @pytest.mark.asyncio
    async def test_missing_user_key_returns_null_not_500(self) -> None:
        # No auth middleware ever ran (shouldn't happen in prod — every route
        # sits behind it — but the handler should still degrade safely
        # rather than raising on a missing dict key).
        req = make_mocked_request("GET", "/api/feedback/identity")

        resp = await feedback.api_feedback_identity(req)

        assert resp.status == 200
        assert json.loads(resp.body) == {"identityHash": None}


class TestCustomerResponses:
    """Unit coverage for the pure ``_customer_responses`` builder."""

    def test_missing_rating_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            feedback._customer_responses({})

    def test_blank_rating_raises_value_error(self) -> None:
        # A whitespace-only rating is treated as missing (it is ``.strip()``'d).
        with pytest.raises(ValueError):
            feedback._customer_responses({"rating": "   "})

    def test_rating_only_produces_single_radio_response(self) -> None:
        out = feedback._customer_responses({"rating": "Good"})
        assert out == [
            {
                "question": feedback._RATING_QUESTION,
                "pii": False,
                "response": {"responseType": "radio", "responseValue": "Good"},
            }
        ]

    def test_rating_is_stripped(self) -> None:
        out = feedback._customer_responses({"rating": "  Excellent  "})
        assert out[0]["response"]["responseValue"] == "Excellent"

    def test_feedback_appended_as_textarea_non_pii(self) -> None:
        out = feedback._customer_responses({"rating": "Fair", "feedback": "it was ok"})
        assert len(out) == 2
        fb = out[1]
        assert fb["question"] == feedback._FEEDBACK_QUESTION
        assert fb["pii"] is False
        assert fb["response"] == {"responseType": "textArea", "responseValue": "it was ok"}

    def test_email_appended_as_text_pii(self) -> None:
        out = feedback._customer_responses({"rating": "Good", "email": "a@b.com"})
        assert len(out) == 2
        em = out[1]
        assert em["question"] == feedback._EMAIL_QUESTION
        assert em["pii"] is True
        assert em["response"] == {"responseType": "text", "responseValue": "a@b.com"}

    def test_all_three_answers_in_order(self) -> None:
        out = feedback._customer_responses(
            {"rating": "Excellent", "feedback": "great", "email": "a@b.com"}
        )
        assert [r["response"]["responseType"] for r in out] == ["radio", "textArea", "text"]

    def test_blank_feedback_and_email_are_dropped(self) -> None:
        # Whitespace-only optional fields must not add empty responses.
        out = feedback._customer_responses({"rating": "Poor", "feedback": "   ", "email": "  "})
        assert len(out) == 1

    def test_feedback_and_email_are_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both free-text fields pass through the standard redaction helpers
        # before leaving the host. Patch them to a marker-returning spy so the
        # assertion doesn't depend on the exact redaction rules.
        calls: list[str] = []

        def _spy(text: str) -> tuple[str, int]:
            calls.append(text)
            return (f"<redacted:{text}>", 1)

        monkeypatch.setattr(feedback, "redact_exfiltration_urls", _spy)
        monkeypatch.setattr(feedback, "redact_credentials", _spy)

        out = feedback._customer_responses(
            {"rating": "Good", "feedback": "secret", "email": "e@x.com"}
        )
        # Each field runs through BOTH helpers, so both raw values were seen.
        assert "secret" in calls
        assert "e@x.com" in calls
        # The value that actually leaves is the redacted one.
        assert out[1]["response"]["responseValue"].startswith("<redacted:")
        assert out[2]["response"]["responseValue"].startswith("<redacted:")


class TestApiFeedbackSubmit:
    """Route-level coverage for ``api_feedback_submit`` and its failure modes."""

    @pytest.mark.asyncio
    async def test_unparseable_body_returns_400_invalid_body(self) -> None:
        resp = await feedback.api_feedback_submit(_submit_req_bad_json())
        assert resp.status == 400
        assert json.loads(resp.body) == {"code": "invalid_body"}

    @pytest.mark.asyncio
    async def test_non_dict_body_returns_400_invalid_body(self) -> None:
        resp = await feedback.api_feedback_submit(_submit_req(["not", "a", "dict"]))
        assert resp.status == 400
        assert json.loads(resp.body) == {"code": "invalid_body"}

    @pytest.mark.asyncio
    async def test_missing_rating_returns_400_missing_rating(self) -> None:
        resp = await feedback.api_feedback_submit(_submit_req({"feedback": "hi"}))
        assert resp.status == 400
        assert json.loads(resp.body) == {"code": "missing_rating"}

    @pytest.mark.asyncio
    async def test_success_returns_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_session(monkeypatch, _FakeResp(200, text_body="{}"))
        req = _submit_req(
            {
                "rating": "Good",
                "feedback": "nice",
                "email": "a@b.com",
                "sessionId": "chat-1-2",
                "kiroCrewVersion": "1.2.3",
                "userId": "U01",
            }
        )
        resp = await feedback.api_feedback_submit(req)
        assert resp.status == 200
        assert json.loads(resp.body) == {"ok": True}

    @pytest.mark.asyncio
    async def test_aperture_non_2xx_returns_502_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_session(monkeypatch, _FakeResp(400, text_body="bad form"))
        resp = await feedback.api_feedback_submit(_submit_req({"rating": "Good"}))
        assert resp.status == 502
        assert json.loads(resp.body) == {"code": "aperture_rejected"}

    @pytest.mark.asyncio
    async def test_aperture_unreachable_returns_502_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_session(monkeypatch, raise_on_call=True)
        resp = await feedback.api_feedback_submit(_submit_req({"rating": "Good"}))
        assert resp.status == 502
        assert json.loads(resp.body) == {"code": "aperture_unreachable"}


class TestApiFeedbackEligible:
    """Route-level coverage for ``api_feedback_eligible`` \u2014 fails CLOSED."""

    @pytest.mark.asyncio
    async def test_missing_user_id_returns_400(self) -> None:
        req = make_mocked_request("GET", "/api/feedback/eligible")
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 400
        assert json.loads(resp.body) == {"code": "missing_user_id"}

    @pytest.mark.asyncio
    async def test_eligible_when_aperture_returns_non_null_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_session(monkeypatch, _FakeResp(200, json_body={"prompt": "x"}))
        req = make_mocked_request("GET", "/api/feedback/eligible?userId=U01")
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": True}

    @pytest.mark.asyncio
    async def test_not_eligible_when_aperture_returns_null_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_session(monkeypatch, _FakeResp(200, json_body=None))
        req = make_mocked_request("GET", "/api/feedback/eligible?userId=U01")
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_not_eligible_on_non_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_session(monkeypatch, _FakeResp(503))
        req = make_mocked_request("GET", "/api/feedback/eligible?userId=U01")
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}

    @pytest.mark.asyncio
    async def test_not_eligible_when_request_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install_fake_session(monkeypatch, raise_on_call=True)
        req = make_mocked_request("GET", "/api/feedback/eligible?userId=U01")
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": False}


class TestSetupFeedbackRoutes:
    """The route registration helper wires all three endpoints."""

    def test_registers_all_three_routes(self) -> None:
        app = web.Application()
        feedback.setup_feedback_routes(app)
        registered = {(route.method, route.resource.canonical) for route in app.router.routes()}
        assert ("POST", "/api/feedback/submit") in registered
        assert ("GET", "/api/feedback/eligible") in registered
        assert ("GET", "/api/feedback/identity") in registered


class TestServerDerivedIdentity:
    """The submit + eligible handlers must key on the install's server-derived
    owner identity for configured owners, ignoring the client-supplied id, and
    fall back to the client id only for unconfigured-owner installs (GPT review
    finding: client identity bypasses configured-owner dedup)."""

    class _CapResp:
        status = 200

        async def text(self) -> str:
            return "{}"

        async def json(self) -> object:
            return {"prompt": "x"}

        async def __aenter__(self) -> "TestServerDerivedIdentity._CapResp":
            return self

        async def __aexit__(self, *_a: object) -> bool:
            return False

    def _capturing_session(self, captured: dict) -> object:
        resp = self._CapResp()

        class _CapSession:
            async def __aenter__(self) -> "_CapSession":
                return self

            async def __aexit__(self, *_a: object) -> bool:
                return False

            def post(self, _url: str, *, json: object = None, **_kw: object) -> object:
                captured["json"] = json
                return resp

            def get(self, _url: str, *, headers: object = None, **_kw: object) -> object:
                captured["headers"] = headers
                return resp

        return lambda *_a, **_kw: _CapSession()

    @pytest.mark.asyncio
    async def test_submit_configured_owner_uses_server_hash_not_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        monkeypatch.setattr(feedback.aiohttp, "ClientSession", self._capturing_session(captured))
        req = _submit_req({"rating": "Good", "userId": "client-random-xyz"})
        req["user"] = "U01234ABCDE"  # configured owner
        resp = await feedback.api_feedback_submit(req)
        assert resp.status == 200
        meta = {m["key"]: m["value"] for m in captured["json"]["metadataList"]}
        assert meta["userId"] == feedback._hashed_survey_identity("U01234ABCDE")
        assert meta["userId"] != "client-random-xyz"

    @pytest.mark.asyncio
    async def test_submit_unconfigured_owner_falls_back_to_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        monkeypatch.setattr(feedback.aiohttp, "ClientSession", self._capturing_session(captured))
        req = _submit_req({"rating": "Good", "userId": "client-random-xyz"})
        req["user"] = "local-app"  # unconfigured sentinel -> no server identity
        resp = await feedback.api_feedback_submit(req)
        assert resp.status == 200
        meta = {m["key"]: m["value"] for m in captured["json"]["metadataList"]}
        assert meta["userId"] == "client-random-xyz"

    @pytest.mark.asyncio
    async def test_eligible_configured_owner_uses_server_hash_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        monkeypatch.setattr(feedback.aiohttp, "ClientSession", self._capturing_session(captured))
        req = make_mocked_request("GET", "/api/feedback/eligible?userId=client-random")
        req["user"] = "U01234ABCDE"  # configured owner
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 200
        assert captured["headers"]["userid"] == feedback._hashed_survey_identity("U01234ABCDE")
        assert captured["headers"]["userid"] != "client-random"

    @pytest.mark.asyncio
    async def test_eligible_configured_owner_needs_no_client_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A configured owner is never rejected for a missing userId query param:
        # the server derives the identity itself.
        captured: dict = {}
        monkeypatch.setattr(feedback.aiohttp, "ClientSession", self._capturing_session(captured))
        req = make_mocked_request("GET", "/api/feedback/eligible")  # no userId query
        req["user"] = "U01234ABCDE"
        resp = await feedback.api_feedback_eligible(req)
        assert resp.status == 200
        assert json.loads(resp.body) == {"eligible": True}
