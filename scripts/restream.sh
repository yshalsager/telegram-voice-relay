#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <sample_rate> <channels> <ffmpeg args...>" >&2
  exit 2
fi

sample_rate="$1"
shift
channels="$1"
shift
queue_size="${THREAD_QUEUE_SIZE:-1024}"
retry_delay="${FFMPEG_RESTART_DELAY:-0.5}"

# Preserve the incoming PCM stream on fd 3 so ffmpeg restarts keep reading it.
exec 3<&0
trap '' PIPE

while true; do
  if ffmpeg -hide_banner -loglevel info \
    -thread_queue_size "$queue_size" \
    -f s16le -ar "$sample_rate" -ac "$channels" -i pipe:3 \
    "$@"; then
    status=0
  else
    status=$?
  fi
  printf 'ffmpeg exited (%s); retrying in %ss\n' "$status" "$retry_delay" >&2
  sleep "$retry_delay"
done
