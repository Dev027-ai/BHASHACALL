from __future__ import annotations
import os, sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.getenv('DATABASE_PATH', str(ROOT / 'bhashacall.db')))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

@contextmanager
def connect():
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS rooms (code TEXT PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP, source_language TEXT NOT NULL, target_language TEXT NOT NULL, owner_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS room_members (room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE, participant_id TEXT NOT NULL, joined_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(room_code, participant_id));
        CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, room_code TEXT, conversation_id TEXT, sender_id TEXT NOT NULL, original_text TEXT NOT NULL, translated_text TEXT NOT NULL, source_language TEXT NOT NULL, target_language TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_code, created_at);
        CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, original_name TEXT NOT NULL, original_path TEXT NOT NULL, translated_path TEXT, source_language TEXT, target_language TEXT, status TEXT NOT NULL DEFAULT 'uploaded', extracted_text TEXT, translated_text TEXT, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS document_shares (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, room_code TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE, shared_by TEXT NOT NULL, include_original INTEGER NOT NULL DEFAULT 1, include_translation INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(document_id, room_code));
        CREATE TABLE IF NOT EXISTS audio_cache (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, chunk_index INTEGER NOT NULL, language TEXT NOT NULL, voice_key TEXT NOT NULL, file_path TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(document_id, chunk_index, language, voice_key));
        ''')
