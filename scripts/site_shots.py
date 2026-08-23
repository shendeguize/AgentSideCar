#!/usr/bin/env python3
"""Regenerate or validate deterministic Agent Sidecar site screenshots."""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SITE_OUTPUT = ROOT / "_site"
SHOT_OUTPUT = ROOT / "site" / "assets" / "shots"
LOOPBACK_HOST = "127.0.0.1"
VIEWPORT = (1440, 960)
SERVER_START_TIMEOUT_SECONDS = 5.0
CHROME_TIMEOUT_SECONDS = 30.0
PROCESS_TERM_TIMEOUT_SECONDS = 3.0
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

MACOS_CHROME_PATHS = (
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    Path(
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing"
    ),
    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
)

LINUX_CHROME_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
)


class ScreenshotError(RuntimeError):
    """A screenshot preflight, capture, or validation failure."""


class IPv4ThreadingHTTPServer(ThreadingHTTPServer):
    """A non-reusing, IPv4-only server for one screenshot run."""

    address_family = socket.AF_INET
    allow_reuse_address = False
    daemon_threads = True


class QuietSiteHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def validate_png(
    path: Path,
    *,
    expected_dimensions: Tuple[int, int] = VIEWPORT,
) -> Tuple[int, int]:
    """Validate a regular PNG's signature, IHDR, and exact dimensions."""
    if not path.is_file() or path.is_symlink():
        raise ScreenshotError(
            "missing screenshot {} (run python3 scripts/site_shots.py)".format(path)
        )
    try:
        prefix = path.read_bytes()[:33]
    except OSError as error:
        raise ScreenshotError("cannot read screenshot {}: {}".format(path, error))
    if len(prefix) < 24 or prefix[:8] != PNG_SIGNATURE:
        raise ScreenshotError("{} is not a PNG file".format(path))
    if prefix[12:16] != b"IHDR" or struct.unpack(">I", prefix[8:12])[0] != 13:
        raise ScreenshotError("{} has an invalid PNG IHDR".format(path))
    width, height = struct.unpack(">II", prefix[16:24])
    if (width, height) != expected_dimensions:
        raise ScreenshotError(
            "{} has dimensions {}x{}; expected {}x{}".format(
                path,
                width,
                height,
                expected_dimensions[0],
                expected_dimensions[1],
            )
        )
    return width, height


def locate_chrome(explicit: Optional[Path] = None) -> Path:
    """Locate an executable Chrome/Chromium on supported operator systems."""
    if explicit is not None:
        candidate = explicit.expanduser()
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return candidate.resolve()
        raise ScreenshotError(
            "Chrome is not executable at {}; pass --chrome with a valid binary".format(
                explicit
            )
        )

    for candidate in MACOS_CHROME_PATHS:
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            return candidate.resolve()
    for name in LINUX_CHROME_NAMES:
        resolved = shutil.which(name)
        if resolved:
            return Path(resolved).resolve()
    raise ScreenshotError(
        "Chrome/Chromium was not found. Install Google Chrome or Chromium, "
        "or pass --chrome /path/to/executable."
    )


def create_server(
    directory: Path,
    *,
    host: str = LOOPBACK_HOST,
    port: int = 0,
) -> IPv4ThreadingHTTPServer:
    """Create an IPv4 loopback server, using an ephemeral port by default."""
    if host != LOOPBACK_HOST:
        raise ScreenshotError("screenshot server must bind numeric 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ScreenshotError("screenshot server port must be an integer 0..65535")
    if not directory.is_dir():
        raise ScreenshotError("built site is missing: {}".format(directory))

    handler = lambda *args, **kwargs: QuietSiteHandler(  # noqa: E731
        *args,
        directory=str(directory),
        **kwargs,
    )
    try:
        return IPv4ThreadingHTTPServer((host, port), handler)
    except OSError as error:
        raise ScreenshotError("cannot bind screenshot server: {}".format(error))


def _wait_for_server(url: str, marker: bytes) -> None:
    deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
    last_error = "server did not answer"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                body = response.read(256 * 1024)
                if response.status == 200 and marker in body:
                    return
                last_error = "unexpected status or page marker"
        except (OSError, URLError) as error:
            last_error = str(error)
        time.sleep(0.05)
    raise ScreenshotError(
        "screenshot server did not become ready within {:.0f}s: {}".format(
            SERVER_START_TIMEOUT_SECONDS,
            last_error,
        )
    )


@contextlib.contextmanager
def serve_site(directory: Path) -> Iterator[str]:
    """Serve a built site on one bounded numeric-loopback listener."""
    server = create_server(directory)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="agent-sidecar-site-shots",
        daemon=False,
    )
    thread.start()
    base_url = "http://{}:{}/".format(LOOPBACK_HOST, server.server_port)
    try:
        _wait_for_server(base_url, b"Agent Sidecar")
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=SERVER_START_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise ScreenshotError("screenshot server did not shut down cleanly")


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, AttributeError):
        process.terminate()
    try:
        process.communicate(timeout=PROCESS_TERM_TIMEOUT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        process.kill()
    process.communicate()


def _png_is_complete(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            stream.seek(-12, os.SEEK_END)
            return stream.read(12) == b"\x00\x00\x00\x00IEND\xaeB`\x82"
    except (OSError, ValueError):
        return False


def _run_chrome(arguments: Sequence[str], expected_output: Path) -> None:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as diagnostic:
        try:
            process = subprocess.Popen(
                list(arguments),
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=diagnostic,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            raise ScreenshotError("cannot start Chrome: {}".format(error))

        deadline = time.monotonic() + CHROME_TIMEOUT_SECONDS
        completed_png = False
        while time.monotonic() < deadline:
            completed_png = _png_is_complete(expected_output)
            if completed_png or process.poll() is not None:
                break
            time.sleep(0.05)

        if process.poll() is None:
            _terminate_process(process)
        else:
            process.wait()
        completed_png = completed_png or _png_is_complete(expected_output)
        if completed_png:
            return

        diagnostic.seek(0)
        details = diagnostic.read().strip()
        if time.monotonic() >= deadline:
            raise ScreenshotError(
                "Chrome capture exceeded {:.0f}s: {}".format(
                    CHROME_TIMEOUT_SECONDS,
                    details[-2000:] or "no diagnostic output",
                )
            )
        raise ScreenshotError(
            "Chrome capture failed with exit {}: {}".format(
                process.returncode,
                details[-2000:] or "no diagnostic output",
            )
        )


def _build_local_site() -> None:
    try:
        completed = subprocess.run(
            (
                sys.executable,
                str(ROOT / "scripts" / "build_site.py"),
                "--base-path",
                "./",
            ),
            cwd=str(ROOT),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScreenshotError("site build failed: {}".format(error))
    if completed.returncode:
        raise ScreenshotError(
            "site build failed: {}".format(
                completed.stderr.strip() or completed.stdout.strip()
            )
        )


def _capture_one(
    chrome: Path,
    *,
    user_data_dir: Path,
    url: str,
    destination: Path,
) -> None:
    temporary = destination.with_name("." + destination.stem + ".tmp.png")
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
    arguments = (
        str(chrome),
        "--headless=new",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-features=Translate",
        "--disable-gpu",
        "--disable-sync",
        "--force-device-scale-factor=1",
        "--hide-scrollbars",
        "--metrics-recording-only",
        "--no-default-browser-check",
        "--no-first-run",
        "--run-all-compositor-stages-before-draw",
        "--safebrowsing-disable-auto-update",
        "--virtual-time-budget=1500",
        "--window-size={},{}".format(*VIEWPORT),
        "--user-data-dir={}".format(user_data_dir),
        "--screenshot={}".format(temporary),
        url,
    )
    try:
        _run_chrome(arguments, temporary)
        validate_png(temporary)
        os.replace(str(temporary), str(destination))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def capture_screenshots(
    *,
    chrome: Optional[Path] = None,
    output: Path = SHOT_OUTPUT,
) -> None:
    """Build, serve, and atomically regenerate both canonical screenshots."""
    executable = locate_chrome(chrome)
    _build_local_site()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="agent-sidecar-chrome-") as temporary:
        profile = Path(temporary) / "profile"
        with serve_site(SITE_OUTPUT) as base_url:
            _capture_one(
                executable,
                user_data_dir=profile,
                url=base_url,
                destination=output / "landing.png",
            )
            _capture_one(
                executable,
                user_data_dir=profile,
                url=urljoin(base_url, "demo/"),
                destination=output / "panel.png",
            )


def check_screenshots(output: Path = SHOT_OUTPUT) -> None:
    """Validate tracked screenshots without building or launching Chrome."""
    validate_png(output / "landing.png")
    validate_png(output / "panel.png")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate tracked PNGs without rebuilding or launching Chrome",
    )
    parser.add_argument(
        "--chrome",
        type=Path,
        default=None,
        metavar="PATH",
        help="explicit Chrome/Chromium executable",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.check:
            check_screenshots()
            print("site screenshots passed: 2 PNGs at {}x{}".format(*VIEWPORT))
        else:
            capture_screenshots(chrome=args.chrome)
            print("site screenshots regenerated in {}".format(SHOT_OUTPUT))
    except ScreenshotError as error:
        print("site screenshots failed: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
