#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
PROMPT_PATH = ROOT / "prompts" / "book-translation-alignment.md"


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


def openai_key():
    env = load_env(ENV_PATH)
    return (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("OPEN_AI_API_KEY")
        or env.get("OPENAI_API_KEY")
        or env.get("OPEN_AI_API_KEY")
    )


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def extract_english_chapter(text):
    matches = list(re.finditer(r"^CHAPTER 1\. Loomings\.\s*$", text, flags=re.MULTILINE))
    if len(matches) < 2:
        raise ValueError("Could not find the body occurrence of English CHAPTER 1")

    start = matches[1].end()
    next_match = re.search(r"^CHAPTER 2\. The Carpet-Bag\.\s*$", text[start:], flags=re.MULTILINE)
    if not next_match:
        raise ValueError("Could not find English CHAPTER 2 marker")
    return text[start : start + next_match.start()].strip()


def post_openai(payload, api_key, timeout):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    last_error = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI returned {error.code}: {detail}") from error
        except (TimeoutError, urllib.error.URLError) as error:
            last_error = error
            if attempt == 3:
                break
            print(f"retry OpenAI request after transient error: {error}", file=sys.stderr)
    raise RuntimeError(f"OpenAI request failed after retries: {last_error}") from last_error


def response_text(response):
    if response.get("output_text"):
        return response["output_text"]

    fragments = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                fragments.append(content["text"])
    return "\n".join(fragments)


def compact_unit(unit):
    return {
        "id": unit["id"],
        "paragraph": unit.get("paragraph"),
        "german": unit["display_text"],
    }


def unit_index_bounds(target_units, chapter_units, context_units):
    index_by_id = {unit["id"]: index for index, unit in enumerate(chapter_units)}
    start_index = min(index_by_id[unit["id"]] for unit in target_units)
    end_index = max(index_by_id[unit["id"]] for unit in target_units) + 1
    return (
        max(0, start_index - context_units),
        min(len(chapter_units), end_index + context_units),
    )


def english_window_for_units(english_chapter, target_units, chapter_units, margin_words):
    words = english_chapter.split()
    if not words:
        return ""

    start_index, end_index = unit_index_bounds(target_units, chapter_units, 0)
    unit_count = max(1, len(chapter_units))

    start_word = int((start_index / unit_count) * len(words)) - margin_words
    end_word = int((end_index / unit_count) * len(words)) + margin_words
    start_word = max(0, start_word)
    end_word = min(len(words), end_word)
    return " ".join(words[start_word:end_word])


def structured_payload(model, english_chapter, manifest, target_units, chapter_units, margin_words):
    context_start, context_end = unit_index_bounds(target_units, chapter_units, 8)
    context_units = chapter_units[context_start:context_end]
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "en"],
        "properties": {
            "id": {"type": "string"},
            "en": {"type": "string"},
        },
    }
    return {
        "model": model,
        "input": [
            {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "book": manifest.get("title", "Unknown book"),
                        "chapter_id": manifest.get("chapter_id") or manifest.get("page_id"),
                        "english_source_window": english_window_for_units(
                            english_chapter,
                            target_units,
                            chapter_units,
                            margin_words,
                        ),
                        "context_reading_units": [compact_unit(unit) for unit in context_units],
                        "target_reading_units": [compact_unit(unit) for unit in target_units],
                        "target_ids": [unit["id"] for unit in target_units],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "book_translation_alignment",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["reading_units"],
                    "properties": {
                        "reading_units": {
                            "type": "array",
                            "minItems": len(target_units),
                            "maxItems": len(target_units),
                            "items": item_schema,
                        }
                    },
                },
            }
        },
    }


def parse_items(response):
    text = response_text(response)
    if not text:
        raise RuntimeError("OpenAI response did not include text output")
    return json.loads(text)["reading_units"]


def validate_alignment(reading_units, generated):
    by_id = {unit["id"]: unit for unit in reading_units}
    seen = set()
    for item in generated:
        item_id = item.get("id")
        if item_id not in by_id:
            raise ValueError(f"OpenAI returned unknown reading unit id: {item_id}")
        if item_id in seen:
            raise ValueError(f"OpenAI returned duplicate reading unit id: {item_id}")
        seen.add(item_id)

    expected = set(by_id)
    if seen != expected:
        missing = ", ".join(sorted(expected - seen))
        raise ValueError(f"OpenAI response is missing reading unit id(s): {missing}")


def split_alignment_results(reading_units, generated):
    validate_alignment(reading_units, generated)
    valid = []
    empty_ids = []
    for item in generated:
        if normalize_text(item.get("en", "")):
            valid.append(item)
        else:
            empty_ids.append(item["id"])
    return valid, empty_ids


def batches(items, size):
    for index in range(0, len(items), size):
        yield index // size + 1, items[index : index + size]


def write_manifest(manifest_path, manifest, model):
    metadata = manifest.setdefault("openai_preprocessing", {})
    metadata["book_alignment"] = {
        "model": model,
        "prompt": "prompts/book-translation-alignment.md",
        "field": "reading_units.en",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Align English book text to German reading units.")
    parser.add_argument("--book-dir", default="data/books/moby-dick")
    parser.add_argument("--manifest", default="chapter-001.json")
    parser.add_argument("--model", default=os.environ.get("OPENAI_TEXT_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--window-margin-words", type=int, default=450)
    parser.add_argument("--start-index", type=int, default=1, help="1-based reading unit index to start from.")
    parser.add_argument("--end-index", type=int, default=None, help="1-based reading unit index to stop at.")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    api_key = openai_key()
    if not api_key:
        print("OPENAI_API_KEY or OPEN_AI_API_KEY is missing in .env", file=sys.stderr)
        return 1

    book_dir = ROOT / args.book_dir
    manifest_path = book_dir / args.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reading_units = manifest.get("reading_units") or []
    start = max(1, args.start_index)
    end = args.end_index or len(reading_units)
    scoped_units = reading_units[start - 1 : end]
    target_units = scoped_units if args.force else [unit for unit in scoped_units if not unit.get("en")]
    if not target_units:
        print("skip: all reading units already have en")
        return 0

    english_source_path = book_dir / manifest.get("source", {}).get("en", "source/en.txt")
    english_chapter = normalize_text(extract_english_chapter(english_source_path.read_text(encoding="utf-8")))
    units_by_id = {unit["id"]: unit for unit in reading_units}
    total_batches = (len(target_units) + args.batch_size - 1) // args.batch_size

    print(f"align {len(target_units)} reading units with {args.model} in {total_batches} batch(es)")
    for batch_number, unit_batch in batches(target_units, args.batch_size):
        print(f"alignment batch {batch_number}/{total_batches}: {len(unit_batch)} reading units")
        response = post_openai(
            structured_payload(
                args.model,
                english_chapter,
                manifest,
                unit_batch,
                reading_units,
                args.window_margin_words,
            ),
            api_key,
            args.timeout,
        )
        generated = parse_items(response)
        valid_items, empty_ids = split_alignment_results(unit_batch, generated)
        generated = valid_items

        if empty_ids:
            unit_by_id = {unit["id"]: unit for unit in unit_batch}
            print(f"retry empty alignment(s): {', '.join(empty_ids)}")
            for empty_id in empty_ids:
                retry_unit = unit_by_id[empty_id]
                retry_response = post_openai(
                    structured_payload(
                        args.model,
                        english_chapter,
                        manifest,
                        [retry_unit],
                        reading_units,
                        args.window_margin_words,
                    ),
                    api_key,
                    args.timeout,
                )
                retry_generated = parse_items(retry_response)
                valid_retry_items, retry_empty_ids = split_alignment_results([retry_unit], retry_generated)
                if retry_empty_ids:
                    raise ValueError(f"{empty_id} still has an empty en value after retry")
                generated.extend(valid_retry_items)

        for item in generated:
            units_by_id[item["id"]]["en"] = normalize_text(item["en"])
        write_manifest(manifest_path, manifest, args.model)

    print(f"done: updated {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
