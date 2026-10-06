"""
Tests for the tool downloader.

All offline: the HTTP layer is replaced with fake payloads, so these cover
the install logic (staging, zip-slip refusal, flattening, smoke check) and
the platform guard without touching the network.
"""

from __future__ import annotations

import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acbtoolkit import fetch as fetch_mod  # noqa: E402
from acbtoolkit.fetch import FetchError, install_ffmpeg, install_vgmstream  # noqa: E402


def _fake_zip(members: dict[str, bytes] | None = None) -> bytes:
    if members is None:
        members = {"vgmstream-cli.exe": b"MZ fake",
                   "avcodec-vgmstream-59.dll": b"fake dll"}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _fake_downloader(payload: bytes):
    """Stand-in for _download: records the URL, writes `payload`."""
    calls: list[str] = []

    def download(url: str, dest: Path, timeout: int = 120) -> None:
        calls.append(url)
        Path(dest).write_bytes(payload)

    download.calls = calls                      # type: ignore[attr-defined]
    return download


@pytest.fixture()
def fast_paths(monkeypatch):
    """Keep tempdirs small and the smoke check instant."""
    monkeypatch.setattr(fetch_mod.tempfile, "prefix", "t_")
    yield


@pytest.fixture()
def windows(monkeypatch):
    """Make the installers believe they are running on Windows.

    They are Windows-only on purpose -- that is where upstream publishes
    prebuilt binaries -- so the success paths can only be exercised by
    simulating the platform.  Tests asserting the *guard* set a non-Windows
    value afterwards.
    """
    monkeypatch.setattr(fetch_mod.sys, "platform", "win32")
    return monkeypatch


def test_url_is_the_stable_latest_release_asset():
    assert fetch_mod.VGMSTREAM_RELEASE_URL == (
        "https://github.com/vgmstream/vgmstream/releases/latest/download/"
        "vgmstream-win64.zip")


def test_installs_into_tools_dir(tmp_path, windows, monkeypatch):
    dl = _fake_downloader(_fake_zip())
    monkeypatch.setattr(fetch_mod, "_download", dl)
    monkeypatch.setattr(fetch_mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, '{"version":"r2117"}'))

    dest = install_vgmstream(tmp_path)

    assert (dest / "vgmstream-cli.exe").is_file()
    assert (dest / "avcodec-vgmstream-59.dll").is_file()
    assert dl.calls == [fetch_mod.VGMSTREAM_RELEASE_URL]


def test_existing_install_is_reused(tmp_path, windows, monkeypatch):
    dest = tmp_path / "vgmstream"
    dest.mkdir()
    (dest / "vgmstream-cli.exe").write_bytes(b"MZ existing")

    def boom(*a, **k):
        raise AssertionError("existing install must not trigger a download")

    monkeypatch.setattr(fetch_mod, "_download", boom)
    assert install_vgmstream(tmp_path) == dest
    assert (dest / "vgmstream-cli.exe").read_bytes() == b"MZ existing"


def test_force_replaces_a_working_install(tmp_path, windows, monkeypatch):
    dest = tmp_path / "vgmstream"
    dest.mkdir()
    (dest / "vgmstream-cli.exe").write_bytes(b"MZ old")

    dl = _fake_downloader(_fake_zip())
    monkeypatch.setattr(fetch_mod, "_download", dl)
    monkeypatch.setattr(fetch_mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, '{"version":"r2117"}'))

    install_vgmstream(tmp_path, force=True)

    assert (dest / "vgmstream-cli.exe").read_bytes() == b"MZ fake"
    assert (dest / "avcodec-vgmstream-59.dll").is_file()


def test_zip_slip_member_is_refused(tmp_path, windows, monkeypatch):
    evil = _fake_zip({"vgmstream-cli.exe": b"MZ",
                      "../escape.txt": b"outside"})
    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(evil))

    with pytest.raises(FetchError, match="escapes"):
        install_vgmstream(tmp_path)
    assert (tmp_path / "escape.txt").exists() is False


def test_flattens_a_wrapper_folder(tmp_path, windows, monkeypatch):
    wrapped = _fake_zip({"vgmstream-win64/vgmstream-cli.exe": b"MZ fake",
                         "vgmstream-win64/README.md": b"readme"})
    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(wrapped))
    monkeypatch.setattr(fetch_mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, '{"version":"r2117"}'))

    dest = install_vgmstream(tmp_path)

    assert (dest / "vgmstream-cli.exe").is_file()
    assert (dest / "README.md").is_file()
    assert not [d for d in dest.iterdir() if d.is_dir()], \
        "wrapper folder should be gone"


def test_non_zip_payload_is_reported(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_download",
                        _fake_downloader(b"<html>probably an error page</html>"))

    with pytest.raises(FetchError, match="not a zip"):
        install_vgmstream(tmp_path)
    # Nothing was installed.
    assert not (tmp_path / "vgmstream").exists()


def test_archive_without_the_binary_is_reported(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_download",
                        _fake_downloader(_fake_zip({"readme.txt": b"hi"})))

    with pytest.raises(FetchError, match="vgmstream-cli"):
        install_vgmstream(tmp_path)


def test_failed_smoke_check_blocks_install(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(_fake_zip()))

    def broken(*a, **k):
        raise OSError("cannot execute")

    monkeypatch.setattr(fetch_mod.subprocess, "run", broken)

    with pytest.raises(FetchError, match="failed to run"):
        install_vgmstream(tmp_path)
    assert not (tmp_path / "vgmstream").exists(), \
        "a binary that cannot run must never be installed"


def test_non_windows_gets_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod.sys, "platform", "linux")

    with pytest.raises(FetchError, match="Windows"):
        install_vgmstream(tmp_path)


# ==================================================================== ffmpeg

def _fake_ffmpeg_zip() -> bytes:
    return _fake_zip({
        "ffmpeg-8.0-essentials_build/bin/ffmpeg.exe": b"MZ fake",
        "ffmpeg-8.0-essentials_build/bin/avcodec-61.dll": b"dll",
        "ffmpeg-8.0-essentials_build/doc/ffmpeg.1": b"docs",
        "ffmpeg-8.0-essentials_build/presets/ffpreset": b"preset",
    })


def _ok_ffmpeg_smoke(*a, **k):
    return subprocess.CompletedProcess([], 0, "ffmpeg version 8.0 ...")


def test_ffmpeg_url_is_the_essentials_permalink():
    assert fetch_mod.FFMPEG_RELEASE_URL == (
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")


def test_installs_ffmpeg_and_hoists_bin(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(_fake_ffmpeg_zip()))
    monkeypatch.setattr(fetch_mod.subprocess, "run", _ok_ffmpeg_smoke)

    dest = install_ffmpeg(tmp_path)

    # bin/ contents land at the root, docs and presets do not survive
    assert (dest / "ffmpeg.exe").is_file()
    assert (dest / "avcodec-61.dll").is_file()
    assert not (dest / "doc").exists() and not (dest / "presets").exists()
    assert not [d for d in dest.iterdir() if d.is_dir()]


def test_ffmpeg_existing_install_is_reused(tmp_path, windows, monkeypatch):
    dest = tmp_path / "ffmpeg"
    dest.mkdir()
    (dest / "ffmpeg.exe").write_bytes(b"MZ existing")

    def boom(*a, **k):
        raise AssertionError("existing install must not trigger a download")

    monkeypatch.setattr(fetch_mod, "_download", boom)
    assert install_ffmpeg(tmp_path) == dest


def test_ffmpeg_force_replaces(tmp_path, windows, monkeypatch):
    dest = tmp_path / "ffmpeg"
    dest.mkdir()
    (dest / "ffmpeg.exe").write_bytes(b"MZ old")

    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(_fake_ffmpeg_zip()))
    monkeypatch.setattr(fetch_mod.subprocess, "run", _ok_ffmpeg_smoke)

    install_ffmpeg(tmp_path, force=True)
    assert (dest / "ffmpeg.exe").read_bytes() == b"MZ fake"


def test_ffmpeg_without_binary_is_reported(tmp_path, windows, monkeypatch):
    members = {"ffmpeg-8.0-essentials_build/doc/only-docs.txt": b"x"}
    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(_fake_zip(members)))

    with pytest.raises(FetchError, match="ffmpeg.exe"):
        install_ffmpeg(tmp_path)
    assert not (tmp_path / "ffmpeg").exists()


def test_ffmpeg_smoke_requires_version_banner(tmp_path, windows, monkeypatch):
    monkeypatch.setattr(fetch_mod, "_download", _fake_downloader(_fake_ffmpeg_zip()))
    monkeypatch.setattr(fetch_mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0, "garbage"))

    with pytest.raises(FetchError, match="version"):
        install_ffmpeg(tmp_path)
    assert not (tmp_path / "ffmpeg").exists()


def test_ffmpeg_non_windows_gets_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_mod.sys, "platform", "linux")

    with pytest.raises(FetchError, match="Windows"):
        install_ffmpeg(tmp_path)


# ==================================================================== 交互询问

def test_prompt_defaults_to_yes(monkeypatch):
    import acbtoolkit.cli as cli

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert cli._ask_auto_install("vgmstream") is True


def test_prompt_declines_on_n(monkeypatch):
    import acbtoolkit.cli as cli

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert cli._ask_auto_install("ffmpeg") is False


def test_prompt_skipped_without_a_terminal(monkeypatch):
    import acbtoolkit.cli as cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    def no_ask(prompt=""):
        raise AssertionError("must not prompt without a terminal")

    monkeypatch.setattr("builtins.input", no_ask)
    assert cli._ask_auto_install("vgmstream") is False


def test_prompt_skipped_off_windows(monkeypatch):
    import acbtoolkit.cli as cli

    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input",
                        lambda prompt="": pytest.fail("must not prompt"))
    assert cli._ask_auto_install("ffmpeg") is False


def test_main_declined_prompt_exits_2(tmp_path, monkeypatch, capsys):
    """Without consent the run ends with the plain error, exit code 2."""
    import acbtoolkit.cli as cli
    from acbtoolkit.external import ToolError

    monkeypatch.setattr(cli, "find_vgmstream",
                        lambda explicit=None: (_ for _ in ()).throw(
                            ToolError("vgmstream-cli not found.")))
    monkeypatch.setattr(cli, "_ask_auto_install", lambda name: False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["build", str(tmp_path)])
    assert excinfo.value.code == 2
    assert "not found" in capsys.readouterr().err


def test_main_accepted_prompt_installs_then_proceeds(tmp_path, monkeypatch, capsys):
    """Consent runs the installer, then the command carries on."""
    import acbtoolkit.cli as cli
    from acbtoolkit.external import ToolError

    calls = {"install": 0, "find": 0}

    def fake_find(explicit=None):
        calls["find"] += 1
        if calls["find"] == 1:
            raise ToolError("vgmstream-cli not found.")
        return tmp_path / "tools" / "vgmstream" / "vgmstream-cli.exe"

    def fake_installer(tools_dir, force=False, timeout=600):
        calls["install"] += 1
        return tmp_path / "tools" / "vgmstream"

    monkeypatch.setattr(cli, "find_vgmstream", fake_find)
    monkeypatch.setattr(cli, "_ask_auto_install", lambda name: True)
    monkeypatch.setattr(cli, "_INSTALLERS", {"vgmstream": fake_installer,
                                             "ffmpeg": fake_installer})
    monkeypatch.setattr(cli, "find_ffmpeg",
                        lambda explicit=None: tmp_path / "ffmpeg.exe")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    # Give the directory something to act on, so the run reaches the tool flow
    # rather than stopping at "no .acb files matched".
    (tmp_path / "sample.acb").write_bytes(b"@UTF")

    cli.main(["build", str(tmp_path), "--dry-run"])
    assert calls["install"] == 1
    assert calls["find"] == 2
    out = capsys.readouterr()
    assert "installed at" in out.out
