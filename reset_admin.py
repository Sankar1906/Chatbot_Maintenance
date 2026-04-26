"""
reset_admin.py — Run this ONCE in your project folder to fix admin credentials.
Usage:  python reset_admin.py
"""
import hashlib, json
from pathlib import Path

USERS_FILE = Path("users.json")

# Load existing users.json or create fresh
if USERS_FILE.exists():
    users = json.loads(USERS_FILE.read_text(encoding="utf-8"))
    print(f"[INFO] Found existing users.json with keys: {list(users.keys())}")
else:
    users = {}
    print("[INFO] No users.json found. Creating new one.")

NEW_PASSWORD = "admin123"
new_hash = hashlib.sha256(NEW_PASSWORD.encode()).hexdigest()

# Reset / create admin entry
users["admin"] = {
    "user_id": "admin",
    "name":    "Admin",
    "password": new_hash,
    "role":    "admin"
}

# Keep op001 if it exists, else add default
if "op001" not in users:
    users["op001"] = {
        "user_id":  "op001",
        "name":     "Operator 1",
        "password": hashlib.sha256(b"op001").hexdigest(),
        "role":     "operator"
    }

USERS_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")

print(f"\n✅ Done! users.json updated.")
print(f"   Admin login  →  user_id: admin    password: {NEW_PASSWORD}")
print(f"   Hash written →  {new_hash}")
print(f"\n   Restart your server, then log in at /admin.html")
