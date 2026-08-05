# SDR-S50 Converter

A small, dependency-free tool for turning Panasonic/JVC camcorder **`.MOD` /
`.TOD`** files (as produced by the Panasonic **SDR-S50** and many JVC Everio /
Canon SD camcorders) into clean, modern **MP4** — or into a lossless **MKV**
archive.

Under the hood a `.MOD` file is just an **MPEG-2 Program Stream** with an
unusual extension. It plays nowhere by default, and it's **interlaced**, so on a
progressive display it shows comb artefacts. This tool deinterlaces, rescales to
square pixels (so 16:9 anamorphic footage looks right), and carries the
recording timestamp from the sidecar `.MOI` into the output.

There are two front-ends over the same engine:

| | |
|---|---|
| **`mod_convert_gui.py`** | A Tkinter desktop GUI — pick files/folders, choose mode + quality, Convert. |
| **`mod-convert.sh`** | A batch CLI for scripting and large collections. |

## The three modes

| Mode | What it does | Output |
|---|---|---|
| **`mp4`** *(default)* | H.264/AAC, `bwdif` with field-doubling → **50p**, square pixels. Keeps the temporal resolution of the interlacing instead of throwing away every other field. | `.mp4` |
| **`mp4-25p`** | Same chain, fields woven to **25p**. Smaller, less smooth motion. | `.mp4` |
| **`archive`** | Lossless remux — video/audio streams copied untouched. | `.mkv` |

Extras that apply to the MP4 modes: the aspect ratio is derived from the
container SAR (so **NTSC** works too), `-a 16:9` overrides it for
mis-flagged files, the field order is detected via `ffprobe`, the recording
date is read from the `.MOI` and written as `creation_time`, and the file's
modification time is preserved. Folders are walked recursively for `.MOD` and
`.TOD`.

## Requirements

- **ffmpeg** and **ffprobe** on your `PATH` — `brew install ffmpeg` on macOS.
- For the GUI: **Python 3.9+ with Tkinter**. The python.org and Homebrew Python
  builds include it. (`python3 -c "import tkinter"` should not error.)

## GUI

```bash
python3 mod_convert_gui.py
```

Add files or a folder, pick a mode, set the CRF quality slider, optionally force
an aspect ratio or an output folder, then **Convert**. Progress is shown
per-file and overall, and conversions run on a background thread so the window
stays responsive; **Cancel** stops after the current file's ffmpeg is
terminated (partial output is removed).

## CLI

```bash
./mod-convert.sh /Volumes/SD/SD_VIDEO/PRG001        # everything -> MP4
./mod-convert.sh -m archive -o /archiv /Volumes/SD  # raw safety copy first
./mod-convert.sh -a 16:9 MOV0*.MOD                  # force the aspect flag
./mod-convert.sh -q 22 clip.MOD                     # smaller files
```

```
Options:
  -m MODE   mp4 (default) | mp4-25p | archive
  -o DIR    output folder (default: next to each source file)
  -q CRF    x264 quality, lower = better/larger (default: 20)
  -a A:B    force display aspect (e.g. 16:9)
  -y        overwrite existing outputs
  -h        help
```

### The one-liner, without the script

```bash
ffmpeg -fflags +genpts -i MOV00F.MOD \
  -vf "bwdif=mode=send_field:parity=tff,scale=trunc(iw*sar/2)*2:ih:flags=lanczos,setsar=1" \
  -c:v libx264 -crf 20 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart MOV00F.mp4
```

## Sizes & quality

For SD footage with sensor noise at 50p, **CRF 20** lands near the original
size; drop to **CRF 22** if storage matters. On the reference clip (6.3 MB
source): CRF 20 → ~6.9 MB, CRF 22 → ~4.6 MB.

## Notes

- **Just want to look at a clip?** Rename it to `.mpg` — VLC plays it straight
  away. The conversion is for compatibility with everything else and for getting
  rid of the interlacing.
- **Editing rather than archiving?** Use an intermediate codec instead of
  long-GOP H.264 — much kinder to Resolve/Premiere:
  `-c:v prores_ks -profile:v 3 -c:a pcm_s16le out.mov`.
- **Before deleting originals in bulk**, spot-check a few clips for A/V sync.
  Camcorder `.MOD` files occasionally have timestamp jumps at segment
  boundaries that `-fflags +genpts` smooths over — usually correctly, but not
  guaranteed.

## Development

The conversion logic lives in `mod_convert_core.py` with no GUI dependency, so
it can be tested headlessly (no ffmpeg needed to build/validate commands):

```bash
python3 -m unittest test_core -v
```
