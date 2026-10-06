# acbtoolkit

A general-purpose CRIWARE `ACB` / `AWB` audio container parser and data-block
reassembler, built on [vgmstream](https://github.com/vgmstream/vgmstream).

**[中文说明 →](README.zh.md)**

---

## 1. About

`ACB` (cue sheet) and `AWB` (wave bank) are container formats used by games
built on the CRI Atom middleware. In the common case an `ACB` contains no audio
of its own: it describes *what* to play and *when*, while the bitstreams live in
the `.awb` beside it.

Internally a cue sheet is a database of indexed blocks, so this library reads
that structure and reassembles the audio data blocks back into complete pieces.

## 2. Features

- Parses the `@UTF` table structure of `ACB` files, reading the cue, block and
  waveform indices.
- Decodes audio streams from `AWB` containers by calling vgmstream.
- Reassembles audio data blocks into complete clips, following the indices.
- Outputs common audio formats, using ffmpeg.

## 3. Scope and limits

- This tool **contains no decryption capability**. An encrypted HCA bitstream
  causes it to exit with an error.
- It ships **no game audio, no keys, no game-specific configuration, and no
  binary dependencies**.
- It performs **container parsing, data-block reassembly, and format conversion
  only**.
- It **only reads** input files, and writes nothing to the input directory.

## 4. Requirements

| Component | Licence | Role |
| --- | --- | --- |
| [vgmstream](https://github.com/vgmstream/vgmstream) | ISC | Decodes `AWB` containers and `HCA` bitstreams |
| [ffmpeg](https://ffmpeg.org/) | LGPL / GPL | Encoding, mixing, concatenation |
| Python 3.9+ | — | Runtime |

**Dependencies must be installed by the user. This repository distributes no
binaries.** Licence texts are collected in
[`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/).

## 5. Installation

Python 3.9 or newer is required. No third-party Python packages are needed.

```bash
git clone https://github.com/tlipoca-desu/acbtoolkit.git
cd acbtoolkit
```

Then either run it in place:

```bash
python -m acbtoolkit --help
```

or install the `acbtoolkit` command:

```bash
pip install .
```

## 6. Usage

```bash
acbtoolkit build <input directory> -o <output directory>
```

`<input directory>` must contain `.acb` / `.awb` files. It is always given
explicitly — either a directory or a single `.acb`; the tool does not search
your filesystem for input.

**The tool reads from it only** — nothing is written to the input directory;
all output goes to the directory you specify.

## 7. Licence

The code in this repository is released under the **MIT Licence**. See
[LICENSE](LICENSE).

Licence texts for third-party components are collected in
[`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/).

## 8. Disclaimer

This is a general-purpose CRIWARE `ACB`/`AWB` container parsing and data
reassembly library. It does not target any particular game.

The tool contains, bundles, and provides no game audio files, no keys, and no
decryption capability.

Users processing audio data from any game must independently ensure that their
conduct complies with the licence terms published by that game's rights holder.

Any and all consequences arising from a user's use of the processing results —
including public distribution, redistribution, or any other use beyond personal,
private use — are borne solely by that user.

This tool is provided "as is", and the developers make no express or implied
warranties as to its accuracy, reliability, or fitness for any particular
purpose.

The developers accept no liability for any direct, indirect, incidental,
special, or consequential damages arising from the use of, or inability to use,
this tool.

The full text is in [DISCLAIMER.md](DISCLAIMER.md).

## 9. Takedown requests

The developers undertake to remove or adjust this tool promptly upon receipt of
a valid notice from a rights holder.

Contact: open an issue on this repository.

## 10. Format notes

Details of the `@UTF` table layout, the ACB reference chains, and the AWB /
HCA structures this toolkit reads are documented in
[`docs/FORMAT.md`](docs/FORMAT.md) ([中文版](docs/FORMAT.zh.md)).

## 11. Third-party components

**vgmstream** — ISC Licence. The copyright notice and the full licence text are
retained in [`THIRD_PARTY_LICENSES/vgmstream.txt`](THIRD_PARTY_LICENSES/vgmstream.txt).

**ffmpeg** — LGPL / GPL depending on the build. Licence terms, source locations
and redistribution obligations are noted in
[`THIRD_PARTY_LICENSES/ffmpeg.txt`](THIRD_PARTY_LICENSES/ffmpeg.txt).
