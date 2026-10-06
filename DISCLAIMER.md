# Disclaimer

*[中文版 →](DISCLAIMER.zh.md)*

---

### General-purpose library

This is a general-purpose CRIWARE `ACB`/`AWB` container parsing and data
reassembly library. **It does not target any particular game.**

### No bundled content, no decryption

The tool contains, bundles, and provides no game audio files, no keys, and no
decryption capability. An encrypted HCA bitstream causes it to exit with an
error rather than attempting to decode it.

This repository ships no game assets, no game-specific configuration, and no
binary dependencies.

### Compliance is the user's responsibility

Users processing audio data from any game must independently ensure that their
conduct complies with the licence terms published by that game's rights holder.

Any and all consequences arising from a user's use of the processing results —
including public distribution, redistribution, or any other use beyond personal,
private use — are borne solely by that user.

### No warranty

The software is provided "as is", and the developers make no express or implied
warranties as to its accuracy, reliability, or fitness for any particular
purpose.

### Limitation of liability

The developers accept no liability for any direct, indirect, incidental,
special, or consequential damages arising from the use of, or inability to use,
this tool.

### Takedown requests

The developers undertake to remove or adjust this tool promptly upon receipt of
a valid notice from a rights holder.

Contact: open an issue on this repository.

### Third-party components

- **vgmstream** — ISC Licence. The copyright notice and the full licence text
  are retained in [`THIRD_PARTY_LICENSES/vgmstream.txt`](THIRD_PARTY_LICENSES/vgmstream.txt).
- **ffmpeg** — LGPL / GPL depending on the build. See
  [`THIRD_PARTY_LICENSES/ffmpeg.txt`](THIRD_PARTY_LICENSES/ffmpeg.txt).
