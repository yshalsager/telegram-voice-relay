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

# Preserve the incoming PCM stream on fd 3 so ffmpeg restarts keep reading it.
exec 3<&0
trap '' PIPE

while true; do
  ffmpeg -hide_banner -loglevel info \
    -thread_queue_size "$queue_size" \
    -f s16le -ar "$sample_rate" -ac "$channels" -i pipe:3 \
    "$@"
  status=$?
  printf 'ffmpeg exited (%s); retrying in 2s\n' "$status" >&2
  sleep 2
done
