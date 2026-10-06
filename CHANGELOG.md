# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-10-06

First release. A general-purpose CRIWARE `ACB` / `AWB` audio container parser
and data-block reassembler.

### Added

**Container parsing**

- `@UTF` table reader covering the generic CRI table format: all column types,
  DEFAULT/ROW column addressing, nested tables, and cycle-safe traversal.
- `ACB` semantic model following the full reference chain —
  `CueName → Cue → BlockSequence → Block → Track → TrackEvent → Synth →
  Waveform` — including the TLV command stream that links tracks to audio, the
  `ReferenceType` variants, and embedded vs streamed waveform resolution
  (`StreamAwbId` / `MemoryAwbId`).

**Data-block reassembly**

- `extract` — decodes each reachable stream to its own file. Stream names are
  resolved through the cue table rather than left as bare numbers. Handles
  external wave banks, audio embedded in the `.acb`, and the hybrid case where
  both hold *different* material; the two sources are merged with content
  hashing to drop duplicates.
- `build` — reassembles a cue's blocks into one continuous clip, honouring the
  timing rules that govern this format:
  - a sustained waveform may outlive the block that introduces it, so material
    is placed on an absolute timeline and never clipped to its own block;
  - a note's written duration is the gap before the *next* note on that track,
    not a cut-off, so audio plays to its natural end;
  - notes on one track play in sequence, not as a chord, so later notes are
    never pulled back onto the block start.
- Simultaneous layers are mixed; blocks with no note commands are rendered as
  silence so later sections stay in place.
- Loopable sections and branch targets are reported so interactive structure
  stays visible.
- `--dry-run` inspects the layout without writing anything.
- Output formats: `mp3`, `m4a`, `ogg`, `opus`, `flac`, `wav`.

**Tool management**

- `fetch-tools` downloads vgmstream and ffmpeg on Windows from stable
  "latest release" permalinks, unpacks into `tools/`, smoke-checks the binaries,
  and only then swaps them in — a broken download cannot clobber a working
  install. Archive members that would escape the install directory are refused.
- Interactive runs offer the download when a required tool is missing, with the
  size shown. Scripts and pipes fail fast with install hints instead of
  blocking on a prompt.

**Usability**

- `INPUT` is always explicit: a directory holding `.acb` / `.awb` files, or a
  single `.acb`. A single file means "just this one"; a directory means every
  bank inside it. The tool never searches the filesystem for input.
- `info` describes cue sheets (cues, blocks, durations, loop points, orphan
  blocks) without writing anything; `--json` for scripting.
- Parallel extraction and encoding (`-j`).

**Safety**

- Inputs are read-only: no code path writes into the input directory, and a
  regression test asserts it.
- No decryption capability: an encrypted HCA bitstream exits with an error.
- Size watchdog on every ffmpeg invocation, with separate caps for
  uncompressed intermediates and finished lossy files. A run whose output
  outgrows its cap is killed and deleted rather than completed.
- Silence padding is only emitted together with an explicit target length,
  since unbounded padding will fill a disk.
- Scratch space is per-job and cleaned up as each job finishes, so peak disk
  use tracks the worker count rather than the batch size.

**Documentation and tests**

- `docs/FORMAT.md` — notes on the `@UTF` table format, the ACB reference
  chains, AWB/AFS2 headers, HCA, and the field quirks that matter in practice.
- 60 tests covering the parser, timeline builder, tool installers and install
  prompts; no network access required.
- English and Chinese READMEs, plus a DISCLAIMER.

### Notes

- This is a general-purpose tool; it does not target any particular game, and
  it ships no game assets, keys, or binaries.
- Audio is decoded by vgmstream and encoded by ffmpeg; both are external
  programs located at runtime and installed by the user.
- Licence texts for both are in `THIRD_PARTY_LICENSES/`.

[0.1.0]: https://github.com/tlipoca-desu/acbtoolkit/releases/tag/v0.1.0
