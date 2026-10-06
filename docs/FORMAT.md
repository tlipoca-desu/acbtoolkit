# CRIWARE ACB / AWB format notes

Working notes on the parts of the format this toolkit reads. Written from
CRIWARE 1.37–1.46 cue sheets plus the reference implementations in
[vgmstream](https://github.com/vgmstream/vgmstream) (`src/util/cri_utf.c`,
`src/meta/acb.c`, `src/meta/awb.c`, `src/meta/hca.c`).

Everything below was verified against real files, not inferred from other
games' documentation, so it may not describe every CRIWARE version.

---

## 1. `@UTF` tables

CRI's generic serialisation format: a row/column database with four sections.
`ACB`, `ACF`, `CPK` and others all use it.

```
offset  size  field
0x00    4     "@UTF"
0x04    u32be table_size - 8        table ends at base + 8 + value
0x08    u16be version               0 or 1
0x0a    u16be rows_offset    - 8
0x0c    u32be strings_offset - 8
0x10    u32be data_offset    - 8
0x14    u32be name_offset           table name, within the string section
0x18    u16be column_count
0x1a    u16be row_width             bytes per row
0x1c    u32be row_count
```

Two details cost real debugging time:

- **Section offsets are stored biased by 8**, because they are measured from
  the end of the magic+size pair. Every one needs `+8` to become a real offset.
  `name_offset` is the exception: it is already relative to the string section.

- **`row_width` cannot be trusted as a column budget.** Since HCA v3 some
  tables — notably the ACB `Header` — declare a `row_width` smaller than the
  sum of their row columns, and let the last columns read past it into blank
  space. Validating against `row_width` rejects readable files. vgmstream
  documents the same quirk. Validate against the row *section* size instead.

Sections, in order:

| section | contents |
| --- | --- |
| schema | one entry per column: `u8 info`, `u32be name_offset`, then the constant value when DEFAULT is set |
| rows | `row_count` fixed-width records |
| strings | NUL-terminated strings, shared by table name, column names and string values |
| data | variable-length payloads (VLDATA), often nested `@UTF` tables |

### Column entries

```
info = (flags << 4) | type
```

Flags:

| flag | meaning |
| --- | --- |
| `0x10` | NAME — the column has a name in the string section |
| `0x20` | DEFAULT — one constant, stored inline in the schema section |
| `0x40` | ROW — a value per row, stored in the row section |
| `0x80` | UNDEFINED — should not appear |

A column is DEFAULT *or* ROW, never both in practice (DEFAULT wins if both are
set). **A DEFAULT column's value lives in the schema section, so its address is
`table_base + offset`.** For the root table `table_base == 0` and that
coincides with a plain offset, which hides the mistake — nested tables are the
only place it shows up.

Types and their widths:

| code | type | bytes |
| --- | --- | --- |
| `0x00` | u8 | 1 |
| `0x01` | s8 | 1 |
| `0x02` | u16be | 2 |
| `0x03` | s16be | 2 |
| `0x04` | u32be | 4 |
| `0x05` | s32be | 4 |
| `0x06` | u64be | 8 |
| `0x07` | s64be | 8 |
| `0x08` | f32be | 4 |
| `0x09` | f64be | 8 |
| `0x0a` | string | 4 (offset into the string section) |
| `0x0b` | VLDATA | 8 (u32 offset into the data section + u32 size) |
| `0x0c` | u128 | 16 |

Helper: files often store plain integer arrays (`TrackIndex`, `BlockIndex`,
`ReferenceItems`) as VLDATA holding consecutive big-endian u16 values. See
`UtfTable.u16_array`.

---

## 2. ACB: the cue sheet

An `.acb` holds a `Header` table that points at every other table. There is
usually no audio inside it — see §3 for when there is.

### Reference chain

```
CueName ──► Cue ──► (depends on ReferenceType)
```

`Cue.ReferenceType` values seen in practice:

| value | chain |
| --- | --- |
| `0x01` | Cue → Waveform |
| `0x02` | Cue → Synth → Waveform |
| `0x03` | Cue → Sequence → Track → TrackEvent → Command → Synth → Waveform |
| `0x08` | Cue → **BlockSequence** → Block → Track → TrackEvent → Command → Synth → Waveform |

`0x03` is a linear sequence (one or more tracks), `0x08` is block music.
Others exist (`0x05` outsideLink, `0x09` insideLinkBlockSequence, …) but are
rare; this toolkit ignores them.

### Reaching the audio

Tracks carry **no** direct waveform reference. The link goes through a command
blob:

```
Track.EventIndex ──► TrackEvent[EventIndex].Command   (VLDATA)
```

`Command` is a stream of TLV records:

```
u16be code
u8    size
bytes payload (size bytes)
```

Codes that start a sound:

| code | meaning |
| --- | --- |
| 2000 | noteOn |
| 2003 | noteOnWithNo — same payload |
| 2004 | noteOnWithDuration — no reference |

A noteOn payload begins with a reference item:

```
u16be item_type
u16be item_index
```

| item_type | target |
| --- | --- |
| `0x01` | Waveform[item_index] |
| `0x02` | Synth[item_index] |
| `0x03` | Sequence[item_index] |

`Synth.ReferenceItems` uses the same `(type, index)` pairs, so a Synth may
point at a Waveform, another Synth, or a Sequence. Depth is small (≤3).

Other codes (volume, pan, mute, stop, callbacks…) do not affect extraction and
are skipped.

### Waveform

| column | meaning |
| --- | --- |
| `StreamAwbId` | 0-based index of the stream inside the external `.awb` |
| `MemoryAwbId` | 0-based index inside the `.acb`'s own embedded bank |
| `Streaming` | 0 = embedded, 1 = streamed, 2 = prefetch + stream |
| `SamplingRate`, `NumSamples` | exact duration |
| `LoopFlag` | 1 = no loop, 2 = loop |

Exactly one of `StreamAwbId` / `MemoryAwbId` is meaningful; the unused one
reads as `0xFFFF`. **A single cue can mix both kinds** — a real example has 9
blocks whose 9 waveforms are 8 from the `.awb` plus 1 embedded in the `.acb`.

Loop points, when present, live in a separate `WaveformExtensionData` table
(`LoopStart`, `LoopEnd`), referenced through `Waveform.ExtensionData`.

### Block music

`BlockSequence` is the playlist:

| column | meaning |
| --- | --- |
| `NumBlocks` | number of sections played |
| `BlockIndex` | VLDATA, `NumBlocks` × u16 — **the authoritative order** |
| `NumTracks`, `TrackIndex` | tracks active for the whole sequence |

`Block` is one section:

| column | meaning |
| --- | --- |
| `NumTracks`, `TrackIndex` | tracks sounding **simultaneously** in this section |
| `Length` | section length in ms |
| `StartPosition` | offset from the start of the cue, in ms |
| `PlaybackType` | 0 = play once, non-zero = repeatable |
| `NumLoops` | loop count |
| `DestinationBlocks` | VLDATA of u16 — where this section may jump to |
| `JumpDestination`, `LastTransitionBlockNo` | alternate jump bookkeeping |
| `Name` | human-facing section number |

Cue duration = `max(StartPosition + Length)`. Sections are laid out
end-to-end, and `StartPosition` is cumulative — which is what makes a linear
rebuild possible.

#### Two things that determine whether a rebuild sounds right

**A waveform may be longer than its block.** Example, verified: `Block.Length`
= 3720 ms but its waveform is 7442 ms, and `14883 + 7442 = 22325` — exactly the
`StartPosition` of the block *after* next. The note is a sustained layer held
across the following sections. Material must therefore be placed on an absolute
timeline; clipping it to the introducing block cuts the note off.

**A block with no noteOn command is a rest.** It still consumes `Length`. Real
files contain these (a 3.8 s block with three tracks, none of them issuing
noteOn, and a 1.9 s one likewise); they are written silence between sections.
Dropping them shifts everything after them earlier and shortens the track.

#### Branches

`DestinationBlocks` encodes runtime re-routing — battle phases, layer switches.
A real battle cue:

```
blk0   9.0s   intro                      (no jump)
blk1   6.0s   loop point                 (self)
blk2  72.0s   main loop                  (self)
blk3   3.0s
blk4  24.0s
blk5  22.5s   → blk7
blk6   1.5s   → blk2    transition stab
blk7   1.5s   → blk4    transition
blk8  12.0s   outro
```

The written `BlockSequence` order is static; the jumps are what the game
chooses between. Rendering one full pass of the sequence therefore yields the
complete piece, transitions included.

---

## 3. AWB: the wave bank

An `.awb` is an AFS2 container, optionally followed by the raw HCA streams.

```
0x00  4  "AFS2"
0x04  u8 version        (2)
0x05  u8 offset_size    (4 = 32-bit offsets)
0x06  u8 id_size        (4 = 32-bit ids)
0x07  u8 flags          (0, or 1 = little-endian ids)
0x08  u32be num_files
0x0c  u16be alignment   (32)
0x0e  u16be subkey      HCA decryption key, 0 = none
0x10  entries[num_files]: { u32be offset, u32be size }  (at offset_size width)
```

Streams follow, aligned. Each decoded payload starts with an HCA header, so
offset/size pairs can also be derived by scanning for `HCA\0`.

`subkey` is the per-bank HCA key. When it is `0x0000` the audio is plaintext —
check this before assuming a game uses encryption.

### Embedded banks

An `.acb` may carry its own AFS2 bank in the `AwbFile` VLDATA column of
`Header`. When that column is present but **zero-length**, the cue sheet has no
audio of its own and refers to an external `.awb`.

An ACB can both embed audio *and* reference an external bank. The two sets do
not overlap — verified by comparing per-stream content hashes across 18 such
files (total external streams lost if only the ACB were read: 63+; one bank
alone held 234 embedded streams against 1 external, with zero shared). Both
must be extracted or material is lost silently.

---

## 4. HCA: the codec

```
0x00  4  "HCA\0"
0x04  u16be version      0x0200 / 0x0300
0x06  u16be header_size
0x08  chunks...
```

Chunks to look for:

| chunk | meaning |
| --- | --- |
| `fmt\0` | channels, sample rate, block size |
| `comp` | compression parameters |
| `dec\0` | decoder-specific |
| `loop` | loop start/end |
| `cipher` | encryption type — **absent means plaintext** |
| `vbr` | variable bitrate |

A plaintext HCA has no `cipher` chunk and begins at a readable `HCA\0`
signature. Encrypted streams still show `HCA\0` but carry `cipher`, and their
payload needs the key from the AWB/AHX header or an external source.

Verify before hunting for keys: many titles — including some Unity games that
bundle the CRIWARE runtime but never set a key — ship plaintext audio.

---

## 5. Practical notes

- **Keep `.acb` and `.awb` together and identically named.** Name resolution
  runs the cue table against the bank's stream ids; a lone `.awb` yields bare
  numbers, because the mapping lives in the cue sheet.
- **Stream ids are 0-based; `vgmstream -s` is 1-based.** Off-by-one here gives
  plausible but wrong audio.
- **Do not name vgmstream output with `?n`.** For a stream with no cue name it
  substitutes the whole input path, and the resulting `/` characters make it
  refuse to write the file. Use `?s` (subsong number) and read names from the
  `stream name:` lines on stdout — they arrive in subsong order.
- Some banks declare more or fewer `Waveform` rows than there are streams;
  always resolve through `StreamAwbId` rather than assuming index equality.
