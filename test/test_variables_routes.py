"""Tests for the /api/variables dashboard routes.

Hermetic: every case redirects ``config_path`` at a ``tmp_path`` file and hands the
handler a config object directly, so nothing reads or writes the real data home
and the loader's fingerprint cache never participates.
"""

from __future__ import annotations

import inspect
import json
import os
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, WorkspaceConfig
from kiro_crew.dashboard.handlers import variables as vh

_NOT_POSIX = os.name == "nt"

# Every test here awaits a handler directly.
pytestmark = pytest.mark.asyncio


def _request(method: str, body: Any = ...):
    """A mocked request. ``body=None`` models a malformed payload, which is what
    the handler's ``except Exception -> 400`` branch is written for."""
    req = make_mocked_request(method, "/api/variables")
    if body is None:
        req.json = AsyncMock(side_effect=ValueError("not json"))  # type: ignore[method-assign]
    elif body is not ...:
        req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


def _config() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.variables = {"baseUrl": "https://global.test", "orgName": "Acme"}
    cfg.workspaces = {
        "default": WorkspaceConfig(dir="workspace"),
        "ops": WorkspaceConfig(dir="workspace-ops", variables={"baseUrl": "https://ops.test"}),
    }
    cfg.default_workspace = "default"
    cfg.agents = {
        "crew1": KiroCrewAgentConfig(
            kiro_agent="kirocrew", workspace="ops", variables={"queue": "oncall"}
        )
    }
    cfg.default_agent = "crew1"
    return cfg


@pytest.fixture()
def wired(monkeypatch, tmp_path: Path):
    """Redirect the handler at a temp config file and a fixed config object."""
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspaces": {"ops": {"dir": "workspace-ops"}}}), encoding="utf-8")
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    return cfg, path


async def test_get_reports_every_scope(wired):
    cfg, _ = wired
    resp = await vh.api_variables(_request("GET"))
    assert resp.status == 200
    payload = json.loads(resp.text)
    assert payload["global"] == {"baseUrl": "https://global.test", "orgName": "Acme"}
    assert payload["workspaces"]["ops"] == {"baseUrl": "https://ops.test"}
    assert payload["crews"]["crew1"] == {"queue": "oncall"}


async def test_get_reports_resolution_and_provenance(wired):
    resp = await vh.api_variables(_request("GET"))
    payload = json.loads(resp.text)
    # crew1 binds workspace ops, so the workspace value wins over global.
    assert payload["effective"]["baseUrl"] == "https://ops.test"
    assert payload["winning_scope"]["baseUrl"] == "workspace"
    assert payload["shadowed"]["baseUrl"] == ["global"]
    assert payload["effective"]["queue"] == "oncall"
    assert payload["winning_scope"]["queue"] == "crew"
    assert payload["active_workspace"] == "ops"
    assert payload["active_agent"] == "crew1"


async def test_put_global_persists(wired):
    _, path = wired
    resp = await vh.api_variables(
        _request("PUT", {"scope": "global", "values": {"a": "1", "b": ""}})
    )
    assert resp.status == 200
    assert json.loads(resp.text)["ok"] is True
    assert json.loads(path.read_text(encoding="utf-8"))["variables"] == {"a": "1", "b": ""}


async def test_put_is_a_whole_scope_replace(wired):
    """Absence from values is how a pair is deleted; there is no unset verb."""
    _, path = wired
    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1", "b": "2"}}))
    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1"}}))
    assert json.loads(path.read_text(encoding="utf-8"))["variables"] == {"a": "1"}


async def test_put_workspace_persists_under_that_workspace(wired):
    _, path = wired
    resp = await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "values": {"queue": "tier2"}})
    )
    assert resp.status == 200
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["workspaces"]["ops"]["variables"] == {"queue": "tier2"}
    assert data["workspaces"]["ops"]["dir"] == "workspace-ops"


async def test_put_widens_a_legacy_flat_workspace_entry(monkeypatch, tmp_path: Path):
    """The flat form maps a name straight to its directory; assigning a key onto
    that string would raise, and replacing it would lose the directory."""
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"workspaces": {"ops": "legacy-dir"}}), encoding="utf-8")
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    resp = await vh.api_variables(
        _request("PUT", {"scope": "workspace", "workspace": "ops", "values": {"a": "1"}})
    )
    assert resp.status == 200
    entry = json.loads(path.read_text(encoding="utf-8"))["workspaces"]["ops"]
    assert entry == {"dir": "legacy-dir", "variables": {"a": "1"}}


async def test_put_preserves_unrelated_config_keys(monkeypatch, tmp_path: Path):
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"agent": {"model": "auto"}, "workspaces": {"ops": {"dir": "d"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1"}}))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent"] == {"model": "auto"}
    assert data["workspaces"]["ops"] == {"dir": "d"}


@pytest.mark.skipif(_NOT_POSIX, reason="POSIX file modes")
async def test_put_preserves_existing_file_permissions(monkeypatch, tmp_path: Path):
    """Widening an operator's tightened config.json on an unrelated save would be
    a silent downgrade."""
    cfg = _config()
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(vh, "config_path", lambda: path)
    monkeypatch.setattr(vh.KiroCrewConfig, "load", classmethod(lambda cls: cfg))

    await vh.api_variables(_request("PUT", {"scope": "global", "values": {"a": "1"}}))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestRejections:
    """Every non-2xx body carries a machine-readable ``code`` — the dashboard
    renders server prose verbatim, so the identifier is what a client switches on."""

    async def test_malformed_json(self, wired):
        resp = await vh.api_variables(_request("PUT", None))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_json"

    async def test_non_object_body(self, wired):
        resp = await vh.api_variables(_request("PUT", ["not", "an", "object"]))
        assert json.loads(resp.text)["code"] == "variables_invalid_body"

    async def test_unknown_scope(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "crew", "values": {}}))
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_scope"

    async def test_missing_values(self, wired):
        resp = await vh.api_variables(_request("PUT", {"scope": "global"}))
        assert json.loads(resp.text)["code"] == "variables_invalid_values"

    async def test_unknown_workspace(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "workspace", "workspace": "nope", "values": {}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_unknown_workspace"

    async def test_invalid_name_names_the_key(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"1bad": "x"}})
        )
        assert resp.status == 400
        payload = json.loads(resp.text)
        assert payload["code"] == "variables_invalid_pair"
        assert payload["key"] == "1bad"

    async def test_reserved_name_is_refused(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"MAX_SUBAGENTS": "9"}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_pair"

    async def test_control_character_is_refused(self, wired):
        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"a": "one\ntwo"}})
        )
        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_invalid_pair"

    async def test_a_rejected_write_persists_nothing(self, wired):
        _, path = wired
        before = path.read_text(encoding="utf-8")
        await vh.api_variables(_request("PUT", {"scope": "global", "values": {"1bad": "x"}}))
        assert path.read_text(encoding="utf-8") == before


async def test_payload_satisfies_the_frontend_interface(wired):
    """The panel reads a typed `VariablesView`; a field renamed on one side and
    not the other type-checks fine and fails only in a browser.

    The parser is brace-aware and strips the optional marker. Both matter: the
    first version stopped at the first ``}``, which a nested object type closes
    early, and it kept the ``?`` from an optional field, so ``overlay_owned?`` was
    compared against a payload key spelled ``overlay_owned`` and reported missing.
    Optional fields are still required to be PRESENT here — the handler is their
    only producer, so absence would mean the field was renamed or dropped.
    """
    client = (
        Path(__file__).resolve().parents[1] / "website" / "src" / "api" / "client.ts"
    ).read_text(encoding="utf-8")
    start = client.index("export interface VariablesView")
    body_start = client.index("{", start)
    depth = 0
    declared: set[str] = set()
    for line in client[body_start:].splitlines():
        stripped = line.strip()
        if stripped.startswith(("/*", "*", "//")):
            continue
        # Collect only depth-1 members: a nested object's own fields belong to that
        # inner type, not to VariablesView.
        if depth == 1 and ":" in stripped:
            name = stripped.split(":")[0].strip().rstrip("?")
            if name:
                declared.add(name)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    assert declared, "could not parse VariablesView — the interface moved or was renamed"

    resp = await vh.api_variables(_request("GET"))
    payload = json.loads(resp.text)
    missing = declared - set(payload)
    assert not missing, f"handler payload is missing fields the panel reads: {sorted(missing)}"


class TestTheWriteIsLockedAndOffLoop:
    """A whole-file replacement must not race another config writer, and must not
    block the gateway's event loop."""

    async def test_the_mutation_goes_through_the_locked_helper(self, wired):
        _, path = wired
        seen: dict[str, object] = {}

        def _fake(target, *, mutate, **kwargs):
            seen["path"] = target
            data = {"agent": {"model": "auto"}}
            seen["result"] = mutate(data)
            return data

        with patch.object(vh, "update_config_locked", _fake) as _:
            resp = await vh.api_variables(
                _request("PUT", {"scope": "global", "values": {"a": "1"}})
            )
        assert resp.status == 200
        assert seen["path"] == path
        # The callback applied the scope write to the dict the lock handed it,
        # rather than to a copy read before the lock was taken.
        assert seen["result"]["variables"] == {"a": "1"}
        assert seen["result"]["agent"] == {"model": "auto"}

    async def test_the_locked_helper_runs_off_the_event_loop(self, wired):
        """It reads, locks and fsyncs; on the loop that freezes every task."""
        source = inspect.getsource(vh)
        assert "asyncio.to_thread(update_config_locked" in source
        # The unlocked WRITE forms must be gone. Match the CALL form, not the bare
        # name: prose explaining why the write goes through the locked helper names
        # these functions, and a guard that cannot tell a mention from a call fails
        # on a comment while a real call stays invisible.
        assert "write_config_atomically(" not in source
        # Reads of the base document DO exist (the deletion refusal has to know the
        # file's own prior state, which the merged config cannot report), so the
        # rule is that each is dispatched to a thread, not that none exists.
        #
        # Scoped to the async handler deliberately: it is the only place running on
        # the loop. A module-wide substring scan flags `_read_overlay()` inside
        # `_base_state` — already off-loop, since _base_state itself is only ever
        # reached through to_thread — and cannot tell that nesting from a real
        # violation.
        handler = inspect.getsource(vh.api_variables)
        assert "asyncio.to_thread(\n        _base_state" in handler
        assert "asyncio.to_thread(_view_inputs)" in handler
        for on_loop in ("= _base_state(", "= _view_inputs(", "_read_overlay("):
            assert on_loop not in handler, f"{on_loop} runs on the event loop"

    async def test_a_corrupt_config_fails_closed_without_writing(self, wired):
        def _raise(*_args, **_kwargs):
            raise vh.ConfigReadError("bad json")

        with patch.object(vh, "update_config_locked", _raise):
            resp = await vh.api_variables(
                _request("PUT", {"scope": "global", "values": {"a": "1"}})
            )
        assert resp.status == 500
        assert json.loads(resp.text)["code"] == "config_corrupt"

    async def test_a_workspace_write_carries_the_known_directory(self, wired):
        """The fallback dir is captured BEFORE the lock, so the callback stays a
        pure function of already-resolved state.

        Modelled as a FIRST write: the base document holds no workspaces mapping, so
        the entry is materialized rather than refused. The earlier version of this
        test handed the callback ``{}`` while the base file on disk still listed the
        workspace — a state that now reads as "present, then gone", i.e. exactly the
        concurrent deletion the handler refuses.
        """
        _, path = wired
        path.write_text(json.dumps({}), encoding="utf-8")
        captured: dict[str, object] = {}

        def _fake(_target, *, mutate, **_kwargs):
            data: dict = {}
            captured["result"] = mutate(data)
            return data

        with patch.object(vh, "update_config_locked", _fake):
            resp = await vh.api_variables(
                _request(
                    "PUT",
                    {"scope": "workspace", "workspace": "ops", "values": {"q": "1"}},
                )
            )
        assert resp.status == 200
        entry = captured["result"]["workspaces"]["ops"]
        assert entry["variables"] == {"q": "1"}
        assert entry["dir"] == "workspace-ops"


class TestAConcurrentWorkspaceDeletion:
    """The ``workspace not in cfg.workspaces`` pre-check reads UNLOCKED, so its
    answer can go stale before the locked write runs.

    Asserting on an already-absent workspace would only re-test the pre-check. The
    workspace has to vanish in the window BETWEEN the pre-check and the mutate
    callback — the interleaving where the old code rebuilt the entry from a
    ``fallback_dir`` captured before the lock, resurrecting a workspace the
    operator had just deleted.
    """

    async def test_a_vanished_workspace_is_refused_not_resurrected(self, wired):
        _, path = wired
        path.write_text(
            json.dumps({"workspaces": {"ops": {"dir": "workspace-ops"}}}),
            encoding="utf-8",
        )
        real_update = vh.update_config_locked

        def _delete_then_update(target, *, mutate, **kwargs):
            # A concurrent writer removing the workspace after the pre-check passed.
            data = json.loads(Path(target).read_text(encoding="utf-8"))
            del data["workspaces"]["ops"]
            Path(target).write_text(json.dumps(data), encoding="utf-8")
            return real_update(target, mutate=mutate, **kwargs)

        with patch.object(vh, "update_config_locked", _delete_then_update):
            resp = await vh.api_variables(
                _request(
                    "PUT",
                    {"scope": "workspace", "workspace": "ops", "values": {"a": "1"}},
                )
            )

        assert resp.status == 400
        assert json.loads(resp.text)["code"] == "variables_unknown_workspace"
        after = json.loads(path.read_text(encoding="utf-8"))
        assert "ops" not in after["workspaces"], (
            "the concurrently-deleted workspace was resurrected by the write"
        )

    async def test_the_refusal_leaves_other_scopes_untouched(self, wired):
        """Refusing must abort the whole write, not land a partial one."""
        _, path = wired
        path.write_text(
            json.dumps(
                {"workspaces": {"ops": {"dir": "workspace-ops"}}, "agent": {"model": "auto"}}
            ),
            encoding="utf-8",
        )
        real_update = vh.update_config_locked

        def _delete_then_update(target, *, mutate, **kwargs):
            data = json.loads(Path(target).read_text(encoding="utf-8"))
            del data["workspaces"]["ops"]
            Path(target).write_text(json.dumps(data), encoding="utf-8")
            return real_update(target, mutate=mutate, **kwargs)

        with patch.object(vh, "update_config_locked", _delete_then_update):
            resp = await vh.api_variables(
                _request(
                    "PUT",
                    {"scope": "workspace", "workspace": "ops", "values": {"a": "1"}},
                )
            )

        assert resp.status == 400
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["agent"] == {"model": "auto"}
        assert "variables" not in after


class TestTheLocalOverlay:
    """``KiroCrewConfig.load()`` deep-merges ``config.local.json`` over
    ``config.json``, but this endpoint writes ``config.json`` alone.

    Nothing in the original suite constructed an overlay at all, which is why a
    deterministic bug lived here: judged by the merged view an overlay-declared
    workspace exists, judged by the base mapping it does not, so the concurrent-
    deletion refusal fired on a workspace the same endpoint had just listed and it
    could never be saved. The refusal is now keyed on the BASE document's own prior
    state, and overlay-owned values are subtracted the way
    ``KiroCrewConfig.save()`` subtracts them.
    """

    def _overlay(self, tmp_path: Path, payload: dict) -> Path:
        local = tmp_path / "config.local.json"
        local.write_text(json.dumps(payload), encoding="utf-8")
        return local

    async def test_a_workspace_declared_only_in_the_overlay_can_be_saved(
        self, wired, monkeypatch, tmp_path
    ):
        cfg, path = wired
        # Base file has ops only; the overlay contributes a second workspace, which
        # the merged config (and therefore GET) reports.
        path.write_text(
            json.dumps({"workspaces": {"ops": {"dir": "workspace-ops"}}}), encoding="utf-8"
        )
        local = self._overlay(tmp_path, {"workspaces": {"overlaid": {"dir": "ws-overlaid"}}})
        monkeypatch.setattr(vh, "config_local_path", lambda: local)
        cfg.workspaces["overlaid"] = WorkspaceConfig(dir="ws-overlaid")

        resp = await vh.api_variables(
            _request(
                "PUT",
                {"scope": "workspace", "workspace": "overlaid", "values": {"a": "1"}},
            )
        )

        assert resp.status == 200, (
            "a workspace the GET view lists was refused as concurrently deleted"
        )
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["workspaces"]["overlaid"]["variables"] == {"a": "1"}

    async def test_an_overlay_owned_value_is_not_copied_into_the_base(
        self, wired, monkeypatch, tmp_path
    ):
        """Mirrors save()'s _subtract_overlay: a value the overlay supplies must not
        be materialized into config.json, where it would look locally owned."""
        _, path = wired
        path.write_text(json.dumps({}), encoding="utf-8")
        local = self._overlay(tmp_path, {"variables": {"FROM_OVERLAY": "keep"}})
        monkeypatch.setattr(vh, "config_local_path", lambda: local)

        resp = await vh.api_variables(
            _request(
                "PUT",
                {
                    "scope": "global",
                    # The overlay pair echoed back unchanged, plus a genuinely new one.
                    "values": {"FROM_OVERLAY": "keep", "MINE": "yes"},
                },
            )
        )

        assert resp.status == 200
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["variables"] == {"MINE": "yes"}, (
            "the overlay-owned pair leaked into config.json"
        )

    async def test_changing_an_overlay_owned_value_still_writes_it(
        self, wired, monkeypatch, tmp_path
    ):
        """Subtraction is by value, not by key: an actual EDIT must take effect,
        otherwise the base could never override the overlay's default."""
        _, path = wired
        path.write_text(json.dumps({}), encoding="utf-8")
        local = self._overlay(tmp_path, {"variables": {"FROM_OVERLAY": "old"}})
        monkeypatch.setattr(vh, "config_local_path", lambda: local)

        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"FROM_OVERLAY": "new"}})
        )

        assert resp.status == 200
        after = json.loads(path.read_text(encoding="utf-8"))
        assert after["variables"] == {"FROM_OVERLAY": "new"}

    async def test_the_view_reports_which_keys_the_overlay_owns(
        self, wired, monkeypatch, tmp_path
    ):
        """Reported so the panel can say a pair is not deletable here, instead of
        showing a delete that succeeds and then reappears on the next read."""
        _, _path = wired
        local = self._overlay(
            tmp_path,
            {
                "variables": {"FROM_OVERLAY": "x"},
                "workspaces": {"ops": {"variables": {"WS_OVERLAY": "y"}}},
            },
        )
        monkeypatch.setattr(vh, "config_local_path", lambda: local)

        resp = await vh.api_variables(_request("GET"))

        payload = json.loads(resp.text)
        assert payload["overlay_owned"]["global"] == ["FROM_OVERLAY"]
        assert payload["overlay_owned"]["workspaces"]["ops"] == ["WS_OVERLAY"]

    async def test_a_malformed_overlay_is_treated_as_absent(
        self, wired, monkeypatch, tmp_path
    ):
        """save() swallows exactly these two errors rather than refusing to persist
        the base config, so this path matches it."""
        _, path = wired
        path.write_text(json.dumps({}), encoding="utf-8")
        local = tmp_path / "config.local.json"
        local.write_text("{ not json", encoding="utf-8")
        monkeypatch.setattr(vh, "config_local_path", lambda: local)

        resp = await vh.api_variables(
            _request("PUT", {"scope": "global", "values": {"MINE": "yes"}})
        )

        assert resp.status == 200
        assert json.loads(path.read_text(encoding="utf-8"))["variables"] == {"MINE": "yes"}
