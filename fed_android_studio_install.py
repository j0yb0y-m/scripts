#!/usr/bin/env python3
"""
Install Android Studio on Fedora Linux.

Downloads the official Linux tarball (the latest stable build by default, or a
URL you pass in), installs it under /opt, wires up the virtualization packages
Android Studio's emulator needs, and adds the IDE to the user's PATH. Safe to
re-run: it upgrades in place and does not duplicate PATH entries.

Already have the tarball? Pass --skip-tarball-download to install from the copy
in the download directory without touching the network.

Standard library only (Python 3.9+); run with --help for the options.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.client
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

PROGRAM = Path(sys.argv[0]).name or "fed_android_studio_install.py"

DEFAULT_INSTALL_DIR = Path("/opt/android-studio")
TOP_LEVEL_DIR = "android-studio"  # what the tarball extracts to
FALLBACK_ARCHIVE_NAME = "android-studio.tar.gz"
STUDIO_PAGE = "https://developer.android.com/studio"

PACKAGES = (
    "java-latest-openjdk-devel",
    "@virtualization",
    "android-tools",
    "adb-enhanced",
)

USER_AGENT = "fed_android_studio_install/1.0"
HTTP_TIMEOUT = 60  # seconds, per socket operation
RETRIES = 3
CHUNK_SIZE = 1 << 20

# Best-effort scraping of developer.android.com/studio (newlines are stripped
# first, so a match can span the original line breaks).
_URL_RE = re.compile(r'https://[^"]*android-studio[^"]*linux[^"]*\.tar\.gz')
_SHA_CONTEXT_RE = re.compile(r"linux\.tar\.gz</button>(.{0,200})")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_ARCHIVE_NAME_RE = re.compile(r"(?!\.)[A-Za-z0-9._+-]+\.tar\.gz")

T = TypeVar("T")


class InstallError(Exception):
    """A fatal, user-facing problem; main() prints it and exits 1."""


def log(message: str) -> None:
    print(f"[{PROGRAM}] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[{PROGRAM}] warning: {message}", file=sys.stderr, flush=True)



# Configuration



@dataclass(frozen=True)
class Config:
    install_dir: Path
    download_dir: Path
    home: Path
    url: str
    sha256: str
    dry_run: bool
    full_update: bool
    no_launch: bool
    skip_download: bool


def _sha256_arg(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"invalid SHA-256 (expected 64 hex characters): {value}"
        )
    return value.lower()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        usage="%(prog)s [OPTIONS] [<download-url> [sha256]]",
        description="Install Android Studio on Fedora Linux.",
        epilog=(
            "environment overrides:\n"
            "  ANDROID_STUDIO_DIR           Install directory (default: "
            f"{DEFAULT_INSTALL_DIR}).\n"
            "                               The tarball extracts to "
            f"'{TOP_LEVEL_DIR}', so the\n"
            f"                               basename must stay '{TOP_LEVEL_DIR}'.\n"
            "  ANDROID_STUDIO_DOWNLOAD_DIR  Tarball directory (default: ~/apps).\n"
            "\n"
            "examples:\n"
            "  %(prog)s\n"
            "  %(prog)s --skip-tarball-download\n"
            "  ANDROID_STUDIO_DOWNLOAD_DIR=~/Downloads %(prog)s --skip-tarball-download\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        metavar="<download-url>",
        help=(
            "URL of the Android Studio Linux tarball (.tar.gz). If omitted, the "
            "latest stable URL is read from developer.android.com/studio. "
            "Ignored with --skip-tarball-download."
        ),
    )
    parser.add_argument(
        "sha256_arg",
        nargs="?",
        type=_sha256_arg,
        default=None,
        metavar="sha256",
        help=(
            "expected SHA-256 of the tarball. When the URL is auto-detected the "
            "published checksum is used automatically; against a URL you pass in "
            "it is optional, and the computed hash is printed if you leave it out."
        ),
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="print the actions without changing anything",
    )
    parser.add_argument(
        "--full-update",
        action="store_true",
        help="run 'dnf update -y' before installing packages",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="do not offer to launch Android Studio at the end",
    )
    parser.add_argument(
        "--skip-tarball-download",
        action="store_true",
        help=(
            "do not download anything: install from a tarball that is already in "
            "the download directory (the newest android-studio*.tar.gz there), "
            "then continue with the normal setup. No network lookup is made."
        ),
    )
    parser.add_argument(
        "--sha256",
        dest="sha256_opt",
        type=_sha256_arg,
        default=None,
        metavar="HASH",
        help=(
            "expected SHA-256 of the tarball. Same as the positional [sha256], "
            "and the way to verify a tarball you are reusing with "
            "--skip-tarball-download."
        ),
    )
    return parser


def _home_dir() -> Path:
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise InstallError("HOME is not set") from exc
    if not home.is_dir():
        raise InstallError(f"HOME does not exist: {home}")
    return home


def _path_from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if not raw:
        return default
    return Path(os.path.normpath(os.path.expanduser(raw)))


def parse_args(argv: Sequence[str] | None = None) -> Config:
    parser = build_parser()
    args = parser.parse_intermixed_args(argv)

    if args.sha256_arg and args.sha256_opt:
        parser.error("SHA-256 given twice (positional argument and --sha256)")
    sha256: str = args.sha256_arg or args.sha256_opt or ""

    url: str = args.url or ""
    if args.skip_tarball_download and url:
        warn(
            "ignoring <download-url> because of --skip-tarball-download "
            "(use --sha256 to verify the tarball)"
        )
        url = ""
    if url:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            parser.error(f"invalid download URL (expected http:// or https://): {url}")

    home = _home_dir()
    install_dir = _path_from_env("ANDROID_STUDIO_DIR", DEFAULT_INSTALL_DIR)
    download_dir = _path_from_env("ANDROID_STUDIO_DOWNLOAD_DIR", home / "apps")

    # install_dir is handed to 'rm -rf' when upgrading, so be strict about it.
    if not install_dir.is_absolute():
        raise InstallError(
            f"ANDROID_STUDIO_DIR must be an absolute path: '{install_dir}'"
        )
    if install_dir.name != TOP_LEVEL_DIR:
        raise InstallError(
            f"ANDROID_STUDIO_DIR must end in '{TOP_LEVEL_DIR}' "
            f"(the tarball's top-level directory): '{install_dir}'"
        )

    return Config(
        install_dir=install_dir,
        download_dir=Path(os.path.abspath(download_dir)),
        home=home,
        url=url,
        sha256=sha256,
        dry_run=args.dry_run,
        full_update=args.full_update,
        no_launch=args.no_launch,
        skip_download=args.skip_tarball_download,
    )



# Network helpers



def _retry(action: Callable[[], T], what: str) -> T:
    """Run action, retrying transient network/IO failures with a short backoff."""
    for attempt in range(1, RETRIES + 1):
        try:
            return action()
        except (OSError, http.client.HTTPException) as exc:
            if attempt == RETRIES:
                raise InstallError(
                    f"{what} failed after {RETRIES} attempts: {exc}"
                ) from exc
            warn(f"{what} interrupted ({exc}); retrying ({attempt}/{RETRIES})")
            time.sleep(2 * attempt)
    raise AssertionError("unreachable")


def _open(
    url: str,
    headers: dict[str, str] | None = None,
    passthrough: tuple[int, ...] = (),
) -> Any:
    """urlopen with our User-Agent; client errors (4xx) are fatal, not retried."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, **(headers or {})}
    )
    try:
        return urllib.request.urlopen(request, timeout=HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in passthrough:
            raise InstallError(f"HTTP {exc.code} {exc.reason}: {url}") from exc
        raise


def _get_text(url: str) -> str:
    def fetch() -> str:
        with _open(url) as response:
            return response.read().decode("utf-8", errors="replace")

    return _retry(fetch, f"fetching {url}")


class _Progress:
    """One-line download progress on a terminal; silent otherwise."""

    def __init__(self, total: int | None, start: int) -> None:
        self.total = total
        self.start = start
        self.enabled = sys.stderr.isatty()
        self.began = time.monotonic()
        self.last = 0.0

    def update(self, done: int, *, final: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not final and now - self.last < 0.25:
            return
        self.last = now
        rate = (done - self.start) / max(now - self.began, 1e-6) / 1e6
        if self.total:
            line = (
                f"{done / 1e6:8.1f} / {self.total / 1e6:.1f} MB "
                f"({done * 100 // self.total}%)  {rate:5.1f} MB/s"
            )
        else:
            line = f"{done / 1e6:8.1f} MB  {rate:5.1f} MB/s"
        sys.stderr.write("\r" + line + ("\n" if final else ""))
        sys.stderr.flush()


def _content_range_total(value: str | None) -> int | None:
    """Total size from a 'Content-Range: bytes */1234' style header."""
    match = re.search(r"/(\d+)\s*$", value or "")
    return int(match.group(1)) if match else None


def _fetch_into(url: str, part: Path) -> None:
    """Download url into part, resuming from whatever part already holds."""
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}

    try:
        response = _open(url, headers, passthrough=(416,))
    except urllib.error.HTTPError as exc:
        if exc.code != 416:
            raise
        # Range not satisfiable: the partial file is either already complete
        # or no longer matches what the server has.
        if _content_range_total(exc.headers.get("Content-Range")) == offset:
            return
        part.unlink()
        raise OSError("partial download no longer matches the server; restarting") from exc

    with response:
        resumed = offset > 0 and response.status == 206
        start = offset if resumed else 0  # a plain 200 means Range was ignored
        length = response.headers.get("Content-Length")
        expected = start + int(length) if length else None
        if resumed:
            log(f"resuming at {start / 1e6:.1f} MB")

        written = start
        progress = _Progress(expected, start)
        with part.open("ab" if resumed else "wb") as handle:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
                progress.update(written)
        progress.update(written, final=True)

    if expected is not None and written != expected:
        raise OSError(f"connection closed early ({written} of {expected} bytes)")


def download(url: str, dest: Path) -> None:
    """Download url to dest via dest.part, so dest only ever exists when complete."""
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _retry(lambda: _fetch_into(url, part), "download")
    part.replace(dest)



# Archive helpers



def archive_name_from_url(url: str) -> str:
    """The tarball's own file name; it carries the version, so a newer release
    never resumes or overwrites an older download."""
    name = PurePosixPath(urllib.parse.urlparse(url).path).name
    return name if _ARCHIVE_NAME_RE.fullmatch(name) else FALLBACK_ARCHIVE_NAME


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def check_tarball(path: Path) -> None:
    """Make sure path is a complete gzip tarball laid out as 'android-studio/...'.

    Walking every member reads the whole archive, so truncation is caught here
    too. This runs before anything on the system is touched.
    """
    count = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if not parts:  # a bare "./" entry
                    continue
                count += 1
                if parts[0] != TOP_LEVEL_DIR or ".." in parts:
                    raise InstallError(
                        f"unexpected tarball layout (entry: '{member.name}'); "
                        f"expected everything under a top-level '{TOP_LEVEL_DIR}/' directory"
                    )
    except (tarfile.TarError, EOFError, OSError, zlib.error) as exc:
        raise InstallError(
            f"not a valid, fully downloaded gzip tarball (delete it and retry): {path} ({exc})"
        ) from exc
    if count == 0:
        raise InstallError(f"tarball is empty: {path}")



# The installer



class Installer:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self.url = config.url
        self.sha256 = config.sha256
        self.archive = config.download_dir / FALLBACK_ARCHIVE_NAME

    # plumbing 

    def _need(self, command: str) -> None:
        if shutil.which(command):
            return
        if self.cfg.dry_run:
            warn(f"required command not found: {command} (dry-run, continuing anyway)")
            return
        raise InstallError(f"required command not found: {command}")

    def _run(self, command: Sequence[str], *, privileged: bool = False) -> None:
        """Run command (prefixed with sudo when needed), or just print it in dry-run."""
        argv = list(command)
        if privileged and os.geteuid() != 0:
            argv.insert(0, "sudo")
        if self.cfg.dry_run:
            print(f"+ {shlex.join(argv)}")
            return
        try:
            subprocess.run(argv, check=True)
        except subprocess.CalledProcessError as exc:
            raise InstallError(
                f"command failed (exit {exc.returncode}): {shlex.join(argv)}"
            ) from exc
        except FileNotFoundError as exc:
            raise InstallError(f"required command not found: {argv[0]}") from exc

    # steps 

    def preflight(self) -> None:
        if not sys.platform.startswith("linux"):
            raise InstallError("this installer only supports Linux")
        if not os.environ.get("SHELL"):
            warn("SHELL is not set; PATH setup may be skipped")

        if os.geteuid() == 0:
            warn(f"running as root; files under {self.cfg.install_dir} will be owned by root")
        else:
            self._need("sudo")
        self._need("dnf")
        self._need("tar")

    def resolve_latest(self) -> None:
        """Read the current stable Linux URL and its published SHA-256 from
        developer.android.com. Scraping is best-effort: if the layout changes,
        the checksum lookup warns and the script still reports the computed hash
        after downloading."""
        log("reading the latest Android Studio release from developer.android.com")
        page = _get_text(STUDIO_PAGE)
        if not page:
            raise InstallError("could not fetch the Android Studio download page")
        flat = page.replace("\n", "")

        url_match = _URL_RE.search(flat)
        if not url_match:
            raise InstallError("could not find the Linux .tar.gz link on the download page")
        self.url = url_match.group(0)

        for context in _SHA_CONTEXT_RE.finditer(flat):
            hash_match = _HEX64_RE.search(context.group(1))
            if hash_match:
                self.sha256 = hash_match.group(0)
                break

        log(f"latest tarball: {archive_name_from_url(self.url)}")
        if self.sha256:
            log(f"published SHA-256: {self.sha256}")
        else:
            warn("could not read the published SHA-256; the computed hash will only be reported")

    def locate_existing_archive(self) -> None:
        """--skip-tarball-download: pick the newest android-studio*.tar.gz in the
        download directory. That matches both the upstream file name
        (android-studio-<version>-linux.tar.gz) and older runs of this tool."""
        directory = self.cfg.download_dir
        found = sorted(
            (p for p in directory.glob("android-studio*.tar.gz") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if found:
            self.archive = found[0]
            if len(found) > 1:
                warn(f"{len(found)} tarballs in {directory}; using the newest")
            log(f"skipping download; using existing tarball: {self.archive}")
        elif self.cfg.dry_run:
            warn(f"no android-studio*.tar.gz in {directory} (dry-run, continuing anyway)")
        else:
            raise InstallError(
                f"no android-studio*.tar.gz found in {directory} "
                "(set ANDROID_STUDIO_DOWNLOAD_DIR to the directory that holds it)"
            )

    def download_archive(self) -> None:
        self.archive = self.cfg.download_dir / archive_name_from_url(self.url)

        # dest only exists once a download has finished (partials live in
        # dest.part), so reuse it rather than fetching ~1 GB again.
        # verify_archive() still checks it.
        if self.archive.is_file():
            log(f"already downloaded: {self.archive} (delete it to force a fresh download)")
            return
        if self.cfg.dry_run:
            print(f"+ download {self.url} -> {self.archive}")
            return

        log(f"downloading {self.archive.name}")
        download(self.url, self.archive)

    def obtain_archive(self) -> None:
        if self.cfg.skip_download:
            self.locate_existing_archive()
            return
        if not self.url:
            self.resolve_latest()
        self.download_archive()

    def verify_archive(self) -> None:
        if self.cfg.dry_run:
            return
        if not self.archive.is_file():
            raise InstallError(f"tarball not found: {self.archive}")

        # Check the layout now, before extract_archive removes an existing install.
        check_tarball(self.archive)

        actual = sha256_of(self.archive)
        if not self.sha256:
            log(f"SHA-256 (not verified against a published value): {actual}")
        elif actual == self.sha256.lower():
            log("SHA-256 verified")
        else:
            raise InstallError(f"SHA-256 mismatch: expected {self.sha256}, got {actual}")

    def install_prerequisites(self) -> None:
        if self.cfg.full_update:
            self._run(["dnf", "update", "-y"], privileged=True)
        self._run(["dnf", "install", "-y", *PACKAGES], privileged=True)
        self._run(["systemctl", "enable", "--now", "libvirtd"], privileged=True)

        user = os.environ.get("USER") or getpass.getuser()
        if self._in_group(user, "libvirt"):
            log(f"user '{user}' is already in the libvirt group")
        else:
            self._run(["usermod", "-aG", "libvirt", user], privileged=True)
            log("log out and back in for the libvirt group membership to take effect")

    @staticmethod
    def _in_group(user: str, group: str) -> bool:
        try:
            result = subprocess.run(
                ["id", "-nG", user], capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0 and group in result.stdout.split()

    def extract_archive(self) -> None:
        install_dir = self.cfg.install_dir
        parent = install_dir.parent

        if os.path.lexists(install_dir):
            log(f"replacing existing installation at {install_dir}")
            self._run(["rm", "-rf", "--", str(install_dir)], privileged=True)

        self._run(["mkdir", "-p", "--", str(parent)], privileged=True)
        # --no-same-owner: files end up owned by whoever extracts (root), not by
        # whatever uid the tarball was packed with.
        self._run(
            ["tar", "-xzf", str(self.archive), "-C", str(parent), "--no-same-owner"],
            privileged=True,
        )

        if not self.cfg.dry_run and not install_dir.is_dir():
            raise InstallError(
                f"extraction did not create {install_dir} (is the archive layout correct?)"
            )

    def setup_path(self) -> None:
        bin_dir = self.cfg.install_dir / "bin"
        shell = Path(os.environ.get("SHELL", "")).name

        if shell in ("bash", "zsh"):
            rc_file = self.cfg.home / (".bashrc" if shell == "bash" else ".zshrc")
            line = f'export PATH="$PATH:{bin_dir}"'
        elif shell == "fish":
            rc_file = self.cfg.home / ".config" / "fish" / "config.fish"
            line = f"fish_add_path {shlex.quote(str(bin_dir))}"
        else:
            warn(f"unsupported shell '{shell or 'unknown'}'; add {bin_dir} to your PATH manually")
            return

        if self.cfg.dry_run:
            print(f"+ append to {rc_file}: {line}")
            return

        existing = (
            rc_file.read_text(encoding="utf-8", errors="replace")
            if rc_file.exists()
            else ""
        )
        if line in existing.splitlines():
            log(f"PATH entry already present in {rc_file}")
            return

        rc_file.parent.mkdir(parents=True, exist_ok=True)
        with rc_file.open("a", encoding="utf-8") as handle:
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(line + "\n")
        log(f"added {bin_dir} to PATH in {rc_file}")

    def maybe_launch(self) -> None:
        if self.cfg.no_launch:
            return

        studio = self.cfg.install_dir / "bin" / "studio.sh"
        if self.cfg.dry_run:
            print(f"+ offer to launch {studio}")
            return
        if not os.access(studio, os.X_OK):
            warn(f"studio.sh not found at {studio}; skipping launch")
            return
        if not sys.stdin.isatty():
            log("not a terminal; skipping the launch prompt")
            return

        try:
            answer = input("Launch Android Studio to complete setup? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            log("launching Android Studio")
            subprocess.Popen(
                [str(studio)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        else:
            log("skipping launch")

    # orchestration

    def run(self) -> None:
        self.preflight()
        # Get and verify the tarball first, so a bad file fails before anything
        # on the system is changed.
        self.obtain_archive()
        self.verify_archive()
        self.install_prerequisites()
        self.extract_archive()
        self.setup_path()
        self.maybe_launch()
        log("done")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        Installer(parse_args(argv)).run()
    except InstallError as exc:
        print(f"[{PROGRAM}] error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())