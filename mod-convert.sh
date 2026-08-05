#!/usr/bin/env bash
# SDR-S50 Converter — convert Panasonic/JVC camcorder .MOD/.TOD files
# Copyright (C) 2026  larszu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# mod-convert.sh — Convert Panasonic/JVC camcorder .MOD / .TOD files to MP4
# (or losslessly archive them), fixing the two things that make these clips
# awkward on modern machines: the unknown extension and the interlacing.
#
# A .MOD file is a plain MPEG-2 Program Stream. This script:
#   * deinterlaces with bwdif (50p field-doubling by default, or 25p),
#   * rescales to square pixels using the container SAR (works for NTSC too),
#   * copies the recording time from the sidecar .MOI into the MP4 metadata,
#   * preserves the file's modification time,
#   * or, in archive mode, remuxes the untouched streams into .mkv or .mp4.
#
# Usage:
#   ./mod-convert.sh [options] <file-or-folder> [more...]
#
# Options:
#   -m MODE   mp4 (default) | mp4-25p | archive
#   -o DIR    output folder (default: next to each source file)
#   -q CRF    x264 quality, lower = better/larger (default: 20)
#   -a A:B    force display aspect (e.g. 16:9) — overrides the container flag
#   -f ORDER  field order: auto (default) | tff | bff. Forcing the wrong one
#             is what causes comb/judder artefacts, so override when needed.
#   -c CONT   archive container: mkv (default) | mp4. Both are lossless.
#   -i        analyse only: print format details, convert nothing
#   -y        overwrite existing outputs
#   -h        show this help
#
# Examples:
#   ./mod-convert.sh /Volumes/SD/SD_VIDEO/PRG001         # everything -> MP4
#   ./mod-convert.sh -m archive -o /archiv /Volumes/SD   # raw safety copy first
#   ./mod-convert.sh -a 16:9 MOV0*.MOD                   # force aspect flag
#   ./mod-convert.sh -f bff clip.MOD                     # fix wrong field order
#   ./mod-convert.sh -m archive -c mp4 clip.MOD          # lossless, but as .mp4
#   ./mod-convert.sh -i /Volumes/SD                      # just analyse
#
# Requires: ffmpeg + ffprobe on PATH.

set -euo pipefail

MODE="mp4"
OUTDIR=""
CRF="20"
ASPECT="auto"
FIELD="auto"
CONTAINER="mkv"
INFO_ONLY=0
OVERWRITE="-n"

# Print the comment block that starts at the description line (skipping the
# licence header) up to the first non-comment line.
usage() {
  awk '/^# mod-convert\.sh/{p=1}
       p && /^#/ {sub(/^# ?/, ""); print; next}
       p {exit}' "$0"
  exit "${1:-0}"
}

while getopts ":m:o:q:a:f:c:iyh" opt; do
  case "$opt" in
    m) MODE="$OPTARG" ;;
    o) OUTDIR="$OPTARG" ;;
    q) CRF="$OPTARG" ;;
    a) ASPECT="$OPTARG" ;;
    f) FIELD="$OPTARG" ;;
    c) CONTAINER="$OPTARG" ;;
    i) INFO_ONLY=1 ;;
    y) OVERWRITE="-y" ;;
    h) usage 0 ;;
    \?) echo "Unknown option: -$OPTARG" >&2; usage 1 ;;
    :) echo "Option -$OPTARG needs an argument" >&2; usage 1 ;;
  esac
done
shift $((OPTIND - 1))

case "$MODE" in
  mp4|mp4-25p|archive) ;;
  *) echo "Invalid mode: $MODE (use mp4 | mp4-25p | archive)" >&2; exit 1 ;;
esac

case "$FIELD" in
  auto|tff|bff) ;;
  *) echo "Invalid field order: $FIELD (use auto | tff | bff)" >&2; exit 1 ;;
esac

case "$CONTAINER" in
  mkv|mp4) ;;
  *) echo "Invalid archive container: $CONTAINER (use mkv | mp4)" >&2; exit 1 ;;
esac

if [ "$#" -eq 0 ]; then
  echo "No input files or folders given." >&2
  usage 1
fi

command -v ffmpeg  >/dev/null || { echo "ffmpeg not found on PATH"  >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found on PATH" >&2; exit 1; }

# ---- helpers -------------------------------------------------------------- #

# Recording timestamp from the matching .MOI sidecar, printed as
# "YYYY-MM-DDTHH:MM:SS", or empty if unreadable/implausible.
moi_creation_time() {
  local mod="$1" moi
  moi="${mod%.*}.MOI"
  [ -f "$moi" ] || moi="${mod%.*}.moi"
  [ -f "$moi" ] || return 0
  # offset 6: year (uint16 BE), month, day, hour, minute, then at offset 12
  # milliseconds-within-the-minute (uint16 BE). The seconds are NOT a plain
  # byte -- 0xBF68 = 49000 ms = second 49.
  local bytes
  bytes=$(od -An -tu1 -j6 -N8 "$moi" 2>/dev/null | tr -s ' ')
  # shellcheck disable=SC2086
  set -- $bytes
  [ "$#" -eq 8 ] || return 0
  local year=$(( ($1 << 8) | $2 )) month="$3" day="$4" hour="$5" min="$6"
  local ms=$(( ($7 << 8) | $8 ))
  [ "$ms" -lt 60000 ] || return 0
  local sec=$(( ms / 1000 ))
  [ "$year" -ge 2000 ] && [ "$year" -le 2099 ] || return 0
  [ "$month" -ge 1 ] && [ "$month" -le 12 ] || return 0
  printf '%04d-%02d-%02dT%02d:%02d:%02d' "$year" "$month" "$day" "$hour" "$min" "$sec"
}

# Probe one value from the first video stream.
vprobe() {
  ffprobe -v error -select_streams v:0 -show_entries "stream=$2" \
    -of default=nw=1:nk=1 "$1" 2>/dev/null | head -1
}

# Print a human-readable analysis of a source file (-i).
describe_one() {
  local src="$1" w h sar fo fps codec dur col acodec moi ct
  codec=$(vprobe "$src" codec_name); w=$(vprobe "$src" width); h=$(vprobe "$src" height)
  sar=$(vprobe "$src" sample_aspect_ratio); fo=$(vprobe "$src" field_order)
  fps=$(vprobe "$src" r_frame_rate); col=$(vprobe "$src" color_space)
  dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$src" 2>/dev/null)
  acodec=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name,sample_rate,channels \
           -of default=nw=1:nk=1 "$src" 2>/dev/null | paste -sd'/' -)
  echo "$src"
  echo "  Video      ${codec:-?} ${w}x${h}   SAR ${sar:-?}   -> square-pixel output $(output_width "$src")x${h}"
  echo "  Scan       ${fo:-?}    Frame rate ${fps:-?}"
  echo "  Colour     ${col:-untagged}   Duration ${dur:-?} s"
  echo "  Audio      ${acodec:-none}"
  ct="$(moi_creation_time "$src" || true)"
  echo "  Recorded   ${ct:-no readable .MOI}"
  case "$fo" in
    tt|tff|tb) echo "  -> interlaced TFF; deinterlace with -f tff (default auto detects this)" ;;
    bb|bff|bt) echo "  -> interlaced BFF; deinterlace with -f bff" ;;
    progressive) echo "  -> progressive; no deinterlacing needed" ;;
  esac
  echo
}

# Square-pixel output width for a given source, honouring -a override.
output_width() {
  local src="$1" w h sar_num sar_den
  read -r w h sar_num sar_den < <(
    ffprobe -v error -select_streams v:0 \
      -show_entries stream=width,height,sample_aspect_ratio \
      -of default=nw=1:nk=1 "$src" | paste -sd' ' - | \
      awk '{ n=$3; sub(":.*","",n); d=$3; sub(".*:","",d);
             if(d==""||d==0){n=1;d=1}; print $1, $2, n, d }'
  )
  if [ "$ASPECT" != "auto" ]; then
    local an=${ASPECT%%:*} ad=${ASPECT##*:}
    awk -v h="$h" -v n="$an" -v d="$ad" 'BEGIN{ printf "%d", int((h*n/d)/2+0.5)*2 }'
  else
    [ "${sar_num:-0}" -gt 0 ] || { sar_num=1; sar_den=1; }
    awk -v w="$w" -v n="$sar_num" -v d="$sar_den" 'BEGIN{ printf "%d", int((w*n/d)/2+0.5)*2 }'
  fi
}

convert_one() {
  local src="$1"
  local base dst dir ow oh fo parity vf ct cs cp ct_trc crange colour_args

  base="$(basename "$src")"; base="${base%.*}"
  dir="${OUTDIR:-$(dirname "$src")}"
  mkdir -p "$dir"

  if [ "$MODE" = "archive" ]; then
    dst="$dir/$base.$CONTAINER"
    echo ">> $src -> $dst  (lossless remux, streams copied)"
    ffmpeg -hide_banner "$OVERWRITE" -fflags +genpts -i "$src" -map 0 -c copy "$dst"
    touch -r "$src" "$dst" 2>/dev/null || true
    return
  fi

  dst="$dir/$base.mp4"
  ow="$(output_width "$src")"
  # Height is unchanged -- only the width is corrected for square pixels.
  # Do NOT use scale=w:-2 here: that preserves the *storage* aspect (720:576)
  # and would stretch 1024x576 into 1024x820.
  oh="$(vprobe "$src" height)"
  oh=$(( (oh / 2) * 2 ))
  fo="$(vprobe "$src" field_order)"

  # Field order: explicit override wins, otherwise trust the stream flag.
  if [ "$FIELD" != "auto" ]; then
    parity="$FIELD"
  else
    case "$fo" in
      bb|bff|bt) parity="bff" ;;
      tt|tff|tb) parity="tff" ;;
      *)         parity="" ;;
    esac
  fi

  vf=""
  if [ -n "$parity" ]; then
    local bmode="send_field"
    [ "$MODE" = "mp4-25p" ] && bmode="send_frame"
    vf="bwdif=mode=$bmode:parity=$parity,"
  fi
  vf="${vf}scale=${ow}:${oh}:flags=lanczos,setsar=1"

  # Carry the source colour characteristics across; PAL material is BT.470BG
  # and would otherwise be rendered as if it were BT.709.
  colour_args=()
  cs=$(vprobe "$src" color_space);      [ -n "$cs" ] && [ "$cs" != "unknown" ] && colour_args+=(-colorspace "$cs")
  cp=$(vprobe "$src" color_primaries);  [ -n "$cp" ] && [ "$cp" != "unknown" ] && colour_args+=(-color_primaries "$cp")
  ct_trc=$(vprobe "$src" color_transfer); [ -n "$ct_trc" ] && [ "$ct_trc" != "unknown" ] && colour_args+=(-color_trc "$ct_trc")
  crange=$(vprobe "$src" color_range);  [ -n "$crange" ] && [ "$crange" != "unknown" ] && colour_args+=(-color_range "$crange")

  ct="$(moi_creation_time "$src" || true)"
  echo ">> $src -> $dst  (mode=$MODE crf=$CRF parity=${parity:-none} scan=${fo:-?})"
  ffmpeg -hide_banner "$OVERWRITE" -fflags +genpts -i "$src" \
    -vf "$vf" \
    -c:v libx264 -crf "$CRF" -preset medium -pix_fmt yuv420p \
    "${colour_args[@]}" \
    -c:a aac -b:a 192k -movflags +faststart \
    ${ct:+-metadata creation_time="$ct"} \
    "$dst"
  touch -r "$src" "$dst" 2>/dev/null || true
}

# ---- discover inputs ------------------------------------------------------ #

# Analyse or convert, depending on -i.
handle_one() {
  if [ "$INFO_ONLY" = "1" ]; then describe_one "$1"; else convert_one "$1"; fi
}

process() {
  local target="$1"
  if [ -d "$target" ]; then
    # Recurse; match .MOD / .TOD case-insensitively.
    while IFS= read -r -d '' f; do
      handle_one "$f"
    done < <(find "$target" -type f \( -iname '*.mod' -o -iname '*.tod' \) -print0 | sort -z)
  elif [ -f "$target" ]; then
    handle_one "$target"
  else
    echo "Skipping (not found): $target" >&2
  fi
}

for arg in "$@"; do
  process "$arg"
done

echo "Done."
