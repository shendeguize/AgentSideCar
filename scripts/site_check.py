#!/usr/bin/env python3
"""Build and verify the deterministic Agent Sidecar static site."""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SITE_SOURCE = ROOT / "site"
SITE_OUTPUT = ROOT / "_site"
BASE_PATH = "/AgentSideCar/"
SITE_ORIGIN = "https://shendeguize.github.io"
SITE_URL = SITE_ORIGIN + BASE_PATH
DEMO_URL = SITE_URL + "demo/"
BUILD_TIMEOUT_SECONDS = 30
HELP_TIMEOUT_SECONDS = 10
PNG_DIMENSIONS = (1440, 960)

REQUIRED_FILES = (
    ".nojekyll",
    "demo/index.html",
    "demo/mock-api.js",
    "favicon.svg",
    "index.html",
    "landing.css",
    "llms.txt",
    "robots.txt",
    "sitemap.xml",
)

CLI_COMMAND_INVENTORY = (
    ("audit", "reset"),
    ("daemon", "start"),
    ("list",),
    ("package", "build"),
    ("send",),
    ("service", "install"),
    ("status",),
    ("tui",),
    ("watch",),
)

EXPECTED_SITE_COMMANDS = {
    ("daemon", "start"),
    ("status",),
    ("tui",),
}

NETWORK_APIS = (
    "EventSource",
    "WebSocket",
    "XMLHttpRequest",
    "navigator.sendBeacon",
)


@dataclass(frozen=True)
class Reference:
    tag: str
    attribute: str
    value: str
    runtime: bool


@dataclass(frozen=True)
class CheckSummary:
    checks: int
    errors: Tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


class Checks:
    """Collect deterministic checks and actionable diagnostics."""

    def __init__(self) -> None:
        self.count = 0
        self.errors: List[str] = []

    def require(self, condition: bool, message: str) -> bool:
        self.count += 1
        if not condition:
            self.errors.append(message)
        return condition

    def extend(self, summary: CheckSummary) -> None:
        self.count += summary.checks
        self.errors.extend(summary.errors)

    def summary(self) -> CheckSummary:
        return CheckSummary(self.count, tuple(sorted(set(self.errors))))


class SiteHTMLParser(HTMLParser):
    """Collect references and metadata without executing page content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.metas: List[Mapping[str, str]] = []
        self.references: List[Reference] = []
        self.scripts: List[Mapping[str, str]] = []
        self.styles: List[Mapping[str, str]] = []
        self.text: List[str] = []
        self.base_tags = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        self._handle_tag(tag, attrs)

    def handle_startendtag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        self._handle_tag(tag, attrs)

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def _handle_tag(
        self,
        tag: str,
        attrs: List[Tuple[str, Optional[str]]],
    ) -> None:
        name = tag.casefold()
        attributes = {
            key.casefold(): "" if value is None else value
            for key, value in attrs
        }
        identifier = attributes.get("id")
        if identifier:
            self.ids.add(identifier)
        if name == "base":
            self.base_tags += 1
        if name == "meta":
            self.metas.append(attributes)
        if name == "script":
            self.scripts.append(attributes)
        if name == "style":
            self.styles.append(attributes)

        if name == "a" and "href" in attributes:
            self.references.append(
                Reference(name, "href", attributes["href"], False)
            )
        elif name == "link" and "href" in attributes:
            relations = set(attributes.get("rel", "").casefold().split())
            runtime = bool(
                relations.intersection(
                    {
                        "dns-prefetch",
                        "icon",
                        "modulepreload",
                        "preconnect",
                        "prefetch",
                        "preload",
                        "stylesheet",
                    }
                )
            )
            if runtime:
                self.references.append(
                    Reference(name, "href", attributes["href"], True)
                )
        elif name == "script" and "src" in attributes:
            self.references.append(
                Reference(name, "src", attributes["src"], True)
            )
        elif name in ("audio", "embed", "iframe", "img", "source", "video"):
            for attribute in ("src", "poster"):
                if attribute in attributes:
                    self.references.append(
                        Reference(name, attribute, attributes[attribute], True)
                    )
        elif name == "object" and "data" in attributes:
            self.references.append(
                Reference(name, "data", attributes["data"], True)
            )


HelpRunner = Callable[[Tuple[str, ...]], Tuple[int, str, str]]


def tree_digest(root: Path) -> str:
    """Return a stable digest over relative paths and file bytes."""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run_build() -> subprocess.CompletedProcess:
    return subprocess.run(
        (sys.executable, str(ROOT / "scripts" / "build_site.py")),
        cwd=str(ROOT),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=BUILD_TIMEOUT_SECONDS,
    )


def check_deterministic_build() -> CheckSummary:
    """Invoke the canonical builder twice and compare complete output trees."""
    checks = Checks()
    try:
        first = _run_build()
    except (OSError, subprocess.TimeoutExpired) as error:
        checks.require(False, "site build invocation failed: {}".format(error))
        return checks.summary()
    if not checks.require(
        first.returncode == 0,
        "first site build failed: {}".format(first.stderr.strip() or first.stdout.strip()),
    ):
        return checks.summary()
    first_digest = tree_digest(SITE_OUTPUT)

    try:
        second = _run_build()
    except (OSError, subprocess.TimeoutExpired) as error:
        checks.require(False, "second site build invocation failed: {}".format(error))
        return checks.summary()
    if not checks.require(
        second.returncode == 0,
        "second site build failed: {}".format(
            second.stderr.strip() or second.stdout.strip()
        ),
    ):
        return checks.summary()
    checks.require(
        first_digest == tree_digest(SITE_OUTPUT),
        "site builds are not byte-for-byte deterministic",
    )
    return checks.summary()


def _parse_html(path: Path, checks: Checks) -> Optional[SiteHTMLParser]:
    try:
        document = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        checks.require(False, "{} is not readable UTF-8: {}".format(path, error))
        return None
    parser = SiteHTMLParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as error:
        checks.require(False, "{} is invalid HTML: {}".format(path, error))
        return None
    checks.require(True, "{} is readable HTML".format(path))
    return parser


def _metadata_values(
    parser: SiteHTMLParser,
    attribute: str,
    key: str,
) -> Tuple[str, ...]:
    return tuple(
        meta.get("content", "")
        for meta in parser.metas
        if meta.get(attribute, "").casefold() == key.casefold()
    )


def _check_required_outputs(site_root: Path, checks: Checks) -> None:
    resolved_root = site_root.resolve()
    checks.require(site_root.is_dir(), "site output directory is missing")
    for relative in REQUIRED_FILES:
        path = site_root / relative
        checks.require(
            path.is_file() and not path.is_symlink(),
            "required site output is missing or unsafe: {}".format(relative),
        )
    for path in sorted(site_root.rglob("*")):
        if path.is_symlink():
            checks.require(
                False,
                "site output must not contain symlinks: {}".format(
                    path.relative_to(site_root)
                ),
            )
        elif path.exists():
            try:
                path.resolve().relative_to(resolved_root)
            except ValueError:
                checks.require(
                    False,
                    "site output escapes its root: {}".format(
                        path.relative_to(site_root)
                    ),
                )


def _check_metadata(
    site_root: Path,
    parsers: Mapping[Path, SiteHTMLParser],
    checks: Checks,
) -> None:
    landing_path = site_root / "index.html"
    demo_path = site_root / "demo" / "index.html"
    landing = parsers.get(landing_path)
    demo = parsers.get(demo_path)
    if landing is None or demo is None:
        return

    charsets = tuple(
        meta.get("charset", "").casefold()
        for meta in landing.metas
        if "charset" in meta
    )
    checks.require(charsets == ("utf-8",), "landing page must declare UTF-8 once")
    checks.require(
        _metadata_values(landing, "name", "viewport")
        == ("width=device-width, initial-scale=1",),
        "landing page viewport metadata is missing or changed",
    )
    descriptions = _metadata_values(landing, "name", "description")
    checks.require(
        len(descriptions) == 1 and bool(descriptions[0].strip()),
        "landing page needs one nonempty description",
    )
    checks.require(
        _metadata_values(landing, "property", "og:url") == (SITE_URL,),
        "landing page og:url must match the Pages URL",
    )
    checks.require(
        len(_metadata_values(landing, "property", "og:title")) == 1,
        "landing page needs one Open Graph title",
    )
    checks.require(
        _metadata_values(landing, "name", "twitter:card") == ("summary",),
        "landing page needs the expected Twitter card metadata",
    )
    landing_document = landing_path.read_text(encoding="utf-8")
    checks.require(
        '<link rel="canonical" href="{}">'.format(SITE_URL) in landing_document,
        "landing page canonical URL is missing or incorrect",
    )
    checks.require(
        not landing.scripts,
        "landing page must remain script-free",
    )
    checks.require(landing.base_tags == 0, "landing page must not use a base element")
    checks.require(demo.base_tags == 0, "demo page must not use a base element")
    checks.require(
        _metadata_values(demo, "name", "robots") == ("noindex",),
        "demo page must remain noindex",
    )


def _csp_directives(policy: str) -> Mapping[str, Tuple[str, ...]]:
    directives: Dict[str, Tuple[str, ...]] = {}
    for raw in policy.split(";"):
        fields = raw.split()
        if fields:
            directives[fields[0].casefold()] = tuple(fields[1:])
    return directives


def _check_demo_boundary(
    site_root: Path,
    parsers: Mapping[Path, SiteHTMLParser],
    checks: Checks,
) -> None:
    demo_path = site_root / "demo" / "index.html"
    mock_path = site_root / "demo" / "mock-api.js"
    parser = parsers.get(demo_path)
    if parser is None or not mock_path.is_file():
        return
    demo = demo_path.read_text(encoding="utf-8")
    mock = mock_path.read_text(encoding="utf-8")
    policies = _metadata_values(parser, "http-equiv", "Content-Security-Policy")
    checks.require(len(policies) == 1, "demo page must declare exactly one CSP")
    if policies:
        directives = _csp_directives(policies[0])
        for name, expected in (
            ("default-src", ("'none'",)),
            ("connect-src", ("'none'",)),
            ("base-uri", ("'none'",)),
            ("form-action", ("'none'",)),
        ):
            checks.require(
                directives.get(name) == expected,
                "demo CSP must set {} {}".format(name, " ".join(expected)),
            )
        script_sources = directives.get("script-src", ())
        style_sources = directives.get("style-src", ())
        checks.require(
            script_sources == ("'nonce-agent-sidecar-static-demo'",),
            "demo CSP script-src must contain only the build-time nonce",
        )
        checks.require(
            style_sources == ("'nonce-agent-sidecar-static-demo'",),
            "demo CSP style-src must contain only the build-time nonce",
        )
        checks.require(
            "'unsafe-inline'" not in policies[0] and "'unsafe-eval'" not in policies[0],
            "demo CSP must not allow unsafe inline or evaluated code",
        )

    checks.require(
        bool(parser.scripts)
        and all(
            script.get("nonce") == "agent-sidecar-static-demo"
            for script in parser.scripts
        ),
        "every demo script must carry the expected nonce",
    )
    checks.require(
        all(
            style.get("nonce") == "agent-sidecar-static-demo"
            for style in parser.styles
        ),
        "every demo style must carry the expected nonce",
    )
    mock_marker = 'src="./mock-api.js"'
    panel_marker = '"use strict";'
    checks.require(
        mock_marker in demo
        and panel_marker in demo
        and demo.index(mock_marker) < demo.index(panel_marker),
        "demo mock transport must load before the product panel script",
    )
    checks.require(
        "window.fetch = function" in mock,
        "demo mock must replace window.fetch",
    )
    checks.require(
        "Static demo blocks all network access" in mock,
        "demo mock must reject unknown network paths",
    )
    checks.require(
        "http://" not in mock and "https://" not in mock and "wss://" not in mock,
        "demo mock must not contain external endpoints",
    )
    for api in NETWORK_APIS:
        checks.require(
            api not in mock,
            "demo mock must not use network API {}".format(api),
        )
    checks.require(
        re.search(r"(?<![\w.])fetch\s*\(", mock) is None,
        "demo mock must not call the native fetch function",
    )
    checks.require(
        mock.count('path !== "/api/v1/status"') == 1
        and mock.count('path !== "/api/v1/events"') == 1,
        "demo mock must allow only the two synthetic panel paths",
    )


def _page_web_path(relative: Path) -> str:
    value = relative.as_posix()
    if value == "index.html":
        return BASE_PATH
    if value.endswith("/index.html"):
        return BASE_PATH + value[: -len("index.html")]
    return BASE_PATH + value


def _path_has_base(path: str) -> bool:
    return path == BASE_PATH.rstrip("/") or path.startswith(BASE_PATH)


def _local_target(
    site_root: Path,
    page_relative: Path,
    raw_reference: str,
    checks: Checks,
    *,
    source: str,
) -> Optional[Tuple[Path, str]]:
    if not raw_reference:
        checks.require(False, "{} has an empty local reference".format(source))
        return None
    if "\\" in raw_reference or "\x00" in raw_reference:
        checks.require(False, "{} uses an unsafe reference".format(source))
        return None
    parsed_raw = urlparse(raw_reference)
    if parsed_raw.scheme.casefold() in ("data", "mailto", "tel"):
        return None
    if parsed_raw.scheme and parsed_raw.scheme.casefold() not in ("http", "https"):
        checks.require(
            False,
            "{} uses forbidden URL scheme {}".format(source, parsed_raw.scheme),
        )
        return None

    page_url = "https://site.invalid" + _page_web_path(page_relative)
    resolved = urlparse(urljoin(page_url, raw_reference))
    if resolved.netloc not in ("site.invalid", "shendeguize.github.io"):
        return None
    decoded_path = unquote(resolved.path)
    normalized = posixpath.normpath(decoded_path)
    if decoded_path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    if not _path_has_base(normalized):
        checks.require(
            False,
            "{} escapes {}: {}".format(source, BASE_PATH, raw_reference),
        )
        return None
    relative_text = normalized[len(BASE_PATH) :] if normalized != BASE_PATH.rstrip("/") else ""
    if not relative_text or relative_text.endswith("/"):
        relative_text += "index.html"
    candidate = site_root / Path(relative_text)
    try:
        candidate.resolve().relative_to(site_root.resolve())
    except ValueError:
        checks.require(
            False,
            "{} escapes the site output: {}".format(source, raw_reference),
        )
        return None
    return candidate, unquote(resolved.fragment)


def _check_references(
    site_root: Path,
    parsers: Mapping[Path, SiteHTMLParser],
    checks: Checks,
) -> None:
    for page_path in sorted(parsers):
        parser = parsers[page_path]
        relative = page_path.relative_to(site_root)
        for reference in parser.references:
            source = "{} {}[{}]".format(
                relative.as_posix(),
                reference.tag,
                reference.attribute,
            )
            raw = reference.value.strip()
            parsed = urlparse(raw)
            external = bool(
                parsed.netloc
                and parsed.netloc not in ("site.invalid", "shendeguize.github.io")
            )
            if reference.runtime and external:
                checks.require(
                    False,
                    "{} loads an external runtime asset: {}".format(source, raw),
                )
                continue
            if external:
                checks.require(True, "{} external navigation is allowed".format(source))
                continue
            target = _local_target(
                site_root,
                relative,
                raw,
                checks,
                source=source,
            )
            if target is None:
                if parsed.scheme.casefold() == "data" and reference.runtime:
                    checks.require(
                        reference.tag == "img",
                        "{} may use data URLs only for images".format(source),
                    )
                continue
            path, fragment = target
            exists = checks.require(
                path.is_file() and not path.is_symlink(),
                "{} has a broken local reference: {}".format(source, raw),
            )
            if fragment and exists:
                target_parser = parsers.get(path)
                checks.require(
                    target_parser is not None and fragment in target_parser.ids,
                    "{} has a missing fragment target: {}".format(source, raw),
                )


def _check_css(site_root: Path, checks: Checks) -> None:
    for path in sorted(site_root.rglob("*.css")):
        document = path.read_text(encoding="utf-8")
        relative = path.relative_to(site_root)
        checks.require(
            re.search(r"@import\b", document, re.IGNORECASE) is None,
            "{} must not import runtime stylesheets".format(relative),
        )
        for match in re.finditer(r"url\s*\(\s*([^)]+?)\s*\)", document, re.IGNORECASE):
            raw = match.group(1).strip("\"'")
            parsed = urlparse(raw)
            checks.require(
                parsed.scheme.casefold() not in ("http", "https")
                and not raw.startswith("//"),
                "{} loads an external CSS asset or font: {}".format(relative, raw),
            )


def _extract_cli_invocations(
    parsers: Iterable[SiteHTMLParser],
) -> Tuple[Tuple[str, ...], ...]:
    invocations = set()
    for parser in parsers:
        for line in "\n".join(parser.text).splitlines():
            candidate = line.strip()
            if not candidate.startswith("agent-sidecar "):
                continue
            try:
                tokens = tuple(shlex.split(candidate))
            except ValueError:
                continue
            if len(tokens) > 1:
                invocations.add(tokens[1:])
    return tuple(sorted(invocations))


def _command_for_invocation(
    invocation: Tuple[str, ...],
) -> Optional[Tuple[str, ...]]:
    matches = tuple(
        command
        for command in CLI_COMMAND_INVENTORY
        if invocation[: len(command)] == command
    )
    return max(matches, key=len) if matches else None


def _default_help_runner(command: Tuple[str, ...]) -> Tuple[int, str, str]:
    try:
        completed = subprocess.run(
            (sys.executable, "-m", "sidecar", *command, "--help"),
            cwd=str(ROOT),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=HELP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return 1, "", str(error)
    return completed.returncode, completed.stdout, completed.stderr


def _check_cli_commands(
    parsers: Iterable[SiteHTMLParser],
    checks: Checks,
    help_runner: HelpRunner,
) -> None:
    invocations = _extract_cli_invocations(parsers)
    discovered_commands = set()
    helps: Dict[Tuple[str, ...], Tuple[int, str, str]] = {}
    for invocation in invocations:
        command = _command_for_invocation(invocation)
        if command is None:
            checks.require(
                False,
                "site documents an unknown CLI command: agent-sidecar {}".format(
                    " ".join(invocation)
                ),
            )
            continue
        discovered_commands.add(command)
        if command not in helps:
            helps[command] = help_runner(command)
        return_code, stdout, stderr = helps[command]
        command_text = "agent-sidecar " + " ".join(command)
        checks.require(
            return_code == 0 and "usage:" in stdout.casefold(),
            "{} is not backed by CLI help: {}".format(
                command_text,
                stderr.strip() or stdout.strip(),
            ),
        )
        for token in invocation[len(command) :]:
            if not token.startswith("--"):
                continue
            option = token.partition("=")[0]
            checks.require(
                option in stdout,
                "{} documents unsupported option {}".format(command_text, option),
            )
    checks.require(
        EXPECTED_SITE_COMMANDS.issubset(discovered_commands),
        "site is missing one or more key CLI command examples",
    )


def _check_text_contracts(site_root: Path, checks: Checks) -> None:
    nojekyll = site_root / ".nojekyll"
    if nojekyll.is_file():
        checks.require(nojekyll.read_bytes() == b"", ".nojekyll must be empty")

    robots = site_root / "robots.txt"
    if robots.is_file():
        document = robots.read_text(encoding="utf-8")
        checks.require(
            document
            == (
                "User-agent: *\n"
                "Allow: /AgentSideCar/\n"
                "Sitemap: {0}sitemap.xml\n".format(SITE_URL)
            ),
            "robots.txt does not match the Pages path contract",
        )

    sitemap = site_root / "sitemap.xml"
    if sitemap.is_file():
        try:
            root = ElementTree.fromstring(sitemap.read_text(encoding="utf-8"))
            locations = {
                element.text
                for element in root.findall(
                    "{http://www.sitemaps.org/schemas/sitemap/0.9}url/"
                    "{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
                )
            }
        except (ElementTree.ParseError, OSError, UnicodeError) as error:
            checks.require(False, "sitemap.xml is invalid: {}".format(error))
        else:
            checks.require(
                locations == {SITE_URL, DEMO_URL},
                "sitemap.xml must contain exactly the landing and demo URLs",
            )

    llms = site_root / "llms.txt"
    if llms.is_file():
        document = llms.read_text(encoding="utf-8")
        for marker in (
            "# Agent Sidecar",
            "local-first",
            "zero-runtime-dependency",
            DEMO_URL,
            "https://github.com/shendeguize/AgentSideCar/blob/main/README.md",
        ):
            checks.require(
                marker in document,
                "llms.txt is missing required marker: {}".format(marker),
            )


def _check_optional_screenshots(source_root: Path, checks: Checks) -> None:
    shots = source_root / "assets" / "shots"
    if not shots.exists():
        return
    try:
        from scripts.site_shots import ScreenshotError, validate_png
    except ImportError as error:
        checks.require(False, "cannot import screenshot validator: {}".format(error))
        return
    for name in ("landing.png", "panel.png"):
        try:
            validate_png(shots / name, expected_dimensions=PNG_DIMENSIONS)
        except ScreenshotError as error:
            checks.require(False, str(error))
        else:
            checks.require(True, "{} is a valid tracked screenshot".format(name))


def inspect_site(
    site_root: Path = SITE_OUTPUT,
    *,
    source_root: Path = SITE_SOURCE,
    help_runner: HelpRunner = _default_help_runner,
    validate_cli: bool = True,
) -> CheckSummary:
    """Inspect one built tree and return every deterministic diagnostic."""
    checks = Checks()
    _check_required_outputs(site_root, checks)
    parsers: Dict[Path, SiteHTMLParser] = {}
    if site_root.is_dir():
        for path in sorted(site_root.rglob("*.html")):
            parser = _parse_html(path, checks)
            if parser is not None:
                parsers[path] = parser
    checks.require(
        set(parsers) == {site_root / "index.html", site_root / "demo" / "index.html"},
        "site must contain exactly the landing and demo HTML pages",
    )
    _check_metadata(site_root, parsers, checks)
    _check_demo_boundary(site_root, parsers, checks)
    _check_references(site_root, parsers, checks)
    _check_css(site_root, checks)
    if validate_cli:
        _check_cli_commands(parsers.values(), checks, help_runner)
    _check_text_contracts(site_root, checks)
    _check_optional_screenshots(source_root, checks)
    return checks.summary()


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description=__doc__)


def main(argv: Optional[Sequence[str]] = None) -> int:
    build_parser().parse_args(argv)
    checks = Checks()
    checks.extend(check_deterministic_build())
    if not checks.errors:
        checks.extend(inspect_site())
    summary = checks.summary()
    if not summary.ok:
        for error in summary.errors:
            print("site check failed: {}".format(error), file=sys.stderr)
        print(
            "site check failed: {} diagnostic(s), {} checks".format(
                len(summary.errors),
                summary.checks,
            ),
            file=sys.stderr,
        )
        return 1
    print("site check passed: {} checks".format(summary.checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
