"""Resolve a cwd-independent command for the current sidecar installation."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


class RuntimeCommandError(RuntimeError):
    """Raised when no unambiguous executable entry point can be resolved."""


def _effective_uid() -> int:
    provider = getattr(os, "geteuid", None)
    return provider() if callable(provider) else 0


def _trusted_owner(details: os.stat_result) -> bool:
    return details.st_uid in (0, _effective_uid())


def _trusted_mode(details: os.stat_result) -> bool:
    return stat.S_IMODE(details.st_mode) & 0o022 == 0


def _lexical_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return Path.cwd() / expanded


def _snapshot_lexical_path(path: Path) -> Optional[List[Tuple[Path, os.stat_result]]]:
    snapshots = []
    current = path
    while True:
        try:
            details = current.lstat()
        except OSError:
            return None
        snapshots.append((current, details))
        if current.parent == current:
            break
        current = current.parent
    return snapshots


def _trusted_ancestor(details: os.stat_result) -> bool:
    if not _trusted_owner(details):
        return False
    if stat.S_ISLNK(details.st_mode):
        return True
    if not stat.S_ISDIR(details.st_mode):
        return False
    if _trusted_mode(details):
        return True
    return (
        details.st_uid == 0
        and stat.S_ISDIR(details.st_mode)
        and bool(stat.S_IMODE(details.st_mode) & stat.S_ISVTX)
    )


def _trusted_target_ancestor(details: os.stat_result) -> bool:
    if (
        not stat.S_ISDIR(details.st_mode)
        or not _trusted_owner(details)
    ):
        return False
    if _trusted_mode(details):
        return True
    return (
        details.st_uid == 0
        and bool(stat.S_IMODE(details.st_mode) & stat.S_ISVTX)
    )


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_uid,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_uid,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _retain_resolved_path(
    snapshots: Sequence[Tuple[Path, os.stat_result]],
) -> Optional[List[Tuple[Path, int, os.stat_result]]]:
    retained = []
    try:
        for index, (path, before) in enumerate(snapshots):
            flags = (
                os.O_RDONLY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0)
            )
            if index == 0:
                flags |= os.O_NONBLOCK
            else:
                flags |= getattr(os, "O_DIRECTORY", 0)
            descriptor = -1
            try:
                descriptor = os.open(str(path), flags)
                after = os.fstat(descriptor)
            except OSError:
                if descriptor >= 0:
                    os.close(descriptor)
                return None
            if not _same_snapshot(before, after):
                os.close(descriptor)
                return None
            retained.append((path, descriptor, before))
    finally:
        if len(retained) != len(snapshots):
            for _, descriptor, _ in retained:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    return retained


def _trusted_executable(
    path: Path,
    *,
    preserve_lexical: bool,
    preserve_symlink_only: bool = False,
) -> Optional[Path]:
    lexical = _lexical_absolute(path)
    lexical_snapshots = _snapshot_lexical_path(lexical)
    if lexical_snapshots is None:
        return None
    entry_details = lexical_snapshots[0][1]
    if (
        not (stat.S_ISREG(entry_details.st_mode) or stat.S_ISLNK(entry_details.st_mode))
        or not _trusted_owner(entry_details)
        or (
            stat.S_ISREG(entry_details.st_mode)
            and not _trusted_mode(entry_details)
        )
        or any(
            not _trusted_ancestor(details)
            for _, details in lexical_snapshots[1:]
        )
    ):
        return None
    try:
        resolved = lexical.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    target_snapshots = _snapshot_lexical_path(resolved)
    if target_snapshots is None:
        return None
    details = target_snapshots[0][1]
    if (
        not resolved.is_absolute()
        or stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or not _trusted_owner(details)
        or not _trusted_mode(details)
        or not bool(stat.S_IMODE(details.st_mode) & 0o111)
        or any(
            not _trusted_target_ancestor(target_details)
            for _, target_details in target_snapshots[1:]
        )
    ):
        return None
    retained = _retain_resolved_path(target_snapshots)
    if retained is None:
        return None
    try:
        for lexical_path, before in lexical_snapshots:
            try:
                after = lexical_path.lstat()
            except OSError:
                return None
            if not _same_snapshot(before, after):
                return None
        for target_path, descriptor, before in retained:
            try:
                path_after = target_path.lstat()
                descriptor_after = os.fstat(descriptor)
            except OSError:
                return None
            if (
                not _same_snapshot(before, path_after)
                or not _same_snapshot(before, descriptor_after)
            ):
                return None
    finally:
        for _, descriptor, _ in retained:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if (
        preserve_lexical
        and (
            not preserve_symlink_only
            or stat.S_ISLNK(entry_details.st_mode)
        )
    ):
        return lexical
    return resolved


def _entry_candidate(
    value: object,
    *,
    allow_relative: bool = False,
    preserve_lexical: bool = False,
    preserve_symlink_only: bool = False,
) -> Optional[Path]:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    candidate = Path(value).expanduser()
    if not allow_relative and not candidate.is_absolute():
        return None
    return _trusted_executable(
        candidate,
        preserve_lexical=preserve_lexical,
        preserve_symlink_only=preserve_symlink_only,
    )


def _checkout_shim(module_file: object) -> Optional[Path]:
    if not isinstance(module_file, str) or not module_file or "\x00" in module_file:
        return None
    try:
        module_path = Path(module_file).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    root = module_path.parent.parent
    if module_path.parent.name != "sidecar":
        return None
    try:
        package = root / "sidecar" / "__init__.py"
        project = root / "pyproject.toml"
        if not package.is_file() or not project.is_file():
            return None
    except OSError:
        return None
    return _trusted_executable(root / "agent-sidecar", preserve_lexical=False)


def _validate_prefix(prefix: Sequence[str]) -> Tuple[str, ...]:
    arguments = tuple(prefix)
    if (
        not arguments
        or any(
            not isinstance(argument, str) or not argument or "\x00" in argument
            for argument in arguments
        )
        or not Path(arguments[0]).is_absolute()
        or _trusted_executable(
            Path(arguments[0]),
            preserve_lexical=True,
        )
        is None
    ):
        raise RuntimeCommandError("runtime command is not an absolute executable")
    return arguments


def resolve_runtime_prefix(
    *,
    argv0: Optional[str] = None,
    executable: Optional[str] = None,
    module_file: Optional[str] = None,
) -> Tuple[str, ...]:
    """Return the canonical entry prefix for the running installation.

    Executable zipapps and console scripts are retained as their resolved
    executable. A source checkout uses its root shim. An installed package
    falls back to the current interpreter and ``-m sidecar``.
    """

    invoked_as = sys.argv[0] if argv0 is None else argv0
    current_module = __file__ if module_file is None else module_file
    current_python = sys.executable if executable is None else executable

    entry = _entry_candidate(
        invoked_as,
        allow_relative=True,
        preserve_lexical=True,
        preserve_symlink_only=True,
    )
    if entry is not None:
        original_name = Path(str(invoked_as)).name
        if entry.suffix.casefold() == ".pyz" or original_name in (
            "agent-sidecar",
            "agent-sidecar.exe",
        ):
            return _validate_prefix((str(entry),))

    shim = _checkout_shim(current_module)
    if shim is not None:
        return _validate_prefix((str(shim),))

    interpreter = _entry_candidate(current_python, preserve_lexical=True)
    if interpreter is None:
        raise RuntimeCommandError(
            "cannot resolve the current Python interpreter as an executable"
        )
    return _validate_prefix((str(interpreter), "-m", "sidecar"))


def validate_runtime_prefix(prefix: Sequence[str]) -> Tuple[str, ...]:
    """Validate an already-resolved prefix without changing lexical semantics."""

    return _validate_prefix(prefix)


__all__ = [
    "RuntimeCommandError",
    "resolve_runtime_prefix",
    "validate_runtime_prefix",
]
