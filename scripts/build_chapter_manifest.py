#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_between_markers(text, start_marker, end_marker):
    start = text.find(start_marker)
    if start < 0:
        raise ValueError(f"Could not find start marker: {start_marker}")

    start += len(start_marker)
    end = text.find(end_marker, start)
    if end < 0:
        raise ValueError(f"Could not find end marker: {end_marker}")

    return text[start:end].strip()


def extract_english_chapter(text):
    matches = list(re.finditer(r"^CHAPTER 1\. Loomings\.\s*$", text, flags=re.MULTILINE))
    if len(matches) < 2:
        raise ValueError("Could not find the body occurrence of English CHAPTER 1")

    start = matches[1].end()
    next_match = re.search(r"^CHAPTER 2\. The Carpet-Bag\.\s*$", text[start:], flags=re.MULTILINE)
    if not next_match:
        raise ValueError("Could not find English CHAPTER 2 marker")

    return text[start : start + next_match.start()].strip()


def paragraph_texts(chapter_text):
    return [normalize_text(paragraph) for paragraph in re.split(r"\n\s*\n", chapter_text) if paragraph.strip()]


def split_paragraph_into_units(paragraph, max_words):
    tokens = paragraph.split()
    units = []
    current = []

    def should_break(token):
        return bool(re.search(r"[.!?,;:]$|[–—]$", token))

    for token in tokens:
        current.append(token)
        if len(current) >= max_words:
            units.append(" ".join(current))
            current = []
        elif len(current) >= 4 and should_break(token):
            units.append(" ".join(current))
            current = []

    if current:
        if units and len(units[-1].split()) + len(current) <= max_words:
            units[-1] = f"{units[-1]} {' '.join(current)}"
        else:
            units.append(" ".join(current))

    return units


def build_reading_units(german_chapter, max_words):
    units = []
    for paragraph_index, paragraph in enumerate(paragraph_texts(german_chapter), start=1):
        for text in split_paragraph_into_units(paragraph, max_words):
            units.append(
                {
                    "id": f"unit-{len(units) + 1:03d}",
                    "display_text": text,
                    "en": "",
                    "en_ai": "",
                    "notes": {},
                    "paragraph": paragraph_index,
                }
            )
    return units


def main():
    parser = argparse.ArgumentParser(description="Build a full chapter manifest from the Moby-Dick sources.")
    parser.add_argument("--book-dir", default="data/books/moby-dick")
    parser.add_argument("--manifest", default="chapter-001.json")
    parser.add_argument("--source-manifest", default="page-001.json")
    parser.add_argument("--max-words", type=int, default=10)
    args = parser.parse_args()

    book_dir = ROOT / args.book_dir
    source_manifest = json.loads((book_dir / args.source_manifest).read_text(encoding="utf-8"))
    german_source = (book_dir / "source" / "de.txt").read_text(encoding="utf-8")
    english_source = (book_dir / "source" / "en.txt").read_text(encoding="utf-8")

    german_chapter = extract_between_markers(german_source, "Erstes Kapitel", "Zweites Kapitel")
    english_chapter = extract_english_chapter(english_source)
    reading_units = build_reading_units(german_chapter, args.max_words)

    manifest = {
        "book_id": source_manifest.get("book_id", "moby-dick"),
        "title": source_manifest.get("title", "Moby-Dick"),
        "page_id": "chapter-001",
        "chapter_id": "chapter-001",
        "chapter_title": "Erstes Kapitel",
        "source": source_manifest.get("source", {"de": "source/de.txt", "en": "source/en.txt"}),
        "audio_dir": "audio/chapter-001",
        "default_model_id": source_manifest.get("default_model_id", "eleven_multilingual_v2"),
        "expressive_model_id": source_manifest.get("expressive_model_id", "eleven_v3"),
        "selected_voice_id": source_manifest.get("selected_voice_id", ""),
        "selected_model_id": source_manifest.get("selected_model_id", ""),
        "chapter_source": {
            "de_start_marker": "Erstes Kapitel",
            "de_end_marker": "Zweites Kapitel",
            "en_start_marker": "CHAPTER 1. Loomings.",
            "en_end_marker": "CHAPTER 2. The Carpet-Bag.",
        },
        "chapter_stats": {
            "de_chars": len(german_chapter),
            "de_words": len(german_chapter.split()),
            "en_chars": len(english_chapter),
            "en_words": len(english_chapter.split()),
            "reading_units": len(reading_units),
            "max_words_per_unit": args.max_words,
        },
        "reading_units": reading_units,
    }

    manifest_path = book_dir / args.manifest
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "done: "
        f"{len(reading_units)} reading units, "
        f"{manifest['chapter_stats']['de_chars']} German chars, "
        f"{manifest['chapter_stats']['de_words']} German words"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
