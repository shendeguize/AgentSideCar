#!/bin/sh

set -eu

REPOSITORY=shendeguize/AgentSideCar
API_ROOT=https://api.github.com/repos/$REPOSITORY
RAW_ROOT=https://raw.githubusercontent.com/$REPOSITORY
SKILL_MARKER=.agent-sidecar-release-skill

die() {
    printf '%s\n' "agent-sidecar installer: $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: install.sh [OPTIONS]

Install the checksum-verified Agent Sidecar release zipapp.

Options:
  --version <vX.Y.Z|latest>  Release to install (default: latest)
  --prefix <path>            Installation prefix (default: $HOME/.local)
  --with-skill               Install the Cursor and Claude skill bundle
  --uninstall                Remove a recognized release installation
  --help                     Show this help

The executable is installed at <prefix>/bin/agent-sidecar. A checkout skill
install uses scripts/install-skill.sh when that can be done without replacing
the release executable. Otherwise the two skill files are copied from the
checkout or fetched from the resolved immutable release tag.
EOF
}

normalize_path() {
    python3 - "$1" <<'PY'
import os
import sys

value = sys.argv[1]
if not value or "\n" in value or "\r" in value or "\0" in value:
    raise SystemExit(1)
print(os.path.abspath(os.path.expanduser(value)))
PY
}

validate_version_request() {
    python3 - "$1" <<'PY'
import re
import sys

if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", sys.argv[1]) is None:
    raise SystemExit(1)
PY
}

validate_zipapp() {
    python3 - "$1" "${2-}" <<'PY'
import ast
import os
import re
import stat
import sys
import zipfile

path, expected = sys.argv[1:]
nofollow = getattr(os, "O_NOFOLLOW", 0)
if not nofollow:
    raise SystemExit(1)
try:
    descriptor = os.open(path, os.O_RDONLY | nofollow)
except OSError:
    raise SystemExit(1)
try:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise SystemExit(1)
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        if stream.read(23) != b"#!/usr/bin/env python3\n":
            raise SystemExit(1)
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                names = set(archive.namelist())
                required = {
                    "__main__.py",
                    "sidecar/__init__.py",
                    "sidecar/__main__.py",
                    "sidecar/cli.py",
                }
                if not required.issubset(names):
                    raise SystemExit(1)
                source = archive.read("sidecar/__init__.py").decode("utf-8")
        except (KeyError, UnicodeError, zipfile.BadZipFile):
            raise SystemExit(1)
    try:
        module = ast.parse(source)
    except SyntaxError:
        raise SystemExit(1)
    version = None
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        value = statement.value
        if isinstance(value, ast.Str):
            version = value.s
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            version = value.value
        break
    if version is None or re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
    ) is None:
        raise SystemExit(1)
    if expected and version != expected:
        raise SystemExit(1)
finally:
    os.close(descriptor)
PY
}

remove_zipapp() {
    python3 - "$1" <<'PY'
import ast
import os
import re
import stat
import sys
import zipfile

path = sys.argv[1]
nofollow = getattr(os, "O_NOFOLLOW", 0)
if not nofollow:
    raise SystemExit("secure no-follow file access is unavailable")
try:
    descriptor = os.open(path, os.O_RDONLY | nofollow)
except OSError as error:
    raise SystemExit("cannot open installation: {}".format(error))
try:
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode):
        raise SystemExit("installation is not a regular file")
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        if stream.read(23) != b"#!/usr/bin/env python3\n":
            raise SystemExit("installation has no Agent Sidecar release signature")
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream) as archive:
                names = set(archive.namelist())
                if not {
                    "__main__.py",
                    "sidecar/__init__.py",
                    "sidecar/__main__.py",
                    "sidecar/cli.py",
                }.issubset(names):
                    raise SystemExit("installation is not an Agent Sidecar zipapp")
                source = archive.read("sidecar/__init__.py").decode("utf-8")
        except (KeyError, UnicodeError, zipfile.BadZipFile):
            raise SystemExit("installation is not an Agent Sidecar zipapp")
    version = None
    try:
        module = ast.parse(source)
    except SyntaxError:
        raise SystemExit("installation has invalid version metadata")
    for statement in module.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            continue
        value = statement.value
        if isinstance(value, ast.Str):
            version = value.s
        elif isinstance(value, ast.Constant) and isinstance(value.value, str):
            version = value.value
        break
    if version is None or re.fullmatch(
        r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
    ) is None:
        raise SystemExit("installation has invalid Agent Sidecar version metadata")
    current = os.lstat(path)
    if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
        raise SystemExit("installation changed during uninstall")
    os.unlink(path)
except BaseException:
    raise
finally:
    os.close(descriptor)
PY
}

remove_copied_skills() {
    python3 - "$1" "$SKILL_MARKER" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

home = Path(sys.argv[1])
marker_name = sys.argv[2]
signature = "agent-sidecar-release-skill-v1"
for destination in (
    home / ".cursor" / "skills" / "agent-sidecar",
    home / ".claude" / "skills" / "agent-sidecar",
):
    if destination.is_symlink():
        print("left checkout or unrelated skill link unchanged: {}".format(destination))
        continue
    if not destination.exists():
        continue
    try:
        details = destination.lstat()
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError("not a directory")
        names = {item.name for item in destination.iterdir()}
        if names != {"SKILL.md", "reference.md", marker_name}:
            raise ValueError("unexpected directory contents")
        for name in names:
            item = destination / name
            item_details = item.lstat()
            if not stat.S_ISREG(item_details.st_mode):
                raise ValueError("non-regular skill file")
        marker = json.loads((destination / marker_name).read_text(encoding="utf-8"))
        if set(marker) != {"signature", "version"}:
            raise ValueError("invalid ownership marker")
        if marker["signature"] != signature or re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
            marker["version"],
        ) is None:
            raise ValueError("invalid ownership marker")
        current = destination.lstat()
        if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
            raise ValueError("directory changed")
        for name in names:
            (destination / name).unlink()
        destination.rmdir()
        print("removed: {}".format(destination))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(
            "left unrecognized skill path unchanged: {} ({})".format(
                destination, error
            ),
            file=sys.stderr,
        )
PY
}

version_request=latest
version_set=0
prefix=
prefix_set=0
with_skill=0
uninstall=0

while [ "$#" -gt 0 ]; do
    case $1 in
        --version)
            [ "$version_set" -eq 0 ] || die "--version may be specified only once"
            [ "$#" -ge 2 ] || die "--version requires a value"
            version_request=$2
            version_set=1
            shift 2
            ;;
        --prefix)
            [ "$prefix_set" -eq 0 ] || die "--prefix may be specified only once"
            [ "$#" -ge 2 ] || die "--prefix requires a value"
            prefix=$2
            prefix_set=1
            shift 2
            ;;
        --with-skill)
            [ "$with_skill" -eq 0 ] || die "--with-skill may be specified only once"
            with_skill=1
            shift
            ;;
        --uninstall)
            [ "$uninstall" -eq 0 ] || die "--uninstall may be specified only once"
            uninstall=1
            shift
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1 (use --help)"
            ;;
    esac
done

command -v python3 >/dev/null 2>&1 || die "Python 3 is required"

if [ "$prefix_set" -eq 0 ]; then
    [ -n "${HOME-}" ] || die "HOME must be set when --prefix is omitted"
    prefix=$HOME/.local
fi
prefix=$(normalize_path "$prefix") || die "invalid installation prefix"
target=$prefix/bin/agent-sidecar

script_dir=
if [ -f "$0" ]; then
    script_dir=$(python3 - "$0" <<'PY'
import os
import sys

path = os.path.realpath(sys.argv[1])
if os.path.isfile(path):
    print(os.path.dirname(path))
PY
)
fi
checkout_helper=
checkout_skill=
checkout_cli=
if [ -n "$script_dir" ] &&
    [ -f "$script_dir/scripts/install-skill.sh" ] &&
    [ -f "$script_dir/skills/agent-sidecar/SKILL.md" ] &&
    [ -f "$script_dir/skills/agent-sidecar/reference.md" ] &&
    [ -f "$script_dir/agent-sidecar" ]; then
    checkout_helper=$script_dir/scripts/install-skill.sh
    checkout_skill=$script_dir/skills/agent-sidecar
    checkout_cli=$script_dir/agent-sidecar
fi

if [ "$uninstall" -eq 1 ]; then
    [ "$version_set" -eq 0 ] ||
        die "--version cannot be combined with --uninstall"
    if [ -e "$target" ] || [ -L "$target" ]; then
        validate_zipapp "$target" ||
            die "refusing to remove unrecognized path: $target"
        remove_zipapp "$target" ||
            die "refusing to remove unrecognized path: $target"
        printf '%s\n' "removed: $target"
    else
        printf '%s\n' "already absent: $target"
    fi

    if [ "$with_skill" -eq 1 ]; then
        [ -n "${HOME-}" ] || die "HOME must be set for --with-skill"
        home=$(normalize_path "$HOME") || die "invalid HOME"
        if [ -n "$checkout_helper" ]; then
            HOME=$home sh "$checkout_helper" --uninstall
        fi
        remove_copied_skills "$home"
    else
        printf '%s\n' "skill installations were left unchanged"
    fi
    exit 0
fi

if [ "$version_request" != latest ]; then
    validate_version_request "$version_request" ||
        die "--version must be latest or vX.Y.Z without leading zeros"
fi

if [ -e "$target" ] || [ -L "$target" ]; then
    validate_zipapp "$target" ||
        die "refusing to replace unrecognized path: $target"
    target_existed=1
else
    target_existed=0
fi

command -v curl >/dev/null 2>&1 || die "curl is required"
platform=$(python3 - <<'PY'
import platform
print(platform.system())
PY
)
case $platform in
    Darwin)
        command -v shasum >/dev/null 2>&1 ||
            die "shasum is required on macOS"
        checksum_command=shasum
        ;;
    Linux)
        command -v sha256sum >/dev/null 2>&1 ||
            die "sha256sum is required on Linux"
        checksum_command=sha256sum
        ;;
    *)
        die "unsupported platform: $platform"
        ;;
esac

temporary=$(python3 - <<'PY'
import tempfile
print(tempfile.mkdtemp(prefix="agent-sidecar-install-"))
PY
) || die "cannot create temporary directory"
cleanup() {
    if [ -n "${temporary-}" ] && [ -d "$temporary" ]; then
        rm -rf "$temporary"
    fi
}
trap cleanup EXIT HUP INT TERM

download() {
    curl \
        --fail \
        --location \
        --proto '=https' \
        --retry 3 \
        --show-error \
        --silent \
        --output "$1" \
        "$2"
}

if [ "$version_request" = latest ]; then
    release_url=$API_ROOT/releases/latest
else
    release_url=$API_ROOT/releases/tags/$version_request
fi
download "$temporary/release.json" "$release_url" ||
    die "cannot download release metadata"

python3 - "$temporary/release.json" "$temporary" "$version_request" <<'PY' ||
import json
import re
import sys
from pathlib import Path

metadata_path = Path(sys.argv[1])
output = Path(sys.argv[2])
requested = sys.argv[3]
if metadata_path.stat().st_size > 2 * 1024 * 1024:
    raise SystemExit("release metadata exceeds 2 MiB")
try:
    release = json.loads(metadata_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit("invalid GitHub release metadata: {}".format(error))
if not isinstance(release, dict):
    raise SystemExit("GitHub release metadata is not an object")
tag = release.get("tag_name")
if not isinstance(tag, str) or re.fullmatch(
    r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag
) is None:
    raise SystemExit("release has an invalid stable tag")
if requested != "latest" and requested != tag:
    raise SystemExit("release tag does not match the requested version")
if release.get("draft") is not False or release.get("prerelease") is not False:
    raise SystemExit("release is draft or prerelease")
version = tag[1:]
artifact_name = "agent-sidecar-{}.pyz".format(version)
checksum_name = "SHA256SUMS"
expected_urls = {
    artifact_name: (
        "https://github.com/shendeguize/AgentSideCar/releases/download/"
        "{}/{}".format(tag, artifact_name)
    ),
    checksum_name: (
        "https://github.com/shendeguize/AgentSideCar/releases/download/"
        "{}/{}".format(tag, checksum_name)
    ),
}
assets = release.get("assets")
if not isinstance(assets, list):
    raise SystemExit("release assets are missing")
resolved = {}
for asset in assets:
    if not isinstance(asset, dict):
        raise SystemExit("release contains invalid asset metadata")
    name = asset.get("name")
    if name not in expected_urls:
        continue
    if name in resolved:
        raise SystemExit("release contains duplicate expected asset {}".format(name))
    url = asset.get("browser_download_url")
    if url != expected_urls[name]:
        raise SystemExit("release asset URL mismatch for {}".format(name))
    resolved[name] = url
if set(resolved) != set(expected_urls):
    missing = sorted(set(expected_urls).difference(resolved))
    raise SystemExit("release is missing exact asset(s): {}".format(", ".join(missing)))
(output / "version").write_text(version + "\n", encoding="utf-8")
(output / "tag").write_text(tag + "\n", encoding="utf-8")
(output / "artifact-name").write_text(artifact_name + "\n", encoding="utf-8")
(output / "artifact-url").write_text(resolved[artifact_name] + "\n", encoding="utf-8")
(output / "checksum-url").write_text(resolved[checksum_name] + "\n", encoding="utf-8")
PY
    die "release metadata did not identify the exact required assets"

IFS= read -r version < "$temporary/version"
IFS= read -r tag < "$temporary/tag"
IFS= read -r artifact_name < "$temporary/artifact-name"
IFS= read -r artifact_url < "$temporary/artifact-url"
IFS= read -r checksum_url < "$temporary/checksum-url"

download "$temporary/$artifact_name" "$artifact_url" ||
    die "cannot download release artifact"
download "$temporary/SHA256SUMS" "$checksum_url" ||
    die "cannot download release checksums"

python3 - "$temporary/SHA256SUMS" "$temporary/expected.sha256" "$artifact_name" <<'PY' ||
import re
import sys
from pathlib import Path

source, destination, expected_name = map(Path, sys.argv[1:])
try:
    document = source.read_text(encoding="utf-8")
except (OSError, UnicodeError) as error:
    raise SystemExit("invalid checksum file: {}".format(error))
matches = []
for line in document.splitlines():
    if not line.strip():
        continue
    match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+(?:\*)?(.+)", line)
    if match is None:
        raise SystemExit("malformed checksum line")
    if match.group(2) == expected_name.name:
        matches.append(match.group(1).lower())
if len(matches) != 1:
    raise SystemExit("checksum file must name the exact artifact once")
destination.write_text(
    "{}  {}\n".format(matches[0], expected_name.name),
    encoding="ascii",
)
PY
    die "checksum file does not match the exact release artifact"

if [ "$checksum_command" = shasum ]; then
    (
        cd "$temporary"
        shasum -a 256 -c expected.sha256 >/dev/null
    ) || die "release artifact checksum verification failed"
else
    (
        cd "$temporary"
        sha256sum -c expected.sha256 >/dev/null
    ) || die "release artifact checksum verification failed"
fi

validate_zipapp "$temporary/$artifact_name" "$version" ||
    die "verified asset is not the expected Agent Sidecar $version zipapp"

helper_used=0
if [ "$with_skill" -eq 1 ]; then
    [ -n "${HOME-}" ] || die "HOME must be set for --with-skill"
    home=$(normalize_path "$HOME") || die "invalid HOME"
    default_prefix=$(normalize_path "$home/.local") || die "invalid HOME"

    if [ -n "$checkout_helper" ] &&
        [ "$prefix" = "$default_prefix" ] &&
        [ "$target_existed" -eq 0 ]; then
        HOME=$home sh "$checkout_helper" ||
            die "checkout skill installer failed"
        python3 - "$target" "$checkout_cli" <<'PY' ||
import os
import stat
import sys

link, expected = sys.argv[1:]
details = os.lstat(link)
if not stat.S_ISLNK(details.st_mode):
    raise SystemExit(1)
if os.path.realpath(link) != os.path.realpath(expected):
    raise SystemExit(1)
current = os.lstat(link)
if (current.st_dev, current.st_ino) != (details.st_dev, details.st_ino):
    raise SystemExit(1)
os.unlink(link)
PY
            die "checkout installer created an unexpected CLI path"
        helper_used=1
    fi

    if [ "$helper_used" -eq 0 ]; then
        skill_stage=$temporary/skill
        mkdir -p "$skill_stage"
        if [ -n "$checkout_skill" ]; then
            python3 - "$checkout_skill" "$skill_stage" <<'PY' ||
import os
import stat
import sys
from pathlib import Path

source, destination = map(Path, sys.argv[1:])
for name in ("SKILL.md", "reference.md"):
    path = source / name
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise SystemExit(1)
    (destination / name).write_bytes(path.read_bytes())
PY
                die "cannot stage checkout skill files"
        else
            download \
                "$skill_stage/SKILL.md" \
                "$RAW_ROOT/$tag/skills/agent-sidecar/SKILL.md" ||
                die "cannot download SKILL.md from immutable tag $tag"
            download \
                "$skill_stage/reference.md" \
                "$RAW_ROOT/$tag/skills/agent-sidecar/reference.md" ||
                die "cannot download reference.md from immutable tag $tag"
        fi

        python3 - \
            "$skill_stage" \
            "$home" \
            "$version" \
            "$SKILL_MARKER" \
            "$checkout_skill" <<'PY' ||
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
home = Path(sys.argv[2])
version = sys.argv[3]
marker_name = sys.argv[4]
checkout_skill = sys.argv[5]
signature = "agent-sidecar-release-skill-v1"
if re.fullmatch(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
) is None:
    raise SystemExit("invalid skill version")
payloads = {}
for name in ("SKILL.md", "reference.md"):
    path = source / name
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode):
        raise SystemExit("staged skill file is not regular")
    payload = path.read_bytes()
    if not payload or len(payload) > 1024 * 1024:
        raise SystemExit("staged skill file has an invalid size")
    payloads[name] = payload
if not payloads["SKILL.md"].startswith(b"---\n"):
    raise SystemExit("SKILL.md is missing frontmatter")
marker_payload = (
    json.dumps(
        {"signature": signature, "version": version},
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
).encode("utf-8")

destinations = (
    home / ".cursor" / "skills" / "agent-sidecar",
    home / ".claude" / "skills" / "agent-sidecar",
)
for destination in destinations:
    parent = destination.parent
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    if destination.is_symlink():
        if checkout_skill and os.path.realpath(destination) == os.path.realpath(
            checkout_skill
        ):
            print("already installed checkout skill: {}".format(destination))
            continue
        else:
            raise SystemExit(
                "refusing to replace checkout or unrelated skill link: {}".format(
                    destination
                )
            )
    if destination.exists():
        details = destination.lstat()
        if not stat.S_ISDIR(details.st_mode):
            raise SystemExit(
                "refusing to replace non-directory skill path: {}".format(
                    destination
                )
            )
        names = {item.name for item in destination.iterdir()}
        if names != {"SKILL.md", "reference.md", marker_name}:
            raise SystemExit(
                "refusing to replace unrecognized skill directory: {}".format(
                    destination
                )
            )
        try:
            marker = json.loads(
                (destination / marker_name).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise SystemExit(
                "refusing to replace invalid skill marker: {}".format(destination)
            )
        if (
            set(marker) != {"signature", "version"}
            or marker.get("signature") != signature
            or re.fullmatch(
                r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)",
                marker.get("version", ""),
            )
            is None
        ):
            raise SystemExit(
                "refusing to replace invalid skill marker: {}".format(destination)
            )
        for name, payload in (
            ("SKILL.md", payloads["SKILL.md"]),
            ("reference.md", payloads["reference.md"]),
            (marker_name, marker_payload),
        ):
            current = destination / name
            current_details = current.lstat()
            if not stat.S_ISREG(current_details.st_mode):
                raise SystemExit(
                    "refusing to replace non-regular skill file: {}".format(current)
                )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".{}.".format(name),
                dir=str(destination),
            )
            try:
                os.fchmod(descriptor, 0o644)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, current)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
    else:
        temporary_dir = Path(
            tempfile.mkdtemp(prefix=".agent-sidecar.", dir=str(parent))
        )
        try:
            for name, payload in (
                ("SKILL.md", payloads["SKILL.md"]),
                ("reference.md", payloads["reference.md"]),
                (marker_name, marker_payload),
            ):
                path = temporary_dir / name
                path.write_bytes(payload)
                path.chmod(0o644)
            os.replace(temporary_dir, destination)
        finally:
            if temporary_dir.exists():
                for item in temporary_dir.iterdir():
                    item.unlink()
                temporary_dir.rmdir()
    print("installed skill: {}".format(destination))
PY
            die "skill installation refused an existing path or invalid bundle"
    fi
fi

python3 - "$temporary/$artifact_name" "$target" "$version" <<'PY' ||
import ast
import os
import re
import stat
import sys
import tempfile
import zipfile

source, target, expected = sys.argv[1:]

def embedded_version(path):
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise ValueError("secure no-follow file access is unavailable")
    descriptor = os.open(path, os.O_RDONLY | nofollow)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("not a regular file")
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            if stream.read(23) != b"#!/usr/bin/env python3\n":
                raise ValueError("missing release signature")
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                names = set(archive.namelist())
                if not {
                    "__main__.py",
                    "sidecar/__init__.py",
                    "sidecar/__main__.py",
                    "sidecar/cli.py",
                }.issubset(names):
                    raise ValueError("missing package signature")
                module = ast.parse(
                    archive.read("sidecar/__init__.py").decode("utf-8")
                )
        version = None
        for statement in module.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(
                isinstance(item, ast.Name) and item.id == "__version__"
                for item in statement.targets
            ):
                continue
            value = statement.value
            if isinstance(value, ast.Str):
                version = value.s
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                version = value.value
            break
        if version is None or re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version
        ) is None:
            raise ValueError("invalid package version")
        return version
    finally:
        os.close(descriptor)

if embedded_version(source) != expected:
    raise SystemExit("staged artifact version mismatch")
parent = os.path.dirname(target)
os.makedirs(parent, mode=0o755, exist_ok=True)
if not stat.S_ISDIR(os.stat(parent).st_mode):
    raise SystemExit("installation parent is not a directory")
if os.path.lexists(target):
    try:
        embedded_version(target)
    except (OSError, UnicodeError, ValueError, SyntaxError, zipfile.BadZipFile):
        raise SystemExit("refusing to replace unrecognized target")

descriptor, temporary_name = tempfile.mkstemp(
    prefix=".agent-sidecar.",
    dir=parent,
)
try:
    os.fchmod(descriptor, 0o755)
    source_descriptor = os.open(
        source,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            offset = 0
            while offset < len(chunk):
                written = os.write(descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("short artifact write")
                offset += written
    finally:
        os.close(source_descriptor)
    os.fsync(descriptor)
    os.close(descriptor)
    descriptor = -1
    if os.path.lexists(target):
        embedded_version(target)
    os.replace(temporary_name, target)
    temporary_name = ""
    directory_descriptor = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
finally:
    if descriptor >= 0:
        os.close(descriptor)
    if temporary_name:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
PY
    die "atomic installation failed"

printf '%s\n' "installed Agent Sidecar $version: $target"
printf '%s\n' "checksum verified from the matching $tag release"
