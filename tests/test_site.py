import hashlib
import struct
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

import sidecar.http_server as http_server
from scripts.build_site import (
    DEMO_NONCE,
    ROOT,
    build_site,
    normalize_base_path,
)
from scripts.site_check import inspect_site
from scripts.site_shots import (
    PNG_SIGNATURE,
    VIEWPORT,
    ScreenshotError,
    build_parser as build_shots_parser,
    create_server,
    validate_png,
)
from sidecar.web_panel import NONCE_PLACEHOLDER, PANEL_HTML, render_panel


class AssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.resources = []
        self.scripts = []
        self.styles = []
        self.policies = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script":
            self.scripts.append(attributes)
            if "src" in attributes:
                self.resources.append(attributes["src"])
        elif tag == "style":
            self.styles.append(attributes)
        elif tag == "link":
            relations = attributes.get("rel", "").split()
            if set(relations).intersection({"stylesheet", "icon"}):
                self.resources.append(attributes["href"])
        elif (
            tag == "meta"
            and attributes.get("http-equiv") == "Content-Security-Policy"
        ):
            self.policies.append(attributes["content"])


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class CanonicalPanelTests(unittest.TestCase):
    def test_http_server_alias_and_nonce_render_remain_equivalent(self):
        nonce = "test-panel-nonce"

        self.assertIs(http_server._PANEL, PANEL_HTML)
        self.assertEqual(2, PANEL_HTML.count(NONCE_PLACEHOLDER))
        self.assertEqual(
            PANEL_HTML.replace(NONCE_PLACEHOLDER, nonce),
            render_panel(nonce),
        )
        self.assertNotIn(NONCE_PLACEHOLDER, render_panel(nonce))
        self.assertIn('fetch("/api/v1/status"', PANEL_HTML)
        self.assertIn('fetch("/api/v1/events"', PANEL_HTML)

    def test_structured_demo_insertions_precede_product_script(self):
        rendered = render_panel(
            DEMO_NONCE,
            head_html='<meta name="demo-head">\n',
            body_html='<aside id="demo-marker"></aside>\n',
            before_script_html='<script src="./mock-api.js"></script>\n',
        )

        self.assertLess(
            rendered.index('<meta name="demo-head">'),
            rendered.index("<style nonce="),
        )
        self.assertLess(
            rendered.index('<aside id="demo-marker">'),
            rendered.index('<form id="auth"'),
        )
        self.assertLess(
            rendered.index('src="./mock-api.js"'),
            rendered.index('"use strict";'),
        )


class StaticSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name) / "_site"

    def tearDown(self):
        self.temporary.cleanup()

    def test_two_builds_are_identical_clean_stale_and_emit_required_files(self):
        build_site(self.output)
        first = tree_digest(self.output)
        (self.output / "stale.txt").write_text("remove me", encoding="utf-8")

        build_site(self.output)
        second = tree_digest(self.output)

        self.assertEqual(first, second)
        self.assertEqual(
            {
                ".nojekyll",
                "demo/index.html",
                "demo/mock-api.js",
                "favicon.svg",
                "index.html",
                "landing.css",
                "llms.txt",
                "robots.txt",
                "sitemap.xml",
            },
            {
                path.relative_to(self.output).as_posix()
                for path in self.output.rglob("*")
                if path.is_file()
            },
        )
        self.assertFalse((self.output / "stale.txt").exists())
        self.assertIn(
            "https://shendeguize.github.io/AgentSideCar/demo/",
            (self.output / "sitemap.xml").read_text(encoding="utf-8"),
        )

    def test_landing_uses_pages_base_metadata_and_accessibility_contracts(self):
        build_site(self.output)
        landing = (self.output / "index.html").read_text(encoding="utf-8")
        css = (self.output / "landing.css").read_text(encoding="utf-8")

        self.assertNotIn("${BASE_PATH}", landing)
        self.assertIn('href="/AgentSideCar/landing.css"', landing)
        self.assertIn('href="/AgentSideCar/demo/"', landing)
        self.assertIn('property="og:title"', landing)
        self.assertIn('name="twitter:card"', landing)
        self.assertIn('<main id="main">', landing)
        self.assertIn("<header", landing)
        self.assertIn("<footer", landing)
        self.assertIn('lang="zh-CN"', landing)
        self.assertIn(":focus-visible", css)
        self.assertIn("prefers-reduced-motion: reduce", css)

    def test_generated_pages_have_no_external_runtime_assets(self):
        build_site(self.output)
        landing = (self.output / "index.html").read_text(encoding="utf-8")
        demo = (self.output / "demo" / "index.html").read_text(encoding="utf-8")
        css = (self.output / "landing.css").read_text(encoding="utf-8")
        mock_api = (self.output / "demo" / "mock-api.js").read_text(
            encoding="utf-8"
        )
        landing_parser = AssetParser()
        demo_parser = AssetParser()
        landing_parser.feed(landing)
        demo_parser.feed(demo)

        self.assertTrue(
            all(resource.startswith("/AgentSideCar/") for resource in landing_parser.resources)
        )
        self.assertEqual(["../favicon.svg", "./mock-api.js"], demo_parser.resources)
        self.assertNotRegex(css, r"@import|url\s*\(")
        self.assertNotRegex(mock_api, r"https?://")
        for network_api in (
            "EventSource",
            "WebSocket",
            "XMLHttpRequest",
            "navigator.sendBeacon",
        ):
            self.assertNotIn(network_api, mock_api)
        self.assertIn("window.fetch = function", mock_api)
        self.assertIn("Static demo blocks all network access", mock_api)

    def test_demo_mock_loads_first_and_csp_allows_only_injected_scripts(self):
        build_site(self.output)
        demo = (self.output / "demo" / "index.html").read_text(encoding="utf-8")
        mock_api = (self.output / "demo" / "mock-api.js").read_text(
            encoding="utf-8"
        )
        parser = AssetParser()
        parser.feed(demo)

        self.assertLess(demo.index('src="./mock-api.js"'), demo.index('"use strict";'))
        self.assertEqual(1, len(parser.policies))
        self.assertIn("connect-src 'none'", parser.policies[0])
        self.assertIn(
            "script-src 'nonce-{}'".format(DEMO_NONCE),
            parser.policies[0],
        )
        self.assertNotIn("'unsafe-inline'", parser.policies[0])
        self.assertTrue(
            all(script.get("nonce") == DEMO_NONCE for script in parser.scripts)
        )
        self.assertTrue(
            all(style.get("nonce") == DEMO_NONCE for style in parser.styles)
        )
        self.assertIn("Synthetic read-only demo.", demo)
        self.assertIn('input.value = demoToken', mock_api)
        self.assertIn('path !== "/api/v1/status"', mock_api)
        self.assertIn('path !== "/api/v1/events"', mock_api)

    def test_local_base_override_and_clean_safety(self):
        self.assertEqual("./", normalize_base_path("."))
        self.assertEqual("/AgentSideCar/", normalize_base_path("/AgentSideCar"))
        with self.assertRaises(ValueError):
            normalize_base_path("../unsafe")
        with self.assertRaises(ValueError):
            build_site(ROOT)

        build_site(self.output, base_path="./")
        landing = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="./landing.css"', landing)
        self.assertIn('href="./demo/"', landing)


class SiteVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "_site"
        build_site(self.output)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def help_runner(command):
        help_by_command = {
            ("daemon", "start"): "usage: agent-sidecar daemon start [--http]\n",
            ("status",): "usage: agent-sidecar status [--json]\n",
            ("tui",): "usage: agent-sidecar tui [--once]\n",
        }
        return 0, help_by_command.get(command, "usage: agent-sidecar\n"), ""

    def test_site_verifier_accepts_complete_generated_tree(self):
        summary = inspect_site(
            self.output,
            source_root=self.root / "source-without-shots",
            help_runner=self.help_runner,
        )

        self.assertTrue(summary.ok, "\n".join(summary.errors))
        self.assertGreater(summary.checks, 40)

    def test_site_verifier_rejects_broken_and_escaping_links(self):
        landing_path = self.output / "index.html"
        landing = landing_path.read_text(encoding="utf-8")
        landing = landing.replace(
            "</main>",
            (
                '<a href="/AgentSideCar/missing/">broken</a>'
                '<a href="/outside/">escape</a></main>'
            ),
        )
        landing_path.write_text(landing, encoding="utf-8")

        summary = inspect_site(
            self.output,
            source_root=self.root / "source-without-shots",
            validate_cli=False,
        )

        self.assertFalse(summary.ok)
        self.assertTrue(
            any("broken local reference" in error for error in summary.errors),
            summary.errors,
        )
        self.assertTrue(
            any("escapes /AgentSideCar/" in error for error in summary.errors),
            summary.errors,
        )

    def test_site_verifier_checks_documented_cli_options_against_help(self):
        landing_path = self.output / "index.html"
        landing = landing_path.read_text(encoding="utf-8")
        landing_path.write_text(
            landing.replace(
                "agent-sidecar status",
                "agent-sidecar status --not-a-real-option",
            ),
            encoding="utf-8",
        )

        summary = inspect_site(
            self.output,
            source_root=self.root / "source-without-shots",
            help_runner=self.help_runner,
        )

        self.assertTrue(
            any("unsupported option --not-a-real-option" in error for error in summary.errors),
            summary.errors,
        )

    def test_screenshot_server_is_ephemeral_numeric_loopback_only(self):
        server = create_server(self.output)
        try:
            self.assertEqual("127.0.0.1", server.server_address[0])
            self.assertGreater(server.server_port, 0)
        finally:
            server.server_close()

        with self.assertRaises(ScreenshotError):
            create_server(self.output, host="localhost")


class ScreenshotValidationTests(unittest.TestCase):
    @staticmethod
    def png_prefix(width, height):
        return (
            PNG_SIGNATURE
            + struct.pack(">I", 13)
            + b"IHDR"
            + struct.pack(">II", width, height)
            + b"\x08\x06\x00\x00\x00"
            + b"\x00\x00\x00\x00"
        )

    def test_png_validator_accepts_signature_and_expected_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shot.png"
            path.write_bytes(self.png_prefix(*VIEWPORT))

            self.assertEqual(VIEWPORT, validate_png(path))

    def test_png_validator_rejects_bad_signature_and_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shot.png"
            path.write_bytes(b"not a png")
            with self.assertRaisesRegex(ScreenshotError, "not a PNG"):
                validate_png(path)

            path.write_bytes(self.png_prefix(800, 600))
            with self.assertRaisesRegex(ScreenshotError, "expected 1440x960"):
                validate_png(path)

    def test_screenshot_argument_parser_keeps_check_side_effect_free(self):
        args = build_shots_parser().parse_args(["--check"])
        self.assertTrue(args.check)
        self.assertIsNone(args.chrome)


if __name__ == "__main__":
    unittest.main()
