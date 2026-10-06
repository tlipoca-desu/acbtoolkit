"""
Fetch external tools that are not bundled.

Both tools publish ready-to-run Windows builds behind stable "always the
newest release" URLs, so a fresh Windows machine can bootstrap itself with
nothing but the standard library:

* vgmstream — GitHub's ``releases/latest/download/`` permalink:

      https://github.com/vgmstream/vgmstream/releases/latest/download/vgmstream-win64.zip

* ffmpeg — the permalink for the recommended Windows "essentials" build
  (https://www.gyan.dev/ffmpeg/builds/, linked from ffmpeg.org):

      https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip

Downloads are unpacked into a staging directory, smoke-checked, and only then
swapped into ``tools/<tool>/`` -- so a broken download can never clobber a
working install.

Official prebuilt releases exist for Windows only.  On other platforms,
install these tools from a package manager; the errors below say as much
rather than pretending the download would work.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

VGMSTREAM_RELEASE_URL = (
    "https://github.com/vgmstream/vgmstream/releases/latest/download/"
    "vgmstream-win64.zip"
)
FFMPEG_RELEASE_URL = (
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)

NON_WINDOWS_HINT = (
    "vgmstream auto-install ships Windows binaries only -- that is where the "
    "upstream project publishes prebuilt releases.  On this platform, install "
    "it from your package manager (apt install vgmstream, brew install "
    "vgmstream, ...) or build from source: "
    "https://github.com/vgmstream/vgmstream/blob/master/doc/BUILD.md"
)
FFMPEG_NON_WINDOWS_HINT = (
    "ffmpeg auto-install ships Windows binaries only.  On this platform, "
    "install it from your package manager (apt install ffmpeg, brew install "
    "ffmpeg, ...) or grab a build from https://ffmpeg.org/download.html"
)


class FetchError(RuntimeError):
    """Downloading or installing a tool went wrong."""


def _download(url: str, dest: Path, timeout: int = 120) -> None:
    """Stream `url` to `dest`.  `timeout` bounds each socket operation."""
    request = urllib.request.Request(url, headers={"User-Agent": "acbtoolkit"})
    with urllib.request.urlopen(request, timeout=timeout) as response, \
            open(dest, "wb") as fh:
        shutil.copyfileobj(response, fh)


def _extract_zip(archive: Path, staging: Path) -> None:
    """Unpack `archive` into `staging`, refusing members that escape it."""
    with zipfile.ZipFile(archive) as zf:
        resolved_root = staging.resolve()
        for member in zf.namelist():
            target = (staging / member).resolve()
            if not target.is_relative_to(resolved_root):
                raise FetchError(
                    f"refusing to extract {member!r}: it escapes the "
                    f"installation directory")
        zf.extractall(staging)


def _smoke_check_vgmstream(exe: Path) -> None:
    """Run the binary's own version check, so a broken download stops here.

    ``vgmstream-cli -V`` prints a JSON version blob but exits 1 -- the CLI
    signals "no input given" that way even for informational flags -- so the
    check looks at the output rather than the exit code.
    """
    try:
        proc = subprocess.run([str(exe), "-V"], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FetchError(f"downloaded vgmstream failed to run: {exc}") from exc
    if '"version"' not in (proc.stdout or ""):
        raise FetchError(
            "downloaded vgmstream did not report a version "
            f"(exit code {proc.returncode})")


def _smoke_check_ffmpeg(exe: Path) -> None:
    """``ffmpeg -version`` prints its banner to stdout and exits 0."""
    try:
        proc = subprocess.run([str(exe), "-version"], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FetchError(f"downloaded ffmpeg failed to run: {exc}") from exc
    if "ffmpeg version" not in (proc.stdout or ""):
        raise FetchError(
            "downloaded ffmpeg did not report a version "
            f"(exit code {proc.returncode})")


def _extract_vgmstream(archive: Path, staging: Path) -> None:
    """Unpack and flatten a single wrapper folder, if the archive has one."""
    _extract_zip(archive, staging)
    exe = staging / "vgmstream-cli.exe"
    if exe.is_file():
        return
    nested = [d / "vgmstream-cli.exe" for d in sorted(staging.iterdir())
              if d.is_dir() and (d / "vgmstream-cli.exe").is_file()]
    if len(nested) == 1:
        for item in nested[0].parent.iterdir():
            shutil.move(str(item), str(staging / item.name))
        nested[0].parent.rmdir()


def _extract_ffmpeg(archive: Path, staging: Path) -> None:
    """Unpack and hoist ``bin/`` to the staging root.

    Windows builds keep everything in one ``bin/`` directory (exe plus the
    DLLs it needs beside it); the surrounding folders are docs and presets.
    """
    _extract_zip(archive, staging)
    matches = sorted(staging.glob("*/bin/ffmpeg.exe")) \
        or sorted(staging.glob("**/ffmpeg.exe"))
    if not matches:
        raise FetchError("archive does not contain ffmpeg.exe")
    bin_dir = matches[0].parent
    if bin_dir != staging:
        for item in bin_dir.iterdir():
            shutil.move(str(item), str(staging / item.name))
        for leftover in sorted(staging.iterdir()):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)


def _install_from_archive(
    tools_dir: str | Path, *, dir_name: str, exe_name: str, url: str,
    unpack, smoke, force: bool, hint: str,
) -> Path:
    """Shared install flow: download → unpack → smoke-check → swap in."""
    if not sys.platform.startswith("win"):
        raise FetchError(hint)

    tools_dir = Path(tools_dir)
    dest = tools_dir / dir_name
    if (dest / exe_name).is_file() and not force:
        return dest

    tools_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="acbtoolkit_") as td:
        staging = Path(td) / dir_name
        staging.mkdir()
        archive = Path(td) / "tool.zip"

        try:
            _download(url, archive)
        except urllib.error.HTTPError as exc:
            raise FetchError(
                f"download failed: HTTP {exc.code} from {url}") from exc
        except OSError as exc:
            raise FetchError(f"download failed: {exc}") from exc

        size_mb = archive.stat().st_size / (1024 * 1024)
        if not zipfile.is_zipfile(archive):
            raise FetchError(
                f"downloaded file is not a zip archive "
                f"({size_mb:.1f} MB) -- the source may have moved")

        unpack(archive, staging)
        exe = staging / exe_name
        if not exe.is_file():
            raise FetchError(f"archive does not contain {exe_name}")
        smoke(exe)

        # Verified; only now touch the real install location.
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(staging), str(dest))

    return dest


def install_vgmstream(tools_dir: str | Path, *, force: bool = False,
                      timeout: int = 600) -> Path:
    """Install vgmstream into ``tools_dir/vgmstream``.

    Returns the installation directory.  An existing, working install is
    reused unless `force` is set.
    """
    return _install_from_archive(
        tools_dir,
        dir_name="vgmstream", exe_name="vgmstream-cli.exe",
        url=VGMSTREAM_RELEASE_URL,
        unpack=_extract_vgmstream, smoke=_smoke_check_vgmstream,
        force=force, hint=NON_WINDOWS_HINT,
    )


def install_ffmpeg(tools_dir: str | Path, *, force: bool = False,
                   timeout: int = 600) -> Path:
    """Install ffmpeg into ``tools_dir/ffmpeg``.

    The essentials build is about 80 MB to download.  An existing, working
    install is reused unless `force` is set.
    """
    return _install_from_archive(
        tools_dir,
        dir_name="ffmpeg", exe_name="ffmpeg.exe",
        url=FFMPEG_RELEASE_URL,
        unpack=_extract_ffmpeg, smoke=_smoke_check_ffmpeg,
        force=force, hint=FFMPEG_NON_WINDOWS_HINT,
    )
