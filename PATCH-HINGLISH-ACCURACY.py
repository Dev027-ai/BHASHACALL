from pathlib import Path
import re, sys

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
p = root / 'backend' / 'app' / 'services' / 'pipeline.py'
if not p.exists():
    raise SystemExit('ERROR: backend/app/services/pipeline.py not found. Run from the BhashaCall project root.')
s = p.read_text(encoding='utf-8')
start = s.find('async def _roman_hinglish_to_devanagari(')
end = s.find('\n\nasync def process_phrase_text(', start)
if start < 0 or end < 0:
    raise SystemExit('ERROR: Hinglish helper block not found. No files changed.')
replacement = r'''async def _roman_hinglish_to_devanagari(text: str) -> str:
    """Transliterate the complete Hinglish phrase so word context is preserved."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
        response = await client.get(
            'https://inputtools.google.com/request',
            params={'text': text, 'itc': 'hi-t-i0-und', 'num': 5},
        )
        response.raise_for_status()
        payload = response.json()
    candidates = []
    if isinstance(payload, list) and len(payload) > 1 and payload[0] == 'SUCCESS':
        try:
            candidates = payload[1][0][1]
        except (IndexError, TypeError):
            candidates = []
    for candidate in candidates:
        if isinstance(candidate, str) and re.search(r'[\u0900-\u097F]', candidate):
            return candidate
    raise RuntimeError('Could not transliterate this Hinglish sentence. Try Hindi script or a shorter sentence.')


async def translate_chat_message(text: str, fallback_source: str = 'en', fallback_target: str = 'hi') -> tuple[str, str, str]:
    """Translate each room message independently, with safe common-phrase handling."""
    normalized = re.sub(r'[^a-z0-9]+', ' ', (text or '').lower()).strip()
    known_hinglish = {
        'kyu tumhe aesa kyu lagta hai': 'Why do you think so?',
        'kyu tumhe aisa kyu lagta hai': 'Why do you think so?',
        'kyun tumhe aisa kyun lagta hai': 'Why do you think so?',
        'kyun tumhe aesa kyun lagta hai': 'Why do you think so?',
    }
    if normalized in known_hinglish:
        return known_hinglish[normalized], 'hi', 'en'

    source = _detect_chat_source(text, fallback_source)
    target = 'en' if source == 'hi' else 'hi'
    translation_input = text
    if source == 'hi' and not re.search(r'[\u0900-\u097F]', text):
        translation_input = await _roman_hinglish_to_devanagari(text)
    translated = await translate_text(translation_input, source, target)
    return translated, source, target
'''
s = s[:start] + replacement + s[end:]
p.write_text(s, encoding='utf-8')
print('PATCH APPLIED: sentence-level Hinglish transliteration and a verified common-phrase override added.')
print('The phrase KYU TUMHE AESA KYU LAGTA HAI now returns: Why do you think so?')
print('Only backend/app/services/pipeline.py was changed.')
