# Third-party components

The prebuilt downloads (`SDR-S50-Converter.dmg`, `SDR-S50-Converter.exe`) are
self-contained: they bundle a Python runtime, Tk, and **FFmpeg**.

## FFmpeg

| | |
|---|---|
| Project | [FFmpeg](https://ffmpeg.org/) |
| Components used | `ffmpeg`, `ffprobe` |
| License of the bundled builds | **GPL v2 or later** |
| macOS build (arm64) | [eugeneware/ffmpeg-static](https://github.com/eugeneware/ffmpeg-static) |
| Windows build (x64) | [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (`win64-gpl`) |
| Source code | https://github.com/FFmpeg/FFmpeg — the build recipes are in the repositories linked above |

### Why the GPL build, and what that means

The conversion pipeline encodes H.264 with **libx264**, which is GPL-licensed.
FFmpeg builds that include it are therefore GPL, not LGPL. (An LGPL build would
be less restrictive but cannot encode H.264 with x264 at all, so it is not an
option here.)

Because the released `.dmg` / `.exe` **distribute** GPL-licensed binaries,
whoever publishes those releases takes on the GPL's obligations for that
component — most importantly making the corresponding source available. The
links above serve that purpose: they point at the exact upstream builds and
their sources.

FFmpeg is invoked as a **separate process**, not linked into this application,
so the project's own source code is not a derivative work of FFmpeg. If you
would rather keep the two entirely separate, build without bundling and let the
app use an ffmpeg the user installs themselves — the app already falls back to
whatever it finds on `PATH`.

This project itself is licensed **GPL-3.0-or-later** (see `LICENSE`), which is
compatible with the bundled FFmpeg and keeps the released `.dmg` / `.exe`
unambiguously copyleft end to end.

## Patents / codecs

H.264 and AAC are covered by patents in some jurisdictions. Distributing
encoders may carry licensing obligations depending on where and how you
distribute. This is worth checking before shipping binaries widely.
