"""
Bulk extraction: .acb / .awb -> audio files.

Handles all three layouts seen in the wild:

**external bank**
    ``name.acb`` + ``name.awb``.  The cue sheet references audio in the wave
    bank.  Most BGM lives here.
**embedded bank**
    ``name.acb`` alone, with the audio inside the ACB itself.  Sounds are
    usually short enough for this.
**hybrid**
    Both.  The two containers hold *different* audio -- verified by comparing
    per-stream content hashes, they do not overlap.  Extracting only one of
    them silently loses material, so both are decoded and merged.

Naming is taken from the ACB's cue table, which is also why a sibling .acb
must stay next to its .awb: vgmstream uses it to resolve stream names, and
without it you only get bare numbers.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .acb import AcbError
from .external import Ffmpeg, ToolError, Vgmstream

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str = "unnamed") -> str:
    """Make a string usable as a filename on every platform."""
    cleaned = _ILLEGAL.sub("_", text).strip().strip(".")
    return cleaned or fallback


@dataclass
class BankResult:
    """Outcome of extracting one ACB."""

    acb: Path
    outdir: Path
    files: list[Path] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    note: str = ""
    error: str | None = None
    elapsed: float = 0.0

    @property
    def count(self) -> int:
        return len(self.files)


def _content_hash(wav: Path) -> str:
    """Hash the PCM payload, ignoring container headers."""
    blob = wav.read_bytes()
    start = blob.find(b"data")
    return hashlib.md5(blob[start + 8:] if start >= 0 else blob).hexdigest()


def _sources_for(acb: Path) -> list[tuple[Path, str]]:
    """Containers to decode for one cue sheet.

    Both are attempted whenever an AWB exists: an ACB may reference an
    external bank while also embedding its own short sounds (hybrid), and a
    cue-sheet-only ACB simply yields nothing from its own pass.
    """
    awb = acb.with_suffix(".awb")
    pairs = [(acb, "acb")]
    if awb.is_file():
        pairs.append((awb, "awb"))
    return pairs


def extract_bank(
    acb: Path,
    outdir: Path | None,
    vgm: Vgmstream,
    ffm: Ffmpeg,
    *,
    fmt: str = "mp3",
    bitrate: int = 192,
    ignore_loop: bool = True,
    workdir: Path | None = None,
) -> BankResult:
    """Decode every stream an ACB can reach into `outdir`.

    When `outdir` is omitted the result goes to ``./<name>_audio`` in the
    current directory -- deliberately *not* next to the source file, so no
    code path can write into the game's own folder.

    Scratch space defaults to a temporary directory beside the output and is
    removed on the way out; see the note in :func:`acbtoolkit.build.build_bank`
    for why it is not the system temp directory.
    """
    started = time.time()
    name = safe_name(acb.stem)
    dest = Path(outdir) / name if outdir else Path.cwd() / f"{name}_audio"
    result = BankResult(acb=acb, outdir=dest)

    if workdir is None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{name}_", dir=dest.parent))
        own_tmp = True
    else:
        tmp = Path(workdir)
        tmp.mkdir(parents=True, exist_ok=True)
        own_tmp = False

    try:
        streams: list[tuple[str, Path]] = []
        seen: dict[str, str] = {}          # content hash -> source label
        duplicates = 0

        for source, label in _sources_for(acb):
            try:
                found = vgm.extract_named(
                    source, tmp / label, ignore_loop=ignore_loop)
            except ToolError:
                continue                    # cue-sheet-only ACB, or no audio
            for stream_name, wav in found:
                digest = _content_hash(wav)
                if digest in seen and seen[digest] != label:
                    duplicates += 1         # same audio in both containers
                    continue
                seen[digest] = label
                streams.append((stream_name or f"{label}{len(streams) + 1}", wav))

        if not streams:
            result.error = "no decodable audio (cue-sheet only)"
            return result

        dest.mkdir(parents=True, exist_ok=True)
        for index, (stream_name, wav) in enumerate(streams, 1):
            target = dest / f"{index}_{safe_name(stream_name)}.{fmt}"
            ffm.encode(wav, target, fmt=fmt, bitrate=bitrate)
            result.files.append(target)

        labels = sorted(set(seen.values()))
        result.sources = labels
        result.note = "+".join(label.upper() for label in labels)
        if duplicates:
            result.note += f" (dedup {duplicates})"
    except (AcbError, ToolError, OSError) as exc:
        result.error = str(exc)[:160]
    finally:
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        result.elapsed = time.time() - started
    return result


def find_banks(root: Path, patterns: Sequence[str] = ()) -> list[Path]:
    """ACB files under `root`, filtered by shell-style patterns on the stem."""
    files = sorted(root.glob("*.acb"))
    if not patterns:
        return files
    return [p for p in files
            if any(fnmatch(p.stem, pat) or fnmatch(p.name, pat)
                   for pat in patterns)]


def run(cfg) -> int:
    """CLI entry point.  `cfg` is the parsed argparse namespace."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    vgm = Vgmstream(cfg.vgmstream)
    ffm = Ffmpeg(cfg.ffmpeg)

    # The CLI already resolved the selection; find_banks() is the library
    # fallback for callers that pass a directory directly.
    jobs = getattr(cfg, "banks", None) or find_banks(cfg.input, cfg.patterns)
    if not jobs:
        print(f"no .acb files matched in {cfg.input}")
        return 1

    with_awb = sum(1 for a in jobs if a.with_suffix(".awb").is_file())
    print("=" * 78)
    print(f"  extracting {len(jobs)} cue sheet(s)  "
          f"({with_awb} with a wave bank, {len(jobs) - with_awb} embedded)")
    print(f"  output    {cfg.output}  [{cfg.fmt}"
          + (f" {cfg.bitrate}k]" if cfg.fmt in ("mp3", "m4a", "ogg", "opus") else "]"))
    print(f"  decoder   {vgm.path}")
    print(f"  encoder   {ffm.path}")
    print("=" * 78)

    cfg.output.mkdir(parents=True, exist_ok=True)
    ok = failed = total = 0
    started = time.time()

    # Scratch space is per job and lives beside the output, so peak disk usage
    # tracks the worker count instead of the size of the whole batch.
    with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as pool:
        futures = {
            pool.submit(
                extract_bank, acb, cfg.output, vgm, ffm,
                fmt=cfg.fmt, bitrate=cfg.bitrate,
                ignore_loop=not cfg.loop,
            ): acb
            for acb in jobs
        }
        for done, future in enumerate(as_completed(futures), 1):
            res = future.result()
            stem = safe_name(res.acb.stem)
            if res.error:
                failed += 1
                print(f"[{done:>3}/{len(jobs)}] ! {stem:<34} {res.error}")
            else:
                ok += 1
                total += res.count
                print(f"[{done:>3}/{len(jobs)}] + {stem:<34} "
                      f"{res.note:<12} {res.count:>4} files  {res.elapsed:.1f}s")
    print("=" * 78)
    print(f"  {ok} ok, {failed} failed, {total} audio files, "
          f"{time.time() - started:.1f}s")
    print(f"  {cfg.output}")
    print("=" * 78)
    return 0 if failed == 0 else 1
