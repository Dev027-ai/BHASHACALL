# BhashaCall backend

## Speech provider: Gnani

This build removes ElevenLabs calls and routes STT/TTS through the Gnani adapter in `app/services/pipeline.py`. Add `GNANI_API_KEY`, `GNANI_STT_URL`, and `GNANI_TTS_URL` to the private `backend/.env`. Endpoint URLs and payload contracts are tenant-specific: use the values and schema supplied by Gnani. The adapter supports configurable auth header/scheme and request/response field names. Never expose the key through Vite variables or browser code.

## PDF extraction and OCR

Selectable PDF text is extracted with pypdf. If the PDF has no usable text layer, pages are rendered with PyMuPDF and passed to local Tesseract OCR. Install Tesseract OCR on Windows separately, including `eng` and `hin` language data, then set `TESSERACT_CMD` if it is not in PATH. Python dependencies are in `requirements.txt`. Check `/api/documents/ocr-capabilities`.

## Run

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```
