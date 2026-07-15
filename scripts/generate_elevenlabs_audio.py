#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"


def load_env(path):
    if not path.exists():
        return {}

    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def elevenlabs_key():
    return os.environ.get("ELEVENLABS_API_KEY") or load_env(ENV_PATH).get("ELEVENLABS_API_KEY")


def post_json(url, payload, api_key):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ElevenLabs returned {error.code}: {detail}") from error


def strip_bracket_directions(text):
    return re.sub(r"\s*\[[^\]]+\]\s*", " ", text)


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def word_timings_from_alignment(display_text, tts_text, alignment):
    if not alignment:
        return []

    characters = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not characters or len(characters) != len(starts) or len(characters) != len(ends):
        return []

    words = []
    active = None

    in_direction = False
    for char, start, end in zip(characters, starts, ends):
        if char == "[":
            in_direction = True
            if active:
                words.append(active)
                active = None
            continue
        if in_direction:
            if char == "]":
                in_direction = False
            continue

        if char.isspace():
            if active:
                words.append(active)
                active = None
            continue

        if active is None:
            active = {"text": char, "start_ms": round(start * 1000), "end_ms": round(end * 1000)}
        else:
            active["text"] += char
            active["end_ms"] = round(end * 1000)

    if active:
        words.append(active)

    display_words = display_text.split()
    if len(words) == len(display_words):
        for word, display_word in zip(words, display_words):
            word["text"] = display_word
    elif normalize_text(strip_bracket_directions(tts_text)) == normalize_text(display_text):
        print(
            f"warning: alignment word count {len(words)} did not match display word count {len(display_words)}",
            file=sys.stderr,
        )
    return words


def desired_tts_text(narration_chunk):
    return narration_chunk.get("tts_text") or narration_chunk["display_text"]


def desired_model_id(narration_chunk, manifest, args):
    if args.model_id:
        return args.model_id
    if "[" in desired_tts_text(narration_chunk):
        return manifest.get("expressive_model_id", "eleven_v3")
    return manifest.get("default_model_id", "eleven_multilingual_v2")


def cache_matches(timing_path, voice_id, model_id, tts_text):
    if not timing_path.exists():
        return False
    try:
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    return (
        timing.get("voice_id") == voice_id
        and timing.get("model_id") == model_id
        and timing.get("tts_text") == tts_text
    )


def map_reading_units(narration_chunk, reading_units, words):
    mapped = []
    cursor = 0
    by_id = {unit["id"]: unit for unit in reading_units}

    for unit_id in narration_chunk.get("reading_unit_ids", []):
        unit = by_id[unit_id]
        unit_words = unit["display_text"].split()
        unit_word_count = len(unit_words)
        unit_timings = [dict(word) for word in words[cursor : cursor + unit_word_count]]
        cursor += unit_word_count

        if len(unit_timings) == unit_word_count:
            for timing, display_word in zip(unit_timings, unit_words):
                timing["text"] = display_word

        mapped.append(
            {
                "id": unit_id,
                "start_ms": unit_timings[0]["start_ms"] if unit_timings else None,
                "end_ms": unit_timings[-1]["end_ms"] if unit_timings else None,
                "words": unit_timings,
            }
        )

    return mapped


def generate_narration_chunk(book_dir, manifest, narration_chunk, reading_units, args, api_key):
    audio_dir = book_dir / manifest.get("audio_dir", "audio/page-001")
    audio_dir.mkdir(parents=True, exist_ok=True)

    audio_rel = f"{manifest.get('audio_dir', 'audio/page-001')}/{narration_chunk['id']}.mp3"
    timing_rel = f"{manifest.get('audio_dir', 'audio/page-001')}/{narration_chunk['id']}.json"
    audio_path = book_dir / audio_rel
    timing_path = book_dir / timing_rel
    model_id = desired_model_id(narration_chunk, manifest, args)
    tts_text = desired_tts_text(narration_chunk)

    if audio_path.exists() and cache_matches(timing_path, args.voice_id, model_id, tts_text) and not args.force:
        print(f"skip cached {narration_chunk['id']}")
        narration_chunk["audio"] = audio_rel
        narration_chunk["timing"] = timing_rel
        return False

    query = urllib.parse.urlencode({"output_format": args.output_format})
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{args.voice_id}/with-timestamps?{query}"
    voice_settings = {"stability": args.stability}
    if model_id != "eleven_v3":
        voice_settings.update(
            {
                "similarity_boost": args.similarity_boost,
                "style": args.style,
                "use_speaker_boost": args.speaker_boost,
                "speed": args.speed,
            }
        )

    payload = {
        "text": tts_text,
        "model_id": model_id,
        "voice_settings": voice_settings,
        "previous_text": narration_chunk.get("previous_text"),
        "next_text": narration_chunk.get("next_text"),
    }
    payload = {key: value for key, value in payload.items() if value is not None}

    print(f"generate {narration_chunk['id']}")
    result = post_json(url, payload, api_key)
    audio_path.write_bytes(base64.b64decode(result["audio_base64"]))

    alignment = result.get("normalized_alignment") or result.get("alignment")
    words = word_timings_from_alignment(narration_chunk["display_text"], payload["text"], alignment)
    timing = {
        "narration_chunk_id": narration_chunk["id"],
        "voice_id": args.voice_id,
        "model_id": payload["model_id"],
        "display_text": narration_chunk["display_text"],
        "tts_text": payload["text"],
        "alignment": result.get("alignment"),
        "normalized_alignment": result.get("normalized_alignment"),
        "words": words,
        "reading_units": map_reading_units(narration_chunk, reading_units, words),
    }
    timing_path.write_text(json.dumps(timing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    narration_chunk["audio"] = audio_rel
    narration_chunk["timing"] = timing_rel
    return True


def main():
    parser = argparse.ArgumentParser(description="Generate cached ElevenLabs audio for a book page.")
    parser.add_argument("--book-dir", default="data/books/moby-dick")
    parser.add_argument("--manifest", default="page-001.json")
    parser.add_argument("--voice-id", required=True)
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--output-format", default="mp3_44100_128")
    parser.add_argument("--stability", type=float, default=0.5)
    parser.add_argument("--similarity-boost", type=float, default=0.75)
    parser.add_argument("--style", type=float, default=0.0)
    parser.add_argument("--speed", type=float, default=0.92)
    parser.add_argument("--speaker-boost", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--chunk-id",
        action="append",
        default=[],
        help="Generate only this chunk id. Can be passed more than once.",
    )
    args = parser.parse_args()

    api_key = elevenlabs_key()
    if not api_key:
        print("ELEVENLABS_API_KEY is missing in .env", file=sys.stderr)
        return 1

    book_dir = ROOT / args.book_dir
    manifest_path = book_dir / args.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    reading_units = manifest.get("reading_units") or manifest.get("chunks") or []
    narration_chunks = manifest.get("narration_chunks")
    if narration_chunks is None:
        narration_chunks = [
            {
                "id": unit["id"],
                "reading_unit_ids": [unit["id"]],
                "display_text": unit["display_text"],
                "tts_text": desired_tts_text(unit),
            }
            for unit in reading_units
        ]
        manifest["narration_chunks"] = narration_chunks

    selected_chunk_ids = set(args.chunk_id)
    chunks = [
        chunk
        for chunk in narration_chunks
        if not selected_chunk_ids or chunk["id"] in selected_chunk_ids
    ]
    missing_chunk_ids = selected_chunk_ids - {chunk["id"] for chunk in chunks}
    if missing_chunk_ids:
        print(f"Unknown chunk id(s): {', '.join(sorted(missing_chunk_ids))}", file=sys.stderr)
        return 1

    generated = 0
    for chunk in chunks:
        if generate_narration_chunk(book_dir, manifest, chunk, reading_units, args, api_key):
            generated += 1

    manifest["selected_voice_id"] = args.voice_id
    selected_model_ids = {desired_model_id(chunk, manifest, args) for chunk in chunks}
    manifest["selected_model_id"] = selected_model_ids.pop() if len(selected_model_ids) == 1 else "mixed"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"done: generated {generated}, reused {len(chunks) - generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
