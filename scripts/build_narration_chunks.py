#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def strip_bracket_directions(text):
    return re.sub(r"\s*\[[^\]]+\]\s*", " ", text)


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def source_units(manifest):
    return manifest.get("reading_units") or manifest.get("chunks") or []


def unit_id(index):
    return f"unit-{index + 1:03d}"


def narration_id(index):
    return f"narration-{index + 1:03d}"


def expressive_text_for_units(units):
    first_cue = None
    clean_parts = []
    for unit in units:
        expressive = unit.get("tts_text_expressive") or unit.get("tts_text") or unit["display_text"]
        if first_cue is None:
            match = re.search(r"\[[^\]]+\]", expressive)
            if match:
                first_cue = match.group(0)
        clean_parts.append(normalize_text(strip_bracket_directions(expressive)))

    text = normalize_text(" ".join(clean_parts))
    return normalize_text(f"{first_cue or '[reflective]'} {text}")


def reading_unit_from_chunk(chunk, index):
    return {
        "id": chunk.get("id") if chunk.get("id", "").startswith("unit-") else unit_id(index),
        "display_text": chunk["display_text"],
        "en": chunk["en"],
        "en_ai": chunk.get("en_ai", ""),
        "notes": chunk.get("notes", {}),
    }


def build_narration_chunks(units, max_chars, previous_chunks=None):
    previous_by_id = {chunk["id"]: chunk for chunk in previous_chunks or []}
    groups = []
    current = []

    for unit in units:
        candidate = current + [unit]
        candidate_text = expressive_text_for_units(candidate)
        if current and len(candidate_text) > max_chars:
            groups.append(current)
            current = [unit]
            continue
        current = candidate

    if current:
        groups.append(current)

    narration_chunks = []
    for index, group in enumerate(groups):
        chunk_id = narration_id(index)
        for unit in group:
            unit["narration_chunk_id"] = chunk_id

        reading_unit_ids = [unit["id"] for unit in group]
        display_text = normalize_text(" ".join(unit["display_text"] for unit in group))
        tts_text = expressive_text_for_units(group)
        previous = previous_by_id.get(chunk_id)
        same_group = (
            previous
            and previous.get("display_text") == display_text
            and previous.get("reading_unit_ids") == reading_unit_ids
        )
        if same_group and previous.get("tts_text"):
            tts_text = previous["tts_text"]

        narration_chunk = {
            "id": chunk_id,
            "reading_unit_ids": reading_unit_ids,
            "display_text": display_text,
            "tts_text": tts_text,
        }
        if same_group and previous.get("tts_text") == tts_text:
            for key in ("audio", "timing"):
                if previous.get(key):
                    narration_chunk[key] = previous[key]
        narration_chunks.append(narration_chunk)

    return narration_chunks


def main():
    parser = argparse.ArgumentParser(description="Build long narration chunks from small reading units.")
    parser.add_argument("--book-dir", default="data/books/moby-dick")
    parser.add_argument("--manifest", default="page-001.json")
    parser.add_argument("--max-chars", type=int, default=2800)
    args = parser.parse_args()

    book_dir = ROOT / args.book_dir
    manifest_path = book_dir / args.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    units = [reading_unit_from_chunk(chunk, index) for index, chunk in enumerate(source_units(manifest))]
    narration_chunks = build_narration_chunks(units, args.max_chars, manifest.get("narration_chunks"))

    manifest["reading_units"] = units
    manifest["narration_chunks"] = narration_chunks
    manifest["narration_chunking"] = {
        "max_chars": args.max_chars,
        "prompt": "prompts/narration-chunking.md",
    }
    manifest.pop("chunks", None)

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"done: {len(units)} reading units, {len(narration_chunks)} narration chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
