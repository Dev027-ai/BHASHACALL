from __future__ import annotations
import asyncio, base64, os, secrets, time, uuid, re, hashlib
from pathlib import Path
from typing import Any
from dotenv import load_dotenv
BASE = Path(__file__).resolve().parents[1]
load_dotenv(BASE / '.env')
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, UploadFile, File, Form, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.database.db import connect, init_db
from app.services.pipeline import process_phrase_text, synthesize_speech, translate_text, translate_chat_message
from app.services.documents.processing import extract_text, export_docx, chunks
from app.services.documents.translate import translate_document, split_translation_chunks

app = FastAPI(title='BhashaCall API', version='1.0.0')
origins = [o.strip() for o in os.getenv('CORS_ORIGINS', 'http://localhost:5173,http://127.0.0.1:5173').split(',') if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
ROOMS: dict[str, dict[str, Any]] = {}
STORAGE = Path(os.getenv('DOCUMENT_STORAGE_PATH', str(BASE / 'storage' / 'documents'))).resolve()
AUDIO_STORAGE = Path(os.getenv('AUDIO_STORAGE_PATH', str(BASE / 'storage' / 'audio'))).resolve()
STORAGE.mkdir(parents=True, exist_ok=True); AUDIO_STORAGE.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD = int(os.getenv('MAX_UPLOAD_BYTES', str(50 * 1024 * 1024)))
ALLOWED = {'.pdf', '.docx', '.txt'}

@app.on_event('startup')
def startup(): init_db()

class RoomCreate(BaseModel):
    source_language: str = 'hi'
    target_language: str = 'en'
    participant_id: str | None = None

class TextMessage(BaseModel):
    text: str
    source_language: str = 'hi'
    target_language: str = 'en'
    sender_id: str = 'guest'
    room_code: str | None = None

class ShareRequest(BaseModel):
    room_code: str
    include_original: bool = True
    include_translation: bool = True
    owner_id: str

class TranslateRequest(BaseModel):
    source_language: str = 'auto'
    target_language: str = 'en'

class ListenRequest(BaseModel):
    text: str
    language: str
    chunk_index: int = 0

@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'bhashacall-api', 'version': '1.0.0', 'time': int(time.time()), 'speech_provider': 'gnani', 'gnani_api_key_configured': bool(os.getenv('GNANI_API_KEY', '').strip()), 'gnani_stt_endpoint_configured': bool(os.getenv('GNANI_STT_URL', '').strip()), 'gnani_tts_endpoint_configured': bool(os.getenv('GNANI_TTS_URL', '').strip()), 'translation_endpoint_configured': bool(os.getenv('TRANSLATION_API_URL', '').strip()), 'document_formats': sorted(ALLOWED), 'persistent_storage': 'sqlite'}

@app.post('/api/rooms')
def create_room(payload: RoomCreate):
    code = secrets.token_urlsafe(5).replace('-', '').replace('_', '')[:7].upper()
    owner = payload.participant_id or str(uuid.uuid4())
    with connect() as db:
        db.execute('INSERT INTO rooms(code,source_language,target_language,owner_id) VALUES(?,?,?,?)', (code,payload.source_language,payload.target_language,owner))
    ROOMS[code] = {'participants': {}, 'source_language': payload.source_language, 'target_language': payload.target_language}
    return {'room_code': code, 'source_language': payload.source_language, 'target_language': payload.target_language, 'owner_id': owner}

@app.get('/api/rooms/{room_code}')
def room_info(room_code: str):
    code=room_code.upper()
    with connect() as db: row=db.execute('SELECT * FROM rooms WHERE code=?',(code,)).fetchone()
    if not row: raise HTTPException(404,'Room not found. Create a new room.')
    return {'room_code':code,'participants':len(ROOMS.get(code,{}).get('participants',{})),'source_language':row['source_language'],'target_language':row['target_language']}

@app.websocket('/ws/{room_code}/{participant_id}')
async def room_socket(ws: WebSocket, room_code: str, participant_id: str):
    code=room_code.upper()
    with connect() as db: room_row=db.execute('SELECT * FROM rooms WHERE code=?',(code,)).fetchone()
    if not room_row: await ws.close(code=1008,reason='Room not found'); return
    await ws.accept()
    room=ROOMS.setdefault(code,{'participants':{},'source_language':room_row['source_language'],'target_language':room_row['target_language']})
    room['participants'][participant_id]=ws
    with connect() as db: db.execute('INSERT OR IGNORE INTO room_members(room_code,participant_id) VALUES(?,?)',(code,participant_id))
    await broadcast(room,{'type':'system','text':f'Participant joined. {len(room["participants"])} connected.'},exclude=participant_id)
    await broadcast(room,{'type':'rtc.peer-ready','from':participant_id},exclude=participant_id)
    try:
        await ws.send_json({'type':'system','text':'Connected. Captions are delivered before optional translated speech.'})
        while True:
            msg=await ws.receive_json(); kind=msg.get('type')
            if kind=='phrase':
                phrase_id=msg.get('phrase_id') or str(uuid.uuid4())
                try:
                    audio=base64.b64decode(msg.get('audio_base64',''),validate=True)
                    source=str(msg.get('source_language','hi')); target=str(msg.get('target_language','en'))
                    original,translated=await process_phrase_text(audio,str(msg.get('mime_type','audio/webm')),source,target)
                    await broadcast(room,{'type':'caption','from':participant_id,'phrase_id':phrase_id,'original':original,'translated':translated,'source_language':source,'target_language':target})
                    await ws.send_json({'type':'phrase_status','phrase_id':phrase_id,'text':'Captions delivered. Preparing optional translated audio…'})
                    asyncio.create_task(send_speech(room,participant_id,phrase_id,translated,target))
                except Exception as exc:
                    await ws.send_json({'type':'processing_error','phrase_id':phrase_id,'text':safe_error(exc)})
            elif kind=='rtc.call-start':
                # Ring every other participant; do not negotiate until the recipient accepts.
                invite_id = str(msg.get('call_id') or uuid.uuid4())
                await broadcast(room, {'type':'rtc.call-invite','from':participant_id,'call_id':invite_id}, exclude=participant_id)
            elif kind=='rtc.call-accept':
                caller = str(msg.get('to') or '')
                if caller in room['participants']:
                    try: await room['participants'][caller].send_json({'type':'rtc.call-accepted','from':participant_id,'call_id':msg.get('call_id')})
                    except Exception: pass
            elif kind=='rtc.call-reject':
                caller = str(msg.get('to') or '')
                if caller in room['participants']:
                    try: await room['participants'][caller].send_json({'type':'rtc.call-rejected','from':participant_id,'call_id':msg.get('call_id')})
                    except Exception: pass
            elif kind=='rtc.call-end':
                await broadcast(room,{'type':'rtc.call-ended','from':participant_id},exclude=participant_id)
            elif kind=='rtc.signal':
                target=msg.get('to'); payload=msg.get('payload') or {}
                if target in room['participants']:
                    try: await room['participants'][target].send_json({'type':'rtc.signal','from':participant_id,'payload':payload})
                    except Exception: pass
            elif kind=='caption.text':
                text=str(msg.get('text','')).strip()
                if not text or len(text)>1500: continue
                source=str(msg.get('source_language','hi')); target=str(msg.get('target_language','en'))
                try:
                    translated=await translate_text(text,source,target)
                    phrase_id=str(uuid.uuid4())
                    await broadcast(room,{'type':'caption.live','from':participant_id,'phrase_id':phrase_id,'original':text,'translated':translated,'source_language':source,'target_language':target})
                    # Send translated TTS to the other participant only; never relay raw mic audio.
                    asyncio.create_task(send_speech(room,participant_id,phrase_id,translated,target))
                except Exception as exc:
                    await ws.send_json({'type':'caption.error','text':safe_error(exc)})
            elif kind=='chat.message':
                text=str(msg.get('text','')).strip()
                client_message_id=str(msg.get('client_message_id') or '')[:80]
                if not text or len(text)>20000:
                    await ws.send_json({'type':'chat.error','client_message_id':client_message_id,'text':'Message must contain 1–20000 characters.'})
                    continue
                fallback_source=str(msg.get('source_language','en')); fallback_target=str(msg.get('target_language','hi'))
                source=fallback_source; target=fallback_target
                translation_error=None
                try:
                    translated, source, target = await translate_chat_message(text, fallback_source, fallback_target)
                except Exception as exc:
                    # Never lose a user's chat message because translation/transliteration is unavailable.
                    translated=text
                    translation_error=safe_error(exc)
                message_id=str(uuid.uuid4())
                with connect() as db:
                    db.execute('INSERT INTO messages(id,room_code,sender_id,original_text,translated_text,source_language,target_language) VALUES(?,?,?,?,?,?,?)',(message_id,code,participant_id,text,translated,source,target))
                    saved=db.execute('SELECT created_at FROM messages WHERE id=?',(message_id,)).fetchone()
                item={'id':message_id,'client_message_id':client_message_id,'room_code':code,'sender_id':participant_id,'original_text':text,'translated_text':translated,'source_language':source,'target_language':target,'created_at':saved['created_at'] if saved else None,'translation_error':translation_error}
                await broadcast(room,{'type':'chat.message',**item})
            elif kind=='document.page':
                await broadcast(room,{'type':'document.page_changed','document_id':msg.get('document_id'),'page':int(msg.get('page',1)),'from':participant_id},exclude=participant_id)
            elif kind=='ping': await ws.send_json({'type':'pong'})
    except WebSocketDisconnect: pass
    finally:
        room['participants'].pop(participant_id,None)
        await broadcast(room,{'type':'rtc.peer-left','from':participant_id},exclude=participant_id)
        with connect() as db: db.execute('DELETE FROM room_members WHERE room_code=? AND participant_id=?',(code,participant_id))
        await broadcast(room,{'type':'system','text':f'A participant left. {len(room["participants"])} connected.'})

async def send_speech(room, sender, phrase_id, text, language):
    try:
        audio,mime=await synthesize_speech(text,language)
        payload={'type':'speech.audio_ready','from':sender,'phrase_id':phrase_id,'audio_base64':base64.b64encode(audio).decode('ascii'),'mime_type':mime}
        await broadcast(room,payload,exclude=sender)
    except Exception as exc:
        if sender in room['participants']:
            try: await room['participants'][sender].send_json({'type':'speech.error','phrase_id':phrase_id,'text':safe_error(exc)})
            except Exception: pass

async def broadcast(room,payload,exclude=None):
    dead=[]
    for pid,client in list(room['participants'].items()):
        if pid==exclude: continue
        try: await client.send_json(payload)
        except Exception: dead.append(pid)
    for pid in dead: room['participants'].pop(pid,None)

def safe_error(exc):
    message=str(exc)
    if any(x in message.lower() for x in ['token','api key','authorization','credential']): return 'AI provider authentication failed. Check backend credentials and restart the server.'
    return message[:350]

def get_document(doc_id, owner_id=None, room_code=None):
    with connect() as db:
        row=db.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
        if not row: raise HTTPException(404,'Document not found.')
        if owner_id and row['owner_id']!=owner_id: raise HTTPException(403,'You do not have access to this private document.')
        if room_code:
            share=db.execute('SELECT * FROM document_shares WHERE document_id=? AND room_code=?',(doc_id,room_code.upper())).fetchone()
            if not share: raise HTTPException(403,'This document has not been shared with that room.')
        return dict(row)

@app.get('/api/documents/ocr-capabilities')
def ocr_capabilities():
    try:
        import fitz, pytesseract
        available=True; error=None
        try: version=pytesseract.get_tesseract_version().__str__()
        except Exception as exc: available=False; version=None; error='Tesseract executable or language data is not installed.'
    except ImportError:
        available=False; version=None; error='Install pymupdf, pytesseract and pillow in the backend virtual environment.'
    return {'available':available,'engine':'Tesseract OCR','version':version,'languages':os.getenv('OCR_LANGUAGES','eng+hin'),'error':error}

@app.post('/api/documents/upload')
async def upload_document(file: UploadFile=File(...), owner_id: str=Form('guest')):
    name=Path(file.filename or 'document').name
    ext=Path(name).suffix.lower()
    if ext not in ALLOWED: raise HTTPException(415,'Only PDF, DOCX and TXT files are supported in this release.')
    content=await file.read(MAX_UPLOAD+1)
    if not content: raise HTTPException(400,'The uploaded file is empty.')
    if len(content)>MAX_UPLOAD: raise HTTPException(413,f'File exceeds the {MAX_UPLOAD//(1024*1024)} MB upload limit.')
    if ext=='.pdf' and not content.startswith(b'%PDF'): raise HTTPException(400,'The file does not appear to be a valid PDF.')
    if ext=='.docx' and not content.startswith(b'PK'): raise HTTPException(400,'The file does not appear to be a valid DOCX.')
    doc_id=str(uuid.uuid4()); folder=STORAGE/doc_id; folder.mkdir(parents=True,exist_ok=True)
    safe_name=re.sub(r'[^A-Za-z0-9._-]','_',name)[:120]
    path=folder/f'original_{safe_name}'; path.write_bytes(content)
    try: extracted, extraction_method=extract_text(path,name,ocr=True)
    except Exception as exc:
        path.unlink(missing_ok=True); folder.rmdir(); raise HTTPException(422,str(exc))
    with connect() as db: db.execute('INSERT INTO documents(id,owner_id,original_name,original_path,extracted_text,status) VALUES(?,?,?,?,?,?)',(doc_id,owner_id,name,str(path),extracted,'uploaded'))
    return {'id':doc_id,'filename':name,'status':'uploaded','characters':len(extracted),'preview':extracted[:5000],'extraction_method':extraction_method}

@app.get('/api/documents')
def list_documents(owner_id: str=Query('guest')):
    with connect() as db: rows=db.execute('SELECT id,original_name,source_language,target_language,status,error,created_at,updated_at FROM documents WHERE owner_id=? ORDER BY created_at DESC',(owner_id,)).fetchall()
    return {'documents':[dict(r) for r in rows]}

@app.get('/api/documents/{doc_id}')
def document_detail(doc_id: str, owner_id: str=Query('guest'), room_code: str|None=Query(None)):
    row=get_document(doc_id,owner_id=owner_id if not room_code else None,room_code=room_code)
    original_text=row['extracted_text']; translated_text=row['translated_text']
    if room_code:
        with connect() as db: share=db.execute('SELECT include_original,include_translation FROM document_shares WHERE document_id=? AND room_code=?',(doc_id,room_code.upper())).fetchone()
        if not share: raise HTTPException(403,'This document has not been shared with that room.')
        if not share['include_original']: original_text=None
        if not share['include_translation']: translated_text=None
    return {'id':row['id'],'filename':row['original_name'],'status':row['status'],'source_language':row['source_language'],'target_language':row['target_language'],'original_text':original_text,'translated_text':translated_text,'error':row['error']}

@app.post('/api/documents/{doc_id}/translate')
async def translate_uploaded_document(doc_id: str, payload: TranslateRequest, owner_id: str=Query('guest')):
    row=get_document(doc_id,owner_id=owner_id)
    source=payload.source_language
    if source=='auto': source='hi' if re.search(r'[\u0900-\u097f]',row['extracted_text']) else 'en'
    target=payload.target_language
    if source==target: raise HTTPException(400,'Choose a different target language.')
    with connect() as db: db.execute("UPDATE documents SET status='translating',source_language=?,target_language=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(source,target,doc_id))
    try:
        translated=await translate_document(row['extracted_text'],source,target)
        output=STORAGE/doc_id/'translated.docx'; export_docx(translated,output)
        with connect() as db: db.execute("UPDATE documents SET status='translated',translated_text=?,translated_path=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(translated,str(output),doc_id))
        return {'id':doc_id,'status':'translated','source_language':source,'target_language':target,'translated_text':translated,'translation_segments':len(split_translation_chunks(row['extracted_text'])),'download_url':f'/api/documents/{doc_id}/download?owner_id={owner_id}'}
    except Exception as exc:
        with connect() as db: db.execute("UPDATE documents SET status='error',error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(safe_error(exc),doc_id))
        raise HTTPException(502,safe_error(exc))

@app.get('/api/documents/{doc_id}/download')
def download_document(doc_id: str, owner_id: str=Query('guest'), room_code: str|None=Query(None)):
    row=get_document(doc_id,owner_id=owner_id if not room_code else None,room_code=room_code)
    path=row['translated_path']
    if room_code:
        with connect() as db: share=db.execute('SELECT include_translation FROM document_shares WHERE document_id=? AND room_code=?',(doc_id,room_code.upper())).fetchone()
        if not share or not share['include_translation']: raise HTTPException(403,'Translated version is not shared with this room.')
    if not path or not Path(path).is_file(): raise HTTPException(404,'Translated document is not ready.')
    return FileResponse(path,media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document',filename=f"{Path(row['original_name']).stem}_translated.docx")


@app.get('/api/documents/{doc_id}/original')
def download_original(doc_id: str, owner_id: str=Query('guest'), room_code: str|None=Query(None)):
    row=get_document(doc_id,owner_id=owner_id if not room_code else None,room_code=room_code)
    if room_code:
        with connect() as db: share=db.execute('SELECT include_original FROM document_shares WHERE document_id=? AND room_code=?',(doc_id,room_code.upper())).fetchone()
        if not share or not share['include_original']: raise HTTPException(403,'Original version is not shared with this room.')
    path=Path(row['original_path'])
    if not path.is_file(): raise HTTPException(404,'Original document is unavailable.')
    import mimetypes
    return FileResponse(path,media_type=mimetypes.guess_type(row['original_name'])[0] or 'application/octet-stream',filename=row['original_name'])

@app.post('/api/documents/{doc_id}/share')
async def share_document(doc_id: str, payload: ShareRequest):
    row=get_document(doc_id,owner_id=payload.owner_id)
    with connect() as db:
        room=db.execute('SELECT code FROM rooms WHERE code=?',(payload.room_code.upper(),)).fetchone()
        if not room: raise HTTPException(404,'Room not found.')
        share_id=str(uuid.uuid4())
        db.execute('INSERT INTO document_shares(id,document_id,room_code,shared_by,include_original,include_translation) VALUES(?,?,?,?,?,?) ON CONFLICT(document_id,room_code) DO UPDATE SET include_original=excluded.include_original,include_translation=excluded.include_translation,shared_by=excluded.shared_by',(share_id,doc_id,payload.room_code.upper(),payload.owner_id,int(payload.include_original),int(payload.include_translation)))
    room=ROOMS.get(payload.room_code.upper())
    if room: asyncio.create_task(broadcast(room,{'type':'document.shared','document_id':doc_id,'filename':row['original_name'],'include_original':payload.include_original,'include_translation':payload.include_translation}))
    return {'shared':True,'document_id':doc_id,'room_code':payload.room_code.upper()}

@app.delete('/api/documents/{doc_id}/share/{room_code}')
async def unshare_document(doc_id: str, room_code: str, owner_id: str=Query('guest')):
    get_document(doc_id,owner_id=owner_id)
    with connect() as db: db.execute('DELETE FROM document_shares WHERE document_id=? AND room_code=?',(doc_id,room_code.upper()))
    room=ROOMS.get(room_code.upper())
    if room: asyncio.create_task(broadcast(room,{'type':'document.unshared','document_id':doc_id}))
    return {'shared':False}

@app.get('/api/rooms/{room_code}/documents')
def room_documents(room_code: str):
    with connect() as db:
        rows=db.execute('SELECT d.id,d.original_name,d.status,d.source_language,d.target_language,s.include_original,s.include_translation FROM documents d JOIN document_shares s ON s.document_id=d.id WHERE s.room_code=?',(room_code.upper(),)).fetchall()
    return {'documents':[dict(r) for r in rows]}

@app.get('/api/rooms/{room_code}/messages')
def room_messages(room_code: str):
    code=room_code.upper()
    with connect() as db:
        room=db.execute('SELECT code FROM rooms WHERE code=?',(code,)).fetchone()
        if not room: raise HTTPException(404,'Room not found.')
        rows=db.execute('SELECT id,room_code,sender_id,original_text,translated_text,source_language,target_language,created_at FROM messages WHERE room_code=? ORDER BY created_at DESC, rowid DESC LIMIT 200',(code,)).fetchall()
    return {'messages':[dict(r) for r in reversed(rows)]}

@app.post('/api/messages/translate')
async def translate_standalone_message(payload: TextMessage):
    text=payload.text.strip()
    if not text or len(text)>20000: raise HTTPException(400,'Message must contain 1–20000 characters.')
    translated=await translate_text(text,payload.source_language,payload.target_language)
    return {'original_text':text,'translated_text':translated,'source_language':payload.source_language,'target_language':payload.target_language}

@app.post('/api/documents/{doc_id}/audio')
async def document_audio(doc_id: str, payload: ListenRequest, owner_id: str=Query('guest'), room_code: str|None=Query(None)):
    row=get_document(doc_id,owner_id=owner_id if not room_code else None,room_code=room_code)
    if room_code:
        with connect() as db: share=db.execute('SELECT include_translation FROM document_shares WHERE document_id=? AND room_code=?',(doc_id,room_code.upper())).fetchone()
        if not share or not share['include_translation']: raise HTTPException(403,'Translated version is not shared with this room.')
    if row['status']!='translated' or not row['translated_text']: raise HTTPException(409,'Translate the document before listening.')
    parts=chunks(row['translated_text'],850)
    if payload.chunk_index<0 or payload.chunk_index>=len(parts): raise HTTPException(404,'Audio paragraph not found.')
    text=parts[payload.chunk_index]
    cache_key=hashlib.sha256(f'{doc_id}:{payload.chunk_index}:{payload.language}:gnani:{text}'.encode()).hexdigest()
    cached=AUDIO_STORAGE/f'{cache_key}.mp3'
    try:
        if cached.is_file(): audio=cached.read_bytes(); mime='audio/mpeg'
        else:
            audio,mime=await synthesize_speech(text,payload.language)
            cached.write_bytes(audio)
    except Exception as exc: raise HTTPException(502,safe_error(exc))
    return {'audio_base64':base64.b64encode(audio).decode('ascii'),'mime_type':mime,'chunk_index':payload.chunk_index,'total_chunks':len(parts),'cached':cached.is_file()}
