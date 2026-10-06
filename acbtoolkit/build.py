"""
Song assembly: turn a block-based cue sheet back into a listenable track.

Block music is an interactive format.  The composer writes numbered sections
and a BlockSequence decides the order they play in; individual sections also
carry jump targets that the game re-routes at runtime -- a battle theme
switching between its calm and combat layers, a boss theme moving to a new
phase.  Nothing about the *sequence* is dynamic, though: it is written into
the cue sheet, so each cue can be flattened into one continuous piece.

The flattening has to respect three things that are easy to get wrong:

**Absolute time, not per-block clipping.**  A waveform may be longer than the
section that introduces it -- a sustained note under the following sections.
Placing material by absolute timestamp and letting it ring across boundaries
is what makes the result sound continuous.

**Simultaneous tracks.**  Several tracks in one block sound *together*, not in
sequence, so they are mixed rather than concatenated.

**Embedded vs streamed audio.**  A block's waveform may live in the external
wave bank or be embedded in the ACB itself; both appear inside one cue, so the
decoder has to pick the right container per waveform.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .acb import AcbError, AcbFile, Cue, Waveform, build_segments
from .external import Ffmpeg, ToolError, Vgmstream

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text: str, fallback: str = "unnamed") -> str:
    cleaned = _ILLEGAL.sub("_", text).strip().strip(".")
    return cleaned or fallback


@dataclass
class SongResult:
    acb: Path
    name: str = ""
    cue: str = ""
    file: Path | None = None
    blocks: int = 0
    waveforms: int = 0
    duration_s: float = 0.0
    loop_blocks: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed: float = 0.0


class Renderer:
    """Renders one cue of one ACB."""

    def __init__(self, acb: AcbFile, waveforms: list[Waveform],
                 vgm: Vgmstream, ffm: Ffmpeg, workdir: Path,
                 fmt: str = "mp3", bitrate: int = 192,
                 ignore_loop: bool = True):
        self.acb = acb
        self.waveforms = waveforms
        self.vgm = vgm
        self.ffm = ffm
        self.workdir = workdir
        self.fmt = fmt
        self.bitrate = bitrate
        self.ignore_loop = ignore_loop
        self._cache: dict[int, Path] = {}
        self.workdir.mkdir(parents=True, exist_ok=True)

    def waveform_wav(self, index: int) -> Path | None:
        """Decode one waveform to WAV, picking its container automatically."""
        if index in self._cache:
            return self._cache[index]
        if not (0 <= index < len(self.waveforms)):
            return None
        wf = self.waveforms[index]
        source: Path | None = None
        subsong: int | None = None

        if wf.stream_id is not None:
            source = self.acb.awb_path
            subsong = wf.stream_id + 1        # vgmstream subsongs are 1-based
        elif wf.memory_id is not None:
            source = self.acb.path            # audio embedded in the ACB
            subsong = wf.memory_id + 1
        if source is None or subsong is None:
            return None

        out = self.workdir / f"wf{index:04d}.wav"
        # Decode into a private subdirectory.  Several waveforms share one
        # workdir, and an extraction run must not disturb its neighbours.
        scratch = self.workdir / f"_src{index:04d}"
        try:
            written = self.vgm.extract(source, scratch, subsongs=[subsong],
                                       ignore_loop=self.ignore_loop)
        except ToolError:
            return None
        if not written:
            return None
        written[0].replace(out)
        shutil.rmtree(scratch, ignore_errors=True)
        self._cache[index] = out
        return out

    def render(self, cue: Cue, dest: Path) -> tuple[bool, float]:
        """Flatten one cue and encode it.  Returns ``(ok, duration)``."""
        segments, total = build_segments(cue, self.waveforms)
        if not segments:
            return False, 0.0

        segdir = self.workdir / "segments"
        if segdir.exists():
            shutil.rmtree(segdir, ignore_errors=True)
        segdir.mkdir(parents=True, exist_ok=True)

        parts: list[Path] = []
        for i, seg in enumerate(segments):
            target = segdir / f"{i:04d}.wav"
            sources, offsets, durations = [], [], []
            for wf_index, offset, length in seg.parts:
                wav = self.waveform_wav(wf_index)
                if wav is not None:
                    sources.append(wav)
                    offsets.append(offset)
                    durations.append(length)
            try:
                if sources:
                    self.ffm.mix(sources, target, offsets=offsets,
                                 durations=durations, total=seg.duration_s)
                else:
                    # A rest: keep the gap so the blocks after it stay in place
                    # instead of sliding earlier.
                    rate = next((w.rate for w in self.waveforms if w.rate), 44100)
                    self.ffm.silence(target, seg.duration_s, rate=rate)
            except ToolError:
                continue
            parts.append(target)

        if not parts:
            return False, 0.0

        dest.parent.mkdir(parents=True, exist_ok=True)
        self.ffm.concat(parts, dest, fmt=self.fmt, bitrate=self.bitrate)
        return True, total


def _render_flat(cue: Cue, acb: AcbFile, renderer: Renderer, ffm: Ffmpeg,
                 target: Path, fmt: str, bitrate: int) -> float:
    """Encode a cue that has no timeline: just its referenced waveforms.

    Returns the resulting duration.  Raises :class:`ToolError` when the cue
    references nothing renderable, so the caller reports it like any other
    failure instead of silently writing an empty file.
    """
    sources: list[Path] = []
    for wf_index in acb.cue_waveforms(cue):
        wav = renderer.waveform_wav(wf_index)
        if wav is not None:
            sources.append(wav)
    if not sources:
        raise ToolError(f"cue {cue.name!r} references no playable waveform")

    mixed = renderer.workdir / "flat.wav"
    ffm.mix(sources, mixed)
    ffm.encode(mixed, target, fmt=fmt, bitrate=bitrate)
    return ffm.duration(target)


def _analyse(cue: Cue, waveforms: list[Waveform],
             segments) -> list[str]:
    """Non-fatal observations worth surfacing to the user."""
    notes: list[str] = []
    for seg in segments:
        if not seg.parts:
            notes.append(f"block {seg.block_name}: {seg.duration_s:.2f}s rest")
            continue
        gap = seg.gap_s
        if gap > 0.3:
            # A block longer than its material is suspicious: a rest would
            # have no parts at all, so this points at missing audio.
            notes.append(
                f"block {seg.block_name}: {gap:.2f}s of silence at the end "
                f"(material is shorter than the block)")
    if cue.loop_blocks:
        names = ", ".join(str(b.name) for b in cue.loop_blocks)
        notes.append(f"loopable blocks: {names} (rendered once)")
    return notes


def build_bank(acb_path: Path, outdir: Path, vgm: Vgmstream, ffm: Ffmpeg,
               *, fmt: str = "mp3", bitrate: int = 192,
               ignore_loop: bool = True,
               workdir: Path | None = None) -> list[SongResult]:
    """Render every cue in one ACB.

    Intermediate WAVs are large (uncompressed), so when no explicit `workdir`
    is given one is created per call and removed on the way out.  It is placed
    beside the output rather than in the system temp directory, so a long run
    cannot exhaust a small system drive.
    """
    started = time.time()
    results: list[SongResult] = []
    stem = safe_name(acb_path.stem)

    if workdir is None:
        outdir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=f".{stem}_", dir=outdir))
        own_tmp = True
    else:
        tmp = Path(workdir)
        tmp.mkdir(parents=True, exist_ok=True)
        own_tmp = False

    try:
        acb = AcbFile(acb_path)
        waveforms = acb.waveforms()
        cues = acb.cues()
        if not cues:
            return [SongResult(acb=acb_path, name=stem,
                               error="no cues found")]

        multiple = len(cues) > 1
        for cue in cues:
            res = SongResult(acb=acb_path, name=stem, cue=cue.name,
                             blocks=len(cue.blocks),
                             waveforms=len(waveforms))
            target_name = (f"{stem}_{safe_name(cue.name)}.{fmt}" if multiple
                           else f"{stem}.{fmt}")
            target = outdir / target_name

            segments, total = build_segments(cue, waveforms)
            if not segments:
                # ReferenceType 1/2/3 cues point straight at waveforms instead
                # of a BlockSequence, so there is no timeline to flatten: the
                # referenced streams already *are* the track.
                renderer = Renderer(acb, waveforms, vgm, ffm, tmp / stem,
                                    fmt, bitrate, ignore_loop)
                try:
                    res.duration_s = _render_flat(cue, acb, renderer, ffm,
                                                  target, fmt, bitrate)
                    res.file = target
                except ToolError as exc:
                    res.error = str(exc)[:140] or "nothing to render"
                res.elapsed = time.time() - started
                results.append(res)
                continue

            renderer = Renderer(acb, waveforms, vgm, ffm, tmp / stem,
                                fmt, bitrate, ignore_loop)
            res.notes = _analyse(cue, waveforms, segments)
            res.loop_blocks = [b.name for b in cue.loop_blocks]
            try:
                ok, duration = renderer.render(cue, target)
                if ok:
                    res.file = target
                    res.duration_s = duration
                else:
                    res.error = "no audio produced"
            except ToolError as exc:
                res.error = str(exc)[:140]
            res.elapsed = time.time() - started
            results.append(res)
    except (AcbError, OSError) as exc:
        results.append(SongResult(acb=acb_path, name=stem,
                                  error=str(exc)[:140]))
    finally:
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return results


def find_banks(root: Path, patterns: Sequence[str] = ()) -> list[Path]:
    files = sorted(root.glob("*.acb"))
    if not patterns:
        return files
    return [p for p in files
            if any(fnmatch(p.stem, pat) or fnmatch(p.name, pat)
                   for pat in patterns)]


def run(cfg) -> int:
    """CLI entry point."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    vgm = Vgmstream(cfg.vgmstream)
    ffm = Ffmpeg(cfg.ffmpeg)

    # The CLI already resolved the selection; find_banks() is the library
    # fallback for callers that pass a directory directly.
    jobs = getattr(cfg, "banks", None) or find_banks(cfg.input, cfg.patterns)
    if not jobs:
        print(f"no .acb files matched in {cfg.input}")
        return 1

    print("=" * 78)
    print(f"  building songs from {len(jobs)} cue sheet(s)")
    print(f"  output  {cfg.output}  [{cfg.fmt}]")
    if cfg.dry_run:
        print("  dry run: parsing only, nothing will be written")
    print("=" * 78)
    songs: list[SongResult] = []
    warnings: list[str] = []
    started = time.time()

    if cfg.dry_run:
        for acb_path in jobs:
            try:
                acb = AcbFile(acb_path)
                waveforms = acb.waveforms()
                for cue in acb.cues():
                    segments, total = build_segments(cue, waveforms)
                    songs.append(SongResult(
                        acb=acb_path, name=safe_name(acb_path.stem),
                        cue=cue.name, blocks=len(cue.blocks),
                        waveforms=len(waveforms), duration_s=total,
                        loop_blocks=[b.name for b in cue.loop_blocks]))
                    warnings += [f"{acb_path.stem}/{cue.name}: {n}"
                                 for n in _analyse(cue, waveforms, segments)]
            except (AcbError, OSError) as exc:
                songs.append(SongResult(acb=acb_path,
                                        name=safe_name(acb_path.stem),
                                        error=str(exc)[:140]))
    else:
        cfg.output.mkdir(parents=True, exist_ok=True)
        # Each bank gets its own scratch directory next to the output and
        # cleans it up as soon as it is done, so peak disk use stays bounded
        # by the number of parallel workers rather than the whole batch.
        with ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as pool:
            futures = {
                pool.submit(build_bank, acb_path, cfg.output, vgm, ffm,
                            fmt=cfg.fmt, bitrate=cfg.bitrate,
                            ignore_loop=not cfg.loop): acb_path
                for acb_path in jobs
            }
            for done, future in enumerate(as_completed(futures), 1):
                for res in future.result():
                    songs.append(res)
                    if res.error:
                        print(f"[{done:>3}/{len(jobs)}] ! {res.name:<28} "
                              f"{res.cue:<16} {res.error}")
                    else:
                        loops = (f" loops={len(res.loop_blocks)}"
                                 if res.loop_blocks else "")
                        print(f"[{done:>3}/{len(jobs)}] + {res.name:<28} "
                              f"{res.cue:<16} {res.blocks:>3} blocks "
                              f"{res.duration_s:>7.1f}s{loops}")
                warnings += [f"{r.name}/{r.cue}: {n}"
                             for r in future.result() for n in r.notes]

    ok = [s for s in songs if s.file or (cfg.dry_run and not s.error)]
    bad = [s for s in songs if s.error]
    total_s = sum(s.duration_s for s in songs)

    print("=" * 78)
    print(f"  {len(ok)} song(s), {len(bad)} failed, "
          f"{total_s / 60:.1f} minutes total, {time.time() - started:.1f}s")
    if warnings:
        print(f"\n  {len(warnings)} note(s):")
        for line in warnings[:20]:
            print(f"    - {line}")
        if len(warnings) > 20:
            print(f"    ... and {len(warnings) - 20} more")
    if not cfg.dry_run:
        print(f"\n  {cfg.output}")
    print("=" * 78)
    return 0 if not bad else 1
