Group reading units into longer narration chunks for text-to-speech generation.

Rules:
- Narration chunks should be as long as practical while staying under the provider limit.
- Use 3000 characters as the hard maximum for ElevenLabs text input.
- Prefer a target maximum of 2500 to 2800 characters to leave room for expressive tags.
- Break at paragraph or sentence boundaries when possible.
- Keep reading units in order.
- Do not change the reading unit text.
- A narration chunk can contain many reading units; the UI can still display one reading unit at a time.
