#!/usr/bin/env python3
"""Build the deterministic, dependency-free GitHub Pages site."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from string import Template
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "site"
DEFAULT_OUTPUT = ROOT / "_site"
DEFAULT_BASE_PATH = "/AgentSideCar/"
DEMO_NONCE = "agent-sidecar-static-demo"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sidecar.web_panel import render_panel  # noqa: E402

FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#07110f"/>
<path d="M17 45 28 18h8l11 27h-8l-2-6H26l-2 6zm11-12h7l-3.5-10z" fill="#72e6b1"/>
</svg>
"""

ROBOTS = """User-agent: *
Allow: /AgentSideCar/
Sitemap: https://shendeguize.github.io/AgentSideCar/sitemap.xml
"""

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://shendeguize.github.io/AgentSideCar/</loc></url>
  <url><loc>https://shendeguize.github.io/AgentSideCar/demo/</loc></url>
</urlset>
"""

LLMS = """# Agent Sidecar

> A local-first, zero-runtime-dependency CLI for observing AI-agent sessions.

Agent Sidecar discovers persisted session metadata, normalizes lifecycle state
and events, and presents read-oriented views as text, JSON, a TUI, or an
opt-in numeric-loopback HTTP panel.

## Links

- Documentation: https://github.com/shendeguize/AgentSideCar/blob/main/README.md
- Security policy: https://github.com/shendeguize/AgentSideCar/blob/main/SECURITY.md
- Releases: https://github.com/shendeguize/AgentSideCar/releases
- Synthetic demo: https://shendeguize.github.io/AgentSideCar/demo/
"""

DEMO_HEAD = """<meta name="description" content="Synthetic, read-only Agent Sidecar panel demo.">
<meta name="robots" content="noindex">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-agent-sidecar-static-demo'; style-src 'nonce-agent-sidecar-static-demo'; connect-src 'none'; img-src 'self' data:; base-uri 'none'; form-action 'none'">
<link rel="icon" href="../favicon.svg" type="image/svg+xml">
<style nonce="agent-sidecar-static-demo">
.demo-banner{display:flex;align-items:center;justify-content:space-between;gap:1rem;margin:0 0 1.5rem;padding:.8rem 1rem;border:1px solid #9a6700;border-radius:.4rem;background:#fff8c5;color:#3b2300}
.demo-banner p{margin:0}.demo-banner a{color:#663c00;font-weight:700}
@media(max-width:42rem){.demo-banner{align-items:flex-start;flex-direction:column}body{margin:1rem}table{display:block;overflow-x:auto}}
</style>
"""

DEMO_BODY = """<aside class="demo-banner" aria-label="Static demo notice">
<p><strong>Synthetic read-only demo.</strong> No backend, real token, transcript, or network request is used.</p>
<a href="../">Back to site</a>
</aside>
"""

DEMO_TRANSPORT = (
    '<script nonce="agent-sidecar-static-demo" src="./mock-api.js"></script>\n'
)


def normalize_base_path(value: str) -> str:
    """Validate one absolute Pages path or the local relative override."""
    if value in (".", "./"):
        return "./"
    if not value.startswith("/") or "\\" in value or "?" in value or "#" in value:
        raise ValueError("base path must be an absolute URL path or './'")
    parts = value.split("/")
    if any(part in (".", "..") for part in parts):
        raise ValueError("base path cannot contain dot segments")
    return value if value.endswith("/") else value + "/"


def _clean_output(destination: Path) -> None:
    resolved = destination.resolve()
    protected = {
        ROOT.resolve(),
        ROOT.parent.resolve(),
        SOURCE.resolve(),
        (ROOT / "sidecar").resolve(),
        (ROOT / "scripts").resolve(),
        (ROOT / "tests").resolve(),
        Path.home().resolve(),
        Path("/").resolve(),
    }
    if destination.is_symlink() or resolved in protected:
        raise ValueError("refusing to clean unsafe site output: {}".format(destination))
    if destination.exists():
        if not destination.is_dir() or (destination / ".git").exists():
            raise ValueError("refusing to clean unsafe site output: {}".format(destination))
        shutil.rmtree(str(destination))
    destination.mkdir(parents=True)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def build_site(
    destination: Path = DEFAULT_OUTPUT,
    *,
    base_path: str = DEFAULT_BASE_PATH,
) -> None:
    """Build the complete static site into a freshly cleaned directory."""
    normalized_base = normalize_base_path(base_path)
    _clean_output(destination)

    landing = Template((SOURCE / "index.html").read_text(encoding="utf-8"))
    _write(destination / "index.html", landing.substitute(BASE_PATH=normalized_base))
    _write(
        destination / "landing.css",
        (SOURCE / "landing.css").read_text(encoding="utf-8"),
    )
    _write(
        destination / "demo" / "mock-api.js",
        (SOURCE / "demo" / "mock-api.js").read_text(encoding="utf-8"),
    )
    _write(
        destination / "demo" / "index.html",
        render_panel(
            DEMO_NONCE,
            head_html=DEMO_HEAD,
            body_html=DEMO_BODY,
            before_script_html=DEMO_TRANSPORT,
        ),
    )
    _write(destination / ".nojekyll", "")
    _write(destination / "robots.txt", ROBOTS)
    _write(destination / "sitemap.xml", SITEMAP)
    _write(destination / "llms.txt", LLMS)
    _write(destination / "favicon.svg", FAVICON)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-path",
        default=DEFAULT_BASE_PATH,
        help="Pages URL prefix (default: %(default)s; use ./ for local browsing)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        build_site(base_path=args.base_path)
    except (OSError, ValueError) as error:
        print("site build failed: {}".format(error), file=sys.stderr)
        return 1
    print("built {}".format(DEFAULT_OUTPUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
