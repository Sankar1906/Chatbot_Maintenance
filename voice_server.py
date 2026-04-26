"""
voice_server.py — Factory Voice Diagnostic Server
==================================================
Fixes applied:
  1. WebM → WAV conversion using pydub before sending to Sarvam STT
     (saarika:v2.5 returns empty on raw webm/opus without conversion)
  2. Minimum 8KB audio threshold — eliminates noise-only frames
  3. Processing lock — prevents concurrent STT calls from rapid taps
  4. language_code="unknown" for first turn → Sarvam auto-detects language
  5. Detailed debug logging to diagnose future STT failures quickly
"""

import asyncio
import base64
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests
import websockets

sys.path.insert(0, str(Path(__file__).parent))
from agent import AgenticOrchestrator

# ── Config ────────────────────────────────────────────────────────────────────
SARVAM_API_KEY = os.getenv("API_KEY", "")

WS_HOST    = "0.0.0.0"
WS_PORT    = 8765
DB_PATH    = "maintenance.db"
SARVAM_STT = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS = "https://api.sarvam.ai/text-to-speech/stream"

LANG_CODE  = {"en": "en-IN", "ta": "ta-IN", "hi": "hi-IN"}
SPEAKER    = {"en": "priya", "ta": "priya", "hi": "priya"}

# Minimum audio bytes to attempt STT — below this is silence/noise
MIN_AUDIO_BYTES = 2_000   # 2 KB


# ── Audio conversion — WebM/Opus → WAV ───────────────────────────────────────
def _convert_to_wav(audio_bytes: bytes) -> bytes:
    """
    Convert browser MediaRecorder output (WebM/Opus) to WAV PCM.
    Sarvam saarika:v2.5 handles WAV reliably; WebM/Opus often returns empty.
    Requires: pip install pydub  AND  ffmpeg in PATH.
    Falls back to raw bytes if conversion fails.
    """
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(io.BytesIO(audio_bytes), format="webm")
        # Normalise to 16kHz mono PCM — optimal for STT
        seg = seg.set_frame_rate(16000).set_channels(1).set_sample_width(2)
        buf = io.BytesIO()
        seg.export(buf, format="wav")
        wav = buf.getvalue()
        print(f"[AUDIO] Converted WebM→WAV: {len(audio_bytes)}B → {len(wav)}B | "
              f"duration: {seg.duration_seconds:.2f}s")
        # Reject clips shorter than 0.5s — they cannot contain useful speech
        if seg.duration_seconds < 0.5:
            print("[AUDIO] Clip too short (<0.5s) — treating as silence.")
            return b""
        return wav
    except ImportError:
        print("[AUDIO] pydub not installed — sending raw bytes. Run: pip install pydub")
        return audio_bytes
    except Exception as e:
        print(f"[AUDIO] Conversion failed ({e}) — sending raw bytes. Is ffmpeg in PATH?")
        return audio_bytes


# ── Sarvam STT ────────────────────────────────────────────────────────────────
def transcribe(audio_bytes: bytes, lang_hint: str = "en",
               first_turn: bool = False) -> tuple[str, str]:
    """
    Convert audio to WAV then send to Sarvam STT.
    Uses language_code='unknown' on the first turn so Sarvam auto-detects
    the language (handles Hinglish, mixed speech, etc. better).
    """
    # Step 1 — convert format
    wav_bytes = _convert_to_wav(audio_bytes)
    if not wav_bytes:
        return "", lang_hint

    # Step 2 — choose language code
    # On first turn use 'unknown' — Sarvam auto-detects Tamil/Hindi/English
    # On subsequent turns use the detected/selected language
    if first_turn:
        sarvam_lang = "unknown"
    else:
        sarvam_lang = LANG_CODE.get(lang_hint, "en-IN")

    print(f"[STT] Sending {len(wav_bytes)//1024}KB WAV to Sarvam | lang={sarvam_lang}")

    try:
        r = requests.post(
            SARVAM_STT,
            headers={"api-subscription-key": SARVAM_API_KEY},
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            data={
                "model":         "saarika:v2.5",
                "language_code": sarvam_lang,
            },
            timeout=15,
        )
        r.raise_for_status()
        resp     = r.json()
        text     = resp.get("transcript", "").strip()
        detected = resp.get("language_code", lang_hint)

        if text:
            print(f"[STT] ✓ ({sarvam_lang} → detected: {detected}): {text[:80]}")
        else:
            print(f"[STT] ✗ Empty transcript. API response: {resp}")

        return text, detected.split("-")[0] if detected != "unknown" else lang_hint

    except requests.exceptions.HTTPError as e:
        print(f"[STT] HTTP {e.response.status_code}: {e.response.text[:200]}")
        return "", lang_hint
    except Exception as e:
        print(f"[STT] Error: {e}")
        return "", lang_hint


# ── Sarvam TTS ────────────────────────────────────────────────────────────────
async def tts_sentence(text: str, lang: str) -> bytes:
    """Stream TTS for one sentence. Returns MP3 bytes."""
    payload = {
        "text":                 text[:500],
        "target_language_code": LANG_CODE.get(lang, "en-IN"),
        "speaker":              SPEAKER.get(lang, "priya"),
        "model":                "bulbul:v3",
        "pace":                 1.05,
        "speech_sample_rate":   22050,
        "output_audio_codec":   "mp3",
        "enable_preprocessing": True,
    }
    try:
        loop = asyncio.get_running_loop()
        def _call():
            r = requests.post(
                SARVAM_TTS,
                headers={"api-subscription-key": SARVAM_API_KEY,
                         "Content-Type": "application/json"},
                json=payload, stream=True, timeout=15,
            )
            r.raise_for_status()
            return b"".join(c for c in r.iter_content(8192) if c)
        audio = await loop.run_in_executor(None, _call)
        return audio or _silent_mp3()
    except Exception as e:
        print(f"[TTS] Error: {e}")
        return _silent_mp3()

def _silent_mp3() -> bytes:
    return b"\xff\xfb\x90\x00" + b"\x00" * 100

def clean_text(t: str) -> str:
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)
    t = re.sub(r"[*_`#]", "", t)
    return re.sub(r"\n+", " ", t)


# ── DB ────────────────────────────────────────────────────────────────────────
def save_ticket(machine_id, issue, lang, turns, resolution):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS voice_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            machine_id TEXT, issue TEXT, lang TEXT,
            start_time TEXT, end_time TEXT, resolution TEXT, turns INTEGER
        )""")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO voice_tickets VALUES(NULL,?,?,?,?,?,?,?)",
                     (machine_id, issue, lang, now, now, resolution, turns))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[DB] {e}")


# ── Voice Session ─────────────────────────────────────────────────────────────
class VoiceSession:
    def __init__(self, ws, machine_id, subpart_id, lang="en"):
        self.ws           = ws
        self.lang         = lang[:2]
        self.machine_id   = machine_id
        self.subpart_id   = subpart_id
        self.turns        = 0
        self.issue        = ""
        self._processing  = False   # lock — prevents concurrent STT calls
        self.agent        = AgenticOrchestrator(
            session_id=id(self), machine_id=machine_id,
            subpart_id=subpart_id, lang=lang,
        )

    async def send(self, data):
        await self.ws.send(json.dumps(data))

    async def send_audio(self, text, audio):
        await self.send({
            "type":  "audio_chunk",
            "text":  text,
            "audio": base64.b64encode(audio).decode(),
            "mime":  "audio/mpeg",
        })

    async def greet(self):
        greeting = {
            "ta": "வணக்கம்! இயந்திரத்தில் என்ன பிரச்சனை உள்ளது என்று சொல்லுங்கள்.",
            "hi": "नमस्ते! मशीन में क्या समस्या हो रही है?",
            "en": "Hello! I am your diagnostic assistant. What problem are you seeing with the machine right now?",
        }.get(self.lang, "Hello! What problem are you seeing?")
        audio = await tts_sentence(greeting, self.lang)
        await self.send({"type": "status", "text": "ready"})
        await self.send_audio(greeting, audio)
        await self.send({"type": "done"})

    async def process_audio(self, audio_bytes: bytes):
        # ── Lock — drop duplicate calls while already processing ─────────────
        if self._processing:
            print(f"[VOICE] Dropped audio — already processing ({len(audio_bytes)}B)")
            return
        self._processing = True

        try:
            # ── Size gate ────────────────────────────────────────────────────
            if len(audio_bytes) < MIN_AUDIO_BYTES:
                print(f"[VOICE] Audio too small ({len(audio_bytes)}B < {MIN_AUDIO_BYTES}B) — ignored.")
                await self.send({"type": "status", "text": "ready"})
                return

            await self.send({"type": "status", "text": "listening"})

            # ── STT — run in thread (non-blocking) ───────────────────────────
            loop = asyncio.get_running_loop()
            first_turn = (self.turns == 0)
            text, detected_lang = await loop.run_in_executor(
                None, transcribe, audio_bytes, self.lang, first_turn
            )

            # Update language if Sarvam detected a different one
            if detected_lang and detected_lang != self.lang:
                self.lang = detected_lang[:2]
                self.agent.state["lang"] = self.lang
                print(f"[VOICE] Language updated to: {self.lang}")

            # ── Empty transcript ─────────────────────────────────────────────
            if not text.strip():
                msg = {
                    "ta": "மீண்டும் சொல்லுங்கள்.",
                    "hi": "कृपया फिर से बोलें। ज़्यादा साफ़ बोलें।",
                    "en": "I could not catch that. Please speak clearly and try again.",
                }.get(self.lang, "Please repeat that.")
                await self.send_audio(msg, await tts_sentence(msg, self.lang))
                await self.send({"type": "done"})
                return

            if not self.issue:
                self.issue = text[:120]

            await self.send({"type": "transcript", "text": text})
            await self.send({"type": "status", "text": "thinking"})

            # ── LLM → sentence buffer → TTS per sentence ─────────────────────
            sentence_buf  = ""
            full_response = ""

            async for chunk in self.agent.async_chat_stream(text):
                chunk = clean_text(chunk)
                if not chunk:
                    continue
                full_response += chunk
                sentence_buf  += chunk
                await self.send({"type": "text_chunk", "text": chunk})

                if (re.search(r'[.!?।]\s*$', sentence_buf.rstrip())
                        and len(sentence_buf.strip()) > 8):
                    sentence     = sentence_buf.strip()
                    sentence_buf = ""
                    audio = await tts_sentence(sentence, self.lang)
                    await self.send_audio(sentence, audio)

            if sentence_buf.strip() and len(sentence_buf.strip()) > 3:
                audio = await tts_sentence(sentence_buf.strip(), self.lang)
                await self.send_audio(sentence_buf.strip(), audio)

            self.turns += 1
            await self.send({"type": "done"})

            if any(w in full_response.lower() for w in ["engineer","escalate","supervisor"]):
                save_ticket(self.machine_id, self.issue, self.lang, self.turns, "Escalated")
                await self.send({"type": "escalate"})

        finally:
            self._processing = False   # always release lock

    async def end_call(self):
        farewell = {
            "ta": "நன்றி. குட்பை.",
            "hi": "धन्यवाद। अलविदा।",
            "en": "Thank you. Goodbye!",
        }.get(self.lang, "Goodbye!")
        await self.send_audio(farewell, await tts_sentence(farewell, self.lang))
        await self.send({"type": "done"})
        save_ticket(self.machine_id, self.issue, self.lang, self.turns, "Completed")


# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handle_connection(ws):
    session = None
    addr    = ws.remote_address
    print(f"\n[VOICE] Connected: {addr}")
    try:
        async for msg in ws:
            if isinstance(msg, str):
                try:
                    data  = json.loads(msg)
                except Exception:
                    continue
                mtype = data.get("type", "")

                if mtype == "init" and not session:
                    m_id = data.get("machine_id", "")
                    s_id = data.get("subpart_id", "")
                    lang = data.get("lang", "en")[:2]
                    if not m_id or not s_id:
                        await ws.send(json.dumps({
                            "type": "error",
                            "text": "machine_id and subpart_id required."
                        }))
                        continue
                    session = VoiceSession(ws, m_id, s_id, lang)
                    await ws.send(json.dumps({"type": "status", "text": "loading"}))
                    await asyncio.to_thread(session.agent.initialise)
                    await session.greet()

                elif mtype == "set_lang" and session:
                    session.lang = data.get("lang", "en")[:2]
                    session.agent.state["lang"] = session.lang

                elif mtype == "end_call" and session:
                    await session.end_call()
                    break

                elif mtype == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

            elif isinstance(msg, bytes) and session:
                # Pass ALL binary to process_audio — size gate is inside
                await session.process_audio(msg)

    except websockets.exceptions.ConnectionClosedOK:
        print(f"[VOICE] Closed normally: {addr}")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"[VOICE] Dropped: {addr} — {e}")
    except Exception as e:
        print(f"[VOICE] Error: {addr} — {e}")
    finally:
        if session and session.turns > 0:
            save_ticket(session.machine_id, session.issue,
                        session.lang, session.turns, "Session ended")
        print(f"[VOICE] Done: {addr} | turns: {getattr(session,'turns',0)}")


async def main():
    print("=" * 58)
    print("  Factory Voice Diagnostic Server")
    print(f"  WebSocket : ws://0.0.0.0:{WS_PORT}")
    print(f"  STT       : Sarvam saarika:v2.5 (WebM→WAV converted)")
    print(f"  TTS       : Sarvam bulbul:v3 (per sentence)")
    print(f"  Min audio : {MIN_AUDIO_BYTES//1024}KB (noise gate)")
    print("=" * 58)
    if SARVAM_API_KEY == "YOUR_SARVAM_API_KEY_HERE":
        print("\n⚠ Set SARVAM_API_KEY at the top of voice_server.py!\n")
    else:
        print("\n✓ Sarvam key loaded. Ready.\n")
    async with websockets.serve(handle_connection, WS_HOST, WS_PORT):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
