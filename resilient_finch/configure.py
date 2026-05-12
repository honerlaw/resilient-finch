from __future__ import annotations

import json
import pathlib
import sys

from . import config

_CONFIG_PATH = pathlib.Path("~/.resilient-finch/config.json").expanduser()

_WHISPER_MODELS = {
    "1": ("large-v3", "most accurate, ~3 GB download"),
    "2": ("medium", "good balance of speed and accuracy"),
    "3": ("base", "fastest, least accurate"),
}

_WHISPER_MODEL_BY_NAME = {v: k for k, (v, _) in _WHISPER_MODELS.items()}


def _load() -> dict[str, object]:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save(cfg: dict[str, object]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def _prompt(label: str, current: str, hint: str = "") -> str:
    """Prompt with current value shown as default. Empty input keeps current."""
    if hint:
        print(f"  {hint}")
    answer = input(f"  {label} [{current}]: ").strip()
    return answer or current


def _section(title: str) -> None:
    print(f"\n{title}")
    print("─" * len(title))


def _configure_outputs(cfg: dict[str, object]) -> dict[str, object]:
    _section("Output format")
    _raw_outputs = cfg.get("outputs", ["text_file"])
    current_outputs: list[str] = (
        [str(x) for x in _raw_outputs] if isinstance(_raw_outputs, list) else ["text_file"]
    )
    has_file = "text_file" in current_outputs
    has_docs = "google_docs" in current_outputs

    if has_file and has_docs:
        current_label = "3"
    elif has_docs:
        current_label = "2"
    else:
        current_label = "1"

    print("  1) text file only")
    print("  2) Google Docs only")
    print("  3) text file + Google Docs")
    choice = _prompt("choice", current_label)

    if choice == "2":
        cfg["outputs"] = ["google_docs"]
    elif choice == "3":
        cfg["outputs"] = ["text_file", "google_docs"]
    else:
        cfg["outputs"] = ["text_file"]

    return cfg


def _configure_google_docs(cfg: dict[str, object]) -> dict[str, object]:
    _section("Google Docs")

    default_path = "~/.resilient-finch/service_account.json"
    current_path = str(cfg.get("google_service_account_path", default_path))
    new_path = _prompt(
        "service account JSON path",
        current_path,
        hint="Path to your Google service account key file.",
    )
    resolved = pathlib.Path(new_path).expanduser()
    if not resolved.exists():
        print(f"  Warning: file not found at {resolved}")
    cfg["google_service_account_path"] = new_path

    current_doc = str(cfg["google_docs_doc_id"]) if cfg.get("google_docs_doc_id") else ""
    doc_input = _prompt(
        "existing doc ID (leave blank to auto-create)",
        current_doc or "auto-create",
        hint="Paste a Google Doc ID to append all sessions there, or leave blank.",
    )
    if doc_input and doc_input != "auto-create":
        cfg["google_docs_doc_id"] = doc_input
    else:
        cfg.pop("google_docs_doc_id", None)

    return cfg


def _configure_whisper(cfg: dict[str, object]) -> dict[str, object]:
    _section("Whisper model")

    current_model = str(cfg.get("whisper_model", "large-v3"))
    current_label = _WHISPER_MODEL_BY_NAME.get(current_model, "1")

    for key, (name, desc) in _WHISPER_MODELS.items():
        print(f"  {key}) {name:<12} {desc}")

    choice = _prompt("choice", current_label)
    model_name = _WHISPER_MODELS.get(choice, (current_model, ""))[0]
    cfg["whisper_model"] = model_name

    return cfg


def _configure_audio_mode(cfg: dict[str, object]) -> dict[str, object]:
    _section("Audio mode")

    current_mode = str(cfg.get("audio_mode", config.AUDIO_MODE))
    current_label = "2" if current_mode == "mic_and_speaker" else "1"

    print("  1) mic_only        — mic only, higher gain (default, for room capture without headphones)")
    print("  2) mic_and_speaker — mic + system audio (for use with headphones + mic)")

    choice = _prompt("choice", current_label)
    if choice == "2":
        cfg["audio_mode"] = "mic_and_speaker"
    else:
        cfg["audio_mode"] = "mic_only"

    return cfg


def _configure_language(cfg: dict[str, object]) -> dict[str, object]:
    _section("Transcription language")

    current_lang = str(cfg.get("whisper_language", "en"))
    new_lang = _prompt(
        "language code or 'auto'",
        current_lang,
        hint="e.g. en, fr, de — or 'auto' to detect per segment.",
    )
    if new_lang.lower() == "auto":
        cfg.pop("whisper_language", None)
    else:
        cfg["whisper_language"] = new_lang.lower()

    return cfg


def main() -> None:
    print("resilient-finch configure")
    print("=" * 25)

    if _CONFIG_PATH.exists():
        print(f"\nLoaded existing config from {_CONFIG_PATH}")
    else:
        print("\nNo existing config found — starting fresh.")

    cfg = _load()

    cfg = _configure_outputs(cfg)

    _outputs = cfg.get("outputs", [])
    if isinstance(_outputs, list) and "google_docs" in _outputs:
        cfg = _configure_google_docs(cfg)
    else:
        cfg.pop("google_service_account_path", None)
        cfg.pop("google_docs_doc_id", None)

    cfg = _configure_audio_mode(cfg)
    cfg = _configure_whisper(cfg)
    cfg = _configure_language(cfg)

    print()
    _save(cfg)
    print(f"Config saved to {_CONFIG_PATH}")
    print()
    print("Current settings:")
    print(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    sys.exit(main())
