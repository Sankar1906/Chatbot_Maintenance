"""
tools.py — Generic Machine Diagnostic Tool Implementations
==========================================================
Stateless helper functions. Now includes Admin CRUD capabilities.
"""

import json
import re
import sqlite3
import shutil
import uuid
from datetime import datetime
from pathlib import Path

DATA_DIR     = Path("data")
MACHINES_DIR = DATA_DIR / "machines"
USERS_JSON   = Path("users.json")
DB_PATH      = Path("maintenance.db")

# ══════════════════════════════════════════════════════════════════════════════
# REGISTRY HELPERS (CRUD FOR ADMIN)
# ══════════════════════════════════════════════════════════════════════════════

def get_registry() -> dict:
    p = DATA_DIR / "registry.json"
    # Fallback to machines.json if registry.json doesn't exist yet
    fallback = Path("machines.json")
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    elif fallback.exists():
        return json.loads(fallback.read_text(encoding="utf-8"))
    return {"lines":[]}

def save_registry(data: dict):
    p = DATA_DIR / "registry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # Also mirror to machines.json for safety
    Path("machines.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

def _sanitise(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(s).strip().lower())

def add_line(line_name: str) -> str:
    reg = get_registry()
    line_id = _sanitise(line_name) + "_" + str(uuid.uuid4())[:4]
    reg["lines"].append({"line_id": line_id, "line_name": line_name, "machines":[]})
    save_registry(reg)
    return line_id

def add_machine(line_id: str, machine_name: str) -> str:
    reg = get_registry()
    machine_id = _sanitise(machine_name) + "_" + str(uuid.uuid4())[:4]
    for l in reg["lines"]:
        if l["line_id"] == line_id:
            l["machines"].append({
                "machine_id": machine_id, 
                "machine_name": machine_name, 
                "type": "Generic", 
                "subparts":[]
            })
            break
    save_registry(reg)
    return machine_id

def add_subpart(machine_id: str, subpart_name: str) -> str:
    reg = get_registry()
    subpart_id = _sanitise(subpart_name) + "_" + str(uuid.uuid4())[:4]
    for l in reg["lines"]:
        for m in l["machines"]:
            if m["machine_id"] == machine_id:
                if "subparts" not in m:
                    m["subparts"] = []
                m["subparts"].append({
                    "subpart_id": subpart_id,
                    "subpart_name": subpart_name
                })
                break
    save_registry(reg)
    return subpart_id

def delete_item(item_type: str, item_id: str):
    reg = get_registry()
    if item_type == "line":
        reg["lines"] =[l for l in reg["lines"] if l["line_id"] != item_id]
        
    elif item_type == "machine":
        for l in reg["lines"]:
            l["machines"] =[m for m in l["machines"] if m["machine_id"] != item_id]
        # Delete the machine folder
        shutil.rmtree(MACHINES_DIR / item_id, ignore_errors=True)
        
    elif item_type == "subpart":
        for l in reg["lines"]:
            for m in l["machines"]:
                if "subparts" in m:
                    m["subparts"] =[s for s in m["subparts"] if s["subpart_id"] != item_id]
        # We would need machine_id to delete the folder cleanly, but DB structure is handled.
    
    save_registry(reg)

# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADERS & DB (Existing Logic)
# ══════════════════════════════════════════════════════════════════════════════


def get_subpart_name(subpart_id: str) -> str:
    """Helper to convert computer ID back to human-readable Excel name."""
    reg = get_registry()
    for l in reg.get("lines",[]):
        for m in l.get("machines", []):
            for s in m.get("subparts",[]):
                if s.get("subpart_id") == subpart_id:
                    return s.get("subpart_name")
    return subpart_id # Fallback

def load_frequency_cache(machine_id: str, subpart_id: str) -> dict:
    """Loads the Excel frequency data for a given machine/subpart.

    Lookup order:
    1. Exact subpart name match (e.g. "Scrap_Conveyor")
    2. Partial machine name match against Excel machine descriptions
    3. "All Machines" aggregate fallback
    """
    path = MACHINES_DIR / machine_id / "frequency_cache.json"
    if not path.exists():
        return {}

    try:
        cache = json.loads(path.read_text(encoding="utf-8"))

        subpart_name = get_subpart_name(subpart_id)

        # 1. Exact subpart name match
        for key in cache.keys():
            if key.lower().strip() == subpart_name.lower().strip():
                return cache[key]

        # 2. Fuzzy machine name match — use similarity ratio to handle typos
        #    (e.g. registry "soenon" vs Excel "SOENEN"). Pick the best-scoring
        #    non-aggregate key whose similarity to machine_id exceeds 0.55.
        import difflib
        machine_id_norm = machine_id.lower().replace("_", " ")
        best_key, best_score = None, 0.0
        for key in cache.keys():
            if key == "All Machines":
                continue
            score = difflib.SequenceMatcher(None, machine_id_norm, key.lower()).ratio()
            if score > best_score:
                best_key, best_score = key, score
        if best_key and best_score >= 0.45:
            return cache[best_key]

        # 3. "All Machines" aggregate fallback
        if "All Machines" in cache:
            return cache["All Machines"]

        return {}
    except Exception as e:
        print(f"[ERROR] Failed to load frequency cache: {e}")
        return {}

def load_page_index(machine_id: str, subpart_id: str) -> dict:
    path = MACHINES_DIR / machine_id / "subparts" / subpart_id / "page_index.json"
    if not path.exists(): return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception: return {}

def load_safety_checklist(machine_id: str, subpart_id: str) -> dict:
    path = MACHINES_DIR / machine_id / "subparts" / subpart_id / "safety_checklist.json"
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except Exception: pass
    return {"items":["Ensure machine is fully powered off", "Wear appropriate PPE", "Inform supervisor before starting"]}

def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, machine_id TEXT, subpart_id TEXT, user_id TEXT,
            problem TEXT, confidence REAL, solution TEXT,
            resolved INTEGER DEFAULT 0, requires_supervisor INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

def save_ticket(session_id: str, machine_id: str, subpart_id: str, user_id: str, problem: str, confidence: float, solution: str, resolved: bool, requires_supervisor: bool):
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("""
            INSERT INTO tickets (session_id, machine_id, subpart_id, user_id, problem, confidence, solution, resolved, requires_supervisor)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (session_id, machine_id, subpart_id, user_id, problem, confidence, solution, int(resolved), int(requires_supervisor)))
        conn.commit()
        conn.close()
    except Exception as e: print(f"save_ticket error: {e}")
def get_recent_tickets(machine_id: str) -> str:
    """
    Fetches the recent breakdown history for the machine.
    Currently acts as a placeholder to prevent crashes.
    """
    # In the future, this can read from a live database. 
    # For now, we return a blank history so the AI doesn't crash.
    return "No past medical history recorded for this machine."    