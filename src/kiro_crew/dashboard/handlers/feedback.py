"""Server-side proxy to AWS Aperture's non-console feedback APIs.

Kiro Crew is self-hosted, open-source software: every install runs on its own
arbitrary origin (``localhost:5476``, a self-hosted domain, whatever a user
picks). Aperture's non-console APIs are browser-CORS-gated and allowlist a
finite, known set of origins — a model that assumes one controlled domain, not
an unbounded set of installs. A direct browser->Aperture fetch is therefore
never reliably reachable for this product, regardless of which domain any one
install happens to use.

This module moves the two calls the in-app session-pulse survey needs
(``SessionPulseSurveyCard.tsx``) to the Kiro Crew backend instead: the frontend
calls these same-origin routes, and the backend makes the actual Aperture
request server-to-server, where browser CORS does not apply at all.

Both the Aperture endpoint and the form namespace (category/name/version) are
hardcoded here, never accepted from the request — the client only ever
supplies the answer content and its own user id, mirroring the "endpoint is
hardcoded, never config-derived" control in ``kiro_usage_api.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

import aiohttp
from aiohttp import web

from kiro_crew.dashboard.token_secret import _get_secret
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Mirrors kiro_prerequisite.py's `_LOCAL_DASHBOARD_OWNER_SUBJECTS`: the auth
# token subject on any install with no `KIROCREW_OWNER_ID` configured — the
# default single-user setup. Duplicated here (rather than imported) to avoid
# a cross-handler-module dependency for a single-use check; if a third
# sentinel is ever added there, add it here too.
_LOCAL_DASHBOARD_OWNER_SUBJECTS = frozenset({"local-app", "local-startup"})

_INGESTION_URL = "https://ingestion.aperture-public-api.feedback.console.aws.dev/form"
_PROMPT_URL = "https://prompt.aperture-public-api.feedback.console.aws.dev/form/prompt"

_FORM_CATEGORY = "KiroCrew"  # brand-ok: literal Aperture portal identifier
_FORM_NAME = "SessionFeedback"
_FORM_VERSION = "1.0.1"
# serviceId/reference are console-navigation concepts (they normally mirror a
# console navId); Aperture's guidance for a non-console form is to use the
# team/product name instead.
_SERVICE_ID = "KiroCrew"  # brand-ok: literal Aperture portal identifier

_REQUEST_TIMEOUT_SECONDS = 10

# Question text and PII flags copied verbatim from the actual registered
# template (GET rendering.../form/template for category=KiroCrew,  # brand-ok: registered category id
# name=SessionFeedback, version=1.0.1) — since ingestion 400s on any
# text/type mismatch against the form-template, not just a semantic one.
_RATING_QUESTION = "How would you rate your experience with KiroCrew today?"  # brand-ok: verbatim registered template text, ingestion 400s on mismatch
_FEEDBACK_QUESTION = "Do you have additional feedback about this experience?"
_EMAIL_QUESTION = (
    "We may want to contact you about your feedback. "
    "Share your email to join our research panel. "
)


def _customer_responses(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Build Aperture's ``customerResponses`` shape from the survey's answers.

    Question text and response types must match what's registered for this
    form in the Aperture portal, or ingestion rejects the submission with a
    400 (form-template/response mismatch).
    """
    rating = str(body.get("rating") or "").strip()
    if not rating:
        raise ValueError("missing rating")
    responses: list[dict[str, Any]] = [
        {
            "question": _RATING_QUESTION,
            "pii": False,
            "response": {"responseType": "radio", "responseValue": rating},
        }
    ]
    # `feedback` is free text the user types; unlike `rating` (a fixed enum
    # from the frontend), it can contain anything they choose to paste or
    # describe, including a credential or an exfiltration-style URL. Redact
    # both before this leaves the host, matching the standard order used
    # everywhere else in this codebase (security.py).
    raw_feedback = str(body.get("feedback") or "").strip()
    feedback, _ = redact_exfiltration_urls(raw_feedback)
    feedback, _ = redact_credentials(feedback)
    if feedback:
        responses.append(
            {
                "question": _FEEDBACK_QUESTION,
                "pii": False,
                "response": {"responseType": "textArea", "responseValue": feedback},
            }
        )
    # Like `feedback`, `email` is user-typed free text -- someone could paste a
    # credential into it instead of a real address -- so it gets the same
    # redaction pass before leaving the host. `pii: True` below is a Aperture
    # disclosure flag (this field may legitimately contain identity data); it
    # does not substitute for content-safety redaction.
    email = str(body.get("email") or "").strip()
    email, _ = redact_exfiltration_urls(email)
    email, _ = redact_credentials(email)
    if email:
        responses.append(
            {
                "question": _EMAIL_QUESTION,
                "pii": True,
                "response": {"responseType": "text", "responseValue": email},
            }
        )
    return responses


async def api_feedback_submit(request: web.Request) -> web.Response:
    """POST /api/feedback/submit — forward a session-pulse survey response to Aperture.

    Body: ``{rating, feedback?, email?, sessionId, kiroCrewVersion, userId}``.
    Never blocks the caller on Aperture trouble — any failure (network, 4xx,
    5xx) is reported as a coded, non-2xx JSON body and the frontend already
    treats submission failures as non-fatal to the chat experience.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": "invalid_body"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"code": "invalid_body"}, status=400)

    try:
        customer_responses = _customer_responses(body)
    except ValueError:
        return web.json_response({"code": "missing_rating"}, status=400)

    session_id = str(body.get("sessionId") or "")
    kiro_crew_version = str(body.get("kiroCrewVersion") or "")
    # Prefer this install's server-derived owner identity when a real owner is
    # configured: it is the authoritative cross-instance identity and is present
    # on every authenticated request. Trusting the client-supplied userId instead
    # lets a browser that fell back to its per-browser random id (e.g. a transient
    # /api/feedback/identity failure) record one configured owner as multiple
    # respondents, defeating the cross-instance dedup this survey depends on. Fall
    # back to the client id only for unconfigured-owner installs, which have no
    # server identity to derive.
    server_identity = _hashed_survey_identity(request.get("user", ""))
    user_id = server_identity if server_identity is not None else str(body.get("userId") or "")

    payload = {
        "category": _FORM_CATEGORY,
        "name": _FORM_NAME,
        "version": _FORM_VERSION,
        "locale": "en_US",
        "customerResponses": customer_responses,
        # Order matters here, not just key/value/pii content: Aperture's
        # ingestion API validates metadataList against the form template
        # positionally rather than as an unordered set — the identical set of
        # keys in a different order 400s with the same "mismatch" error a
        # genuinely wrong key would. This order was empirically confirmed
        # against the template returned by
        # GET rendering.../form/template?category=KiroCrew&name=SessionFeedback&version=1.0.1,  # brand-ok: registered category id
        # whose own metadataList lists userId, sessionId, kiro_crew_version in
        # that order. (v1.0.0's template also registered isInternal, which we
        # could never determine client-side for an open-source app; v1.0.1
        # dropped that field from the form entirely, so it's no longer sent.)
        "metadataList": [
            {"key": "userId", "value": user_id, "pii": True},
            {"key": "sessionId", "value": session_id, "pii": False},
            {"key": "kiro_crew_version", "value": kiro_crew_version, "pii": False},
        ],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _INGESTION_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if 200 <= resp.status < 300:
                    return web.json_response({"ok": True})
                error_body = await resp.text()
                logger.warning(
                    "aperture ingestion rejected submission: http %s: %s",
                    resp.status,
                    error_body[:500],
                )
                return web.json_response({"code": "aperture_rejected"}, status=502)
    except Exception:
        logger.warning("aperture ingestion request failed", exc_info=True)
        return web.json_response({"code": "aperture_unreachable"}, status=502)


async def api_feedback_eligible(request: web.Request) -> web.Response:
    """GET /api/feedback/eligible?userId=... — ask Aperture if this user is due.

    Aperture tracks per-user prompt/cooldown state server-side. A null
    response body means "not eligible yet"; any JSON body means eligible. On
    any failure (network, non-2xx) this fails CLOSED — ``{"eligible": false}``
    — so an unreachable Aperture never surfaces the survey.
    """
    # Same server-derived-identity preference as api_feedback_submit: for a
    # configured owner the eligibility check must key on the authoritative hash,
    # not a client value that may have fallen back to a per-browser random id, or
    # the same person is prompted independently on each browser. Fall back to the
    # client-supplied userId only for unconfigured-owner installs.
    server_identity = _hashed_survey_identity(request.get("user", ""))
    user_id = (
        server_identity if server_identity is not None else request.query.get("userId", "").strip()
    )
    if not user_id:
        return web.json_response({"code": "missing_user_id"}, status=400)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _PROMPT_URL,
                headers={
                    "userid": user_id,
                    "category": _FORM_CATEGORY,
                    "name": _FORM_NAME,
                    "version": _FORM_VERSION,
                    "serviceid": _SERVICE_ID,
                    "content-type": "application/json",
                    "locale": "en_US",
                },
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    logger.info("aperture prompt check failed: http %s", resp.status)
                    return web.json_response({"eligible": False})
                data = await resp.json()
                return web.json_response({"eligible": data is not None})
    except Exception:
        logger.warning("aperture prompt request failed", exc_info=True)
        return web.json_response({"eligible": False})


def _hashed_survey_identity(user_id: str) -> str | None:
    """HMAC-SHA256 of *user_id*, keyed by this install's persisted token-signing
    secret — or ``None`` when *user_id* is one of the unconfigured-install
    sentinel values (no real owner identity exists to hash).

    Reusing the token-signing secret rather than minting a separate one keeps
    this a pure derivation with three properties: the same owner gets the
    same hash on THIS install across every browser/machine that authenticates
    to it (fixing the cross-instance duplicate-survey report — see the Aug
    2026 incident notes); the hash is meaningless without this install's
    secret, so nobody downstream (Aperture, a CSV export, a future data leak)
    can reverse it back to the real owner id; and no new secret file is
    introduced.
    """
    if not user_id or user_id in _LOCAL_DASHBOARD_OWNER_SUBJECTS:
        return None
    return hmac.new(_get_secret(), user_id.encode("utf-8"), hashlib.sha256).hexdigest()


async def api_feedback_identity(request: web.Request) -> web.Response:
    """GET /api/feedback/identity — the session-pulse survey's cross-instance
    identity signal.

    Returns ``{"identityHash": "<hex>" | null}``. Non-null only when this
    install has a real configured owner (``KIROCREW_OWNER_ID``), so the SAME
    person gets the SAME value across every browser/machine hitting this
    install — true per-person Aperture dedup, instead of the per-browser
    random id the frontend falls back to when this is null. No explicit auth
    check beyond the standard middleware, which every route already gets: an
    unauthenticated request never reaches this handler.
    """
    user_id = request.get("user", "")
    return web.json_response({"identityHash": _hashed_survey_identity(user_id)})


def setup_feedback_routes(app: web.Application) -> None:
    """Register the session-pulse survey's Aperture proxy routes."""
    app.router.add_post("/api/feedback/submit", api_feedback_submit)
    app.router.add_get("/api/feedback/eligible", api_feedback_eligible)
    app.router.add_get("/api/feedback/identity", api_feedback_identity)
