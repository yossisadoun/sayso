Split German source text into mobile reading units for a language-learning reader.

Rules:
- Each reading unit should usually contain 4 to 10 words.
- Prefer complete sentences when short enough.
- Otherwise split at natural punctuation, especially commas.
- If punctuation is too sparse, split at syntactic phrase boundaries.
- Preserve the German text exactly.
- Do not rewrite, modernize, or normalize spelling.
- Return structured JSON with stable reading unit ids.
