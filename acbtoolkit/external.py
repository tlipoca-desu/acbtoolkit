"""
Wrappers around the two external programs this toolkit drives.

Decoding is delegated to `vgmstream <https://github.com/vgmstream/vgmstream>`_
(handles CRI HCA and the AFS2/AWB containers) and encoding to FFmpeg.  Neither
is bundled: both are widely packaged, and shipping their binaries would bloat
the repository and drag in their own licences.  :func:`find_vgmstream` and
:func:`find_ffmpeg` look in the usual places, and every command takes an
explicit ``--vgmstream`` / ``--ffmpeg`` override or the ``VGMSTREAM`` /
``FFMPEG`` environment variables.

Two vgmstream behaviours are worth knowing, because both are worked around here:

* ``-o`` supports ``?s`` (subsong number) and ``?n`` (stream name) wildcards.
  ``?n`` looks convenient but fails on any stream whose name is empty -- in
  that case vgmstream falls back to using the whole input path as the name,
  and the resulting ``/`` characters make it refuse to write the file.  So
  output is always named with ``?s``, and names are recovered separately from
  the ``stream name:`` lines on stdout, which arrive in subsong order.

* Sub-song indices are **1-based** (``-s 1`` is the first), while the id
  stored in an .acb ``Waveform.StreamAwbId`` is **0-based**.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from contextlib import closing
from pathlib import Path

_WIN = sys.platform.startswith("win")
_EXE = ".exe" if _WIN else ""

_STREAM_NAME_RE = re.compile(r"^stream name:\s*(.*)$", re.MULTILINE)

# Long operations (a 48-stream bank, a full-length mix) can legitimately take
# minutes on slow disks; fail loudly rather than hang forever.
DEFAULT_TIMEOUT = 1800


class ToolError(RuntimeError):
    """An external program is missing or failed."""


# ==================================================================== discovery

def _candidates(name: str, dir_names: Sequence[str] = ()) -> list[Path]:
    """Places to look besides ``$PATH``, in priority order.

    `dir_names` lets a tool live in a subdirectory that does not exactly match
    its executable name -- the vgmstream release ships as ``tools/vgmstream/``
    but the binary is ``vgmstream-cli``.
    """
    exe = name + _EXE
    here = Path(__file__).resolve().parent
    roots = [here, here.parent, Path.cwd(), Path.cwd().parent]
    subdirs = [name, *dir_names]

    out: list[Path] = []
    for root in roots:
        out.append(root / "tools" / exe)
        out.append(root / "bin" / exe)
        out.append(root / exe)
        for sub in subdirs:
            out.append(root / "tools" / sub / exe)
            out.append(root / sub / exe)

    if _WIN:
        home = Path(os.environ.get("USERPROFILE", "~")).expanduser()
        program = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        out += [
            Path(r"D:\ffmpeg\bin") / exe,
            Path(r"C:\ffmpeg\bin") / exe,
            local / "Microsoft" / "WinGet" / "Links" / exe,
            home / "scoop" / "shims" / exe,
            program / "ffmpeg" / "bin" / exe,
        ]
    else:
        out += [
            Path("/usr/local/bin") / exe,
            Path("/usr/bin") / exe,
            Path("/opt/homebrew/bin") / exe,
            Path("/snap/bin") / exe,
            Path.home() / ".local" / "bin" / exe,
        ]
    return out


def _find(env_var: str, names: Sequence[str],
          dir_names: Sequence[str] = ()) -> Path | None:
    explicit = os.environ.get(env_var)
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
        for cand in _candidates(name, dir_names):
            if cand.is_file():
                return cand
    return None


def find_vgmstream(explicit: str | Path | None = None) -> Path:
    """Locate ``vgmstream-cli``.  Raises :class:`ToolError` with install hints."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        raise ToolError(f"vgmstream not found at {p}")
    found = _find("VGMSTREAM", ["vgmstream-cli", "vgmstream"],
                  dir_names=["vgmstream", "vgmstream-cli"])
    if found:
        return found
    raise ToolError(
        "vgmstream-cli not found.\n"
        "  Windows:  run  acbtoolkit fetch-tools   (downloads it for you)\n"
        "  Or grab it from https://github.com/vgmstream/vgmstream/releases\n"
        "  Then put it on PATH, drop it in ./tools/, or pass --vgmstream PATH\n"
        "  (the VGMSTREAM environment variable also works)"
    )


def find_ffmpeg(explicit: str | Path | None = None) -> Path:
    """Locate ``ffmpeg``.  Raises :class:`ToolError` with install hints."""
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        raise ToolError(f"ffmpeg not found at {p}")
    found = _find("FFMPEG", ["ffmpeg"], dir_names=["ffmpeg"])
    if found:
        return found
    raise ToolError(
        "ffmpeg not found.\n"
        "  Windows:  run  acbtoolkit fetch-tools ffmpeg   (~80 MB)\n"
        "  Or install from https://ffmpeg.org/download.html  (or: apt install\n"
        "  ffmpeg, brew install ffmpeg, winget install ffmpeg)\n"
        "  Then put it on PATH or pass --ffmpeg PATH"
    )


# ==================================================================== vgmstream

class Vgmstream:
    """Thin wrapper over ``vgmstream-cli``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ---------------------------------------------------------------- probing

    def _run(self, args: Sequence[str], timeout: int = DEFAULT_TIMEOUT) -> str:
        try:
            proc = subprocess.run(
                [str(self.path), *args],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"vgmstream timed out on {args[-1]}") from exc
        except OSError as exc:
            raise ToolError(f"cannot run {self.path}: {exc}") from exc
        return (proc.stdout or "") + (proc.stderr or "")

    def info(self, source: str | Path, subsong: int | None = None) -> dict | None:
        """Metadata for one subsong as a dict, or None when unreadable."""
        args = ["-m", "-I"]
        if subsong is not None:
            args += ["-s", str(subsong)]
        args.append(str(source))
        for line in self._run(args, timeout=120).splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None

    def subsong_count(self, source: str | Path) -> int:
        info = self.info(source)
        if not info:
            return 0
        return int((info.get("streamInfo") or {}).get("total") or 0)

    def stream_names(self, source: str | Path) -> list[str]:
        """Stream names in subsong order (empty strings where unnamed)."""
        return [m.strip() for m in _STREAM_NAME_RE.findall(self._run(
            ["-m", "-S", "0", str(source)], timeout=300))]

    # ---------------------------------------------------------------- decoding

    def extract(
        self,
        source: str | Path,
        outdir: str | Path,
        *,
        ignore_loop: bool = True,
        subsongs: Iterable[int] | None = None,
        name_with: str = "?s",
    ) -> list[Path]:
        """Decode to WAV, one file per subsong.  Returns the files written.

        `ignore_loop` writes each stream at its natural length; otherwise
        vgmstream renders two loop passes plus a fade, which is what you want
        for a looping BGM section.

        `subsongs` selects specific 1-based subsong numbers.  Note the flag
        distinction, which is easy to get wrong: ``-s`` chooses a *start*
        subsong, while ``-S`` chooses the *end*.  Requesting one stream with
        ``-S N`` would render subsongs 1..N and quietly hand back the first
        one instead of the one asked for.

        Files are returned in subsong order, so they can be paired with
        :meth:`stream_names` positionally.
        """
        outdir = Path(outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        # Clear only what this call is about to write (digit-named wavs).
        # A blanket *.wav sweep would also delete unrelated files the caller
        # keeps in the same directory, such as other decoded waveforms.
        for stale in outdir.glob("*.wav"):
            if stale.stem.isdigit():
                stale.unlink()

        args: list[str] = []
        if ignore_loop:
            args.append("-i")
        if subsongs is None:
            args += ["-S", "0"]                    # end=0 means "all"
        else:
            wanted = sorted({int(n) for n in subsongs})
            if not wanted:
                return []
            start, end = wanted[0], wanted[-1]
            args += ["-s", str(start)]
            # A contiguous run can be taken in one pass; gaps need one pass
            # each, since vgmstream has no way to skip a subsong mid-range.
            args += ["-S", str(end if end == start + len(wanted) - 1 else start)]
        args += ["-o", str(outdir / f"{name_with}.wav"), str(source)]

        self._run(args)

        files = [p for p in outdir.glob("*.wav") if p.stem.isdigit()]
        files.sort(key=lambda p: int(p.stem))
        if subsongs is not None:
            files = [p for p in files if int(p.stem) in set(wanted)]
        return files

    def extract_named(
        self, source: str | Path, outdir: str | Path, *, ignore_loop: bool = True
    ) -> list[tuple[str, Path]]:
        """Decode every subsong and pair each file with its stream name.

        Names come from a metadata pass rather than from ``?n``; see the module
        docstring for why.
        """
        names = self.stream_names(source)
        files = self.extract(source, outdir, ignore_loop=ignore_loop)
        out = []
        for i, path in enumerate(files):
            name = names[i] if i < len(names) and names[i] else ""
            out.append((name, path))
        return out


# ==================================================================== ffmpeg

# ffmpeg encoder settings per output format.
_FORMATS: dict[str, list[str]] = {
    "mp3":  ["-c:a", "libmp3lame"],
    "m4a":  ["-c:a", "aac"],
    "ogg":  ["-c:a", "libvorbis"],
    "opus": ["-c:a", "libopus"],
    "flac": ["-c:a", "flac", "-compression_level", "8"],
    "wav":  ["-c:a", "pcm_s16le"],
}

# Formats where -b:a means something; flac/wav are lossless and ignore it.
_BITRATE_FORMATS = {"mp3", "m4a", "ogg", "opus"}

SUPPORTED_FORMATS = tuple(_FORMATS)


class Ffmpeg:
    """Thin wrapper over ``ffmpeg``.

    Every invocation is size-guarded, because silence padding and filter
    graphs make it easy to write a command that never terminates, and
    ffmpeg's failure mode for that is to fill the disk.

    The cap has to depend on what is being written.  Intermediate WAVs are
    uncompressed -- a few minutes of 48 kHz stereo exceeds 40 MB -- while a
    finished lossy file is orders of magnitude smaller.  A single limit would
    either kill legitimate renders or fail to catch a runaway one, so
    :meth:`_run` picks one from the output's extension: `wav_limit_mb` for
    uncompressed intermediates, `max_output_mb` for everything else.
    """

    def __init__(self, path: str | Path, *, max_output_mb: int = 2048,
                 wav_limit_mb: int = 8192):
        self.path = Path(path)
        self.max_output_mb = max_output_mb
        self.wav_limit_mb = wav_limit_mb
        # Guard without a known destination: assume the smaller, safer cap.
        self._limit_bytes = max_output_mb * 1024 * 1024

    def _limit_for(self, dest: Path | None) -> int:
        if dest is not None and dest.suffix.lower() in (".wav", ".flac"):
            return self.wav_limit_mb * 1024 * 1024
        return self.max_output_mb * 1024 * 1024

    @staticmethod
    def _guess_output(args: Sequence[str]) -> Path | None:
        """Best-effort guess at the output path, for the size watchdog.

        This is only used to police runaway encodes, so a wrong guess is
        harmless (the guard simply does not fire); commands that know their
        destination pass it via `expect` instead.
        """
        for token in reversed(args):
            if token.startswith("-"):
                continue
            candidate = Path(token)
            if candidate.suffix and not Path(token).is_file():
                return candidate
        return None

    def _run(self, args: Sequence[str], timeout: int = DEFAULT_TIMEOUT,
             expect: str | Path | None = None) -> tuple[bool, str]:
        """Run ffmpeg, enforcing `timeout` and the output size cap.

        stderr goes to a temporary file rather than a pipe.  With a pipe, a
        chatty ffmpeg fills the OS buffer, blocks on write, and never exits --
        while a polling parent waits for an exit that cannot happen.  That
        deadlock is invisible (no output, no error) and looks exactly like a
        hang; a file has no such limit.
        """
        dest = Path(expect) if expect is not None else self._guess_output(args)
        limit = self._limit_for(dest)
        argv = [str(self.path), "-hide_banner", "-loglevel", "error", "-y", *args]

        with closing(tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                            errors="replace")) as err_file:
            try:
                proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                        stderr=err_file)
            except OSError as exc:
                raise ToolError(f"cannot run {self.path}: {exc}") from exc

            # Poll rather than wait(): gives a chance to notice a runaway file
            # and kill it long before it reaches the disk's limit.
            deadline = time.monotonic() + timeout
            killed_for_size = False
            while proc.poll() is None:
                if dest is not None and dest.is_file():
                    try:
                        if dest.stat().st_size > limit:
                            proc.kill()
                            killed_for_size = True
                            break
                    except OSError:
                        pass
                if time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise ToolError(
                        f"ffmpeg timed out after {timeout}s "
                        f"(last args: {' '.join(args[-3:])})")
                time.sleep(0.2)

            if killed_for_size:
                proc.wait()
            err_file.seek(0)
            stderr = err_file.read()

            if killed_for_size:
                # Remove the oversized partial output so it cannot be mistaken
                # for a finished file.
                try:
                    if dest is not None:
                        dest.unlink(missing_ok=True)
                except OSError:
                    pass
                raise ToolError(
                    f"aborted: {dest.name if dest else 'output'} exceeded its "
                    f"{limit // (1024 * 1024)} MB cap (runaway encode)")

            tail = (stderr or "").strip().splitlines()
            return proc.returncode == 0, (tail[-1] if tail else "")

    @staticmethod
    def encode_args(fmt: str, bitrate: int) -> list[str]:
        if fmt not in _FORMATS:
            raise ToolError(
                f"unsupported format {fmt!r}; choose from {', '.join(SUPPORTED_FORMATS)}")
        args = list(_FORMATS[fmt])
        if fmt in _BITRATE_FORMATS:
            args += ["-b:a", f"{bitrate}k"]
        return args

    def encode(self, source: str | Path, dest: str | Path,
               fmt: str = "mp3", bitrate: int = 192) -> Path:
        """Transcode one file."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok, err = self._run(
            ["-i", str(source), *self.encode_args(fmt, bitrate), str(dest)],
            expect=dest)
        if not ok or not dest.is_file():
            raise ToolError(f"encode failed for {Path(source).name}: {err}")
        return dest

    def mix(self, sources: Sequence[str | Path], dest: str | Path,
            *, offsets: Sequence[float] | None = None,
            durations: Sequence[float] | None = None,
            total: float | None = None) -> Path:
        """Overlay several files into one.

        `offsets` seek into each source before mixing and `durations` trim the
        amount taken from it; together they place a long sustained waveform
        under the section that follows it.  `total` pads or cuts the mix so it
        matches the target block length exactly.

        `total` is bounded on purpose.  Padding is only ever added together
        with an explicit `-t`, because ``apad`` with nothing to stop it pads
        forever -- ffmpeg will cheerfully fill the disk with one file.  The
        runaway guard in :meth:`_run` is the second line of defence: an output
        that somehow exceeds `TIMEOUT`/`estimate` limits aborts the process
        rather than being written to completion.
        """
        srcs = list(sources)
        if not srcs:
            raise ToolError("mix() needs at least one input")
        offsets = list(offsets or [0.0] * len(srcs))
        durations = list(durations or [None] * len(srcs))
        if total is not None:
            total = max(0.0, float(total))
        pad = total is not None

        args: list[str] = []
        for src, off, dur in zip(srcs, offsets, durations):
            if off and off > 0.0005:
                args += ["-ss", f"{off:.6f}"]
            if dur is not None:
                args += ["-t", f"{dur:.6f}"]
            args += ["-i", str(src)]

        if len(srcs) == 1:
            if pad:
                args += ["-af", "apad"]
        else:
            labels = "[0:a]" + "".join(f"[{i}:a]" for i in range(1, len(srcs)))
            chain = f"{labels}amix=inputs={len(srcs)}:normalize=0:duration=longest"
            if pad:
                chain += ",apad"
            args += ["-filter_complex", f"{chain}[a]", "-map", "[a]"]
        if total is not None:
            args += ["-t", f"{total:.6f}"]

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok, err = self._run([*args, "-c:a", "pcm_s16le", str(dest)], expect=dest)
        if not ok or not dest.is_file():
            raise ToolError(f"mix failed: {err}")
        return dest

    def silence(self, dest: str | Path, duration: float,
                rate: int = 44100, channels: int = 2) -> Path:
        """Write `duration` seconds of silence, for rests in a timeline."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        layout = "stereo" if channels == 2 else "mono"
        ok, err = self._run([
            "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl={layout}",
            "-t", f"{duration:.6f}", "-c:a", "pcm_s16le", str(dest),
        ], expect=dest)
        if not ok or not dest.is_file():
            raise ToolError(f"silence generation failed: {err}")
        return dest

    def concat(self, parts: Sequence[str | Path], dest: str | Path,
               fmt: str = "mp3", bitrate: int = 192) -> Path:
        """Join files end to end via the concat demuxer."""
        parts = list(parts)
        if not parts:
            raise ToolError("concat() needs at least one input")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # ffmpeg's list parser uses ' as its quote character, escaped as '\''
        def quote(p: Path) -> str:
            return str(p.resolve()).replace("'", "'\\''")

        listing = dest.parent / f".{dest.stem}.concat.txt"
        listing.write_text("".join(f"file '{quote(p)}'\n" for p in parts),
                           encoding="utf-8")
        try:
            ok, err = self._run([
                "-f", "concat", "-safe", "0", "-i", str(listing),
                *self.encode_args(fmt, bitrate), str(dest),
            ], expect=dest)
        finally:
            listing.unlink(missing_ok=True)
        if not ok or not dest.is_file():
            raise ToolError(f"concat failed: {err}")
        return dest

    def duration(self, source: str | Path) -> float:
        """Duration in seconds, or 0.0 when it cannot be read."""
        try:
            proc = subprocess.run(
                [str(self.path), "-hide_banner", "-i", str(source)],
                capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return 0.0
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", proc.stderr or "")
        if not match:
            return 0.0
        hours, minutes, seconds = match.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    def has_encoder(self, name: str) -> bool:
        """Whether this ffmpeg build includes a given encoder."""
        try:
            proc = subprocess.run([str(self.path), "-hide_banner", "-encoders"],
                                  capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return name in (proc.stdout or "")


def wav_duration(path: str | Path) -> float:
    """Duration of a RIFF/WAVE file without shelling out."""
    import wave
    try:
        with wave.open(str(path), "rb") as fh:
            rate = fh.getframerate()
            return fh.getnframes() / rate if rate else 0.0
    except (OSError, wave.Error):
        return 0.0
