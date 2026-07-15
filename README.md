# Say So POC

Run the local server before using API-backed features:

```bash
python3 server.py
```

Then open:

```text
http://127.0.0.1:4173
```

Local admin:

```text
http://127.0.0.1:4173/admin
```

Opening `index.html` directly as a `file://` URL will show the static UI, but voice loading cannot work because the browser cannot call `/api/voices` from a file origin.

Playback uses cached local ElevenLabs MP3 files and their word timing JSON when they exist. Browser speech synthesis is only a fallback for missing cached audio.

The data model separates small on-screen `reading_units` from longer `narration_chunks`. This keeps the UI readable while letting ElevenLabs generate smoother, less cut-up audio.

## Book Folders

Books live in their own folders under `data/books/` so source text, art, manifests, and cached audio stay together.

Current Moby-Dick layout:

```text
data/books/moby-dick/
  assets/moby_dick_icon.png
  source/de.txt
  source/en.txt
  page-001.json
  audio/page-001/
```

## ElevenLabs Cost Control

Generated ElevenLabs audio is cached locally next to the book and reused by default. Generation scripts should skip existing audio/timing files unless explicitly run with a force/regenerate flag.

The generator uses `narration_chunks[*].tts_text` and defaults to the book's `expressive_model_id` (`eleven_v3` for this POC) when the text includes bracketed performance directions. Cached audio is reused only when the timing JSON matches the selected voice, model, and exact TTS text.

Build long narration chunks from reading units:

```bash
python3 scripts/build_narration_chunks.py
```

Generate the first-page POC audio after selecting a valid `voice_id`:

```bash
python3 scripts/generate_elevenlabs_audio.py --voice-id YOUR_VOICE_ID
```

Generate or validate a single narration chunk first:

```bash
python3 scripts/generate_elevenlabs_audio.py --voice-id YOUR_VOICE_ID --chunk-id narration-001
```

Generated files will be written to:

```text
data/books/moby-dick/audio/page-001/narration-001.mp3
data/books/moby-dick/audio/page-001/narration-001.json
```

Regenerate deliberately with:

```bash
python3 scripts/generate_elevenlabs_audio.py --voice-id YOUR_VOICE_ID --force
```

## OpenAI Preprocessing

The one-time preprocessing script reads `OPENAI_API_KEY` or `OPEN_AI_API_KEY` from `.env` and writes generated fields into the page manifest:

```text
reading_units[*].en_ai
reading_units[*].notes
narration_chunks[*].tts_text
```

Run it for the current Moby-Dick POC page:

```bash
python3 scripts/preprocess_book_with_openai.py
```

Force regeneration of those generated fields:

```bash
python3 scripts/preprocess_book_with_openai.py --force
```

Run only one preprocessing stage:

```bash
python3 scripts/preprocess_book_with_openai.py --only translations
python3 scripts/preprocess_book_with_openai.py --only word-notes
python3 scripts/preprocess_book_with_openai.py --only expressive
```

Base prompts live in `prompts/`:

```text
prompts/learner-translation.md
prompts/learner-word-notes.md
prompts/elevenlabs-expressive-tts.md
prompts/reading-unit-segmentation.md
prompts/narration-chunking.md
```

## Admin Processing Order

The admin exposes the pipeline as separate local steps:

1. Upload source text into `data/books/<book-id>/source/`.
2. Build the first manifest and split German text into mobile reading units.
3. Build longer `narration_chunks` from the reading units.
4. Generate learner translations with OpenAI.
5. Generate word notes with OpenAI.
6. Generate expressive TTS text for each narration chunk with OpenAI.
7. Generate cached ElevenLabs audio and timing JSON.

The reader can preview any manifest with:

```text
http://127.0.0.1:4173/index.html?book=moby-dick&manifest=page-001.json
```
