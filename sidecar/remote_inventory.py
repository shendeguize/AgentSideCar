"""Bounded DSH Center inventory discovery and fallback parsing."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from sidecar.process_runner import run_bounded as _run_bounded
from sidecar.remote_types import (
    ELIGIBLE_PHASES,
    MAX_HOSTS,
    MAX_INVENTORY_BYTES,
    MAX_STDERR_BYTES,
    PROBE_TIMEOUT_SECONDS,
    RemoteHost,
    RemoteInventoryError,
    _completed_overflow,
    _completed_returncode,
    _completed_stdout,
    _validate_alias,
    parse_bounded_json,
)


def _inventory_container(
    value: Any,
) -> List[Tuple[Optional[str], Mapping[str, Any]]]:
    container = value
    if isinstance(value, dict):
        if "hosts" not in value:
            raise RemoteInventoryError()
        container = value["hosts"]
    rows: List[Tuple[Optional[str], Mapping[str, Any]]] = []
    if isinstance(container, list):
        if len(container) > MAX_HOSTS:
            raise RemoteInventoryError()
        for row in container:
            if not isinstance(row, dict):
                raise RemoteInventoryError()
            rows.append((None, row))
        return rows
    if isinstance(container, dict):
        if len(container) > MAX_HOSTS:
            raise RemoteInventoryError()
        for alias, row in container.items():
            if not isinstance(row, dict):
                raise RemoteInventoryError()
            rows.append((alias, row))
        return rows
    raise RemoteInventoryError()


def _row_alias(key_alias: Optional[str], row: Mapping[str, Any]) -> str:
    row_alias = row.get("name")
    if key_alias is None:
        alias = row_alias
    else:
        alias = key_alias
        if row_alias is not None and row_alias != key_alias:
            raise RemoteInventoryError()
    try:
        return _validate_alias(alias)
    except ValueError as error:
        raise RemoteInventoryError() from error


def _register_alias(alias: str, aliases: Dict[str, str]) -> None:
    folded = alias.casefold()
    if folded in aliases:
        raise RemoteInventoryError()
    aliases[folded] = alias


def _strict_bool(value: object) -> bool:
    if type(value) is not bool:
        raise RemoteInventoryError()
    return value


def _strict_phase(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise RemoteInventoryError()
    return value


def _hosts_from_canonical(value: Any) -> Tuple[RemoteHost, ...]:
    aliases: Dict[str, str] = {}
    eligible: List[RemoteHost] = []
    for key_alias, row in _inventory_container(value):
        alias = _row_alias(key_alias, row)
        _register_alias(alias, aliases)
        config = row.get("config")
        if config is not None and not isinstance(config, dict):
            raise RemoteInventoryError()
        config = {} if config is None else config
        local = _strict_bool(row.get("local", config.get("local")))
        if local:
            continue
        enabled = _strict_bool(config.get("enabled", row.get("enabled")))
        orphaned = _strict_bool(row.get("orphaned"))
        phase = _strict_phase(row.get("phase"))
        if enabled and not orphaned and phase in ELIGIBLE_PHASES:
            try:
                eligible.append(RemoteHost(alias=alias, phase=phase))
            except ValueError as error:
                raise RemoteInventoryError() from error
    return tuple(sorted(eligible, key=lambda host: (host.alias.casefold(), host.alias)))


def _fallback_rows(value: Any) -> List[Tuple[str, Mapping[str, Any]]]:
    rows = _inventory_container(value)
    aliases: Dict[str, str] = {}
    normalized: List[Tuple[str, Mapping[str, Any]]] = []
    for key_alias, row in rows:
        alias = _row_alias(key_alias, row)
        _register_alias(alias, aliases)
        normalized.append((alias, row))
    return normalized


def _hosts_from_fallback(
    config_value: Any,
    state_value: Any,
) -> Tuple[RemoteHost, ...]:
    config_rows = _fallback_rows(config_value)
    state_rows = _fallback_rows(state_value)
    states = {alias.casefold(): (alias, row) for alias, row in state_rows}
    eligible: List[RemoteHost] = []
    for alias, config in config_rows:
        state_entry = states.get(alias.casefold())
        if state_entry is None:
            continue
        state_alias, state = state_entry
        if state_alias != alias:
            raise RemoteInventoryError()
        enabled = _strict_bool(config.get("enabled"))
        local = _strict_bool(config.get("local"))
        if not enabled or local:
            continue
        phase = _strict_phase(state.get("phase"))
        if phase in ELIGIBLE_PHASES:
            orphaned = _strict_bool(state.get("orphaned"))
            if orphaned:
                continue
            try:
                eligible.append(RemoteHost(alias=alias, phase=phase))
            except ValueError as error:
                raise RemoteInventoryError() from error
    return tuple(sorted(eligible, key=lambda host: (host.alias.casefold(), host.alias)))


def _inventory_root(environment: Mapping[str, str], home: Optional[Path]) -> Path:
    configured = environment.get("DSHC_HOME")
    if configured:
        return Path(configured).expanduser()
    if home is not None:
        return Path(home).expanduser() / ".dsh_center"
    configured_home = environment.get("HOME")
    if configured_home:
        return Path(configured_home).expanduser() / ".dsh_center"
    return Path.home() / ".dsh_center"


def _read_bounded_inventory_file(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(MAX_INVENTORY_BYTES + 1)


def load_remote_hosts(
    *,
    runner: Optional[Callable[..., object]] = None,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    file_reader: Optional[Callable[[Path], bytes]] = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Tuple[RemoteHost, ...]:
    """Load eligible hosts from dshc, falling back to merged config and state.

    A successful ``dshc ls --json`` result is authoritative and malformed
    successful output is rejected. Manager status is not host inventory; when
    ``ls`` is unavailable, the fallback requires both config and state files.
    """

    if not 0 < float(timeout) <= 30:
        raise ValueError("inventory timeout is out of bounds")
    environment = dict(os.environ if env is None else env)
    argv = ("dshc", "ls", "--json")
    try:
        if runner is None:
            completed = _run_bounded(
                argv,
                input_limit=1,
                stdout_limit=MAX_INVENTORY_BYTES,
                stderr_limit=MAX_STDERR_BYTES,
                timeout=float(timeout),
                env=environment,
            )
        else:
            completed = runner(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=float(timeout),
                env=environment,
            )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if (
        completed is not None
        and _completed_overflow(completed) is None
        and _completed_returncode(completed) == 0
    ):
        try:
            value = parse_bounded_json(
                _completed_stdout(completed),
                max_bytes=MAX_INVENTORY_BYTES,
            )
            return _hosts_from_canonical(value)
        except (TypeError, UnicodeError, ValueError) as error:
            raise RemoteInventoryError() from error

    root = _inventory_root(environment, home)
    reader = _read_bounded_inventory_file if file_reader is None else file_reader
    try:
        config_payload = reader(root / "config.json")
        state_payload = reader(root / "state.json")
        config_value = parse_bounded_json(
            config_payload,
            max_bytes=MAX_INVENTORY_BYTES,
        )
        state_value = parse_bounded_json(
            state_payload,
            max_bytes=MAX_INVENTORY_BYTES,
        )
        return _hosts_from_fallback(config_value, state_value)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise RemoteInventoryError() from error


__all__ = ["load_remote_hosts"]
