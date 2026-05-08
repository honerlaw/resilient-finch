# resilient-finch

Captures microphone input and system audio output simultaneously, transcribes both with Whisper large-v3, and writes a labeled session file.

## Requirements

- macOS 14.2 or later (uses the CoreAudio process tap API for system audio capture)

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Configure

```bash
uv run resilient-finch-configure
```

Prompts for output format (text file and/or Google Docs), Whisper model size, and transcription language. Settings are saved to `~/.resilient-finch/config.json`. Re-running shows current values as defaults — safe to run any time.

> The Whisper large-v3 model (~3GB) downloads automatically on first run to `~/.cache/huggingface/hub`.

> **First run:** macOS will prompt for *Screen & System Audio Recording* permission. Grant it — this is what allows the process tap to capture system audio. Your system output device and volume controls are not affected.

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

## Troubleshooting

**System audio not being captured**

macOS requires *Screen & System Audio Recording* permission for the process tap API. Go to **System Settings → Privacy & Security → Screen & System Audio Recording** and enable it for Terminal (or whichever app you run the tool from). Restart the app after granting permission.

**"AudioHardwareCreateProcessTap failed" error**

Same as above — this is the permission check failing at the API level.

**Microphone not found**

Set `MIC_DEVICE_NAME` in `config.py` to a substring of your mic's name as shown by:

```bash
uv run python -c "import sounddevice as sd; print(sd.query_devices())"
```

