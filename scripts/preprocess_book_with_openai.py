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
TRANSLATION_PROMPT_PATH = ROOT / "prompts" / "learner-translation.md"
WORD_NOTES_PROMPT_PATH = ROOT / "prompts" / "learner-word-notes.md"
EXPRESSIVE_PROMPT_PATH = ROOT / "prompts" / "elevenlabs-expressive-tts.md"


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

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI returned {error.code}: {detail}") from error


def response_text(response):
    if response.get("output_text"):
        return response["output_text"]

    fragments = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                fragments.append(content["text"])
    return "\n".join(fragments)


def strip_bracket_directions(text):
    return re.sub(r"\s*\[[^\]]+\]\s*", " ", text)


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def compact_reading_units(reading_units):
    return [
        {
            "id": unit["id"],
            "german": unit["display_text"],
            "current_english": unit.get("en", ""),
        }
        for unit in reading_units
    ]


def clean_word(word):
    return re.sub(r'[.,;:!?"“”„”—–]', "", word)


def visible_words(text):
    words = []
    seen = set()
    for raw_word in text.split():
        word = clean_word(raw_word)
        if word and word not in seen:
            words.append(word)
            seen.add(word)
    return words


def compact_word_note_units(reading_units):
    return [
        {
            "id": unit["id"],
            "german": unit["display_text"],
            "words": visible_words(unit["display_text"]),
            "current_english": unit.get("en_ai") or unit.get("en", ""),
        }
        for unit in reading_units
    ]


def compact_narration_chunks(narration_chunks):
    return [
        {
            "id": chunk["id"],
            "german": chunk["display_text"],
        }
        for chunk in narration_chunks
    ]


def structured_payload(model, instructions, user_payload, schema_name, item_name, item_schema, item_count):
    return {
        "model": model,
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [item_name],
                    "properties": {
                        item_name: {
                            "type": "array",
                            "minItems": item_count,
                            "maxItems": item_count,
                            "items": item_schema,
                        }
                    },
                },
            }
        },
    }


def parse_items(response, item_name):
    text = response_text(response)
    if not text:
        raise RuntimeError("OpenAI response did not include text output")
    return json.loads(text)[item_name]


def validate_ids(source_items, generated_items, item_label):
    by_id = {item["id"]: item for item in source_items}
    seen = set()
    for item in generated_items:
        item_id = item.get("id")
        if item_id not in by_id:
            raise ValueError(f"OpenAI returned unknown {item_label} id: {item_id}")
        if item_id in seen:
            raise ValueError(f"OpenAI returned duplicate {item_label} id: {item_id}")
        seen.add(item_id)

    expected_ids = set(by_id)
    if seen != expected_ids:
        missing = ", ".join(sorted(expected_ids - seen))
        raise ValueError(f"OpenAI response is missing {item_label} id(s): {missing}")
    return by_id


def build_translation_payload(manifest, reading_units, model):
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "en_ai"],
        "properties": {
            "id": {"type": "string"},
            "en_ai": {"type": "string"},
        },
    }
    return structured_payload(
        model=model,
        instructions=TRANSLATION_PROMPT_PATH.read_text(encoding="utf-8"),
        user_payload={
            "book": manifest.get("title", "Unknown book"),
            "page_id": manifest.get("page_id"),
            "reading_units": compact_reading_units(reading_units),
        },
        schema_name="learner_translations",
        item_name="reading_units",
        item_schema=item_schema,
        item_count=len(reading_units),
    )


def build_word_notes_payload(manifest, reading_units, model):
    note_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["word", "label", "hint"],
        "properties": {
            "word": {"type": "string"},
            "label": {"type": "string"},
            "hint": {"type": "string"},
        },
    }
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "notes"],
        "properties": {
            "id": {"type": "string"},
            "notes": {
                "type": "array",
                "items": note_schema,
            },
        },
    }
    return structured_payload(
        model=model,
        instructions=WORD_NOTES_PROMPT_PATH.read_text(encoding="utf-8"),
        user_payload={
            "book": manifest.get("title", "Unknown book"),
            "page_id": manifest.get("page_id"),
            "reading_units": compact_word_note_units(reading_units),
        },
        schema_name="learner_word_notes",
        item_name="reading_units",
        item_schema=item_schema,
        item_count=len(reading_units),
    )


def build_expressive_payload(manifest, narration_chunks, model):
    base_prompt = EXPRESSIVE_PROMPT_PATH.read_text(encoding="utf-8")
    instructions = (
        f"{base_prompt}\n\n"
        "Batch wrapper: apply the instructions independently to each supplied item. "
        "Return structured JSON only because this is a batch API call. Each `tts_text` value must be exactly "
        "the enhanced German text for that item."
    )
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "tts_text"],
        "properties": {
            "id": {"type": "string"},
            "tts_text": {"type": "string"},
        },
    }
    return structured_payload(
        model=model,
        instructions=instructions,
        user_payload={
            "book": manifest.get("title", "Unknown book"),
            "page_id": manifest.get("page_id"),
            "narration_chunks": compact_narration_chunks(narration_chunks),
        },
        schema_name="expressive_tts_text",
        item_name="narration_chunks",
        item_schema=item_schema,
        item_count=len(narration_chunks),
    )


def validate_translations(reading_units, generated):
    by_id = validate_ids(reading_units, generated, "reading unit")
    for item in generated:
        en_ai = normalize_text(item.get("en_ai", ""))
        if not en_ai:
            raise ValueError(f"{item['id']} has an empty en_ai value")
    return by_id


def split_translation_results(reading_units, generated):
    validate_ids(reading_units, generated, "reading unit")
    valid = []
    empty_ids = []
    for item in generated:
        if normalize_text(item.get("en_ai", "")):
            valid.append(item)
        else:
            empty_ids.append(item["id"])
    return valid, empty_ids


def validate_word_notes(reading_units, generated):
    by_id = validate_ids(reading_units, generated, "reading unit")
    for item in generated:
        expected_words = set(visible_words(by_id[item["id"]]["display_text"]))
        seen_words = set()
        for note in item.get("notes", []):
            word = clean_word(normalize_text(note.get("word", "")))
            label = normalize_text(note.get("label", ""))
            hint = normalize_text(note.get("hint", ""))
            if not word:
                raise ValueError(f"{item['id']} returned an empty word note")
            if word not in expected_words:
                raise ValueError(f"{item['id']} returned note for word not in text: {word!r}")
            if word in seen_words:
                continue
            if not label or not hint:
                raise ValueError(f"{item['id']} returned incomplete note for word: {word!r}")
            seen_words.add(word)
        if seen_words != expected_words:
            missing = ", ".join(sorted(expected_words - seen_words))
            raise ValueError(f"{item['id']} is missing word note(s): {missing}")
    return by_id


def normalize_word_notes(notes):
    normalized = {}
    for note in notes:
        word = clean_word(normalize_text(note["word"]))
        label = normalize_text(note["label"]).lower()
        hint = normalize_text(note["hint"])
        normalized[word] = [label, hint]
    return normalized


def validate_expressive_tts(narration_chunks, generated):
    by_id = validate_ids(narration_chunks, generated, "narration chunk")
    for item in generated:
        tts_text = item.get("tts_text", "")
        direction_count = len(re.findall(r"\[[^\]]+\]", tts_text))
        if direction_count < 1 or direction_count > 2:
            raise ValueError(f"{item['id']} must include one or two bracketed directions")

        expected = normalize_text(by_id[item["id"]]["display_text"])
        actual = normalize_text(strip_bracket_directions(tts_text))
        if actual != expected:
            raise ValueError(
                f"{item['id']} expressive text changed the German text: expected {expected!r}, got {actual!r}"
            )
    return by_id


def batches(items, size):
    for index in range(0, len(items), size):
        yield index // size + 1, items[index : index + size]


def set_openai_metadata(manifest, model):
    manifest["openai_preprocessing"] = {
        "model": model,
        "prompts": {
            "learner_translation": "prompts/learner-translation.md",
            "learner_word_notes": "prompts/learner-word-notes.md",
            "expressive_tts": "prompts/elevenlabs-expressive-tts.md",
        },
        "fields": ["en_ai", "notes", "narration_chunks.tts_text"],
    }


def write_manifest(manifest_path, manifest, model):
    set_openai_metadata(manifest, model)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Generate OpenAI learner metadata and expressive TTS text.")
    parser.add_argument("--book-dir", default="data/books/moby-dick")
    parser.add_argument("--manifest", default="page-001.json")
    parser.add_argument("--model", default=os.environ.get("OPENAI_TEXT_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--translation-batch-size", type=int, default=60)
    parser.add_argument("--word-notes-batch-size", type=int, default=35)
    parser.add_argument("--expressive-batch-size", type=int, default=4)
    parser.add_argument(
        "--only",
        choices=("all", "translations", "word-notes", "expressive"),
        default="all",
        help="Limit preprocessing to one stage.",
    )
    args = parser.parse_args()

    book_dir = ROOT / args.book_dir
    manifest_path = book_dir / args.manifest
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reading_units = manifest.get("reading_units") or manifest.get("chunks") or []
    narration_chunks = manifest.get("narration_chunks") or []
    api_key = None

    def require_openai_key():
        nonlocal api_key
        if api_key:
            return api_key
        api_key = openai_key()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY or OPEN_AI_API_KEY is missing in .env")
        return api_key

    if args.only in ("all", "translations"):
        if args.force or not all(unit.get("en_ai") for unit in reading_units):
            target_units = reading_units if args.force else [unit for unit in reading_units if not unit.get("en_ai")]
            manifest_units_by_id = {unit["id"]: unit for unit in reading_units}
            generated_by_id = {}
            total_batches = (len(target_units) + args.translation_batch_size - 1) // args.translation_batch_size
            print(f"translate {len(target_units)} reading units with {args.model} in {total_batches} batch(es)")
            for batch_number, unit_batch in batches(target_units, args.translation_batch_size):
                print(f"translation batch {batch_number}/{total_batches}: {len(unit_batch)} reading units")
                response = post_openai(
                    build_translation_payload(manifest, unit_batch, args.model),
                    require_openai_key(),
                    args.timeout,
                )
                generated = parse_items(response, "reading_units")
                valid_items, empty_ids = split_translation_results(unit_batch, generated)
                generated_by_id.update({item["id"]: item for item in valid_items})
                if empty_ids:
                    unit_by_id = {unit["id"]: unit for unit in unit_batch}
                    print(f"retry empty translation(s): {', '.join(empty_ids)}")
                    for empty_id in empty_ids:
                        retry_unit = unit_by_id[empty_id]
                        retry_response = post_openai(
                            build_translation_payload(manifest, [retry_unit], args.model),
                            require_openai_key(),
                            args.timeout,
                        )
                        retry_generated = parse_items(retry_response, "reading_units")
                        validate_translations([retry_unit], retry_generated)
                        generated_by_id[empty_id] = retry_generated[0]
                for item in generated_by_id.values():
                    manifest_units_by_id[item["id"]]["en_ai"] = normalize_text(item["en_ai"])
                write_manifest(manifest_path, manifest, args.model)
            for unit in reading_units:
                if unit["id"] in generated_by_id:
                    unit["en_ai"] = normalize_text(generated_by_id[unit["id"]]["en_ai"])
        else:
            print("skip translations: all reading units already have en_ai")

    if args.only in ("all", "word-notes"):
        target_units = reading_units if args.force else [unit for unit in reading_units if not unit.get("notes")]
        if target_units:
            manifest_units_by_id = {unit["id"]: unit for unit in reading_units}
            generated_by_id = {}
            total_batches = (len(target_units) + args.word_notes_batch_size - 1) // args.word_notes_batch_size
            print(f"word notes {len(target_units)} reading units with {args.model} in {total_batches} batch(es)")
            for batch_number, unit_batch in batches(target_units, args.word_notes_batch_size):
                print(f"word notes batch {batch_number}/{total_batches}: {len(unit_batch)} reading units")
                response = post_openai(
                    build_word_notes_payload(manifest, unit_batch, args.model),
                    require_openai_key(),
                    args.timeout,
                )
                generated = parse_items(response, "reading_units")
                validate_word_notes(unit_batch, generated)
                generated_by_id.update({item["id"]: item for item in generated})
                for item in generated:
                    manifest_units_by_id[item["id"]]["notes"] = normalize_word_notes(item["notes"])
                write_manifest(manifest_path, manifest, args.model)
            for unit in target_units:
                unit["notes"] = normalize_word_notes(generated_by_id[unit["id"]]["notes"])
        else:
            print("skip word notes: all reading units already have notes")

    if args.only in ("all", "expressive"):
        if not narration_chunks:
            print("skip expressive: manifest has no narration_chunks", file=sys.stderr)
        elif args.force or not all(chunk.get("tts_text") and "[" in chunk.get("tts_text", "") for chunk in narration_chunks):
            target_chunks = [
                chunk
                for chunk in narration_chunks
                if args.force or not (chunk.get("tts_text") and "[" in chunk.get("tts_text", ""))
            ]
            manifest_chunks_by_id = {chunk["id"]: chunk for chunk in narration_chunks}
            generated_by_id = {}
            total_batches = (len(target_chunks) + args.expressive_batch_size - 1) // args.expressive_batch_size
            print(f"expressive tts {len(target_chunks)} narration chunks with {args.model} in {total_batches} batch(es)")
            for batch_number, chunk_batch in batches(target_chunks, args.expressive_batch_size):
                print(f"expressive batch {batch_number}/{total_batches}: {len(chunk_batch)} narration chunks")
                response = post_openai(
                    build_expressive_payload(manifest, chunk_batch, args.model),
                    require_openai_key(),
                    args.timeout,
                )
                generated = parse_items(response, "narration_chunks")
                validate_expressive_tts(chunk_batch, generated)
                generated_by_id.update({item["id"]: item for item in generated})
                for item in generated:
                    chunk = manifest_chunks_by_id[item["id"]]
                    chunk["tts_text"] = item["tts_text"]
                    chunk.pop("audio", None)
                    chunk.pop("timing", None)
                write_manifest(manifest_path, manifest, args.model)
            for chunk in narration_chunks:
                if chunk["id"] in generated_by_id:
                    chunk["tts_text"] = generated_by_id[chunk["id"]]["tts_text"]
                    chunk.pop("audio", None)
                    chunk.pop("timing", None)
        else:
            print("skip expressive: all narration chunks already have bracketed tts_text")

    write_manifest(manifest_path, manifest, args.model)
    print(f"done: updated {manifest_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
