#!/usr/bin/env bash
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
#   * or, in archive mode, remuxes the untouched streams into a .mkv.
#
# Usage:
#   ./mod-convert.sh [options] <file-or-folder> [more...]
#
# Options:
#   -m MODE   mp4 (default) | mp4-25p | archive
#   -o DIR    output folder (default: next to each source file)
#   -q CRF    x264 quality, lower = better/larger (default: 20)
#   -a A:B    force display aspect (e.g. 16:9) — overrides the container flag
#   -y        overwrite existing outputs
#   -h        show this help
#
# Examples:
#   ./mod-convert.sh /Volumes/SD/SD_VIDEO/PRG001         # everything -> MP4
#   ./mod-convert.sh -m archive -o /archiv /Volumes/SD   # raw safety copy first
#   ./mod-convert.sh -a 16:9 MOV0*.MOD                   # force aspect flag
#
# Requires: ffmpeg + ffprobe on PATH.

set -euo pipefail

MODE="mp4"
OUTDIR=""
CRF="20"
ASPECT="auto"
OVERWRITE="-n"

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while getopts ":m:o:q:a:yh" opt; do
  case "$opt" in
    m) MODE="$OPTARG" ;;
    o) OUTDIR="$OPTARG" ;;
    q) CRF="$OPTARG" ;;
    a) ASPECT="$OPTARG" ;;
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
  # year: 2 bytes BE at offset 6; then month, day, hour, min, sec.
  local bytes
  bytes=$(od -An -tu1 -j6 -N7 "$moi" 2>/dev/null | tr -s ' ')
  # shellcheck disable=SC2086
  set -- $bytes
  [ "$#" -eq 7 ] || return 0
  local year=$(( ($1 << 8) | $2 )) month="$3" day="$4" hour="$5" min="$6" sec="$7"
  [ "$year" -ge 2000 ] && [ "$year" -le 2099 ] || return 0
  [ "$month" -ge 1 ] && [ "$month" -le 12 ] || return 0
  printf '%04d-%02d-%02dT%02d:%02d:%02d' "$year" "$month" "$day" "$hour" "$min" "$sec"
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
  local base ext dst dir ow fo parity vf ct
  base="$(basename "$src")"; base="${base%.*}"
  dir="${OUTDIR:-$(dirname "$src")}"
  mkdir -p "$dir"

  if [ "$MODE" = "archive" ]; then
    dst="$dir/$base.mkv"
    echo ">> $src -> $dst  (lossless remux)"
    ffmpeg -hide_banner "$OVERWRITE" -fflags +genpts -i "$src" -map 0 -c copy "$dst"
    touch -r "$src" "$dst" 2>/dev/null || true
    return
  fi

  dst="$dir/$base.mp4"
  ow="$(output_width "$src")"
  fo="$(ffprobe -v error -select_streams v:0 -show_entries stream=field_order \
        -of default=nw=1:nk=1 "$src" 2>/dev/null || echo progressive)"
  case "$fo" in bff) parity="bff" ;; tff) parity="tff" ;; *) parity="" ;; esac

  vf=""
  if [ -n "$parity" ]; then
    local bmode="send_field"
    [ "$MODE" = "mp4-25p" ] && bmode="send_frame"
    vf="bwdif=mode=$bmode:parity=$parity,"
  fi
  vf="${vf}scale=${ow}:-2:flags=lanczos,setsar=1"

  ct="$(moi_creation_time "$src" || true)"
  echo ">> $src -> $dst  (mode=$MODE crf=$CRF fo=${fo:-?})"
  ffmpeg -hide_banner "$OVERWRITE" -fflags +genpts -i "$src" \
    -vf "$vf" \
    -c:v libx264 -crf "$CRF" -preset medium -pix_fmt yuv420p \
    -c:a aac -b:a 192k -movflags +faststart \
    ${ct:+-metadata creation_time="$ct"} \
    "$dst"
  touch -r "$src" "$dst" 2>/dev/null || true
}

# ---- discover inputs ------------------------------------------------------ #

process() {
  local target="$1"
  if [ -d "$target" ]; then
    # Recurse; match .MOD / .TOD case-insensitively.
    while IFS= read -r -d '' f; do
      convert_one "$f"
    done < <(find "$target" -type f \( -iname '*.mod' -o -iname '*.tod' \) -print0 | sort -z)
  elif [ -f "$target" ]; then
    convert_one "$target"
  else
    echo "Skipping (not found): $target" >&2
  fi
}

for arg in "$@"; do
  process "$arg"
done

echo "Done."
