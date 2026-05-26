"""Session manager – Telethon clients per session file."""
import os
from telethon import TelegramClient
from config import API_ID, API_HASH, SESSIONS_DIR


def list_sessions():
    return sorted(
        f[:-len(".session")]
        for f in os.listdir(SESSIONS_DIR)
        if f.endswith(".session")
    )


def session_path(name: str) -> str:
    return os.path.join(SESSIONS_DIR, name)


def make_client(name: str) -> TelegramClient:
    return TelegramClient(session_path(name), API_ID, API_HASH)


def delete_session(name: str):
    for ext in (".session", ".session-journal"):
        p = session_path(name) + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
