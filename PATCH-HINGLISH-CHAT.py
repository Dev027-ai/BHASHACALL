from pathlib import Path
import re, sys

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
backend = root / 'backend' / 'app'
pipeline = backend / 'services' / 'pipeline.py'
main = backend / 'main.py'

if not pipeline.exists() or not main.exists():
    raise SystemExit('ERROR: Run this script from the BhashaCall project root (the folder containing backend and frontend).')

p = pipeline.read_text(encoding='utf-8')
m = main.read_text(encoding='utf-8')

marker = 'async def translate_chat_message('
if marker not in p:
    helper = r'''

# Per-message language handling for room chat. This is intentionally separate
# from the microphone direction setting: either participant may type either language.
_HINGLISH_WORDS = {
    'hai','hain','ho','hu','haan','nahi','nahin','kyu','kyon','kyunki','kya',
    'kaise','kaisa','kaisi','tum','tumhe','tumhein','tumko','aap','apko','apko',
    'mujhe','mera','meri','mere','tera','teri','tere','ham','hum','hume','humein',
    'aisa','aesi','aese','aesa','waisa','waise','lagta','lagti','lagte','accha',
    'achha','acha','theek','thik','kar','karo','karta','karte','karna','batao',
    'bolo','matlab','abhi','kal','ka','ki','ke','se','nahi','bhi','bahut','bohot',
    'kuch','koi','kyun','kaun','kab','kahan','yahan','wahan','pata','samajh',
}
_HINGLISH_DEVANAGARI = {
    'kyu': 'क्यों', 'kyon': 'क्यों', 'kyunki': 'क्योंकि', 'tumhe': 'तुम्हें',
    'tumhein': 'तुम्हें', 'aesa': 'ऐसा', 'aisa': 'ऐसा', 'aesi': 'ऐसी',
    'aese': 'ऐसे', 'hai': 'है', 'hain': 'हैं', 'lagta': 'लगता', 'lagti': 'लगती',
    'lagte': 'लगते', 'mujhe': 'मुझे', 'kya': 'क्या', 'kaise': 'कैसे',
    'kaisa': 'कैसा', 'kaisi': 'कैसी', 'nahi': 'नहीं', 'nahin': 'नहीं',
    'tum': 'तुम', 'tumko': 'तुमको', 'aap': 'आप', 'ho': 'हो', 'haan': 'हाँ',
    'batao': 'बताओ', 'bolo': 'बोलो', 'mera': 'मेरा', 'meri': 'मेरी',
    'mere': 'मेरे', 'accha': 'अच्छा', 'achha': 'अच्छा', 'acha': 'अच्छा',
    'kyun': 'क्यों', 'kuch': 'कुछ', 'bahut': 'बहुत', 'bohot': 'बहुत',
    'abhi': 'अभी', 'kaun': 'कौन', 'kab': 'कब', 'kahan': 'कहाँ',
    'yahan': 'यहाँ', 'wahan': 'वहाँ', 'samajh': 'समझ', 'pata': 'पता',
}


def _detect_chat_source(text: str, fallback_source: str = 'en') -> str:
    if re.search(r'[\u0900-\u097F]', text):
        return 'hi'
    words = re.findall(r"[a-zA-Z']+", text.lower())
    hits = sum(1 for word in words if word in _HINGLISH_WORDS)
    # Require multiple Hinglish indicators to avoid classifying ordinary English
    # containing one ambiguous word as Hindi.
    if hits >= 2 or (hits >= 1 and len(words) <= 3 and words and words[0] in {'hai','hain','nahi','kyu','kyon','haan'}):
        return 'hi'
    # If there is no Hindi script or convincing Hinglish vocabulary, treat it as English.
    # Do not fall back to the UI's direction switch: it is not a per-message language signal.
    return 'en'


async def _roman_hinglish_to_devanagari(text: str) -> str:
    """Transliterate Romanized Hindi using Google Input Tools, with common-word fixes."""
    tokens = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
    output = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=5)) as client:
        for token in tokens:
            if not token or not token[0].isalpha():
                output.append(token)
                continue
            lower = token.lower()
            if lower in _HINGLISH_DEVANAGARI:
                output.append(_HINGLISH_DEVANAGARI[lower])
                continue
            try:
                response = await client.get(
                    'https://inputtools.google.com/request',
                    params={'text': lower, 'itc': 'hi-t-i0-und', 'num': 1},
                )
                response.raise_for_status()
                payload = response.json()
                candidate = payload[1][0][1][0] if payload[0] == 'SUCCESS' else ''
                output.append(candidate or token)
            except Exception:
                # Keep unknown English words intact; known Hindi words are covered above.
                output.append(token)
    converted = ''.join(output)
    if not re.search(r'[\u0900-\u097F]', converted):
        raise RuntimeError('Could not transliterate this Romanized Hinglish message. Try Hindi script or a shorter Hinglish sentence.')
    return converted


async def translate_chat_message(text: str, fallback_source: str = 'en', fallback_target: str = 'hi') -> tuple[str, str, str]:
    source = _detect_chat_source(text, fallback_source)
    target = 'en' if source == 'hi' else 'hi'
    translation_input = text
    if source == 'hi' and not re.search(r'[\u0900-\u097F]', text):
        translation_input = await _roman_hinglish_to_devanagari(text)
    translated = await translate_text(translation_input, source, target)
    return translated, source, target
'''
    # Insert before the existing phrase processor, retaining current Gnani/STT/TTS edits.
    anchor = '\n\nasync def process_phrase_text('
    if anchor not in p:
        raise SystemExit('ERROR: Could not locate process_phrase_text() in pipeline.py; no files changed.')
    p = p.replace(anchor, helper + anchor, 1)

old_import = 'from app.services.pipeline import process_phrase_text, synthesize_speech, translate_text'
new_import = 'from app.services.pipeline import process_phrase_text, synthesize_speech, translate_text, translate_chat_message'
if old_import in m:
    m = m.replace(old_import, new_import, 1)
elif 'translate_chat_message' not in m:
    raise SystemExit('ERROR: Could not safely update the pipeline import in main.py; no files changed.')

old_chat = """                source=str(msg.get('source_language','hi')); target=str(msg.get('target_language','en'))
                translation_error=None
                try:
                    translated=await translate_text(text,source,target)
                except Exception as exc:
                    # Never lose a user's chat message because a translation provider is down.
                    translated=text
                    translation_error=safe_error(exc)
"""
new_chat = """                fallback_source=str(msg.get('source_language','en')); fallback_target=str(msg.get('target_language','hi'))
                source=fallback_source; target=fallback_target
                translation_error=None
                try:
                    translated, source, target = await translate_chat_message(text, fallback_source, fallback_target)
                except Exception as exc:
                    # Never lose a user's chat message because translation/transliteration is unavailable.
                    translated=text
                    translation_error=safe_error(exc)
"""
if old_chat in m:
    m = m.replace(old_chat, new_chat, 1)
elif 'await translate_chat_message(text, fallback_source, fallback_target)' not in m:
    raise SystemExit('ERROR: Could not locate the expected room chat translation block in main.py; no files changed.')

pipeline.write_text(p, encoding='utf-8')
main.write_text(m, encoding='utf-8')
print('PATCH APPLIED: room chat now detects language per message and transliterates common Romanized Hinglish before Hindi→English translation.')
print('Files updated: backend/app/services/pipeline.py and backend/app/main.py')
print('Next: run backend Python compile check, then restart the backend.')
