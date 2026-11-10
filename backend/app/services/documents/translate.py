from __future__ import annotations
import asyncio
import re
from app.services.pipeline import translate_text

# MyMemory's documented free endpoint has a 500-character query ceiling.
# Keep a safety margin for encoded characters and provider-side validation.
PROVIDER_QUERY_LIMIT = 450


def split_translation_chunks(text: str, limit: int = PROVIDER_QUERY_LIMIT) -> list[str]:
    """Split text at paragraph/sentence/word boundaries without exceeding limit."""
    text = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ''

    def emit(value: str):
        value = value.strip()
        if value:
            chunks.append(value)

    def add_piece(piece: str):
        nonlocal current
        piece = piece.strip()
        if not piece:
            return
        candidate = f'{current} {piece}'.strip() if current else piece
        if len(candidate) <= limit:
            current = candidate
        else:
            emit(current)
            current = piece

    for paragraph in re.split(r'\n\s*\n', text):
        paragraph = re.sub(r'[ \t\u00a0]+', ' ', paragraph).strip()
        if not paragraph:
            continue
        if current and len(current) + 2 + len(paragraph) > limit:
            emit(current)
            current = ''
        if len(paragraph) <= limit:
            add_piece(paragraph)
            continue
        # Sentence-aware split; retain punctuation with each sentence.
        sentences = re.split(r'(?<=[.!?।])\s+', paragraph)
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= limit:
                add_piece(sentence)
                continue
            # Very long sentence: split by words, then hard-split only a single
            # unbroken token that itself exceeds the provider limit.
            for word in sentence.split():
                if len(word) <= limit:
                    add_piece(word)
                else:
                    if current:
                        emit(current)
                        current = ''
                    for offset in range(0, len(word), limit):
                        emit(word[offset:offset + limit])
        if current:
            emit(current)
            current = ''
    if current:
        emit(current)
    if any(len(part) > limit for part in chunks):
        raise RuntimeError('Internal document chunking error: a translation segment exceeded the provider limit.')
    return chunks


async def translate_document(text: str, source: str, target: str, progress=None) -> str:
    parts = split_translation_chunks(text)
    output: list[str] = []
    for index, part in enumerate(parts):
        last_error = None
        for attempt in range(3):
            try:
                translated = await translate_text(part, source, target)
                output.append(translated)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.6 * (2 ** attempt))
        if last_error is not None:
            raise RuntimeError(f'Translation failed at section {index + 1} of {len(parts)} after 3 attempts: {last_error}') from last_error
        if progress:
            await progress(index + 1, len(parts))
    return '\n\n'.join(output)
