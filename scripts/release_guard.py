#!/usr/bin/env python3
"""Validate release tags against source, changelog, and protected branches."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, TextIO, Tuple

ROOT = Path(__file__).resolve().parents[1]
TAG_PATTERN = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?P<rc>-rc\.(?:0|[1-9]\d*))?$"
)


class ReleaseGuardError(ValueError):
    """A release candidate violates the repository release policy."""


@dataclass(frozen=True)
class ReleaseMetadata:
    """Validated release facts consumed by GitHub Actions."""

    tag: str
    version: str
    prerelease: bool
    tag_commit: str
    main_ref: str
    release_ref: Optional[str]

    def outputs(self) -> Dict[str, str]:
        return {
            "tag": self.tag,
            "version": self.version,
            "prerelease": str(self.prerelease).lower(),
            "tag_commit": self.tag_commit,
            "main_ref": self.main_ref,
            "release_ref": self.release_ref or "",
        }


class GitRepository:
    """Small subprocess-backed Git interface that is easy to replace in tests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _run(self, arguments: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(arguments),
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )

    def try_resolve_commit(self, ref: str) -> Optional[str]:
        result = self._run(("rev-parse", "--verify", "{}^{{commit}}".format(ref)))
        if result.returncode:
            return None
        commit = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            raise ReleaseGuardError("git returned an invalid commit for {!r}".format(ref))
        return commit.lower()

    def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        result = self._run(("merge-base", "--is-ancestor", ancestor, descendant))
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        detail = result.stderr.strip() or "git merge-base failed"
        raise ReleaseGuardError(detail)


def parse_tag(tag: str) -> Tuple[str, bool]:
    """Return the source version and whether *tag* is an RC release."""
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ReleaseGuardError(
            "tag must match vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.NUMBER"
        )
    return tag[1:], match.group("rc") is not None


def read_project_version(init_path: Path) -> str:
    """Read a literal ``sidecar.__version__`` without importing application code."""
    try:
        source = init_path.read_text(encoding="utf-8")
        module = ast.parse(source, filename=str(init_path))
    except (OSError, SyntaxError) as error:
        raise ReleaseGuardError(
            "cannot read project version from {}".format(init_path)
        ) from error

    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        if isinstance(statement.value, ast.Str):
            return statement.value.s
        if isinstance(statement.value, ast.Constant) and isinstance(
            statement.value.value, str
        ):
            return statement.value.value
        break
    raise ReleaseGuardError(
        "{} must define __version__ as a string literal".format(init_path)
    )


def extract_changelog_section(changelog: str, version: str) -> str:
    """Extract exactly one Keep a Changelog section, including its heading."""
    heading = re.compile(
        r"^## \[{}\](?: - [^\n]+)?[ \t]*$".format(re.escape(version)),
        re.MULTILINE,
    )
    matches = list(heading.finditer(changelog))
    if len(matches) != 1:
        raise ReleaseGuardError(
            "CHANGELOG.md must contain exactly one section for [{}]".format(version)
        )
    start = matches[0].start()
    following = re.search(r"^## ", changelog[matches[0].end() :], re.MULTILINE)
    end = (
        len(changelog)
        if following is None
        else matches[0].end() + following.start()
    )
    return changelog[start:end].rstrip() + "\n"


def branch_candidates(branch: str, remote: str) -> Tuple[str, ...]:
    """Return local and remote spellings for a branch without ambiguity."""
    if branch.startswith("refs/"):
        return (branch,)
    if "/" in branch:
        return (
            branch,
            "refs/remotes/{}".format(branch),
            "refs/heads/{}".format(branch),
        )
    return (
        "refs/heads/{}".format(branch),
        branch,
        "refs/remotes/{}/{}".format(remote, branch),
        "{}/{}".format(remote, branch),
    )


def resolve_branch(repository: GitRepository, branch: str, remote: str) -> Tuple[str, str]:
    """Resolve the first available local or remote spelling of *branch*."""
    for candidate in branch_candidates(branch, remote):
        commit = repository.try_resolve_commit(candidate)
        if commit is not None:
            return candidate, commit
    raise ReleaseGuardError("cannot resolve branch {!r}".format(branch))


def validate_release(
    tag: str,
    *,
    root: Path = ROOT,
    main_branch: str = "main",
    release_branch: str = "release",
    remote: str = "origin",
    repository: Optional[GitRepository] = None,
) -> Tuple[ReleaseMetadata, str]:
    """Validate *tag* and return metadata plus its changelog section."""
    version, prerelease = parse_tag(tag)
    source_version = read_project_version(root / "sidecar" / "__init__.py")
    if version != source_version:
        raise ReleaseGuardError(
            "tag version {} does not match sidecar.__version__ {}".format(
                version, source_version
            )
        )

    try:
        changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseGuardError("cannot read CHANGELOG.md") from error
    notes = extract_changelog_section(changelog, version)

    git = repository if repository is not None else GitRepository(root)
    tag_ref = "refs/tags/{}".format(tag)
    tag_commit = git.try_resolve_commit(tag_ref)
    if tag_commit is None:
        raise ReleaseGuardError("cannot resolve tag {!r}".format(tag))

    head_commit = git.try_resolve_commit("HEAD")
    if head_commit is None:
        raise ReleaseGuardError("cannot resolve checked-out HEAD")
    if head_commit != tag_commit:
        raise ReleaseGuardError(
            "checked-out HEAD ({}) does not match peeled tag {} ({})".format(
                head_commit, tag_ref, tag_commit
            )
        )

    main_ref, main_commit = resolve_branch(git, main_branch, remote)
    if not git.is_ancestor(tag_commit, main_commit):
        raise ReleaseGuardError(
            "{} ({}) is not an ancestor of {} ({})".format(
                tag, tag_commit, main_ref, main_commit
            )
        )

    resolved_release_ref = None
    if not prerelease:
        resolved_release_ref, release_commit = resolve_branch(
            git, release_branch, remote
        )
        if not git.is_ancestor(tag_commit, release_commit):
            raise ReleaseGuardError(
                "{} ({}) is not an ancestor of {} ({})".format(
                    tag, tag_commit, resolved_release_ref, release_commit
                )
            )

    return (
        ReleaseMetadata(
            tag=tag,
            version=version,
            prerelease=prerelease,
            tag_commit=tag_commit,
            main_ref=main_ref,
            release_ref=resolved_release_ref,
        ),
        notes,
    )


def write_github_outputs(path: Path, metadata: ReleaseMetadata) -> None:
    """Append release metadata using the GitHub Actions output file format."""
    with path.open("a", encoding="utf-8") as output:
        for key, value in metadata.outputs().items():
            output.write("{}={}\n".format(key, value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an immutable release tag and emit release metadata."
    )
    parser.add_argument("tag", help="release tag, for example v0.4.0 or v0.5.0-rc.1")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--main-branch", default="main")
    parser.add_argument("--release-branch", default="release")
    parser.add_argument("--remote", default="origin")
    parser.add_argument(
        "--notes-file",
        type=Path,
        help="write this version's CHANGELOG.md section to this file",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        help="append key=value metadata to a GitHub Actions output file",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    output = sys.stdout if stdout is None else stdout
    error_output = sys.stderr if stderr is None else stderr
    args = build_parser().parse_args(argv)
    try:
        metadata, notes = validate_release(
            args.tag,
            root=args.root.resolve(),
            main_branch=args.main_branch,
            release_branch=args.release_branch,
            remote=args.remote,
        )
        if args.notes_file is not None:
            args.notes_file.parent.mkdir(parents=True, exist_ok=True)
            args.notes_file.write_text(notes, encoding="utf-8")
        if args.github_output is not None:
            write_github_outputs(args.github_output, metadata)
    except (OSError, ReleaseGuardError) as error:
        print("release guard: {}".format(error), file=error_output)
        return 1

    print(json.dumps(metadata.outputs(), sort_keys=True), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
