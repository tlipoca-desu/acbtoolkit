"""
Semantic model for CRI .acb cue sheets.

An .acb answers "what should play, when".  It does not contain the audio
unless the sounds are small enough to be embedded (see :attr:`Waveform.memory_id`).

The graph is::

    CueName -> Cue -> BlockSequence -> Block -> Track -> TrackEvent
                                            -> Synth -> Waveform

``Cue.ReferenceType`` selects how a cue resolves its audio.  Real cue sheets
mostly use:

===== ===============================================================
0x01  Cue > Waveform
0x02  Cue > Synth > Waveform
0x03  Cue > Sequence > Track > TrackEvent > Command > Synth > Waveform
0x08  Cue > BlockSequence > Block > Track > TrackEvent > Command
             > Synth > Waveform      (interactive/block music, e.g. BGM)
===== ===============================================================

Smaller values are simpler; 0x08 is the interesting one because a
BlockSequence is a *playlist* of Blocks, optionally with jump targets that the
game re-routes at runtime (battle phase changes, and so on).

Tracks carry no direct waveform reference.  The link goes through a
``TrackEvent`` command blob, which is a stream of TLV records; the ``noteOn``
codes (2000, 2003) reference a Synth or Waveform.  :class:`AcbFile` follows
that chain so you do not have to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .cri_utf import UtfError, UtfTable

# Cue.ReferenceType
REF_WAVEFORM = 0x01
REF_SYNTH = 0x02
REF_SEQUENCE = 0x03
REF_BLOCK_SEQUENCE = 0x08

# Synth/TrackEvent reference item types
ITEM_WAVEFORM = 0x01
ITEM_SYNTH = 0x02
ITEM_SEQUENCE = 0x03

# TrackEvent command codes that start a sound.
# 2000 = noteOn, 2003 = noteOnWithNo (same payload, one extra field).
NOTE_ON_CODES = (2000, 2003)

# TrackEvent code carrying how long the preceding noteOn lasts, in ms.
NOTE_DURATION_CODE = 2001

# PlaybackType as stored on a Block.  0 renders once; non-zero values mark
# blocks that the runtime can repeat, which is how looping sections are built.
PLAYBACK_ONCE = 0

_UNSET16 = 0xFFFF


@dataclass
class Waveform:
    """One entry of the Waveform table."""

    index: int
    stream_id: int | None      # 0-based index into the external .awb, else None
    memory_id: int | None      # 0-based index into the embedded awb, else None
    rate: int
    samples: int
    loop_flag: int

    @property
    def seconds(self) -> float:
        return self.samples / self.rate if self.rate else 0.0

    @property
    def embedded(self) -> bool:
        """True when the audio lives inside the .acb rather than an .awb."""
        return self.memory_id is not None and self.stream_id is None

    def __str__(self) -> str:
        where = (f"awb#{self.stream_id}" if self.stream_id is not None
                 else f"embedded#{self.memory_id}" if self.memory_id is not None
                 else "unresolved")
        return f"wf{self.index}({where}, {self.seconds:.3f}s)"


@dataclass
class Note:
    """One noteOn placed on a block's timeline.

    `position_ms` is the offset inside the block, computed by laying a track's
    notes end to end.  `duration_ms` is how long the note sounds; 0 means the
    command stream did not say, in which case the waveform's own length is
    used.  A note may carry several waveforms (a Synth referencing more than
    one item sounds them together).
    """

    waveforms: tuple[int, ...]
    position_ms: int
    duration_ms: int

    @property
    def has_duration(self) -> bool:
        return self.duration_ms > 0


@dataclass
class Block:
    """One entry of the Block table: a musical section."""

    index: int
    name: int
    start_ms: int
    length_ms: int
    playback_type: int
    track_indices: list[int] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    destinations: list[int] = field(default_factory=list)

    @property
    def waveform_indices(self) -> list[int]:
        """Waveforms this block plays, de-duplicated, in first-use order."""
        seen: set[int] = set()
        out: list[int] = []
        for note in self.notes:
            for wf in note.waveforms:
                if wf not in seen:
                    seen.add(wf)
                    out.append(wf)
        return out

    @property
    def loops(self) -> bool:
        return self.playback_type != PLAYBACK_ONCE or self.index in self.destinations

    @property
    def silent(self) -> bool:
        """A block with a duration but no noteOn commands -- a written rest."""
        return not self.notes

    @property
    def length_s(self) -> float:
        return self.length_ms / 1000.0

    def __str__(self) -> str:
        tag = f" -> blk{self.destinations[0]}" if self.destinations else ""
        tag += " [loop]" if self.loops else ""
        return (f"blk{self.index}(name={self.name}, {self.length_s:.2f}s)"
                f"{tag}")


@dataclass
class Cue:
    """A named entry point.  This is what the game asks for by name."""

    index: int
    name: str
    reference_type: int
    blocks: list[Block] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        if not self.blocks:
            return 0.0
        return max(b.start_ms + b.length_ms for b in self.blocks) / 1000.0

    @property
    def loop_blocks(self) -> list[Block]:
        return [b for b in self.blocks if b.loops]

    @property
    def is_block_music(self) -> bool:
        return self.reference_type == REF_BLOCK_SEQUENCE

    def __str__(self) -> str:
        return (f"cue[{self.name}] {len(self.blocks)} blocks "
                f"{self.duration_s:.1f}s")


@dataclass
class Segment:
    """A slice of the final timeline: what to play from `start` for `duration`."""

    start_s: float
    duration_s: float
    parts: list[tuple[int, float, float]]   # (waveform index, source offset, length)
    block_name: int = 0
    playback_type: int = PLAYBACK_ONCE

    @property
    def loops(self) -> bool:
        return self.playback_type != PLAYBACK_ONCE

    @property
    def material_s(self) -> float:
        """Furthest point covered by source material, relative to segment start."""
        return max((off + dur for _, off, dur in self.parts), default=0.0)

    @property
    def gap_s(self) -> float:
        """Uncovered tail.  Non-zero only for written rests, never for real audio."""
        return max(0.0, self.duration_s - self.material_s)


class AcbError(ValueError):
    """Raised when a file is not a usable .acb."""


def _blocks_of(sequence_index: int, sequences: list[list[int]],
               blocks: dict[int, Block]) -> list[Block]:
    """Resolve a BlockSequence row into Block objects, skipping bad indices."""
    if not (0 <= sequence_index < len(sequences)):
        return []
    return [blocks[i] for i in sequences[sequence_index] if i in blocks]


class AcbFile:
    """Parsed .acb.  Instantiate once, then query cues/waveforms/blocks."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self._data = self.path.read_bytes()
            self.header = UtfTable(self._data, 0)
        except (OSError, UtfError) as exc:
            raise AcbError(f"cannot read {self.path}: {exc}") from exc

        if self.header.name != "Header":
            raise AcbError(f"{self.path.name}: root table is "
                           f"{self.header.name!r}, expected 'Header'")

        self.tables: dict[str, UtfTable] = {}
        for _, t in self.header.walk():
            if t.name and t.name not in self.tables:
                self.tables[t.name] = t

    # ---------------------------------------------------------------- basics

    def table(self, name: str) -> UtfTable | None:
        return self.tables.get(name)

    @property
    def name(self) -> str:
        return self.header.text(0, "Name") or self.path.stem

    @property
    def version(self) -> str:
        raw = self.header.text(0, "VersionString")
        return raw.strip().splitlines()[0] if raw else ""

    @property
    def awb_path(self) -> Path | None:
        """The external .awb next to this .acb, when present."""
        cand = self.path.with_suffix(".awb")
        return cand if cand.is_file() else None

    def __repr__(self) -> str:                                    # noqa: D105
        return f"<AcbFile {self.path.name!r} cues={len(self.cues())}>"

    # ---------------------------------------------------------------- waveforms

    def waveforms(self) -> list[Waveform]:
        tb = self.table("Waveform")
        if tb is None:
            return []
        out = []
        for r in range(tb.row_count):
            sid = tb.u16(r, "StreamAwbId", _UNSET16)
            mid = tb.u16(r, "MemoryAwbId", _UNSET16)
            out.append(Waveform(
                index=r,
                stream_id=None if sid == _UNSET16 else sid,
                memory_id=None if mid == _UNSET16 else mid,
                rate=tb.u32(r, "SamplingRate"),
                samples=tb.u32(r, "NumSamples"),
                loop_flag=tb.u16(r, "LoopFlag"),
            ))
        return out

    # ---------------------------------------------------------------- reference walk

    def _synth_waveforms(self, index: int, depth: int = 0) -> list[int]:
        tb = self.table("Synth")
        if tb is None or depth > 3 or not (0 <= index < tb.row_count):
            return []
        out: list[int] = []
        blob = tb.raw(index, "ReferenceItems")
        for i in range(0, len(blob) - 3, 4):
            kind = int.from_bytes(blob[i:i + 2], "big")
            ref = int.from_bytes(blob[i + 2:i + 4], "big")
            if kind == ITEM_WAVEFORM:
                out.append(ref)
            elif kind == ITEM_SYNTH:
                out.extend(self._synth_waveforms(ref, depth + 1))
        return out

    def _track_notes(self, index: int, depth: int = 0) -> list[Note]:
        """Notes on one track, laid out sequentially.

        A track's command stream is a program: `noteOn` starts a sound and the
        `2001` command that follows carries how long that sound lasts.  Several
        noteOns on one track therefore happen *one after another*, each taking
        its own duration -- verified against real banks, where the durations on
        a track sum to at most the block length and never exceed it.  Treating
        them as simultaneous (a plausible first guess) overlays later notes on
        top of earlier ones, which is audible as a stray repeat near the end of
        a track.
        """
        tb = self.table("Track")
        if tb is None or depth > 3 or not (0 <= index < tb.row_count):
            return []
        event = tb.u16(index, "EventIndex", _UNSET16)
        if event == _UNSET16:
            return []

        # v1.28+ calls it TrackEvent, earlier versions Command.
        cmd_tb = self.table("TrackEvent") or self.table("Command")
        if cmd_tb is None or not (0 <= event < cmd_tb.row_count):
            return []
        blob = cmd_tb.raw(event, "Command")

        notes: list[Note] = []
        pending: Note | None = None
        pos = 0
        while pos + 3 <= len(blob):
            code = int.from_bytes(blob[pos:pos + 2], "big")
            size = blob[pos + 2]
            body = blob[pos + 3:pos + 3 + size]

            if code in NOTE_ON_CODES and len(body) >= 4:
                kind = int.from_bytes(body[0:2], "big")
                ref = int.from_bytes(body[2:4], "big")
                if kind == ITEM_SYNTH:
                    wfs = self._synth_waveforms(ref)
                elif kind == ITEM_WAVEFORM:
                    wfs = [ref]
                elif kind == ITEM_SEQUENCE:
                    wfs = self._sequence_waveforms(ref, depth + 1)
                else:
                    wfs = []
                if wfs:
                    notes.append(Note(waveforms=tuple(wfs),
                                      position_ms=0, duration_ms=0))
                    pending = notes[-1]
            elif code == NOTE_DURATION_CODE and len(body) == 4 and pending is not None:
                if not pending.has_duration:          # first one wins
                    pending.duration_ms = int.from_bytes(body, "big")
            pos += 3 + size

        # Walk the notes placing each where the previous one ended, and use the
        # waveform's own length when the stream never gave a duration (which is
        # what a truncated or hand-built bank looks like).
        cursor = 0
        for note in notes:
            note.position_ms = cursor
            if not note.has_duration:
                note.duration_ms = int(round(max(
                    (self._waveform_seconds(w) for w in note.waveforms
                     if 0 <= w < len(self.waveforms())), default=0.0) * 1000))
            cursor += note.duration_ms
        return notes

    def _waveform_seconds(self, index: int) -> float:
        tb = self.table("Waveform")
        if tb is None or not (0 <= index < tb.row_count):
            return 0.0
        rate = tb.u32(index, "SamplingRate")
        return tb.u32(index, "NumSamples") / rate if rate else 0.0

    def _sequence_waveforms(self, index: int, depth: int = 0) -> list[int]:
        """Waveforms a Sequence references, for simple (non-block) cues.

        Only used when a cue points straight at a Sequence rather than a
        BlockSequence; there is no per-block timeline to build, so the
        waveforms are simply collected in order.
        """
        tb = self.table("Sequence")
        if tb is None or depth > 3 or not (0 <= index < tb.row_count):
            return []
        out: list[int] = []
        for track in tb.u16_array(index, "TrackIndex"):
            for note in self._track_notes(track, depth + 1):
                out.extend(note.waveforms)
        return out

    def _block_notes(self, index: int) -> list[Note]:
        """Every note a Block plays, from all of its tracks.

        Tracks are independent voices sounding together; notes within one track
        are sequential.  Positions are relative to the block start.
        """
        tb = self.table("Block")
        if tb is None or not (0 <= index < tb.row_count):
            return []
        out: list[Note] = []
        for track in tb.u16_array(index, "TrackIndex"):
            out.extend(self._track_notes(track))
        return out

    # ---------------------------------------------------------------- blocks

    def blocks(self) -> list[Block]:
        tb = self.table("Block")
        if tb is None:
            return []
        out = []
        for r in range(tb.row_count):
            starts = tb.u32(r, "StartPosition")
            length = tb.u32(r, "Length")
            # Guard against absurd values in malformed tables.
            if length > 24 * 3600 * 1000:
                length = 0
            out.append(Block(
                index=r,
                name=tb.u16(r, "Name"),
                start_ms=min(starts, 24 * 3600 * 1000),
                length_ms=length,
                playback_type=tb.u16(r, "PlaybackType"),
                track_indices=tb.u16_array(r, "TrackIndex"),
                notes=self._block_notes(r),
                destinations=tb.u16_array(r, "DestinationBlocks"),
            ))
        return out

    def block_sequences(self) -> list[list[int]]:
        """One playlist of Block indices per row of BlockSequence."""
        tb = self.table("BlockSequence")
        if tb is None:
            return []
        return [tb.u16_array(r, "BlockIndex") for r in range(tb.row_count)]

    # ---------------------------------------------------------------- cues

    def cue_names(self) -> list[tuple[int, str]]:
        """``(cue index, cue name)`` pairs."""
        tb = self.table("CueName")
        if tb is None:
            return []
        return [(tb.u16(r, "CueIndex"), tb.text(r, "CueName"))
                for r in range(tb.row_count)]

    def cues(self) -> list[Cue]:
        cue_tb = self.table("Cue")
        blocks = {b.index: b for b in self.blocks()}
        sequences = self.block_sequences()
        out: list[Cue] = []

        for cue_index, cue_name in self.cue_names():
            if cue_tb is None or not (0 <= cue_index < cue_tb.row_count):
                continue
            ref_type = cue_tb.u16(cue_index, "ReferenceType")
            ref_index = cue_tb.u16(cue_index, "ReferenceIndex")
            cue = Cue(index=cue_index, name=cue_name,
                      reference_type=ref_type)

            if ref_type == REF_BLOCK_SEQUENCE:
                cue.blocks = _blocks_of(ref_index, sequences, blocks)
            out.append(cue)

        # Cues without a CueName entry still exist in the table.
        named = {c.index for c in out}
        if cue_tb is not None:
            for r in range(cue_tb.row_count):
                if r in named:
                    continue
                cue = Cue(index=r, name=f"cue{r}",
                          reference_type=cue_tb.u16(r, "ReferenceType"))
                if cue.reference_type == REF_BLOCK_SEQUENCE:
                    cue.blocks = _blocks_of(cue_tb.u16(r, "ReferenceIndex"),
                                            sequences, blocks)
                out.append(cue)
        return out

    def cue_waveforms(self, cue: Cue) -> list[int]:
        """Waveforms referenced by a non-block cue (simple reference types)."""
        cue_tb = self.table("Cue")
        if cue_tb is None:
            return []
        ref_index = cue_tb.u16(cue.index, "ReferenceIndex")
        if cue.reference_type == REF_WAVEFORM:
            return [ref_index]
        if cue.reference_type == REF_SYNTH:
            return self._synth_waveforms(ref_index)
        if cue.reference_type == REF_SEQUENCE:
            return self._sequence_waveforms(ref_index)
        return []

    # ---------------------------------------------------------------- diagnostics

    def orphan_blocks(self) -> list[Block]:
        """Blocks carrying audio that no BlockSequence ever plays.

        Useful as a completeness check: if this is non-empty, something in
        the cue sheet is reachable only through jumps and a linear render
        would miss it.
        """
        used: set[int] = set()
        for seq in self.block_sequences():
            used.update(seq)
        return [b for b in self.blocks()
                if b.index not in used and b.waveform_indices]


# ==================================================================== timeline

def build_segments(cue: Cue, waveforms: list[Waveform]) -> tuple[list[Segment], float]:
    """Lay a cue out on an absolute timeline.

    Returns ``(segments, total_seconds)``.

    Two properties of this format drive the layout:

    * A waveform may be **longer than the block that introduces it** -- a
      sustained note held under the following sections.  Example: a 7.44 s
      waveform inside a 3.72 s block, where ``start + 7.44`` lands exactly on
      the start of the block after next.  So material is placed by absolute
      time, never clipped to its own block.

    * Blocks are **not** all the same length, and a block with no noteOn
      commands is a written rest that still consumes time.
    """
    blocks = list(cue.blocks)
    if not blocks:
        return [], 0.0

    def note_audio_ms(note) -> float | None:
        """How long a note sounds, in ms -- the waveform's own length.

        The `duration` next to a noteOn is not a cut-off; it is the gap before
        the *next* note on that track, which is why the durations on a track
        sum to at most the block length.  The audio itself plays for as long as
        it is: some pads deliberately ring past their own slot into the
        following sections, and their end lands exactly on a later block
        boundary (verified across real banks -- 20 of 34 such notes end on a
        boundary, usually to the millisecond).
        """
        lengths = [waveforms[w].seconds * 1000 for w in note.waveforms
                   if 0 <= w < len(waveforms)]
        return max(lengths) if lengths else None

    # Track where each note's audio reaches, so a cue is as long as the
    # material needs rather than only as long as its written blocks.
    ends: list[float] = []
    for b in blocks:
        furthest = 0.0
        for note in b.notes:
            length = note_audio_ms(note)
            if length is not None:
                furthest = max(furthest, note.position_ms + length)
        ends.append(b.start_ms + max(b.length_ms, furthest))
    total_ms = max(ends)

    segments: list[Segment] = []
    # Every note onset becomes a segment boundary.  That keeps the renderer
    # simple -- each slice starts at the head of its segment, so all it has to
    # do is mix -- while still placing notes where they belong in time.  A
    # note that begins mid-block (several noteOns on one track play in
    # sequence) would otherwise be pulled back to the block start and overlap
    # the sound before it.
    boundaries: list[float] = []
    for b in blocks:
        boundaries.append(float(b.start_ms))
        for note in b.notes:
            if note_audio_ms(note) is not None:
                boundaries.append(float(b.start_ms + note.position_ms))
    boundaries = sorted(set(boundaries))

    for bi, block in enumerate(blocks):
        block_end = (blocks[bi + 1].start_ms if bi + 1 < len(blocks) else total_ms)
        if block_end <= block.start_ms:
            continue

        # The windows covering this block, cut at every note onset inside it.
        cuts = [t for t in boundaries if block.start_ms <= t < block_end]
        windows = [(cuts[i], cuts[i + 1] if i + 1 < len(cuts) else block_end)
                   for i in range(len(cuts))]
        if not windows:
            windows = [(block.start_ms, block_end)]

        for seg_start, seg_end in windows:
            if seg_end <= seg_start:
                continue
            parts: list[tuple[int, float, float]] = []
            # Any earlier note reaching into this window contributes -- that is
            # how a sustained layer keeps sounding under later sections.
            for prev in blocks[:bi + 1]:
                for note in prev.notes:
                    length = note_audio_ms(note)
                    if length is None:
                        continue
                    note_start = prev.start_ms + note.position_ms
                    if note_start > seg_start:
                        continue          # starts in a later window
                    overlap_end = min(note_start + length, seg_end)
                    if overlap_end <= seg_start:
                        continue
                    offset = (seg_start - note_start) / 1000.0
                    span = (overlap_end - seg_start) / 1000.0
                    for wf_index in note.waveforms:
                        if 0 <= wf_index < len(waveforms):
                            parts.append((wf_index, offset, span))

            # Emitted even when `parts` is empty.  An empty window is a rest,
            # and dropping it would shift everything after it earlier and
            # shorten the track; the renderer turns it into matching silence.
            segments.append(Segment(
                start_s=(seg_start - blocks[0].start_ms) / 1000.0,
                duration_s=(seg_end - seg_start) / 1000.0,
                parts=parts,
                block_name=block.name,
                playback_type=block.playback_type,
            ))

    return segments, total_ms / 1000.0
