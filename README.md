# BhashaCall — Gnani + Document OCR update

This update removes ElevenLabs from the speech execution path and makes Gnani the only speech adapter. It adds selectable-text PDF extraction plus a scanned-PDF OCR fallback, and fixes the standalone Messages translation handler.

## Important Gnani integration note

Gnani API endpoints and payload/auth contracts can differ by product, tenant and enabled service. This project deliberately does **not** invent a universal Gnani URL. Obtain the STT and TTS endpoint URLs, authentication format, request fields and response schema from your Gnani integration contact/portal. Configure those values in `backend/.env` using `backend/.env.example` as a template. Defaults are `Authorization: Bearer <key>`, multipart STT fields `file` + `language`, and JSON TTS fields `text` + `language`; each can be mapped through environment variables. If your tenant uses a different contract, adjust the mappings or the isolated adapter in `backend/app/services/pipeline.py`.

The browser never receives the Gnani key. Do not put it in a `VITE_*` variable, frontend source, screenshots or Git commits. Translation remains a separate configurable service; speech recognition and speech synthesis are Gnani-only.

## Install backend dependencies

From `backend` in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Enable OCR for scanned PDFs (Windows)

The Python packages install the PDF renderer and Tesseract wrapper, but **Tesseract itself is a separate Windows application**. Install Tesseract OCR (for example, the UB Mannheim Windows build) and include the `English` (`eng`) and `Hindi` (`hin`) language data during installation. If it is not in PATH, set `TESSERACT_CMD` in `backend/.env` to the full `tesseract.exe` path. Configure `OCR_LANGUAGES=eng+hin`. The API reports readiness at `GET /api/documents/ocr-capabilities`.

Selectable PDF text is extracted first. When a PDF has no useful text layer, the backend renders pages with PyMuPDF and OCRs them individually. OCR is bounded by `OCR_MAX_PAGES` and `OCR_DPI`; review OCR output for tables, low-resolution scans, handwriting and complex layouts. This is not handwriting recognition or a layout-perfect PDF reconstruction system.

## Configure backend/.env

Copy the Gnani/OCR values from `backend/.env.example` into your existing `backend/.env`. Preserve your database/storage settings and any other unrelated configuration. Remove old `ELEVENLABS_*` settings and `SPEECH_PROVIDER` from the local file; the new code does not read them.

At minimum, configure:

```dotenv
GNANI_API_KEY=your_private_key
GNANI_STT_URL=https://the-exact-stt-url-from-your-gnani-tenant
GNANI_TTS_URL=https://the-exact-tts-url-from-your-gnani-tenant
GNANI_AUTH_HEADER=Authorization
GNANI_AUTH_SCHEME=Bearer
TRANSLATION_API_URL=https://api.mymemory.translated.net/get
OCR_LANGUAGES=eng+hin
```

Do not use the example endpoint strings literally. They are explanatory placeholders.

## Run

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

Then check `http://127.0.0.1:8000/docs`, `GET /api/health`, and `GET /api/documents/ocr-capabilities`. Start the frontend separately with `npm run dev` from `frontend`.

## Scope and known limits

- PDF, DOCX and TXT uploads are supported; scanned PDF OCR uses installed Tesseract language packs.
- OCR is local; document contents are not sent to Gnani for OCR.
- Translation is separate from Gnani speech and uses the configured translation endpoint.
- Speech rooms deliver captions after STT + translation; TTS is an optional follow-on operation and cannot block caption creation.
- The adapter is ready to be mapped to your tenant's Gnani API contract, but it is not possible to certify live Gnani connectivity without your actual tenant endpoints, key and API documentation.
- Authentication/authorization still needs a production-grade user identity layer before exposing private documents on the public internet. Current owner IDs are client-supplied identifiers and are suitable only for a local prototype.

## Live room calling update

Voice rooms now include a phone-style live audio call using browser WebRTC peer-to-peer audio. Room WebSockets are used only for WebRTC offer/answer/ICE signaling and translated caption events; audio is sent directly between browsers. Participants should join the same room, then either participant can start a live call. New participants joining while a call is active are offered a peer connection.

Live caption capture currently uses the browser Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`) in continuous mode, then sends finalized speech text to the backend translation adapter. Use a current Chrome or Edge browser and allow microphone access. This is a browser capability, not Gnani streaming STT. The existing Gnani STT adapter remains phrase-upload based until the user's specific Gnani tenant provides its documented streaming WebSocket/SDK endpoint and protocol. Do not treat this as a verified Gnani live-stream integration.

WebRTC currently uses public STUN servers for connectivity discovery. Some restrictive corporate/mobile networks require a TURN relay; configure a trusted TURN service before relying on calls across all networks. Use HTTPS in deployed environments (localhost is permitted for development). Current room participant authorization is prototype-level and must be hardened before public production deployment.


## Room messaging (submission build)

Room chat uses the existing room WebSocket (`/ws/{room_code}/{participant_id}`) and SQLite `messages` table. Messages are persisted before being broadcast to every connected participant in the room. The room history endpoint returns the latest 200 messages in chronological order. The frontend shows an optimistic “Sending…” bubble, replaces it when the server broadcasts the saved message, and surfaces send/translation errors. If the translation provider is unavailable, the original message is still delivered and saved; the UI labels the translation as unavailable rather than dropping the message.

Quick manual verification: open the same room in two separate browser profiles/devices, send a Hindi message from one and confirm both clients display it; reverse the direction; refresh one client and confirm history reloads. Temporarily disable the translation endpoint to confirm original text still arrives with a warning.

## Incoming-call / pick-up flow (2026-11-09 update)

The room call flow now follows a phone-style invitation:

1. Caller grants microphone permission and sends a room call invitation.
2. Other connected room participants see an incoming-call sheet with **Accept** and **Decline**.
3. The caller begins WebRTC offer/answer negotiation only after a participant accepts.
4. Microphone tracks are attached to existing peer connections, audio receive transceivers are reserved, and early ICE candidates are queued until the remote description is set.
5. Decline, hang-up and peer departure update the other side and clean up call media.

This update fixes application-level call signaling and negotiation issues found in the previous build. It has been syntax-checked on the backend, but a real two-browser microphone test is still required on the user's machine. This project still uses STUN only; networks that block direct peer-to-peer paths need a configured TURN relay. For a same-computer Edge + Brave test, allow microphone access in both browsers and use headphones to avoid acoustic feedback. If a noisy `shhh` remains, test each browser's selected microphone and OS input level separately.

The incoming-call event is for connected participants in the room. Calls are not silently auto-answered. The caller can hang up while ringing to cancel the attempt.
