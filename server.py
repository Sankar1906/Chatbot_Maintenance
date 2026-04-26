"""
server.py — Generic AI Diagnostic API Server
============================================
Handles Admin file uploads (Async), dynamic Subpart routing, 
Safety checklists, and Doctor-style diagnostic sessions.
"""

import asyncio
import base64, hashlib, json, os, re, sqlite3, uuid, shutil
from pathlib import Path
from datetime import datetime
import requests as _requests

# ✅ FIX 1: Added WebSocket and WebSocketDisconnect to imports
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from agent import DiagnosticAgent
import tools as T
from llm_client import LLMClient
from excel_analyzer import analyze_excel
from page_indexer import build_page_index
# At the very top, add this import
from contextlib import asynccontextmanager

# ── Replace this ──────────────────────────────────────────
# app = FastAPI(title="AI Diagnostic Platform - Generic")

# ── With this ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──────────────────────────────────────────
    import context_engine as _ce
    from sentence_transformers import SentenceTransformer

    def _preload():
        print("[STARTUP] Loading embedding model...")
        _ce._EMBEDDING_MODEL = SentenceTransformer('all-MiniLM-L6-v2')
        print("[STARTUP] Embedding model ready.")

        reg = T.get_registry()
        for line in reg.get("lines", []):
            for machine in line.get("machines", []):
                for subpart in machine.get("subparts", []):
                    try:
                        _ce.get_context_engine(
                            machine["machine_id"],
                            subpart["subpart_id"]
                        )
                        print(f"[STARTUP] Index ready: {machine['machine_id']}/{subpart['subpart_id']}")
                    except Exception as e:
                        print(f"[STARTUP] Skipped {subpart['subpart_id']}: {e}")

        print("[STARTUP] All indexes loaded. Server ready.")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _preload)

    yield  # ← server runs here

    # ── SHUTDOWN (optional cleanup) ───────────────────────
    print("[SHUTDOWN] Server stopping.")


app = FastAPI(title="AI Diagnostic Platform - Generic", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

DB_PATH      = "maintenance.db"
USERS_JSON   = Path("users.json")
DATA_DIR     = Path("data/machines")

# In-memory session tracking
agent_sessions: dict[str, DiagnosticAgent] = {}
# With this:
_TOKENS_FILE = Path("data/auth_tokens.json")

def _load_tokens():
    if _TOKENS_FILE.exists():
        try:
            return json.load(open(_TOKENS_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_tokens():
    _TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(auth_tokens, open(_TOKENS_FILE, "w"), indent=2)

auth_tokens: dict[str, dict] = _load_tokens()

# Ensure DB & Users
T.init_db()
if not USERS_JSON.exists():
    default_users = {
        "admin": {"user_id": "admin", "name": "Admin", "password": hashlib.sha256(b"admin123").hexdigest(), "role": "admin"},
        "op001": {"user_id": "op001", "name": "Operator 1", "password": hashlib.sha256(b"op001").hexdigest(), "role": "operator"}
    }
    json.dump(default_users, open(USERS_JSON, "w"), indent=2)

def _get_user(token: str) -> dict:
    user = auth_tokens.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    return user

# ══════════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/auth/login")
async def login(user_id: str = Form(...), password: str = Form(...)):
    users = json.load(open(USERS_JSON, encoding="utf-8"))
    user  = users.get(user_id.strip().lower())
    if not user or user["password"] != hashlib.sha256(password.encode()).hexdigest():
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = str(uuid.uuid4())
    auth_tokens[token] = {"user_id": user["user_id"], "name": user["name"], "role": user.get("role", "operator")}
    _save_tokens()  
    return {"token": token, "user_id": user["user_id"], "name": user["name"], "role": user.get("role", "operator")}
     
@app.post("/auth/logout")
async def logout(token: str = Form(...)):
    auth_tokens.pop(token, None)
    _save_tokens() 
    return {"status": "logged out"}

# ══════════════════════════════════════════════════════════════════════════════
# DIRECTORY & SAFETY ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/machines")
def get_machines():
    return T.get_registry()

@app.get("/machines/{machine_id}/subparts/{subpart_id}/safety")
def get_safety_checklist(machine_id: str, subpart_id: str):
    return T.load_safety_checklist(machine_id, subpart_id)

# ══════════════════════════════════════════════════════════════════════════════
# ADMIN DIRECTORY MANAGEMENT (CREATE / DELETE)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/admin/create")
async def admin_create(
    token: str = Form(...), type: str = Form(...), name: str = Form(...), parent_id: str = Form(None)
):
    user = _get_user(token)
    if user["role"] != "admin": raise HTTPException(status_code=403, detail="Admin only")
    
    if type == "line": T.add_line(name)
    elif type == "machine": T.add_machine(parent_id, name)
    elif type == "subpart": T.add_subpart(parent_id, name)
    else: raise HTTPException(status_code=400, detail="Invalid type")
        
    return {"status": "success", "message": f"{type.capitalize()} '{name}' created successfully."}

@app.post("/admin/delete")
async def admin_delete(token: str = Form(...), type: str = Form(...), item_id: str = Form(...)):
    user = _get_user(token)
    if user["role"] != "admin": raise HTTPException(status_code=403, detail="Admin only")
    T.delete_item(type, item_id)
    return {"status": "success", "message": f"Deleted successfully."}

def process_excel_background(excel_path: Path, output_dir: Path):
    analyze_excel(excel_path, output_dir)

def process_manual_background(pdf_path: Path, output_dir: Path):
    llm = LLMClient()
    build_page_index(pdf_path, llm, output_dir)

@app.post("/admin/upload_excel")
async def upload_excel(background_tasks: BackgroundTasks, token: str = Form(...), machine_id: str = Form(...), file: UploadFile = File(...)):
    user = _get_user(token)
    if user["role"] != "admin": raise HTTPException(status_code=403, detail="Admin only")
    
    machine_dir = DATA_DIR / machine_id
    machine_dir.mkdir(parents=True, exist_ok=True)
    file_path = machine_dir / file.filename  # use the actual uploaded filename

    # 1. Save the newly uploaded file temporarily
    temp_path = machine_dir / f"temp_{file.filename}"
    with open(temp_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    # 2. Append Logic (Merge old and new data)
    import pandas as pd
    if file_path.exists():
        print("[SYSTEM] Existing Excel found. Appending new data...")
        try:
            df_old = pd.read_excel(file_path)
            df_new = pd.read_excel(temp_path)

            # Combine both files
            df_combined = pd.concat([df_old, df_new], ignore_index=True)

            # Drop exact duplicate rows so we don't count the same ticket twice!
            df_combined.drop_duplicates(inplace=True)

            # Save the merged file back using the same filename
            df_combined.to_excel(file_path, index=False)
            print(f"[SYSTEM] Merged successfully! Total rows: {len(df_combined)}")
        except Exception as e:
            print(f"[ERROR] Could not merge Excel files: {e}")
            shutil.move(str(temp_path), str(file_path))
    else:
        shutil.move(str(temp_path), str(file_path))

    # Clean up temp file
    if temp_path.exists():
        temp_path.unlink()

    # 3. Run the Analyzer on the newly combined file
    background_tasks.add_task(process_excel_background, file_path, machine_dir)
    return {"status": "success", "message": "Excel data appended successfully! AI is analyzing..."}

@app.post("/admin/upload_manual")
async def upload_manual(background_tasks: BackgroundTasks, token: str = Form(...), machine_id: str = Form(...), subpart_id: str = Form(...), file: UploadFile = File(...)):
    user = _get_user(token)
    if user["role"] != "admin": raise HTTPException(status_code=403, detail="Admin only")
    
    subpart_dir = DATA_DIR / machine_id / "subparts" / subpart_id
    subpart_dir.mkdir(parents=True, exist_ok=True)
    file_path = subpart_dir / "manual.pdf"
    with open(file_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
        
    background_tasks.add_task(process_manual_background, file_path, subpart_dir)
    return {"status": "success", "message": "Manual uploaded. AI is indexing pages in background."}


# ══════════════════════════════════════════════════════════════════════════════
# VOICE HELPERS — Sarvam STT + TTS (sync wrappers, called via asyncio.to_thread)
# ══════════════════════════════════════════════════════════════════════════════

_VOICE_LANG_CODE = {"en": "en-IN", "ta": "ta-IN", "hi": "hi-IN"}
_VOICE_GREETINGS = {
    "ta": "வணக்கம்! இயந்திரத்தில் என்ன பிரச்சனை உள்ளது என்று சொல்லுங்கள்.",
    "hi": "नमस्ते! मशीन में क्या समस्या हो रही है?",
    "en": "Hello! I am your diagnostic assistant. What problem are you seeing with the machine right now?",
}

def _sarvam_transcribe(audio_bytes: bytes, lang: str) -> str:
    key  = os.getenv("API_KEY", "")
    lang_code = _VOICE_LANG_CODE.get(lang, "en-IN")
    try:
        r = _requests.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": key},
            files={"file": ("audio.webm", audio_bytes, "audio/webm")},
            data={"model": "saarika:v2.5", "language_code": lang_code},
            timeout=12,
        )
        r.raise_for_status()
        text = r.json().get("transcript", "").strip()
        print(f"[STT] ({lang_code}): {text[:80]}")
        return text
    except Exception as e:
        print(f"[STT] Error: {e}")
        return ""

def _sarvam_tts(text: str, lang: str) -> bytes:
    key = os.getenv("API_KEY", "")
    try:
        r = _requests.post(
            "https://api.sarvam.ai/text-to-speech/stream",
            headers={"api-subscription-key": key, "Content-Type": "application/json"},
            json={
                "text": text[:500],
                "target_language_code": _VOICE_LANG_CODE.get(lang, "en-IN"),
                "speaker": "priya",
                "model": "bulbul:v3",
                "pace": 1.05,
                "speech_sample_rate": 22050,
                "output_audio_codec": "mp3",
                "enable_preprocessing": True,
            },
            stream=True, timeout=15,
        )
        r.raise_for_status()
        audio = b"".join(c for c in r.iter_content(8192) if c)
        return audio if audio else b"\xff\xfb\x90\x00" + b"\x00" * 100
    except Exception as e:
        print(f"[TTS] Error: {e}")
        return b"\xff\xfb\x90\x00" + b"\x00" * 100

def _clean_for_tts(t: str) -> str:
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)
    t = re.sub(r"[*_`#]", "", t)
    return re.sub(r"\n+", " ", t).strip()


# ══════════════════════════════════════════════════════════════════════════════
# VOICE WEBSOCKET — full Sarvam STT→LLM→TTS pipeline on /ws/voice-audio
# ══════════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/voice-audio")
async def websocket_voice_audio(websocket: WebSocket):
    await websocket.accept()
    session = None   # dict: lang, machine_id, subpart_id, agent

    async def _send(data: dict):
        await websocket.send_text(json.dumps(data))

    async def _send_audio(text: str, mp3: bytes):
        await _send({
            "type":  "audio_chunk",
            "text":  text,
            "audio": base64.b64encode(mp3).decode(),
            "mime":  "audio/mpeg",
        })

    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break

            audio_bytes = raw.get("bytes")
            text_frame  = raw.get("text")

            # ── Binary audio frame (push-to-talk recording) ────────────────
            if audio_bytes and len(audio_bytes) > 500 and session:
                lang  = session["lang"]
                agent = session["agent"]

                await _send({"type": "status", "text": "listening"})
                transcript = await asyncio.to_thread(_sarvam_transcribe, audio_bytes, lang)

                if not transcript.strip():
                    retry_msg = {"ta": "மீண்டும் சொல்லுங்கள்.", "hi": "कृपया फिर बोलें।",
                                 "en": "Could you repeat that please?"}.get(lang, "Please repeat.")
                    mp3 = await asyncio.to_thread(_sarvam_tts, retry_msg, lang)
                    await _send_audio(retry_msg, mp3)
                    await _send({"type": "done"})
                    continue

                await _send({"type": "transcript", "text": transcript})
                await _send({"type": "status", "text": "thinking"})

                sentence_buf = ""
                full_response = ""
                async for chunk in agent.async_chat_stream(transcript):
                    chunk = _clean_for_tts(chunk)
                    if not chunk:
                        continue
                    full_response += chunk
                    sentence_buf  += chunk
                    await _send({"type": "text_chunk", "text": chunk})

                    if re.search(r'[.!?।]\s*$', sentence_buf.rstrip()) and len(sentence_buf.strip()) > 8:
                        sentence = sentence_buf.strip()
                        sentence_buf = ""
                        mp3 = await asyncio.to_thread(_sarvam_tts, sentence, lang)
                        await _send_audio(sentence, mp3)

                if sentence_buf.strip() and len(sentence_buf.strip()) > 3:
                    mp3 = await asyncio.to_thread(_sarvam_tts, sentence_buf.strip(), lang)
                    await _send_audio(sentence_buf.strip(), mp3)

                await _send({"type": "done"})

            # ── JSON control message ───────────────────────────────────────
            elif text_frame:
                try:
                    data = json.loads(text_frame)
                except Exception:
                    continue
                mtype = data.get("type", "")

                if mtype == "init" and session is None:
                    m_id = data.get("machine_id", "")
                    s_id = data.get("subpart_id", "")
                    lang = data.get("lang", "en")[:2]
                    if not m_id or not s_id:
                        await _send({"type": "error", "text": "machine_id and subpart_id required."})
                        continue

                    agent = DiagnosticAgent(
                        session_id=id(websocket),
                        machine_id=m_id,
                        subpart_id=s_id,
                        lang=lang,
                    )
                    session = {"lang": lang, "machine_id": m_id, "subpart_id": s_id, "agent": agent}

                    await _send({"type": "status", "text": "loading"})
                    await asyncio.to_thread(agent.initialise)

                    greeting = _VOICE_GREETINGS.get(lang, _VOICE_GREETINGS["en"])
                    mp3 = await asyncio.to_thread(_sarvam_tts, greeting, lang)
                    await _send({"type": "status", "text": "ready"})
                    await _send_audio(greeting, mp3)
                    await _send({"type": "done"})

                elif mtype == "set_lang" and session:
                    session["lang"] = data.get("lang", "en")[:2]
                    if session["agent"]:
                        session["agent"].state["lang"] = session["lang"]

                elif mtype == "end_call" and session:
                    farewell = {"ta": "நன்றி. குட்பை.", "hi": "धन्यवाद।",
                                "en": "Thank you. Goodbye!"}.get(session["lang"], "Goodbye!")
                    mp3 = await asyncio.to_thread(_sarvam_tts, farewell, session["lang"])
                    await _send_audio(farewell, mp3)
                    await _send({"type": "done"})
                    break

                elif mtype == "ping":
                    await _send({"type": "pong"})

    except WebSocketDisconnect:
        print("[VOICE-AUDIO] Client disconnected.")
    except Exception as e:
        print(f"[VOICE-AUDIO] Error: {e}")
        import traceback; traceback.print_exc()


# ══════════════════════════════════════════════════════════════════════════════
# SESSION (FALLBACK HTTP FLOW)
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/session/start")
async def session_start(token: str = Form(...), machine_id: str = Form(...), subpart_id: str = Form(...), lang: str = Form("en-IN")):
    import uuid
    from datetime import datetime
    from agent import DiagnosticAgent
    
    session_token = str(uuid.uuid4())
    session_id    = int(datetime.now().timestamp())

    # Start the agent
    agent = DiagnosticAgent(session_id=session_id, machine_id=machine_id, subpart_id=subpart_id, lang=lang)
    greeting = agent.initialise()
    
    # Save to global dictionary so /ask can find it
    agent_sessions[session_token] = agent

    return {"session_token": session_token, "machine_id": machine_id, "subpart_id": subpart_id, "greeting": greeting}

@app.post("/ask")
async def ask(query: str = Form(...), session_token: str = Form(...)):
    agent = agent_sessions.get(session_token)
    if not agent:
        async def expired():
            yield f'data: {json.dumps({"chunk": "Session expired. Please refresh."})}\n\n'
            yield f'data: {json.dumps({"done": True})}\n\n'
        return StreamingResponse(expired(), media_type="text/event-stream")

    # Special case: opening greeting on session start
    if query == "__greeting__":
        async def greeting_stream():
            async for chunk in agent.opening_greeting_stream():
                yield f'data: {json.dumps({"chunk": chunk})}\n\n'
            yield f'data: {json.dumps({"done": True})}\n\n'
        return StreamingResponse(greeting_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # Normal conversation turn — stream tokens as they arrive
    async def reply_stream():
        full_reply = ""
        try:
            async for chunk in agent.async_chat_stream(query):
                full_reply += chunk
                yield f'data: {json.dumps({"chunk": chunk})}\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"chunk": f"Error: {str(e)[:50]}"})}\n\n'
        finally:
            yield f'data: {json.dumps({"done": True})}\n\n'

    return StreamingResponse(
        reply_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # prevents nginx from buffering the stream
        }
    )


    
# ── Serve Static Frontend Files ────────────────────────────────────────────────
if Path("static").exists():
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
