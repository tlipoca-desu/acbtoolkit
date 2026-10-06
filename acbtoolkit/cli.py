"""
Command line interface.

    acbtoolkit doctor                 check that the external tools are usable
    acbtoolkit info GAME/            describe the cue sheets in a directory
    acbtoolkit extract GAME/ -o out  dump every playable stream to audio files
    acbtoolkit build GAME/ -o songs  flatten block music into complete tracks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .acb import AcbError, AcbFile, build_segments
from .external import SUPPORTED_FORMATS, Ffmpeg, ToolError, find_ffmpeg, find_vgmstream
from .fetch import FetchError, install_ffmpeg, install_vgmstream

# Download sizes shown in the "shall I fetch it?" prompt, so the user knows
# what they are agreeing to before a ~80 MB ffmpeg download starts.
_INSTALLERS = {"vgmstream": install_vgmstream, "ffmpeg": install_ffmpeg}
_DOWNLOAD_SIZE = {"vgmstream": "4 MB", "ffmpeg": "80 MB"}


def tool_root() -> Path:
    """The directory the tool lives in.

    Default output is placed here so a portable copy keeps its results beside
    itself instead of scattering them into whatever the current directory
    happens to be.  Falls back to the working directory when the package is
    installed rather than run from a checkout.
    """
    package_dir = Path(__file__).resolve().parent
    candidate = package_dir.parent
    if (candidate / "pyproject.toml").is_file():
        return candidate
    return Path.cwd()


def default_output(kind: str) -> Path:
    """Default destination: ``<tool>/output/<kind>``."""
    return tool_root() / "output" / kind


def _resolve_input(raw: str | None) -> Path:
    """Resolve the path the user gave.

    The source is always explicit -- either a directory containing `.acb` /
    `.awb` files, or a single `.acb`.  Nothing is searched for or inferred:
    pointing the tool at a location is the caller's decision, not the tool's.

    A single file is returned as-is so callers can keep it narrow -- see
    :func:`_select_banks`.
    """
    if not raw:
        raise SystemExit(
            "no input given\n"
            "  pass a directory containing .acb / .awb files, or a single .acb")

    path = Path(raw).expanduser()
    if path.is_file():
        if path.suffix.lower() == ".acb":
            return path
        raise SystemExit(f"INPUT is not a .acb file: {path}")
    if path.is_dir():
        return path
    raise SystemExit(f"INPUT does not exist: {path}")


def _input_arg(args) -> str | None:
    """INPUT positional wins over --input when both are given."""
    return args.path if getattr(args, "path", None) else args.input


def _select_banks(root: Path, patterns: list[str]) -> list[Path]:
    """Pick the cue sheets to act on.

    A single ``.acb`` given on the command line means "just this one"; a
    directory means every bank inside it (optionally narrowed by patterns).
    Keeping the distinction matters, because resolving a file to its parent
    directory would silently widen the request to the whole game.
    """
    from fnmatch import fnmatch

    if root.is_file() and root.suffix.lower() == ".acb":
        return [root]

    banks = sorted(root.glob("*.acb"))
    if patterns:
        banks = [b for b in banks
                 if any(fnmatch(b.stem, p) or fnmatch(b.name, p)
                        for p in patterns)]
    return banks


# ==================================================================== commands

def cmd_doctor(args) -> int:
    print(f"acbtoolkit {__version__}  (python {sys.version.split()[0]})")
    print()
    status = 0
    for label, finder, override in (
        ("vgmstream", find_vgmstream, args.vgmstream),
        ("ffmpeg", find_ffmpeg, args.ffmpeg),
    ):
        try:
            path = finder(override)
        except ToolError as exc:
            status = 1
            print(f"  [missing] {label}")
            for line in str(exc).splitlines()[1:]:
                print(f"            {line.strip()}")
        else:
            note = ""
            if label == "ffmpeg":
                ffm = Ffmpeg(path)
                missing = [e for e in ("libmp3lame", "flac", "aac")
                           if not ffm.has_encoder(e)]
                if missing:
                    note = f"  (encoders unavailable: {', '.join(missing)})"
            print(f"  [ok]      {label:<10} {path}{note}")
    print()
    print("Done." if not status else "Install the missing tool(s) above, then re-run.")
    return status


def cmd_info(args) -> int:
    root = _resolve_input(_input_arg(args))
    banks = _select_banks(root, args.patterns)
    if not banks:
        print(f"no .acb files in {root}")
        return 1

    out = []
    for bank in banks:
        try:
            acb = AcbFile(bank)
        except AcbError as exc:
            if args.json:
                out.append({"file": str(bank), "error": str(exc)})
            else:
                print(f"! {bank.name}: {exc}")
            continue

        waveforms = acb.waveforms()
        cues = acb.cues()
        orphans = acb.orphan_blocks()

        def cue_duration(cue, acb=acb, waveforms=waveforms) -> float:
            """Length of a cue in seconds.

            Block music gets it from the flattened timeline.  Simple cues
            (ReferenceType 1/2/3, such as a single streamed track) have no
            blocks at all, so fall back to the waveforms they reference.
            """
            if cue.blocks:
                return cue.duration_s
            durations = (waveforms[w].seconds for w in acb.cue_waveforms(cue)
                         if 0 <= w < len(waveforms))
            return max(durations, default=0.0)

        entry = {
            "file": str(bank),
            "name": acb.name,
            "version": acb.version,
            "awb": bool(acb.awb_path),
            "waveforms": len(waveforms),
            "streamed": sum(1 for w in waveforms if w.stream_id is not None),
            "embedded": sum(1 for w in waveforms if w.embedded),
            "cues": [
                {"name": c.name,
                 "reference_type": f"{c.reference_type:#04x}",
                 "blocks": len(c.blocks),
                 "duration_s": round(cue_duration(c), 2),
                 "loop_blocks": [b.name for b in c.loop_blocks]}
                for c in cues
            ],
            "orphan_blocks": [
                {"index": b.index, "name": b.name,
                 "length_s": round(b.length_s, 2)}
                for b in orphans
            ],
        }
        out.append(entry)

        if args.json:
            continue

        print("=" * 78)
        print(f"{bank.name}")
        print(f"  {acb.version}")
        print(f"  wave bank: {'yes, ' + acb.awb_path.name if acb.awb_path else 'none (audio embedded)'}")
        print(f"  waveforms: {len(waveforms)} "
              f"({entry['streamed']} streamed, {entry['embedded']} embedded)")
        for cue in cues:
            loops = f", {len(cue.loop_blocks)} loopable block(s)" if cue.loop_blocks else ""
            shape = (f"{len(cue.blocks)} blocks" if cue.blocks
                     else f"single stream (ref {cue.reference_type:#04x})")
            print(f"  cue {cue.name!r}: {shape}, "
                  f"{cue_duration(cue):.1f}s{loops}")
        if orphans:
            print(f"  ! {len(orphans)} block(s) with audio are not in any sequence:")
            for b in orphans:
                print(f"      blk{b.index} name={b.name} {b.length_s:.2f}s")
        for cue in cues:
            segments, total = build_segments(cue, waveforms)
            gaps = [s for s in segments if s.gap_s > 0.3 and s.parts]
            if gaps:
                print(f"  ! cue {cue.name!r}: {len(gaps)} segment(s) shorter than "
                      f"their block (silence padding)")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_extract(args) -> int:
    from .extract import run as run_extract
    args.input = _resolve_input(_input_arg(args))
    args.banks = _select_banks(args.input, args.patterns)
    # Default lands in <tool>/output/extracted so results live with the tool.
    args.output = (Path(args.output).expanduser() if args.output
                   else default_output("extracted"))
    return run_extract(args)


def cmd_build(args) -> int:
    from .build import run as run_build
    args.input = _resolve_input(_input_arg(args))
    args.banks = _select_banks(args.input, args.patterns)
    args.output = (Path(args.output).expanduser() if args.output
                   else default_output("songs"))
    return run_build(args)


def cmd_fetch(args) -> int:
    tools_dir = tool_root() / "tools"

    wanted = {
        "vgmstream": ("vgmstream-cli.exe", install_vgmstream),
        "ffmpeg": ("ffmpeg.exe", install_ffmpeg),
    }
    if args.tool != "all":
        wanted = {args.tool: wanted[args.tool]}

    failures = 0
    for name, (exe, installer) in wanted.items():
        target = tools_dir / name
        if (target / exe).is_file() and not args.force:
            print(f"{name}: already installed at {target} "
                  f"(use --force to re-download)")
            continue
        print(f"{name}: downloading (~{_DOWNLOAD_SIZE[name]}) ...")
        try:
            installed = installer(tools_dir, force=args.force)
        except FetchError as exc:
            print(f"{name}: error: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(f"{name}: installed {installed / exe}")
    if failures:
        return 1
    print("run 'acbtoolkit doctor' to confirm")
    return 0


# ==================================================================== parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acbtoolkit",
        description="General-purpose CRIWARE ACB/AWB audio container "
                    "parser and data-block reassembler.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  acbtoolkit fetch-tools                  download missing vgmstream / ffmpeg
  acbtoolkit fetch-tools ffmpeg           fetch just ffmpeg (~80 MB)
  acbtoolkit doctor                       check vgmstream / ffmpeg availability
  acbtoolkit info    <dir with .acb>      list cues, blocks and durations
  acbtoolkit extract <dir with .acb> -o out -p "theme_*" -b 320
  acbtoolkit build   <dir with .acb> -o songs --dry-run
  acbtoolkit build   <dir with .acb> -o songs -p "theme_battle_*"
  acbtoolkit build   path/to/one.acb -o songs

INPUT must be given explicitly: either a directory holding .acb / .awb files,
or a single .acb. The tool never searches your filesystem for them.

General-purpose container tool; it does not target any particular game and
contains no decryption capability. You are responsible for complying with the
licence terms of any content you process. See DISCLAIMER.md for details.
""")
    parser.add_argument("--version", action="version",
                        version=f"acbtoolkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, need_input=True):
        if need_input:
            p.add_argument("path", nargs="?", default=None,
                           metavar="INPUT",
                           help="directory containing .acb / .awb files, "
                                "or a single .acb")
            p.add_argument("-i", "--input", default=None,
                           help="same as INPUT, for scripts")
        p.add_argument("--vgmstream", default=None, help="path to vgmstream-cli")
        p.add_argument("--ffmpeg", default=None, help="path to ffmpeg")

    p = sub.add_parser("fetch-tools",
                       help="download vgmstream / ffmpeg automatically (Windows)")
    add_common(p, need_input=False)
    p.add_argument("tool", nargs="?", default="all",
                   choices=["all", "vgmstream", "ffmpeg"],
                   help="which tool to fetch (default: all)")
    p.add_argument("--force", action="store_true",
                   help="re-download even if already installed")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("doctor", help="check external tools")
    add_common(p, need_input=False)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("info", help="describe cue sheets without extracting")
    add_common(p)
    p.add_argument("-p", "--pattern", dest="patterns", action="append", default=[],
                   help="glob filter on file names (repeatable)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("extract",
                       help="dump every playable stream to separate audio files")
    add_common(p)
    p.add_argument("-o", "--output", default=None,
                   help="output directory (default: <tool>/output/extracted)")
    p.add_argument("-p", "--pattern", dest="patterns", action="append", default=[],
                   help="glob filter on file names (repeatable)")
    p.add_argument("-f", "--format", dest="fmt", default="mp3",
                   choices=list(SUPPORTED_FORMATS),
                   help="output format (default: mp3)")
    p.add_argument("-b", "--bitrate", type=int, default=192,
                   help="bitrate in kbps for lossy formats (default: 192)")
    p.add_argument("-j", "--jobs", type=int, default=6,
                   help="parallel workers (default: 6)")
    p.add_argument("--loop", action="store_true",
                   help="render loop points (2 passes + fade) for looping "
                        "sections instead of the natural length")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("build",
                       help="reassemble block music into complete tracks")
    add_common(p)
    p.add_argument("-o", "--output", default=None,
                   help="output directory (default: <tool>/output/songs)")
    p.add_argument("-p", "--pattern", dest="patterns", action="append", default=[],
                   help="glob filter on file names (repeatable)")
    p.add_argument("-f", "--format", dest="fmt", default="mp3",
                   choices=list(SUPPORTED_FORMATS),
                   help="output format (default: mp3)")
    p.add_argument("-b", "--bitrate", type=int, default=192,
                   help="bitrate in kbps (default: 192)")
    p.add_argument("-j", "--jobs", type=int, default=4,
                   help="parallel workers (default: 4)")
    p.add_argument("--dry-run", action="store_true",
                   help="parse and report only, write nothing")
    p.add_argument("--loop", action="store_true",
                   help="respect loop points in source sections")
    p.set_defaults(func=cmd_build)

    return parser


def _ask_auto_install(name: str) -> bool:
    """Ask whether to download a missing tool.

    Only prompts when both stdin and stdout are terminals, so scripts and
    pipes keep the old fail-fast behaviour instead of hanging on a question
    nobody can answer.  Windows only, because that is where the installers
    have prebuilt downloads to offer.
    """
    if not sys.platform.startswith("win"):
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False
    try:
        answer = input(
            f"{name} is required but was not found.  "
            f"download it automatically (~{_DOWNLOAD_SIZE[name]})? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip().lower() not in ("n", "no")


def _resolve_tool(kind: str, finder, explicit, command: str):
    """Find a tool; when missing and interactive, offer to download it."""
    try:
        return finder(explicit)
    except ToolError as first_error:
        error = first_error

    # fetch-tools exists to install tools, doctor reports on them.
    if command in ("doctor", "fetch-tools"):
        return None

    if not _ask_auto_install(kind):
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2)

    print(f"downloading {kind} (~{_DOWNLOAD_SIZE[kind]}) ...")
    try:
        dest = _INSTALLERS[kind](tool_root() / "tools")
    except FetchError as exc:
        print(f"error: {kind} auto-install failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    print(f"{kind}: installed at {dest}")
    try:
        return finder(explicit)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve tool paths once, so every command reports the same clear error.
    args.vgmstream = _resolve_tool(
        "vgmstream", find_vgmstream, args.vgmstream, args.command)
    args.ffmpeg = _resolve_tool(
        "ffmpeg", find_ffmpeg, args.ffmpeg, args.command)

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
