"""Deterministic, durable release zipapp creation."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, List, Optional, Set, Tuple

from sidecar import remote_transport
from sidecar.remote_types import MAX_ARTIFACT_BYTES


DEFAULT_RELEASE_PATH = Path("dist/agent-sidecar.pyz")
RELEASE_SHEBANG = b"#!/usr/bin/env python3\n"
MAX_RELEASE_BYTES = MAX_ARTIFACT_BYTES
_MAX_SYMLINK_DEPTH = 16
_ROOT_UID = 0


class ReleaseError(RuntimeError):
    """Raised when a release artifact cannot be created safely."""


@dataclass(frozen=True)
class ReleaseArtifact:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class _DirectoryLink:
    parent_fd: int
    name: str
    child_fd: int
    identity: Tuple[int, int]


@dataclass(frozen=True)
class _SymlinkLink:
    parent_fd: int
    name: str
    identity: Tuple[int, int]
    target: str


def _release_bytes() -> bytes:
    try:
        archive = remote_transport.build_zipapp_bytes()
    except (OSError, ValueError) as error:
        raise ReleaseError("cannot build the zipapp artifact") from error
    if not isinstance(archive, bytes) or not archive:
        raise ReleaseError("zipapp builder returned an invalid artifact")
    artifact = RELEASE_SHEBANG + archive
    if len(artifact) > MAX_RELEASE_BYTES:
        raise ReleaseError("release artifact exceeds the size limit")
    return artifact


def _directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise ReleaseError("secure output path traversal is unavailable")
    return (
        os.O_RDONLY
        | nofollow
        | directory
        | getattr(os, "O_CLOEXEC", 0)
    )


def _validate_directory(details: os.stat_result) -> None:
    if not stat.S_ISDIR(details.st_mode):
        raise ReleaseError("output path ancestor is not a directory")
    mode = stat.S_IMODE(details.st_mode)
    writable_by_others = bool(mode & (stat.S_IWGRP | stat.S_IWOTH))
    sticky_root = bool(
        mode & stat.S_ISVTX
        and details.st_uid == _ROOT_UID
    )
    if writable_by_others and not sticky_root:
        raise ReleaseError("output path ancestor has unsafe permissions")


def _secure_creation_anchor(details: os.stat_result) -> bool:
    return bool(
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == os.geteuid()
        and not stat.S_IMODE(details.st_mode)
        & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _sticky_root_anchor(details: os.stat_result) -> bool:
    mode = stat.S_IMODE(details.st_mode)
    return bool(
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == _ROOT_UID
        and mode & stat.S_ISVTX
        and mode & stat.S_IWOTH
    )


def _root_owned_nonwritable(details: os.stat_result) -> bool:
    return bool(
        stat.S_ISDIR(details.st_mode)
        and details.st_uid == _ROOT_UID
        and not stat.S_IMODE(details.st_mode)
        & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _open_directory_at(parent_fd: int, name: str) -> Tuple[int, os.stat_result]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise ReleaseError("cannot inspect an output path ancestor") from error
    if stat.S_ISLNK(before.st_mode):
        raise ReleaseError("output path ancestors must not be symlinks")
    _validate_directory(before)
    try:
        descriptor = os.open(
            name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
    except OSError as error:
        raise ReleaseError("cannot open an output path ancestor") from error
    try:
        after = os.fstat(descriptor)
        _validate_directory(after)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ReleaseError("output path ancestor changed during traversal")
        return descriptor, after
    except BaseException:
        os.close(descriptor)
        raise


def _read_trusted_symlink(
    parent_fd: int,
    name: str,
    parent_details: os.stat_result,
    before: os.stat_result,
) -> str:
    if (
        before.st_uid != _ROOT_UID
        or not _root_owned_nonwritable(parent_details)
    ):
        raise ReleaseError("output path ancestor symlink is not trusted")
    try:
        target = os.readlink(name, dir_fd=parent_fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ReleaseError("cannot resolve output path ancestor") from error
    if (
        not isinstance(target, str)
        or not target
        or not stat.S_ISLNK(after.st_mode)
        or after.st_uid != _ROOT_UID
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
    ):
        raise ReleaseError("output path ancestor symlink changed")
    return target


def _open_root_directory() -> Tuple[int, os.stat_result]:
    try:
        descriptor = os.open(os.path.sep, _directory_flags())
    except OSError as error:
        raise ReleaseError("cannot open the filesystem root") from error
    try:
        details = os.fstat(descriptor)
        _validate_directory(details)
        return descriptor, details
    except BaseException:
        os.close(descriptor)
        raise


def _path_components(target: Path) -> Tuple[str, Tuple[str, ...]]:
    parts = target.parts
    if target.is_absolute():
        anchor = target.anchor
        components = parts[1:-1]
    else:
        anchor = "."
        components = parts[:-1]
    if (
        not anchor
        or any(part in ("", ".", "..") for part in components)
        or target.name in ("", ".", "..")
    ):
        raise ReleaseError("output path must not traverse parent directories")
    return anchor, tuple(components)


def _open_output_parent(
    target: Path,
) -> Tuple[
    int,
    List[int],
    List[_DirectoryLink],
    List[_SymlinkLink],
]:
    anchor, components = _path_components(target)
    descriptors: List[int] = []
    directory_links: List[_DirectoryLink] = []
    symlink_links: List[_SymlinkLink] = []
    visited_symlinks: Set[Tuple[int, int]] = set()
    pending: Deque[str] = deque(components)
    try:
        try:
            current_fd = os.open(anchor, _directory_flags())
        except OSError as error:
            raise ReleaseError("cannot open the output path root") from error
        descriptors.append(current_fd)
        current_details = os.fstat(current_fd)
        _validate_directory(current_details)
        canonical_stack = [(current_fd, current_details)]

        while pending:
            component = pending.popleft()
            if component in ("", "."):
                continue
            if component == "..":
                if len(canonical_stack) <= 1:
                    raise ReleaseError(
                        "output symlink target escapes its trusted root"
                    )
                canonical_stack.pop()
                current_fd, current_details = canonical_stack[-1]
                continue
            try:
                before = os.stat(
                    component,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if _secure_creation_anchor(current_details):
                    mode = 0o755
                elif _sticky_root_anchor(current_details):
                    mode = 0o700
                else:
                    raise ReleaseError(
                        "output parent is missing below an unsafe anchor"
                    )
                try:
                    os.mkdir(component, mode, dir_fd=current_fd)
                    os.fsync(current_fd)
                except OSError as error:
                    raise ReleaseError(
                        "cannot create the output directory"
                    ) from error
                child_fd, child_details = _open_directory_at(
                    current_fd,
                    component,
                )
                if not _secure_creation_anchor(child_details):
                    os.close(child_fd)
                    raise ReleaseError(
                        "created output directory is not securely owned"
                    )
            except OSError as error:
                raise ReleaseError(
                    "cannot inspect an output path ancestor"
                ) from error
            else:
                if stat.S_ISLNK(before.st_mode):
                    identity = (before.st_dev, before.st_ino)
                    if (
                        identity in visited_symlinks
                        or len(visited_symlinks) >= _MAX_SYMLINK_DEPTH
                    ):
                        raise ReleaseError(
                            "output path ancestor symlink loop"
                        )
                    link_target = _read_trusted_symlink(
                        current_fd,
                        component,
                        current_details,
                        before,
                    )
                    visited_symlinks.add(identity)
                    symlink_links.append(
                        _SymlinkLink(
                            parent_fd=current_fd,
                            name=component,
                            identity=identity,
                            target=link_target,
                        )
                    )
                    target_path = Path(link_target)
                    target_parts = target_path.parts
                    if target_path.is_absolute():
                        root_fd, root_details = _open_root_directory()
                        descriptors.append(root_fd)
                        canonical_stack = [(root_fd, root_details)]
                        current_fd, current_details = canonical_stack[-1]
                        target_parts = target_parts[1:]
                    for target_component in reversed(target_parts):
                        pending.appendleft(target_component)
                    continue
                child_fd, child_details = _open_directory_at(
                    current_fd,
                    component,
                )
            descriptors.append(child_fd)
            directory_links.append(
                _DirectoryLink(
                    parent_fd=current_fd,
                    name=component,
                    child_fd=child_fd,
                    identity=(child_details.st_dev, child_details.st_ino),
                )
            )
            current_fd = child_fd
            current_details = child_details
            canonical_stack.append((child_fd, child_details))

        if not _secure_creation_anchor(current_details):
            raise ReleaseError("output parent is not securely owned")
        return current_fd, descriptors, directory_links, symlink_links
    except BaseException:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _validate_path_links(
    directory_links: List[_DirectoryLink],
    symlink_links: List[_SymlinkLink],
) -> None:
    for link in directory_links:
        try:
            details = os.stat(
                link.name,
                dir_fd=link.parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ReleaseError(
                "output path ancestor changed during packaging"
            ) from error
        if stat.S_ISLNK(details.st_mode):
            raise ReleaseError("output path ancestor changed during packaging")
        _validate_directory(details)
        opened = os.fstat(link.child_fd)
        _validate_directory(opened)
        identity = (details.st_dev, details.st_ino)
        if identity != link.identity or identity != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise ReleaseError("output path ancestor changed during packaging")
    for link in symlink_links:
        parent_details = os.fstat(link.parent_fd)
        try:
            details = os.stat(
                link.name,
                dir_fd=link.parent_fd,
                follow_symlinks=False,
            )
            target = os.readlink(link.name, dir_fd=link.parent_fd)
        except OSError as error:
            raise ReleaseError(
                "output path ancestor symlink changed during packaging"
            ) from error
        if (
            not _root_owned_nonwritable(parent_details)
            or not stat.S_ISLNK(details.st_mode)
            or details.st_uid != _ROOT_UID
            or (details.st_dev, details.st_ino) != link.identity
            or target != link.target
        ):
            raise ReleaseError(
                "output path ancestor symlink changed during packaging"
            )


def _target_identity(parent_fd: int, name: str) -> Optional[Tuple[int, int]]:
    try:
        details = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReleaseError("cannot inspect the output target") from error
    if not stat.S_ISREG(details.st_mode):
        raise ReleaseError("refusing to replace a non-regular output target")
    return details.st_dev, details.st_ino


def _create_temporary(parent_fd: int, target_name: str) -> Tuple[int, str]:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    prefix = target_name[:64]
    for _attempt in range(16):
        name = ".{}.{}.tmp".format(prefix, secrets.token_hex(8))
        try:
            descriptor = os.open(
                name,
                flags,
                0o755,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        return descriptor, name
    raise ReleaseError("cannot allocate a temporary release artifact")


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("short release artifact write")
        offset += written


def build_release_zipapp(
    output: Path = DEFAULT_RELEASE_PATH,
) -> ReleaseArtifact:
    """Atomically write an executable deterministic zipapp.

    Missing output directories are created only beneath an existing directory
    owned by the effective user with no group or other write permission.
    """

    target = Path(output).expanduser()
    if not target.name:
        raise ReleaseError("output target must name a file")
    artifact = _release_bytes()
    descriptors: List[int] = []
    directory_links: List[_DirectoryLink] = []
    symlink_links: List[_SymlinkLink] = []
    parent_fd = -1
    descriptor = -1
    temporary: Optional[str] = None
    try:
        (
            parent_fd,
            descriptors,
            directory_links,
            symlink_links,
        ) = _open_output_parent(target)
        original_target = _target_identity(parent_fd, target.name)

        descriptor, temporary = _create_temporary(parent_fd, target.name)
        os.fchmod(descriptor, 0o755)
        _write_all(descriptor, artifact)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1

        _validate_path_links(directory_links, symlink_links)
        if not _secure_creation_anchor(os.fstat(parent_fd)):
            raise ReleaseError("output parent changed during packaging")
        if _target_identity(parent_fd, target.name) != original_target:
            raise ReleaseError("output target changed during packaging")
        os.replace(
            temporary,
            target.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        temporary = None
        os.fsync(parent_fd)
    except ReleaseError:
        raise
    except OSError as error:
        raise ReleaseError("cannot write the release artifact") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for directory_fd in reversed(descriptors):
            try:
                os.close(directory_fd)
            except OSError:
                pass

    return ReleaseArtifact(
        path=target,
        sha256=hashlib.sha256(artifact).hexdigest(),
        size=len(artifact),
    )


__all__ = [
    "DEFAULT_RELEASE_PATH",
    "MAX_RELEASE_BYTES",
    "RELEASE_SHEBANG",
    "ReleaseArtifact",
    "ReleaseError",
    "build_release_zipapp",
]
