# resilient-finch

Captures microphone input and system audio output simultaneously, transcribes both with Whisper large-v3, and writes a labeled session file.

## Setup

### 1. Install BlackHole

```bash
brew install blackhole-2ch
```

If BlackHole doesn't appear in Audio MIDI Setup after installing, restart Core Audio:

```bash
sudo killall coreaudiod
```

### 2. Install dependencies

```bash
uv sync
```

### 3. Configure

```bash
uv run resilient-finch-configure
```

Prompts for output format (text file and/or Google Docs), Whisper model size, and transcription language. Settings are saved to `~/.resilient-finch/config.json`. Re-running shows current values as defaults — safe to run any time.

### 4. Run audio setup

```bash
uv run python setup.py
```

This creates two audio devices and sets your system output:

| Device | Purpose |
|---|---|
| **Resilient Finch Output** | Multi-Output Device — routes audio to your speakers *and* BlackHole simultaneously. Set as system default output. |
| **Resilient Finch Capture** | Aggregate Device — wraps BlackHole at 16 kHz. What the tool reads from. |

Safe to run more than once — skips devices that already exist.

To revert: **System Settings → Sound → Output** → select your speakers, then delete the created devices in **Audio MIDI Setup**.

> The Whisper large-v3 model (~3GB) downloads automatically on first run to `~/.cache/huggingface/hub`.

## Usage

```bash
uv run python main.py
```

With a session topic (included in filename and file header):

```bash
uv run python main.py --topic "daily standup"
uv run python main.py -t "1:1 with Alex"
```

Press **Ctrl+C** to stop. The tool flushes remaining buffered audio before exiting (up to 60s).

### Session file format

Files are written to `sessions/` with the format `session_YYYYMMDD_HHMMSS[_slug].txt`:

```
============================================================
  Topic:   daily standup
  Date:    2026-05-08
  Started: 14:02:22
============================================================

[14:02:30] [MIC] Hello, can everyone hear me?
[14:02:31] [SPEAKER] Yes, loud and clear.
[14:02:45] [MIC] Great, let's get started.

============================================================
  Ended:    14:17:05
  Duration: 14m 43s
============================================================
```

Without a topic, the header omits the Topic line. Filename: `session_20260508_140222.txt`.

## Configuration

All tunable settings are in `resilient_finch/config.py`:

| Setting | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `large-v3` | Change to `medium` or `base` for faster inference |
| `SEGMENT_SECONDS` | `20.0` | Seconds to accumulate before sending to Whisper |
| `BLACKHOLE_DEVICE_NAME` | `BlackHole 2ch` | Partial match — adjust if your device name differs |
| `WHISPER_LANGUAGE` | `en` | Set to `None` for auto-detect |

## MCP Server

The MCP server exposes resilient-finch as tools an LLM can call directly.

### Tools

| Tool | Description |
|---|---|
| `start_session(topic?)` | Start capturing audio and transcribing |
| `stop_session()` | Stop and flush (up to 60s) |
| `get_transcript()` | Full transcript from the running session |
| `list_sessions()` | All saved session files, newest first |
| `read_session(filename)` | Full content of a specific session file |

### Run the server

```bash
uv run resilient-finch-mcp
```

The server loads the Whisper model on startup (~10-30s), then listens on stdio for MCP clients.

### Configure Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "resilient-finch": {
      "command": "uv",
      "args": ["run", "resilient-finch-mcp"],
      "cwd": "/Users/derekhonerlaw/Development/notes/resilient-finch"
    }
  }
}
```

Restart Claude Desktop. You can then ask Claude to start a transcription session, retrieve the transcript mid-meeting, or read back a saved session.

## Development

```bash
make check   # lint + type check
make fmt     # auto-format
```

## Verify audio devices

```bash
uv run python -c "import sounddevice as sd; print(sd.query_devices())"
```

BlackHole 2ch should appear as an input device.

