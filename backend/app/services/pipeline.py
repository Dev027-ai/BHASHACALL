"""Provider adapters for BhashaCall. Gnani is the only speech provider.

Gnani's exact tenant endpoint/payload contract is account-specific; configure the
STT/TTS URLs supplied by Gnani rather than assuming a public endpoint.
"""
from __future__ import annotations
import re
import os
import socket
import asyncio
import json
import base64
import io
import wave
from urllib.parse import urlparse
from typing import Any
import httpx
import websockets


def _required(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing {name}. Configure it in backend/.env and restart the API.')
    return value


def _gnani_headers() -> dict[str, str]:
    key = _required('GNANI_API_KEY')
    header = os.getenv('GNANI_AUTH_HEADER', 'X-API-Key-ID').strip() or 'X-API-Key-ID'
    scheme = os.getenv('GNANI_AUTH_SCHEME', '').strip()
    value = f'{scheme} {key}'.strip() if scheme else key
    return {header: value, 'Accept': 'application/json'}


def _lang(language: str) -> str:
    value = (language or 'auto').lower()
    if value.startswith('hi'): return 'hi'
    if value.startswith('en'): return 'en'
    return value



async def transcribe_with_gnani(audio_bytes: bytes, mime_type: str, language: str) -> str:
    """Transcribe a recorded phrase using Gnani's official Vachana STT SDK.

    Browser WebM/Opus recordings are decoded and converted to mono 16 kHz WAV
    before being submitted to Gnani's documented file-transcription API.
    """
    if not audio_bytes:
        raise RuntimeError("The recorded audio is empty.")

    key = _required("GNANI_API_KEY")

    try:
        import av
        from gnani.stt import GnaniSTTClient
    except ImportError as exc:
        raise RuntimeError(
            "Gnani STT dependencies are missing. Install backend requirements."
        ) from exc

    lang = (language or "en").lower()

    if lang.startswith("hi"):
        locale = os.getenv("GNANI_STT_LANGUAGE_HI", "hi-IN").strip() or "hi-IN"
    elif lang.startswith("en"):
        locale = os.getenv("GNANI_STT_LANGUAGE_EN", "en-IN").strip() or "en-IN"
    else:
        locale = language if "-" in language else "en-IN"

    def convert_to_wav() -> bytes:
        try:
            with av.open(io.BytesIO(audio_bytes)) as container:
                audio_stream = next(
                    (stream for stream in container.streams if stream.type == "audio"),
                    None
                )

                if audio_stream is None:
                    raise RuntimeError("No audio stream was found in the recording.")

                resampler = av.AudioResampler(
                    format="s16",
                    layout="mono",
                    rate=16000
                )

                pcm = bytearray()

                for frame in container.decode(audio_stream):
                    for converted in resampler.resample(frame):
                        pcm.extend(converted.to_ndarray().tobytes())

                for converted in resampler.resample(None):
                    pcm.extend(converted.to_ndarray().tobytes())

                if not pcm:
                    raise RuntimeError("The recording contains no decodable audio.")

                output = io.BytesIO()
                with wave.open(output, "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(16000)
                    wav_file.writeframes(bytes(pcm))

                return output.getvalue()

        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Could not decode the recording ({type(exc).__name__}). "
                "Try recording again."
            ) from exc

    wav_bytes = await asyncio.to_thread(convert_to_wav)

    def call_gnani():
        client = GnaniSTTClient(api_key=key)
        return client.transcribe_bytes(
            wav_bytes,
            filename="bhashacall-phrase.wav",
            language_code=locale
        )

    try:
        result = await asyncio.to_thread(call_gnani)

    except Exception as exc:
        status = getattr(exc, "status_code", None)

        if status in (401, 403):
            raise RuntimeError(
                "Gnani STT authentication failed. Verify the API key and STT access."
            ) from exc

        if status == 429:
            raise RuntimeError(
                "Gnani STT rate limit reached. Please retry later."
            ) from exc

        raise RuntimeError(
            f"Gnani STT request failed ({type(exc).__name__}). "
            "Check your API access and network connection."
        ) from exc

    transcript = result.get("transcript") if isinstance(result, dict) else None

    if not isinstance(transcript, str) or not transcript.strip():
        raise RuntimeError("Gnani STT returned an empty transcript.")

    return transcript.strip()


async def synthesize_with_gnani(text: str, language: str) -> tuple[bytes, str]:
    """Call Gnani Timbre over its documented WebSocket API and collect audio events."""
    url = _required('GNANI_TTS_URL')
    key = _required('GNANI_API_KEY')
    if not url.lower().startswith(('wss://', 'ws://')):
        raise RuntimeError('GNANI_TTS_URL must be GnaniÃ¢â‚¬â„¢s WebSocket URL (wss://...), not an HTTP REST URL.')

    header_name = os.getenv('GNANI_AUTH_HEADER', 'X-API-Key-ID').strip() or 'X-API-Key-ID'
    scheme = os.getenv('GNANI_AUTH_SCHEME', '').strip()
    header_value = f'{scheme} {key}'.strip() if scheme else key
    headers = {header_name: header_value, 'Content-Type': 'application/json'}

    lang = (language or 'en').lower()
    if lang.startswith('hi'):
        gnani_language = os.getenv('GNANI_TTS_LANGUAGE_HI', 'hi-IN').strip()
        voice = os.getenv('GNANI_TTS_VOICE_HI', '').strip() or os.getenv('GNANI_TTS_VOICE', '').strip()
    elif lang.startswith('en'):
        gnani_language = os.getenv('GNANI_TTS_LANGUAGE_EN', 'en-IN').strip()
        voice = os.getenv('GNANI_TTS_VOICE_EN', '').strip() or os.getenv('GNANI_TTS_VOICE', '').strip()
    else:
        gnani_language = os.getenv('GNANI_TTS_LANGUAGE', language).strip()
        voice = os.getenv('GNANI_TTS_VOICE', '').strip()
    if not voice:
        raise RuntimeError('Gnani TTS voice is not configured. Set GNANI_TTS_VOICE (or GNANI_TTS_VOICE_HI / GNANI_TTS_VOICE_EN) in backend/.env to a voice enabled for your account.')

    request = {
        'text': text,
        'voice': voice,
        'model': os.getenv('GNANI_TTS_MODEL', 'timbre-v2.5').strip() or 'timbre-v2.5',
        'language': gnani_language,
        'speed': float(os.getenv('GNANI_TTS_SPEED', '1.0')),
        'audio_config': {
            'sample_rate': int(os.getenv('GNANI_TTS_SAMPLE_RATE', '48000')),
            'num_channels': 1,
            'sample_width': 2,
            'encoding': 'linear_pcm',
            'container': 'wav'
        }
    }

    audio_parts: list[bytes] = []
    completed = False
    try:
        # websockets 14+ uses additional_headers.
        connection = websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=20,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=None,
            max_size=8 * 1024 * 1024,
        )
        async with connection as ws:
            await ws.send(json.dumps(request))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=float(os.getenv('GNANI_TTS_TIMEOUT', '90')))
                if isinstance(raw, bytes):
                    audio_parts.append(raw)
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get('event') or event.get('type') or '').lower()
                data = event.get('data') or {}
                if not isinstance(data, dict):
                    data = {}

                # Log event metadata only; never log credentials or audio content.
                print(
                    f"[Gnani TTS] event={event_type or 'unknown'} "
                    f"keys={list(event.keys())}"
                )

                if event_type == 'audio':
                    encoded = data.get('audio') or event.get('audio')
                    if encoded:
                        try:
                            audio_parts.append(base64.b64decode(encoded, validate=True))
                        except Exception as exc:
                            raise RuntimeError('Gnani returned invalid base64 audio data.') from exc

                    # Some streaming APIs mark the final audio chunk itself.
                    if event.get('is_final') is True or data.get('is_final') is True:
                        completed = True
                        print("[Gnani TTS] Final audio event received.")
                        break

                elif event_type in ('complete', 'completed', 'done'):
                    completed = True
                    print("[Gnani TTS] Completion event received.")
                    break

                elif event_type == 'error' or event.get('error'):
                    detail = event.get('message') or event.get('error') or data or 'Unknown provider error'
                    detail_text = str(detail)
                    if 'auth' in detail_text.lower() or '401' in detail_text or '403' in detail_text:
                        raise RuntimeError('Gnani rejected the WebSocket TTS authentication. Confirm this API key has Timbre TTS access and that GNANI_AUTH_HEADER is X-API-Key-ID.')
                    raise RuntimeError(f'Gnani TTS error: {detail_text[:500]}')
    except RuntimeError:
        raise
    except Exception as exc:
        name = type(exc).__name__
        detail = str(exc)
        if 'InvalidStatus' in name or 'InvalidHandshake' in name:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (401, 403) or '401' in detail or '403' in detail:
                raise RuntimeError('Gnani rejected the WebSocket TTS handshake (401/403). Verify the keyÃ¢â‚¬â„¢s TTS entitlement and X-API-Key-ID header.') from exc
            raise RuntimeError(f'Gnani TTS WebSocket handshake failed: {detail[:350]}') from exc
        if isinstance(exc, asyncio.TimeoutError):
            raise RuntimeError('Gnani TTS timed out before completing audio generation.') from exc
        raise RuntimeError(f'Gnani TTS WebSocket connection failed ({name}): {detail[:350]}') from exc

    if not completed:
        raise RuntimeError('Gnani TTS connection closed before the completion event.')
    if not audio_parts:
        raise RuntimeError('Gnani TTS completed but returned no audio data.')

    # Gnani may send a single complete WAV or multiple PCM/WAV chunks.
    if len(audio_parts) == 1 and audio_parts[0].startswith(b'RIFF'):
        return audio_parts[0], 'audio/wav'
    pcm_parts: list[bytes] = []
    for part in audio_parts:
        if part.startswith(b'RIFF'):
            try:
                with wave.open(io.BytesIO(part), 'rb') as wav:
                    pcm_parts.append(wav.readframes(wav.getnframes()))
            except wave.Error as exc:
                raise RuntimeError('Gnani returned an invalid WAV audio chunk.') from exc
        else:
            pcm_parts.append(part)
    pcm = b''.join(pcm_parts)
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(int(os.getenv('GNANI_TTS_SAMPLE_RATE', '48000')))
        wav.writeframes(pcm)
    return output.getvalue(), 'audio/wav'


async def translate_text(text: str, source_language: str, target_language: str) -> str:
    """Translation adapter remains independent from speech recognition/synthesis."""
    endpoint = os.getenv('TRANSLATION_API_URL', '').strip()
    if not endpoint: raise RuntimeError('Configure TRANSLATION_API_URL in backend/.env.')
    source = _lang(source_language); target = _lang(target_language)
    api_key = os.getenv('TRANSLATION_API_KEY', '').strip()
    async with httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10)) as client:
        if endpoint.rstrip('/').endswith('/get') or 'mymemory.translated.net' in endpoint:
            try:
                response = await client.get(endpoint, params={'q': text, 'langpair': f'{source}|{target}'})
                response.raise_for_status()
            except httpx.ConnectError as exc:
                host = urlparse(endpoint).hostname or 'unknown host'
                raise RuntimeError(f'Cannot connect to translation provider ({host}). DNS/network lookup failed. Check internet/DNS and TRANSLATION_API_URL in backend/.env.') from exc
            payload = response.json()
            translated = str(payload.get('responseData', {}).get('translatedText', '')).strip()
        else:
            body = {'q': text, 'source': source, 'target': target, 'format': 'text'}
            if api_key: body['api_key'] = api_key
            response = await client.post(endpoint, json=body); response.raise_for_status()
            translated = str(response.json().get('translatedText', '')).strip()
    if not translated: raise RuntimeError('Translation provider returned empty text.')
    return translated


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
    'kyu': 'à¤•à¥à¤¯à¥‹à¤‚', 'kyon': 'à¤•à¥à¤¯à¥‹à¤‚', 'kyunki': 'à¤•à¥à¤¯à¥‹à¤‚à¤•à¤¿', 'tumhe': 'à¤¤à¥à¤®à¥à¤¹à¥‡à¤‚',
    'tumhein': 'à¤¤à¥à¤®à¥à¤¹à¥‡à¤‚', 'aesa': 'à¤à¤¸à¤¾', 'aisa': 'à¤à¤¸à¤¾', 'aesi': 'à¤à¤¸à¥€',
    'aese': 'à¤à¤¸à¥‡', 'hai': 'à¤¹à¥ˆ', 'hain': 'à¤¹à¥ˆà¤‚', 'lagta': 'à¤²à¤—à¤¤à¤¾', 'lagti': 'à¤²à¤—à¤¤à¥€',
    'lagte': 'à¤²à¤—à¤¤à¥‡', 'mujhe': 'à¤®à¥à¤à¥‡', 'kya': 'à¤•à¥à¤¯à¤¾', 'kaise': 'à¤•à¥ˆà¤¸à¥‡',
    'kaisa': 'à¤•à¥ˆà¤¸à¤¾', 'kaisi': 'à¤•à¥ˆà¤¸à¥€', 'nahi': 'à¤¨à¤¹à¥€à¤‚', 'nahin': 'à¤¨à¤¹à¥€à¤‚',
    'tum': 'à¤¤à¥à¤®', 'tumko': 'à¤¤à¥à¤®à¤•à¥‹', 'aap': 'à¤†à¤ª', 'ho': 'à¤¹à¥‹', 'haan': 'à¤¹à¤¾à¤',
    'batao': 'à¤¬à¤¤à¤¾à¤“', 'bolo': 'à¤¬à¥‹à¤²à¥‹', 'mera': 'à¤®à¥‡à¤°à¤¾', 'meri': 'à¤®à¥‡à¤°à¥€',
    'mere': 'à¤®à¥‡à¤°à¥‡', 'accha': 'à¤…à¤šà¥à¤›à¤¾', 'achha': 'à¤…à¤šà¥à¤›à¤¾', 'acha': 'à¤…à¤šà¥à¤›à¤¾',
    'kyun': 'à¤•à¥à¤¯à¥‹à¤‚', 'kuch': 'à¤•à¥à¤›', 'bahut': 'à¤¬à¤¹à¥à¤¤', 'bohot': 'à¤¬à¤¹à¥à¤¤',
    'abhi': 'à¤…à¤­à¥€', 'kaun': 'à¤•à¥Œà¤¨', 'kab': 'à¤•à¤¬', 'kahan': 'à¤•à¤¹à¤¾à¤',
    'yahan': 'à¤¯à¤¹à¤¾à¤', 'wahan': 'à¤µà¤¹à¤¾à¤', 'samajh': 'à¤¸à¤®à¤', 'pata': 'à¤ªà¤¤à¤¾',
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


async def process_phrase_text(audio_bytes: bytes, mime_type: str, source_language: str, target_language: str) -> tuple[str, str]:
    if not audio_bytes: raise RuntimeError('The recorded phrase was empty. Please record again.')
    if len(audio_bytes) > 8 * 1024 * 1024: raise RuntimeError('Phrase is too large (8 MB maximum).')
    original = await transcribe_with_gnani(audio_bytes, mime_type, source_language)
    translated = await translate_text(original, source_language, target_language)
    return original, translated


async def synthesize_speech(text: str, language: str) -> tuple[bytes, str]:
    return await synthesize_with_gnani(text, language)
