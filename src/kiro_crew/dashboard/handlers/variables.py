"""Dashboard handlers for the crew-variables cascade.

``GET`` reports every scope's own pairs plus the resolved map and where each
winning value came from. ``PUT`` replaces ONE scope's pairs wholesale rather than
patching single keys: deleting a pair is then just its absence from ``values``,
with no second verb and no ambiguity between "unset" and "set to empty string" —
an empty string is a legal value that still overrides a broader scope, so the two
cannot share an encoding.

Validation refuses rather than drops. The config loader deliberately drops a bad
pair with a warning so one hand-edited mistake cannot cost the rest of a scope or
fail a load, but a dashboard write is interactive: silently discarding a pair the
user just typed would look like a save that worked.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import web

from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewConfig,
    config_local_path,
    config_path,
    resolve_variables,
    update_config_locked,
)
from kiro_crew.sel import sel
from kiro_crew.variables import validate_pair

logger = logging.getLogger(__name__)

SCOPE_GLOBAL = "global"
SCOPE_WORKSPACE = "workspace"
_WRITABLE_SCOPES = (SCOPE_GLOBAL, SCOPE_WORKSPACE)


def _read_overlay() -> dict:
    """The raw ``config.local.json`` document, or ``{}``.

    Blocking; call from a thread. Never raises: an unreadable or malformed overlay
    is treated as absent, matching :meth:`KiroCrewConfig.save`, which swallows the
    same two errors rather than refusing to persist the base config.
    """
    local_path = config_local_path()
    if not local_path.is_file():
        return {}
    try:
        raw = json.loads(local_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _base_state(path: Path, workspace: str) -> tuple[dict, bool]:
    """The overlay document, and whether *workspace* is a key in the BASE mapping.

    Both are read from disk rather than from ``KiroCrewConfig.load()`` because that
    returns the two files merged, and this endpoint writes only the base one.
    Blocking; call from a thread.
    """
    overlay = _read_overlay()
    present = False
    if workspace:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        workspaces = raw.get("workspaces") if isinstance(raw, dict) else None
        present = isinstance(workspaces, dict) and workspace in workspaces
    return overlay, present


def _overlay_keys(overlay: dict, scope: str, workspace: str) -> set[str]:
    """Variable names this scope inherits from ``config.local.json``."""
    if scope == SCOPE_GLOBAL:
        owned = overlay.get("variables")
    else:
        workspaces = overlay.get("workspaces")
        entry = workspaces.get(workspace) if isinstance(workspaces, dict) else None
        owned = entry.get("variables") if isinstance(entry, dict) else None
    return set(owned) if isinstance(owned, dict) else set()


def _without_overlay(values: dict[str, str], overlay: dict, scope: str, workspace: str) -> dict:
    """Drop pairs whose value the overlay already supplies unchanged.

    Mirrors :func:`_subtract_overlay`, used by :meth:`KiroCrewConfig.save` for the
    same reason: a value owned by ``config.local.json`` must not be copied into
    ``config.json``, where it would look locally owned and would be shadowed by the
    overlay on the next load anyway. A pair the user genuinely CHANGED is kept, so
    an edit still takes effect in the base document.
    """
    owned = _overlay_keys(overlay, scope, workspace)
    if not owned:
        return values
    if scope == SCOPE_GLOBAL:
        source = overlay.get("variables") or {}
    else:
        workspaces = overlay.get("workspaces") or {}
        entry = workspaces.get(workspace) or {}
        source = entry.get("variables") or {}
    return {k: v for k, v in values.items() if not (k in owned and source.get(k) == v)}


class _WorkspaceVanished(Exception):
    """The target workspace was deleted between the pre-check and the locked write.

    Raised from inside the ``update_config_locked`` mutate callback. The callback
    is invoked unguarded inside the lock hold, so raising aborts the write before
    ``write_config_atomically`` runs and releases the lockfile on the way out —
    which is what makes refusal, rather than a resurrecting write, expressible
    from in there at all.
    """


def _view_inputs() -> tuple[KiroCrewConfig, dict]:
    """The config and the raw overlay. Blocking; call from a thread."""
    return KiroCrewConfig.load(), _read_overlay()


def _view(cfg: KiroCrewConfig, overlay: dict) -> dict:
    """Every scope's own pairs, plus the resolved map for the active context."""
    resolution = resolve_variables(cfg)
    return {
        "global": dict(cfg.variables),
        "workspaces": {name: dict(ws.variables) for name, ws in cfg.workspaces.items()},
        "crews": {name: dict(agent.variables) for name, agent in cfg.agents.items()},
        "effective": dict(resolution.values),
        "winning_scope": dict(resolution.winning_scope),
        "shadowed": {key: list(scopes) for key, scopes in resolution.shadowed.items()},
        "active_workspace": resolution.workspace_name,
        "active_agent": resolution.agent_name,
        # Pairs that come from config.local.json rather than config.json. This
        # endpoint writes only config.json, so these are NOT deletable here: a
        # delete would drop a key the base file may not even hold and the overlay
        # would re-supply it on the next load. Reported so the panel can say so
        # instead of showing a save that silently does nothing.
        "overlay_owned": {
            "global": sorted(_overlay_keys(overlay, SCOPE_GLOBAL, "")),
            "workspaces": {
                name: sorted(_overlay_keys(overlay, SCOPE_WORKSPACE, name))
                for name in cfg.workspaces
                if _overlay_keys(overlay, SCOPE_WORKSPACE, name)
            },
        },
    }


async def api_variables(request: web.Request) -> web.Response:
    """GET/PUT /api/variables — read the cascade, or replace one scope."""
    if request.method != "PUT":
        return web.json_response(_view(*(await asyncio.to_thread(_view_inputs))))

    caller = request.get("user", "dashboard")

    def _deny(code: str, error: str) -> web.Response:
        """Refuse a malformed request.

        The status is a literal rather than a parameter: the error-code contract
        gate counts a computed ``status=`` separately precisely because hoisting
        it into a variable would defeat the static check, and every refusal on
        this path is a 400 anyway.
        """
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="denied",
            error=error,
        )
        return web.json_response({"error": error, "code": code}, status=400)

    try:
        body = await request.json()
    except Exception:
        return _deny("variables_invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("variables_invalid_body", "body must be an object")

    scope = body.get("scope")
    if scope not in _WRITABLE_SCOPES:
        return _deny(
            "variables_invalid_scope",
            f"scope must be one of {', '.join(_WRITABLE_SCOPES)}",
        )

    raw_values = body.get("values")
    if not isinstance(raw_values, dict):
        return _deny("variables_invalid_values", "values must be an object")

    values: dict[str, str] = {}
    for key, value in raw_values.items():
        name, outcome = validate_pair(key, value)
        if name is None:
            sel().log_api_access(
                caller=caller,
                operation="variables.update",
                outcome="denied",
                error=f"invalid variable: {outcome}",
            )
            return web.json_response(
                {"error": outcome, "code": "variables_invalid_pair", "key": str(key)},
                status=400,
            )
        values[name] = outcome

    cfg = KiroCrewConfig.load()
    workspace = body.get("workspace") or ""
    if scope == SCOPE_WORKSPACE:
        if not isinstance(workspace, str) or workspace not in cfg.workspaces:
            return _deny(
                "variables_unknown_workspace",
                f"unknown workspace: {workspace!r}",
            )

    path = config_path()
    fallback_dir = cfg.workspaces[workspace].dir if scope == SCOPE_WORKSPACE else ""

    # ``cfg`` above is the MERGED view: KiroCrewConfig.load() deep-merges
    # config.local.json over config.json. This endpoint writes config.json ALONE,
    # so the read authority and the write target are different documents, and the
    # refusal below must not be decided from the merged one:
    #
    #   * A workspace declared only in the overlay is absent from config.json's
    #     mapping. Judged by the merged view it "exists", judged by the mapping it
    #     does not — so the deletion refusal would fire on a workspace GET had just
    #     reported, making it permanently unsavable.
    #   * Overlay-owned pairs must not be materialized into config.json.
    #     KiroCrewConfig.save() strips them with _subtract_overlay for exactly this
    #     reason; writing the merged map back would copy another file's values into
    #     the base and make them look locally owned.
    #
    # So: read the base document's own prior state, off-loop, and let THAT decide.
    overlay, base_had_workspace = await asyncio.to_thread(
        _base_state, path, workspace if scope == SCOPE_WORKSPACE else ""
    )
    values = _without_overlay(values, overlay, scope, workspace)

    def _mutate(data: dict) -> dict:
        """Apply this scope's replacement inside the locked critical section."""
        if scope == SCOPE_GLOBAL:
            data["variables"] = values
            return data
        raw_workspaces = data.get("workspaces")
        # The isinstance is repeated rather than derived into a bool: a bool does
        # not narrow a type, so deriving it leaves the value as
        # ``dict | Any | None`` and the assignments below fail to type-check.
        workspaces: dict = raw_workspaces if isinstance(raw_workspaces, dict) else {}
        if not isinstance(raw_workspaces, dict):
            data["workspaces"] = workspaces
        entry = workspaces.get(workspace)
        if isinstance(entry, str):
            # The legacy flat form maps a workspace name straight to its directory.
            # Widening it in place keeps the directory the operator set; assigning
            # a key onto the string would raise.
            entry = {"dir": entry}
            workspaces[workspace] = entry
        elif entry is None and base_had_workspace:
            # The workspace WAS a key in config.json's own mapping when this
            # request started and is gone now, so a concurrent writer deleted it
            # mid-flight. Recreating it would resurrect a workspace the operator
            # just removed, rebuilt from ``fallback_dir`` — a directory read before
            # the lock and therefore already stale. Refuse.
            #
            # Absence WITHOUT that prior presence is not a deletion: it is a first
            # write, or a workspace that lives in the overlay, and both must
            # materialize normally.
            raise _WorkspaceVanished
        elif not isinstance(entry, dict):
            entry = {"dir": fallback_dir}
            workspaces[workspace] = entry
        entry["variables"] = values
        return data

    # update_config_locked is the required path for a new config.json mutation: it
    # holds an advisory lock across the whole read-modify-write, so a concurrent CLI
    # or dashboard write cannot land between the read and the rename and have its
    # settings deleted by this whole-file replacement. It also preserves the file's
    # permission bits. Offloaded because it reads, locks and fsyncs — blocking work
    # that must not run on the gateway event loop.
    try:
        await asyncio.to_thread(update_config_locked, path, mutate=_mutate)
    except _WorkspaceVanished:
        return _deny(
            "variables_unknown_workspace",
            f"unknown workspace: {workspace!r}",
        )
    except ConfigReadError:
        sel().log_api_access(
            caller=caller,
            operation="variables.update",
            outcome="error",
            error="config.json is corrupt",
        )
        return web.json_response(
            {"error": "config.json is corrupt", "code": "config_corrupt"}, status=500
        )

    sel().log_api_access(
        caller=caller,
        operation="variables.update",
        outcome="ok",
        resources=f"{scope}:{workspace}" if scope == SCOPE_WORKSPACE else scope,
    )
    return web.json_response(
        {"ok": True, **_view(*(await asyncio.to_thread(_view_inputs)))}
    )
