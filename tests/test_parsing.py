"""
Tests for the pure-Python half of the toolkit.

Nothing here needs vgmstream or ffmpeg: the @UTF reader, the cue model and the
timeline builder are all self-contained, so they are exercised against buffers
built by :class:`UtfBuilder` below.

The builder exists mainly for one regression: **a DEFAULT column's value is
stored in the schema section, so its address is relative to the table's own
start, not to the buffer start.**  When the root table sits at offset 0 those
two readings agree, so a root-only fixture passes even with the bug present.
Reproducing it requires a *nested* table, which is why the fixture supports
VLDATA payloads holding another table.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acbtoolkit.acb import (  # noqa: E402
    PLAYBACK_ONCE,
    Block,
    Cue,
    Note,
    Waveform,
    build_segments,
)
from acbtoolkit.cri_utf import UtfError, UtfTable  # noqa: E402

_MISSING = object()

# Bytes occupied per column type (COLUMN_TYPE_* in the CRI format).
_SIZES = {0x00: 1, 0x01: 1, 0x02: 2, 0x03: 2, 0x04: 4, 0x05: 4,
          0x06: 8, 0x07: 8, 0x08: 4, 0x09: 8, 0x0A: 4, 0x0B: 8}


class UtfBuilder:
    """Emit a byte-exact @UTF table, for tests only."""

    def __init__(self, name: str):
        self.name = name
        self._cols: list[tuple[str, int, object]] = []
        self._rows: list[dict] = []

    def col(self, name: str, type_: int, default=_MISSING) -> UtfBuilder:
        self._cols.append((name, type_, default))
        return self

    def row(self, **values) -> UtfBuilder:
        self._rows.append(values)
        return self

    # ---------------------------------------------------------------- internals

    @staticmethod
    def _encode_scalar(type_: int, value) -> bytes:
        if type_ == 0x00:
            return struct.pack(">B", value)
        if type_ == 0x01:
            return struct.pack(">b", value)
        if type_ == 0x02:
            return struct.pack(">H", value)
        if type_ == 0x03:
            return struct.pack(">h", value)
        if type_ == 0x04:
            return struct.pack(">I", value)
        if type_ == 0x05:
            return struct.pack(">i", value)
        if type_ == 0x06:
            return struct.pack(">Q", value)
        if type_ == 0x07:
            return struct.pack(">q", value)
        if type_ == 0x08:
            return struct.pack(">f", value)
        if type_ == 0x09:
            return struct.pack(">d", value)
        raise AssertionError(f"unsupported scalar type {type_:#x}")

    @staticmethod
    def _as_bytes(value) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, (list, tuple)):
            return b"".join(struct.pack(">H", v) for v in value)
        raise AssertionError(f"cannot serialise {value!r}")

    def build(self, base: int = 0) -> bytes:
        cols, rows = self._cols, self._rows
        count = len(rows)

        # 1. string section: column names, table name, string values
        strings = bytearray()
        str_off: dict[str, int] = {}

        def intern(text: str) -> int:
            if text not in str_off:
                str_off[text] = len(strings)
                strings.extend(text.encode("utf-8") + b"\x00")
            return str_off[text]

        name_off = intern(self.name)
        for cname, _, _ in cols:
            intern(cname)
        for cname, type_, default in cols:
            if type_ != 0x0A:
                continue
            if default is not _MISSING:
                intern(str(default))
            for r in rows:
                intern(str(r.get(cname, "")))

        # 2. data section: VLDATA payloads.  Identical payloads share one slot,
        # matching how CRI stores a table referenced from several columns.
        data = bytearray()
        data_off: dict[tuple[int, int], tuple[int, int]] = {}
        shared: dict[bytes, tuple[int, int]] = {}
        for r in range(count):
            for i, (cname, type_, default) in enumerate(cols):
                if default is not _MISSING or type_ != 0x0B:
                    continue
                payload = self._as_bytes(rows[r].get(cname, b""))
                if payload in shared:
                    data_off[(r, i)] = shared[payload]
                    continue
                slot = (len(data), len(payload))
                shared[payload] = slot
                data_off[(r, i)] = slot
                data.extend(payload)

        # 3. row layout: assign an offset to every non-default column
        row_slots: list[tuple[int, int]] = []      # (column index, offset in row)
        width = 0
        for i, (_, type_, default) in enumerate(cols):
            if default is not _MISSING:
                continue
            row_slots.append((i, width))
            width += _SIZES[type_]

        # 4. row section
        rows_blob = bytearray()
        for r in range(count):
            for i, _, in row_slots:
                cname, type_, _ = cols[i]
                value = rows[r].get(cname)
                if type_ == 0x0A:
                    rows_blob.extend(struct.pack(">I", intern(str(value))))
                elif type_ == 0x0B:
                    off, size = data_off[(r, i)]
                    rows_blob.extend(struct.pack(">II", off, size))
                else:
                    rows_blob.extend(self._encode_scalar(type_, value))

        # 5. schema section
        schema = bytearray()
        for cname, type_, default in cols:
            info = 0x10 | type_                       # NAME flag always set
            if default is not _MISSING:
                info |= 0x20                          # DEFAULT
            else:
                info |= 0x40                          # ROW
            schema.append(info)
            schema.extend(struct.pack(">I", str_off[cname]))
            if default is not _MISSING:
                assert type_ not in (0x0A, 0x0B), "constants must be scalar"
                schema.extend(self._encode_scalar(type_, default))

        # 6. header + concatenation
        schema_off = 0x20
        rows_rel = schema_off + len(schema)
        strs_rel = rows_rel + len(rows_blob)
        data_rel = strs_rel + len(strings)
        total_size = data_rel + len(data)

        head = bytearray(b"@UTF")
        # The stored value is the table length minus the 8 bytes of magic+size,
        # which is why the reader adds 8 back on the way in.
        head += struct.pack(">I", total_size - 8)
        head += struct.pack(">H", 0)                  # version
        head += struct.pack(">H", rows_rel - 8)
        head += struct.pack(">I", strs_rel - 8)
        head += struct.pack(">I", data_rel - 8)
        head += struct.pack(">I", name_off)
        head += struct.pack(">H", len(cols))
        head += struct.pack(">H", width)
        head += struct.pack(">I", count)

        blob = bytes(head + schema + rows_blob + strings + data)
        return (b"\x00" * base) + blob if base else blob


# ==================================================================== @UTF

def test_reads_rows_and_default_columns():
    blob = (UtfBuilder("Demo")
            .col("Count", 0x04)
            .col("Big", 0x02, default=1000)
            .row(Count=7)
            .row(Count=9)
            .build())
    table = UtfTable(blob, 0)
    assert table.name == "Demo"
    assert table.row_count == 2
    assert table.column_names() == ["Count", "Big"]
    assert [table.u32(r, "Count") for r in range(2)] == [7, 9]
    # One shared constant, read from the schema section for both rows.
    assert [table.u16(r, "Big") for r in range(2)] == [1000, 1000]


def test_nested_table_default_column_is_addressed_from_its_own_base():
    """Regression: DEFAULT columns must be read relative to the table start.

    The inner table lands in the outer table's data section, so its base is
    well past 0.  An implementation that treats the schema-relative offset as
    a buffer-absolute one reads the wrong bytes here (and happens to work on
    root-only tables, where both readings coincide).
    """
    inner = (UtfBuilder("Inner")
             .col("Value", 0x04)
             .col("Shared", 0x04, default=0xDEADBEEF)
             .row(Value=1)
             .row(Value=2)
             .build())
    outer = (UtfBuilder("Outer")
             .col("Child", 0x0B)
             .col("Tag", 0x04)
             .row(Child=inner, Tag=42)
             .build())

    table = UtfTable(outer, 0)
    assert table.u32(0, "Tag") == 42
    sub = table.subtable(0, "Child")
    assert sub is not None, "nested table not resolved"
    assert sub.base > 0
    assert sub.name == "Inner"
    assert [sub.u32(r, "Value") for r in range(2)] == [1, 2]
    assert sub.u32(0, "Shared") == 0xDEADBEEF
    assert sub.u32(1, "Shared") == 0xDEADBEEF


def test_string_and_array_columns():
    blob = (UtfBuilder("Str")
            .col("Label", 0x0A)
            .col("Idxs", 0x0B)
            .row(Label="hello", Idxs=[3, 5, 8])
            .build())
    table = UtfTable(blob, 0)
    assert table.text(0, "Label") == "hello"
    assert table.u16_array(0, "Idxs") == [3, 5, 8]


def test_missing_columns_return_defaults():
    blob = (UtfBuilder("T").col("A", 0x04).row(A=1).build())
    table = UtfTable(blob, 0)
    assert table.index_of("Nope") == -1
    assert table.has("A") and not table.has("Nope")
    assert table.u32(0, "Nope", default=99) == 99
    assert table.text(0, "Nope", default="fallback") == "fallback"
    assert table.raw(0, "Nope") == b""


def test_walk_reaches_nested_tables_once():
    """A table referenced twice is visited once, not once per reference."""
    inner = UtfBuilder("Child").col("V", 0x04).row(V=1).build()
    blob = (UtfBuilder("Root")
            .col("A", 0x0B)
            .col("B", 0x0B)
            .row(A=inner, B=inner)      # same table referenced twice
            .build())
    tables = [t for _, t in UtfTable(blob, 0).walk()]
    names = [t.name for t in tables]
    assert names.count("Root") == 1
    assert names.count("Child") == 1
    assert len(tables) == 2


def test_walk_survives_a_self_reference():
    """A table whose payload points back at itself must not loop forever.

    Built by hand rather than via UtfBuilder: the payload has to be a @UTF
    signature planted at data offset 0, so that resolving the VLDATA column
    lands on the root table again.
    """
    # Minimal one-column table whose single VLDATA payload is at data offset 0
    # and whose bytes begin with "@UTF" -- so subtable() re-enters the root.
    inner = UtfBuilder("Root").col("Me", 0x0B).row(Me=b"\x00" * 8).build()

    # Overwrite the payload with the table's own content, truncated to fit.
    root = UtfTable(inner, 0)
    payload_at = root._data_abs                    # absolute start of data section
    room = len(inner) - payload_at
    patched = bytearray(inner)
    patched[payload_at:payload_at + room] = inner[:room]

    table = UtfTable(bytes(patched), 0)
    seen = list(table.walk())                      # must terminate
    assert len(seen) >= 1


@pytest.mark.parametrize("blob", [
    b"",
    b"@UTF",
    b"NOPE" + b"\x00" * 64,
    b"@UTF" + b"\xff" * 64,
    b"@UTF" + b"\x00" * 0x20,          # zero counts
])
def test_rejects_unusable_buffers(blob):
    with pytest.raises(UtfError):
        UtfTable(blob, 0)


def test_rejects_out_of_range_base():
    good = UtfBuilder("T").col("A", 0x04).row(A=1).build()
    with pytest.raises(UtfError):
        UtfTable(good, len(good) + 100)


# ==================================================================== timeline

def _wf(index: int, seconds: float, stream: int = 0) -> Waveform:
    return Waveform(index=index, stream_id=stream, memory_id=None,
                    rate=1000, samples=int(seconds * 1000), loop_flag=1)


def _block(index, name, start_ms, length_ms, wfs, *, ptype=0, dest=(),
           duration_ms: int = 0, position_ms: int = 0):
    """A block whose notes all begin at `position_ms` with duration_ms each.

    `duration_ms=0` means "let the waveform decide", matching a command
    stream that never states a length.
    """
    notes = [Note(waveforms=(w,), position_ms=position_ms,
                  duration_ms=duration_ms) for w in wfs]
    return Block(index=index, name=name, start_ms=start_ms, length_ms=length_ms,
                 playback_type=ptype, track_indices=[], notes=notes,
                 destinations=list(dest))


def test_blocks_play_in_sequence_order():
    wfs = [_wf(0, 2.0), _wf(1, 3.0)]
    cue = Cue(0, "c", 0x08, [
        _block(0, 1, 0, 2000, [0]),
        _block(1, 2, 2000, 3000, [1]),
    ])
    segments, total = build_segments(cue, wfs)
    assert total == pytest.approx(5.0)
    assert [round(s.duration_s, 3) for s in segments] == [2.0, 3.0]
    assert [s.block_name for s in segments] == [1, 2]
    assert all(s.gap_s == 0 for s in segments)


def test_sustained_waveform_rings_across_following_blocks():
    """A note longer than its own block keeps sounding under the next ones.

    Block 1 lasts 1 s but its waveform runs 4 s, so the cue is 4 s long and
    every later segment carries a slice of that note.  Clipping material to
    the block that introduces it -- the obvious implementation -- cuts the
    note off at 1 s and shortens the track.
    """
    wfs = [_wf(0, 4.0), _wf(1, 1.0), _wf(2, 1.0)]
    cue = Cue(0, "c", 0x08, [
        _block(0, 1, 0, 1000, [0]),
        _block(1, 2, 1000, 1000, [1]),
        _block(2, 3, 2000, 1000, [2]),
    ])
    segments, total = build_segments(cue, wfs)
    # The sustain outlives the last block, so the cue is as long as the note.
    assert total == pytest.approx(4.0)

    for i, seg in enumerate(segments):
        assert 0 in [w for w, _, _ in seg.parts], f"segment {i} lost the sustain"
    # Consumed one second at a time, with matching source offsets.
    offsets = [next(o for w, o, _ in s.parts if w == 0) for s in segments]
    assert offsets == pytest.approx([0.0, 1.0, 2.0])
    # The last window stretches to the end of the sustain, so the final note
    # rings out rather than being cut at the block boundary.
    assert [round(s.duration_s, 3) for s in segments] == [1.0, 1.0, 2.0]
    assert segments[-1].parts[0][2] == pytest.approx(2.0)   # 2 s of note left


def test_overlapping_tracks_share_one_segment():
    """Several tracks inside a block sound together, so they mix, not queue."""
    wfs = [_wf(0, 2.0), _wf(1, 2.0)]
    cue = Cue(0, "c", 0x08, [_block(0, 1, 0, 2000, [0, 1])])
    segments, _ = build_segments(cue, wfs)
    assert len(segments) == 1
    assert sorted(w for w, _, _ in segments[0].parts) == [0, 1]


def test_note_less_block_becomes_a_rest():
    """No noteOn commands means a written rest, not missing audio."""
    wfs = [_wf(0, 1.0), _wf(1, 1.0)]
    cue = Cue(0, "c", 0x08, [
        _block(0, 1, 0, 1000, [0]),
        _block(1, 2, 1000, 500, []),                 # rest
        _block(2, 3, 1500, 1000, [1]),
    ])
    segments, total = build_segments(cue, wfs)
    assert total == pytest.approx(2.5)
    rest = [s for s in segments if s.block_name == 2]
    assert len(rest) == 1
    assert rest[0].parts == []
    assert rest[0].gap_s == pytest.approx(0.5)
    assert rest[0].material_s == 0.0


def test_loop_detection():
    assert _block(0, 1, 0, 1000, [0], ptype=2).loops
    assert _block(1, 2, 0, 1000, [0], dest=[1]).loops    # self jump
    plain = _block(2, 3, 0, 1000, [0])
    assert not plain.loops
    assert plain.playback_type == PLAYBACK_ONCE


def test_cue_helpers():
    blocks = [_block(0, 1, 0, 1000, [0], ptype=2),
              _block(1, 2, 1000, 2000, [0])]
    cue = Cue(0, "c", 0x08, blocks)
    assert cue.duration_s == pytest.approx(3.0)
    assert [b.name for b in cue.loop_blocks] == [1]
    assert cue.is_block_music


def test_empty_cue_is_safe():
    cue = Cue(0, "empty", 0x08, [])
    assert build_segments(cue, []) == ([], 0.0)


def test_unknown_waveform_index_is_skipped_not_crashed():
    """A block referencing a waveform that does not exist must not explode."""
    cue = Cue(0, "c", 0x08, [_block(0, 1, 0, 1000, [0, 99])])
    segments, total = build_segments(cue, [_wf(0, 1.0)])
    assert total == pytest.approx(1.0)
    assert [w for w, _, _ in segments[0].parts] == [0]


# ==================================================================== helpers

def test_waveform_reports_its_container():
    streamed = Waveform(0, stream_id=5, memory_id=None, rate=1, samples=1, loop_flag=1)
    embedded = Waveform(1, stream_id=None, memory_id=0, rate=1, samples=1, loop_flag=1)
    unresolved = Waveform(2, stream_id=None, memory_id=None, rate=1, samples=1, loop_flag=1)
    assert not streamed.embedded and streamed.memory_id is None
    assert embedded.embedded
    assert "awb#5" in str(streamed)
    assert "embedded#0" in str(embedded)
    assert "unresolved" in str(unresolved)


def test_waveform_seconds_handles_zero_rate():
    assert Waveform(0, 1, None, 0, 1000, 1).seconds == 0.0


def test_encode_args_per_format():
    from acbtoolkit.external import _BITRATE_FORMATS, SUPPORTED_FORMATS, Ffmpeg

    for fmt in SUPPORTED_FORMATS:
        args = Ffmpeg.encode_args(fmt, 192)
        assert args, fmt
        if fmt in _BITRATE_FORMATS:
            assert "192k" in args
        else:
            assert "192k" not in args          # lossless formats ignore bitrate


def test_encode_args_rejects_unknown_format():
    from acbtoolkit.external import Ffmpeg, ToolError

    with pytest.raises(ToolError):
        Ffmpeg.encode_args("nope", 192)


def test_safe_name_strips_path_characters():
    from acbtoolkit.extract import safe_name

    assert safe_name("dir/file:01") == "dir_file_01"
    assert safe_name("a<b>c|d?e*f") == "a_b_c_d_e_f"
    assert safe_name("   ") == "unnamed"
    assert safe_name("...") == "unnamed"
    assert safe_name("normal_name") == "normal_name"


# ==================================================================== safety

def test_mix_never_pads_without_a_target_length(tmp_path):
    """`apad` with no `-t` pads forever, and ffmpeg will fill the disk.

    Regression for a runaway encode that produced a 40 GB WAV: a bare
    ``apad`` must only be added when a total length is given.
    """
    from acbtoolkit.external import Ffmpeg

    captured: list[list[str]] = []

    class Recorder(Ffmpeg):
        def _run(self, args, timeout=0, expect=None):     # noqa: D102
            captured.append(list(args))
            if expect is not None:
                Path(expect).parent.mkdir(parents=True, exist_ok=True)
                Path(expect).write_bytes(b"RIFF")
            return True, ""

    ff = Recorder(sys.executable)      # never actually invoked
    src = tmp_path / "in.wav"
    out = tmp_path / "out.wav"

    ff.mix([src], out)
    assert not any("apad" in a for a in captured[-1]), \
        "apad emitted without a total length"

    captured.clear()
    ff.mix([src], out, total=5.0)
    args = captured[-1]
    assert any("apad" in a for a in args)
    assert "-t" in args and "5.000000" in args


def test_mix_guard_rejects_unbounded_filter_graph(tmp_path):
    """Same rule for the multi-input path."""
    from acbtoolkit.external import Ffmpeg

    captured: list[list[str]] = []

    class Recorder(Ffmpeg):
        def _run(self, args, timeout=0, expect=None):     # noqa: D102
            captured.append(list(args))
            if expect is not None:
                Path(expect).parent.mkdir(parents=True, exist_ok=True)
                Path(expect).write_bytes(b"RIFF")
            return True, ""

    ff = Recorder(sys.executable)      # never actually invoked
    ff.mix([tmp_path / "a.wav", tmp_path / "b.wav"], tmp_path / "out.wav")
    graph = " ".join(captured[-1])
    assert "apad" not in graph


def test_size_watchdog_kills_an_oversized_output(tmp_path):
    """The watchdog terminates a run whose output outgrows the cap."""
    from acbtoolkit.external import Ffmpeg, ToolError

    # WAV gets the (larger) intermediate cap, so drive the limit down to keep
    # the test fast and independent of the defaults.
    ff = Ffmpeg(sys.executable, max_output_mb=1, wav_limit_mb=1)
    oversize = tmp_path / "big.wav"
    oversize.write_bytes(b"\x00" * (2 * 1024 * 1024))     # already past the cap

    with pytest.raises(ToolError, match="exceeded"):
        ff._run(["-i", "x", "big.wav"], expect=oversize)
    assert not oversize.exists(), "oversized partial output was left behind"


def test_uncompressed_intermediates_get_a_separate_cap():
    """A WAV cap must not be applied to a finished lossy file.

    Regression: one flat limit killed legitimate renders, because a few
    minutes of uncompressed 48 kHz stereo is far larger than any MP3.
    """
    from acbtoolkit.external import Ffmpeg

    ff = Ffmpeg(sys.executable, max_output_mb=64, wav_limit_mb=4096)
    assert ff._limit_for(Path("a.wav")) == 4096 * 1024 * 1024
    assert ff._limit_for(Path("a.flac")) == 4096 * 1024 * 1024
    assert ff._limit_for(Path("a.mp3")) == 64 * 1024 * 1024
    assert ff._limit_for(Path("a.m4a")) == 64 * 1024 * 1024
    # Unknown destination: assume the smaller cap, which is the safe choice.
    assert ff._limit_for(None) == 64 * 1024 * 1024


def test_guess_output_ignores_flags_and_existing_files(tmp_path):
    from acbtoolkit.external import Ffmpeg

    existing = tmp_path / "in.wav"
    existing.write_bytes(b"x")
    args = ["-i", str(existing), "-b:a", "192k", str(tmp_path / "out.mp3")]
    assert Ffmpeg._guess_output(args) == tmp_path / "out.mp3"


@pytest.mark.skipif(
    not (__import__("shutil").which("ffmpeg") or Path(r"D:\ffmpeg\bin\ffmpeg.exe").is_file()),
    reason="ffmpeg not installed",
)
def test_real_ffmpeg_pad_is_bounded(tmp_path):
    """End-to-end: padding to a target length produces exactly that length."""
    from acbtoolkit.external import Ffmpeg, wav_duration

    exe = __import__("shutil").which("ffmpeg") or r"D:\ffmpeg\bin\ffmpeg.exe"
    ff = Ffmpeg(exe, max_output_mb=64)
    src = tmp_path / "s.wav"
    ff._run(["-f", "lavfi", "-i", "sine=f=440:d=1", "-c:a", "pcm_s16le", str(src)],
            expect=src)

    out = tmp_path / "o.wav"
    ff.mix([src], out, total=3.0)
    assert wav_duration(out) == pytest.approx(3.0, abs=0.05)


@pytest.mark.skipif(
    not (__import__("shutil").which("ffmpeg") or Path(r"D:\ffmpeg\bin\ffmpeg.exe").is_file()),
    reason="ffmpeg not installed",
)
def test_run_does_not_deadlock_on_chatty_stderr(tmp_path):
    """A talkative child must not be able to wedge the watchdog.

    Regression: stderr went to a pipe that nothing read while the parent
    polled for exit.  Once the OS buffer filled, ffmpeg blocked on write and
    the run hung forever with no output and no error.  stderr now goes to a
    file, which has no such limit.
    """
    import shutil as _shutil
    import time as _time

    from acbtoolkit.external import Ffmpeg

    exe = _shutil.which("ffmpeg") or r"D:\ffmpeg\bin\ffmpeg.exe"
    ff = Ffmpeg(exe, max_output_mb=64, wav_limit_mb=512)

    out = tmp_path / "o.wav"
    src = tmp_path / "s.wav"
    ff._run(["-f", "lavfi", "-i", "sine=f=440:d=1", "-c:a", "pcm_s16le", str(src)],
            expect=src)

    started = _time.monotonic()
    # -loglevel warning on a long-ish encode keeps ffmpeg emitting to stderr;
    # the timeout is the backstop that used to never fire.
    ok, _ = ff._run(["-loglevel", "verbose", "-i", str(src),
                     "-t", "2", "-c:a", "pcm_s16le", str(out)],
                    timeout=60, expect=out)
    assert ok, "encode failed"
    assert out.is_file()
    assert _time.monotonic() - started < 60, "run wedged despite the timeout"


def test_notes_on_one_track_play_sequentially():
    """Notes that follow each other on one track must not overlap.

    Regression for an audible glitch: a track whose noteOns play in sequence
    was rendered with every note starting at the block head, so the last note
    (often a reprise of an earlier phrase) rang *on top of* the others and
    landed as a stray repeat at the end of the track.
    """
    wfs = [_wf(0, 2.0), _wf(1, 3.0)]
    # Two notes on one track: 2 s then 3 s, filling a 5 s block exactly.
    block = Block(
        index=0, name=1, start_ms=0, length_ms=5000, playback_type=0,
        track_indices=[0],
        notes=[Note(waveforms=(0,), position_ms=0, duration_ms=2000),
               Note(waveforms=(1,), position_ms=2000, duration_ms=3000)],
    )
    cue = Cue(0, "c", 0x08, [block])
    segments, total = build_segments(cue, wfs)

    assert total == pytest.approx(5.0)
    # The second note occupies the last 3 s and nothing overlaps it.
    tail = [s for s in segments if abs(s.start_s - 2.0) < 0.001]
    assert len(tail) == 1
    assert [w for w, _, _ in tail[0].parts] == [1]
    # The first window covers only the first note.
    head = [s for s in segments if s.start_s == pytest.approx(0.0)]
    assert [w for w, _, _ in head[0].parts] == [0]


def test_sustained_note_rings_across_a_later_note_onset():
    """A long note keeps sounding underneath a note that starts later."""
    wfs = [_wf(0, 6.0), _wf(1, 2.0)]
    block = Block(
        index=0, name=1, start_ms=0, length_ms=6000, playback_type=0,
        track_indices=[],
        notes=[Note(waveforms=(0,), position_ms=0, duration_ms=6000),
               Note(waveforms=(1,), position_ms=2000, duration_ms=2000)],
    )
    cue = Cue(0, "c", 0x08, [block])
    segments, total = build_segments(cue, wfs)

    assert total == pytest.approx(6.0)
    onset = [s for s in segments if abs(s.start_s - 2.0) < 0.001][0]
    indices = sorted(w for w, _, _ in onset.parts)
    assert indices == [0, 1], "the sustain must remain under the new note"
    # The sustain is read from 2 s into its own audio at this point.
    offset = next(o for w, o, _ in onset.parts if w == 0)
    assert offset == pytest.approx(2.0)


def test_audio_rings_past_its_slot_to_its_natural_length():
    """A note's duration is the gap before the next note, not a cut-off.

    Pads that outlast their slot are deliberate: their end lands on a later
    block boundary, so truncating them at the slot would chop off the tail.
    """
    wfs = [_wf(0, 10.0)]
    block = Block(index=0, name=1, start_ms=0, length_ms=2000,
                  playback_type=0, track_indices=[],
                  notes=[Note(waveforms=(0,), position_ms=0, duration_ms=2000)])
    cue = Cue(0, "c", 0x08, [block])
    segments, total = build_segments(cue, wfs)

    # The cue lasts as long as the audio, even though the block is 2 s.
    assert total == pytest.approx(10.0)
    covered = sum(d for s in segments for _, _, d in s.parts)
    assert covered == pytest.approx(10.0)


def test_slot_still_governs_where_the_next_note_starts():
    """Even when audio rings on, the next note starts after the slot."""
    wfs = [_wf(0, 10.0), _wf(1, 1.0)]
    block = Block(index=0, name=1, start_ms=0, length_ms=3000,
                  playback_type=0, track_indices=[],
                  notes=[Note(waveforms=(0,), position_ms=0, duration_ms=2000),
                         Note(waveforms=(1,), position_ms=2000, duration_ms=1000)])
    cue = Cue(0, "c", 0x08, [block])
    segments, total = build_segments(cue, wfs)

    assert total == pytest.approx(10.0)         # the long pad decides
    second = [s for s in segments if abs(s.start_s - 2.0) < 0.001][0]
    indices = sorted(w for w, _, _ in second.parts)
    assert indices == [0, 1], "ringing pad plus the new note"
    assert next(o for w, o, _ in second.parts if w == 0) == pytest.approx(2.0)


def test_extraction_never_writes_beside_the_source(tmp_path, monkeypatch):
    """Omitting `outdir` must not drop files into the game's own folder.

    Regression: the library default was the source file's parent, which put
    extracted audio inside the installed game directory -- exactly the kind of
    write a read-only extraction tool must never perform.
    """

    from acbtoolkit import extract as extract_mod

    game_dir = tmp_path / "Game" / "StreamingAssets"
    game_dir.mkdir(parents=True)
    acb = game_dir / "sample.acb"
    acb.write_bytes(b"@UTF")                     # never actually parsed

    class _NullVgm:
        def extract_named(self, source, outdir, ignore_loop=True):
            return []

    class _NullFfm:
        pass

    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    result = extract_mod.extract_bank(
        acb, None, _NullVgm(), _NullFfm(),
        fmt="mp3", bitrate=192, ignore_loop=True,
        workdir=tmp_path / "scratch")

    assert Path(result.outdir).parent == workdir, \
        "default output must be the working directory, not the source folder"
    assert not list(game_dir.glob("*_audio")), \
        "extraction created a directory inside the source tree"


def test_input_is_never_searched_for(tmp_path, monkeypatch):
    """The tool must not go looking for .acb files on its own.

    Passing a directory that holds no cue sheets is an error, not an
    invitation to scan subdirectories or well-known install locations.
    Pointing the tool at a location is the caller's decision.
    """
    import acbtoolkit.cli as cli

    empty = tmp_path / "somewhere"
    empty.mkdir()
    # A nested folder that *does* contain one: it must not be discovered.
    nested = empty / "Game_Data" / "StreamingAssets"
    nested.mkdir(parents=True)
    (nested / "hidden.acb").write_bytes(b"@UTF")

    # Resolving a directory yields that directory, unchanged.
    assert cli._resolve_input(str(empty)) == empty

    # And nothing in the module searches a filesystem on the user's behalf:
    # no helper for locating cue sheets, and no table of install locations.
    suspicious = [name for name in dir(cli)
                  if name.startswith("_") and any(
                      word in name.lower()
                      for word in ("scan", "locate", "discover", "search"))]
    assert not suspicious, f"filesystem-searching helpers present: {suspicious}"


def test_missing_input_is_an_error(tmp_path):
    """No path given, and a path that does not exist, both fail loudly."""
    import acbtoolkit.cli as cli

    with pytest.raises(SystemExit):
        cli._resolve_input(None)
    with pytest.raises(SystemExit):
        cli._resolve_input(str(tmp_path / "does-not-exist"))
