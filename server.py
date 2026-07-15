#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
BOOKS_DIR = ROOT / "data" / "books"
MAX_JSON_BODY_BYTES = 25 * 1024 * 1024
BOOK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
MANIFEST_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*\.json$")


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


def split_paragraph_into_units(paragraph, max_words):
    tokens = paragraph.split()
    units = []
    current = []

    def should_break(token):
        return bool(re.search(r"[.!?,;:]$|[\u2013\u2014-]$", token))

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


def build_reading_units(german_text, max_words):
    units = []
    paragraphs = [
        normalize_text(paragraph)
        for paragraph in re.split(r"\n\s*\n", german_text)
        if paragraph.strip()
    ]
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
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


def validate_book_id(book_id):
    if not isinstance(book_id, str) or not BOOK_ID_PATTERN.match(book_id):
        raise ValueError("Book id must use lowercase letters, numbers, and hyphens")
    return book_id


def validate_manifest_name(manifest):
    if not isinstance(manifest, str) or not MANIFEST_PATTERN.match(manifest):
        raise ValueError("Manifest must be a lowercase .json filename")
    return manifest


def book_dir_for(book_id):
    return BOOKS_DIR / validate_book_id(book_id)


def manifest_path_for(book_id, manifest):
    return book_dir_for(book_id) / validate_manifest_name(manifest)


def summarize_manifest(book_dir, manifest_path):
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "name": manifest_path.name,
            "error": str(error),
        }

    reading_units = manifest.get("reading_units") or manifest.get("chunks") or []
    narration_chunks = manifest.get("narration_chunks") or []
    translated = sum(1 for unit in reading_units if unit.get("en_ai"))
    noted = sum(1 for unit in reading_units if unit.get("notes"))
    expressive = sum(1 for chunk in narration_chunks if "[" in chunk.get("tts_text", ""))
    audio_ready = 0
    for chunk in narration_chunks:
        audio = chunk.get("audio")
        timing = chunk.get("timing")
        if audio and timing and (book_dir / audio).exists() and (book_dir / timing).exists():
            audio_ready += 1

    return {
        "name": manifest_path.name,
        "title": manifest.get("title") or book_dir.name,
        "page_id": manifest.get("page_id", manifest_path.stem),
        "reading_units": len(reading_units),
        "narration_chunks": len(narration_chunks),
        "translated_units": translated,
        "noted_units": noted,
        "expressive_chunks": expressive,
        "audio_chunks": audio_ready,
        "selected_voice_id": manifest.get("selected_voice_id", ""),
        "reader_url": f"/index.html?book={book_dir.name}&manifest={manifest_path.name}",
    }


def summarize_book(book_dir):
    manifests = [summarize_manifest(book_dir, path) for path in sorted(book_dir.glob("*.json"))]
    title = manifests[0].get("title") if manifests else book_dir.name
    return {
        "book_id": book_dir.name,
        "title": title,
        "manifests": manifests,
    }


class Handler(SimpleHTTPRequestHandler):
    def send_head(self):
        range_header = self.headers.get("Range")
        if range_header:
            path = Path(self.translate_path(self.path))
            if path.is_file():
                return self.send_range_head(path, range_header)
        return super().send_head()

    def send_range_head(self, path, range_header):
        size = path.stat().st_size
        match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
        if not match:
            self.send_error(416, "Requested Range Not Satisfiable")
            return None

        start_text, end_text = match.groups()
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            suffix_length = int(end_text or "0")
            if suffix_length <= 0:
                self.send_error(416, "Requested Range Not Satisfiable")
                return None
            start = max(size - suffix_length, 0)
            end = size - 1

        if start >= size or end < start:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None

        end = min(end, size - 1)
        file = path.open("rb")
        file.seek(start)
        self.range_remaining = end - start + 1

        self.send_response(206)
        self.send_header("Content-type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(self.range_remaining))
        self.end_headers()
        return file

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "range_remaining", None)
        if remaining is None:
            super().copyfile(source, outputfile)
            return

        try:
            while remaining > 0:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)
                remaining -= len(chunk)
        finally:
            self.range_remaining = None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_JSON_BODY_BYTES:
            raise ValueError("Request body is too large")
        body = self.rfile.read(length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/admin":
            self.path = "/admin.html"
            super().do_GET()
            return
        if path == "/api/voices":
            self.handle_voices()
            return
        if path == "/api/admin/books":
            self.handle_admin_books()
            return
        super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/admin/books":
                self.handle_admin_create_book()
                return
            if path == "/api/admin/run":
                self.handle_admin_run()
                return
            self.send_json(404, {"error": "Unknown endpoint"})
        except json.JSONDecodeError:
            self.send_json(400, {"error": "Request body must be JSON"})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except Exception as error:
            self.send_json(500, {"error": str(error)})

    def handle_voices(self):
        api_key = elevenlabs_key()
        if not api_key:
            self.send_json(500, {"error": "ELEVENLABS_API_KEY is missing"})
            return

        request = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={
                "Accept": "application/json",
                "xi-api-key": api_key,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            message = "ElevenLabs voice request failed"
            if error.code in (401, 403):
                message = "ElevenLabs rejected the API key"
            self.send_json(error.code, {"error": message, "status": error.code})
            return
        except Exception:
            self.send_json(502, {"error": "Could not reach ElevenLabs"})
            return

        voices = [
            {
                "voice_id": voice.get("voice_id"),
                "name": voice.get("name"),
                "category": voice.get("category"),
                "labels": voice.get("labels") or {},
                "preview_url": voice.get("preview_url"),
            }
            for voice in data.get("voices", [])
            if voice.get("voice_id") and voice.get("name")
        ]
        voices.sort(key=lambda voice: voice["name"].lower())
        self.send_json(200, {"voices": voices})

    def handle_admin_books(self):
        BOOKS_DIR.mkdir(parents=True, exist_ok=True)
        books = [
            summarize_book(path)
            for path in sorted(BOOKS_DIR.iterdir())
            if path.is_dir() and BOOK_ID_PATTERN.match(path.name)
        ]
        self.send_json(
            200,
            {
                "books": books,
                "secrets": {
                    "openai": bool(openai_key()),
                    "elevenlabs": bool(elevenlabs_key()),
                },
            },
        )

    def handle_admin_create_book(self):
        payload = self.read_json()
        book_id = validate_book_id(payload.get("book_id", ""))
        title = normalize_text(payload.get("title") or book_id.replace("-", " ").title())
        manifest_name = validate_manifest_name(payload.get("manifest") or "page-001.json")
        german_text = payload.get("german_text") or ""
        english_text = payload.get("english_text") or ""
        replace = bool(payload.get("replace"))
        max_words = int(payload.get("max_words") or 10)
        max_words = max(4, min(18, max_words))

        if not german_text.strip():
            raise ValueError("German source text is required")

        book_dir = book_dir_for(book_id)
        manifest_path = manifest_path_for(book_id, manifest_name)
        if manifest_path.exists() and not replace:
            self.send_json(409, {"error": "Manifest already exists. Enable replace to overwrite it."})
            return

        source_dir = book_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "de.txt").write_text(german_text, encoding="utf-8")
        if english_text.strip():
            (source_dir / "en.txt").write_text(english_text, encoding="utf-8")

        reading_units = build_reading_units(german_text, max_words)
        manifest = {
            "book_id": book_id,
            "title": title,
            "page_id": Path(manifest_name).stem,
            "source": {
                "de": "source/de.txt",
                "en": "source/en.txt" if english_text.strip() else "",
            },
            "audio_dir": f"audio/{Path(manifest_name).stem}",
            "default_model_id": "eleven_multilingual_v2",
            "expressive_model_id": "eleven_v3",
            "selected_voice_id": "",
            "selected_model_id": "",
            "source_stats": {
                "de_chars": len(german_text),
                "de_words": len(german_text.split()),
                "en_chars": len(english_text),
                "en_words": len(english_text.split()),
                "reading_units": len(reading_units),
                "max_words_per_unit": max_words,
            },
            "reading_units": reading_units,
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        self.send_json(
            201,
            {
                "book": summarize_book(book_dir),
                "manifest": summarize_manifest(book_dir, manifest_path),
            },
        )

    def handle_admin_run(self):
        payload = self.read_json()
        step = payload.get("step")
        book_id = validate_book_id(payload.get("book_id", ""))
        manifest_name = validate_manifest_name(payload.get("manifest") or "page-001.json")
        manifest_path = manifest_path_for(book_id, manifest_name)
        if not manifest_path.exists():
            raise ValueError("Selected manifest does not exist")

        book_arg = f"data/books/{book_id}"
        force = bool(payload.get("force"))
        command = None

        if step == "narration-chunks":
            max_chars = int(payload.get("max_chars") or 2800)
            max_chars = max(400, min(2800, max_chars))
            command = [
                sys.executable,
                "scripts/build_narration_chunks.py",
                "--book-dir",
                book_arg,
                "--manifest",
                manifest_name,
                "--max-chars",
                str(max_chars),
            ]
        elif step in ("all", "translations", "word-notes", "expressive"):
            command = [
                sys.executable,
                "scripts/preprocess_book_with_openai.py",
                "--book-dir",
                book_arg,
                "--manifest",
                manifest_name,
                "--only",
                step,
            ]
            if force:
                command.append("--force")
        elif step == "audio":
            voice_id = normalize_text(payload.get("voice_id") or "")
            if not voice_id:
                raise ValueError("Voice id is required for audio generation")
            command = [
                sys.executable,
                "scripts/generate_elevenlabs_audio.py",
                "--book-dir",
                book_arg,
                "--manifest",
                manifest_name,
                "--voice-id",
                voice_id,
            ]
            model_id = normalize_text(payload.get("model_id") or "")
            chunk_id = normalize_text(payload.get("chunk_id") or "")
            if model_id:
                command.extend(["--model-id", model_id])
            if chunk_id:
                command.extend(["--chunk-id", chunk_id])
            if force:
                command.append("--force")
        else:
            raise ValueError("Unknown processing step")

        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        elapsed = round(time.monotonic() - started, 2)
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        status = summarize_manifest(book_dir_for(book_id), manifest_path)
        self.send_json(
            200 if result.returncode == 0 else 500,
            {
                "ok": result.returncode == 0,
                "code": result.returncode,
                "duration_seconds": elapsed,
                "command": command,
                "output": output,
                "manifest": status,
            },
        )


def main():
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("127.0.0.1", 4173), Handler)
    print("Serving Say So at http://127.0.0.1:4173")
    server.serve_forever()


if __name__ == "__main__":
    main()
