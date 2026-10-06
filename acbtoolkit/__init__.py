"""
acbtoolkit -- read CRIWARE ACB/AWB audio banks and reassemble block-based music.

Two things this library does that a plain format converter does not:

* **Extraction** resolves each stream's name through the cue sheet, and merges
  both containers when an ACB embeds audio *and* references a wave bank.
* **Assembly** flattens interactive block music back into continuous tracks by
  following the cue's BlockSequence and laying sustained waveforms on an
  absolute timeline.

Decoding is delegated to vgmstream and encoding to FFmpeg; see
:mod:`acbtoolkit.external`.

Typical use::

    from acbtoolkit import AcbFile, build_segments

    acb = AcbFile("example.acb")
    for cue in acb.cues():
        segments, total = build_segments(cue, acb.waveforms())
        print(cue.name, len(segments), f"{total:.1f}s")
"""

from .acb import AcbError, AcbFile, Block, Cue, Segment, Waveform, build_segments
from .cri_utf import UtfError, UtfTable
from .external import Ffmpeg, ToolError, Vgmstream, find_ffmpeg, find_vgmstream

__version__ = "0.1.0"

__all__ = [
    "AcbError", "AcbFile", "Block", "Cue", "Segment", "Waveform",
    "build_segments", "UtfError", "UtfTable",
    "Ffmpeg", "ToolError", "Vgmstream", "find_ffmpeg", "find_vgmstream",
    "__version__",
]
