# Telegram Voice Relay

This project wraps [PyTgCalls](https://github.com/pytgcalls/pytgcalls) and a couple of handy [mise](https://mise.jdx.dev/) tasks so you can:

* capture audio from a Telegram voice chat into a local file, and
* hand that audio off live to an FFmpeg pipeline (for example, to restream a voice chat to YouTube).

Everything runs from one script, `telegram_voice_relay.py`, which drives Telethon + PyTgCalls and can either save an MP3 or stream raw PCM frames to any command that reads them from stdin.

---

## Requirements

* Python 3.13+ (configured via `mise` in this repo)
* [uv](https://github.com/astral-sh/uv) (installed automatically by `mise`)
* FFmpeg in your `PATH` (installable via `mise i ffmpeg` or your package manager)
* A Telegram API ID/hash and an authorised session (Telethon `.session` file) from https://my.telegram.org/

Optional:

* A pre-generated silent video clip (`black-loop.mp4`) if you plan to restream audio to YouTube with a static background. A `mise` task is provided to create it.

---

## Configuration

Environment variables are defined in `mise.toml` under `[env]`:

```toml
[env]
TG_API_ID = 123456
TG_API_HASH = "abcdefghijklmnopqrstuvwxyz"
TG_CHAT = "@username"
TG_SESSION = "telethon_session"
STREAM_SERVER = "rtmp://example.com/live/"
STREAM_KEY = "XXXX-XXXX-XXXX-XXXX"
```

Adjust them to match your Telegram application credentials, the chat/voice chat you want to record, and (if you intend to stream) the RTMP server + key for your destination.

Place your Telethon session file (e.g. `telethon_session.session`) in the project root so the script can reuse it without re-authentication.

---

## Core Script Features

`telegram_voice_relay.py` supports two mutually exclusive outputs:

1. **File recording** (default): outputs an MP3 named `call-<timestamp>.mp3`, or a custom path via `--output`.
2. **Live handoff**: pipe raw PCM directly into an external command using `--live-cmd`. The template can reference `{sample_rate}` and `{channels}` (derived from the selected `AudioQuality`). The command must read little-endian PCM from stdin.

Additional flags:

* `--quality low|medium|high|studio` (maps to 24 kHz mono up to 96 kHz stereo)
* `--duration <seconds>` auto-stops the recording after the specified time
* `--join-as` to join the voice chat as a specific peer (channel or user)
* `--invite-hash` to pass a speaker invite hash when necessary
* `--auto-start` to create the voice chat if it is not already running

The script listens for SIGINT/SIGTERM, leaves the call gracefully, and (in live mode) drains/closes the external process so FFmpeg shuts down cleanly.

---

## Provided `mise` Tasks

### 1. Generate the black-loop video

```bash
mise run generate_black_video
```

Creates `black-loop.mp4` – a 30 second, 640×360, 2 fps black clip encoded once. This file is reused and copied verbatim whenever you stream, so your live pipeline does not spend CPU re-encoding video.

### 2. Record a voice chat into `talk.mp3`

```bash
mise run record_voice_call
```

Runs

```bash
uv run --script telegram_voice_relay.py $TG_CHAT \
  --output talk.mp3 \
  --quality low \
  --overwrite
```

`--quality low` captures 24 kHz mono audio (lower bandwidth, lower CPU) and rewrites the MP3 each time. Adjust the chat handle or quality level as needed.

### 3. Stream a voice chat directly to YouTube

```bash
mise run telegram_to_youtube
```

Internally this executes:

```bash
uv run --script telegram_voice_relay.py $TG_CHAT \
  --quality low \
  --live-cmd "ffmpeg -f s16le -ar {sample_rate} -ac {channels} -i pipe:0 \
      -stream_loop -1 -i black-loop.mp4 -c:v copy \
      -c:a aac -b:a 64k -threads 1 -f flv ${STREAM_SERVER}${STREAM_KEY}"
```

**How it works:**

* PyTgCalls delivers PCM frames into FFmpeg through stdin.
* FFmpeg loops the pre-encoded `black-loop.mp4` and copies its video, so no on-the-fly video encoding is needed.
* Audio is encoded to AAC (`64k`, mono) and pushed to YouTube.

Press `Ctrl+C` to stop; the script terminates FFmpeg, leaves the voice chat, and logs “Live handoff completed.”

---

## Example Custom Live Command

You can stream somewhere other than YouTube by changing `--live-cmd`. For example, to write PCM frames into a FIFO:

```bash
mkfifo live.pcm
uv run --script telegram_voice_relay.py @chat --quality high \
  --live-cmd "cat > live.pcm"
```

Or to run FFmpeg with a different container:

```bash
uv run --script telegram_voice_relay.py @chat \
  --quality high \
  --live-cmd "ffmpeg -f s16le -ar {sample_rate} -ac {channels} -i pipe:0 \
      -c:a flac live.flac"
```

Remember: when `--live-cmd` is used, omit `--output` (and remove `OUTPUT_PATH` from the environment) since the script currently supports one destination at a time.

---

## Tips & Troubleshooting

* **Missing FFmpeg:** install via your package manager (`brew install ffmpeg`, `apt install ffmpeg`, etc.).
* **Authorisation errors:** delete the Telethon session file if you need to re-login, then rerun any task – Telethon will prompt for the Telegram code.
* **Slow CPUs:** keep `--quality low`, reduce the AAC bitrate (`-b:a 64k` or lower), and ensure video stays as `-c:v copy` from a pre-encoded file.
* **Stream ends unexpectedly:** check FFmpeg logs – if it exits early, the script will log the return code and stop.
* **Multiple voice chats:** change `TG_CHAT` (or pass a handle directly to the script) and rerun the task.

---
