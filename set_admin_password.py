"""
Set (or reset) an account's password on the server, without the web UI.

    sudo -u farhanfx /opt/farhanfx/.venv/bin/python /opt/farhanfx/set_admin_password.py [username]

Asks for the new password twice (nothing is echoed) and signs that account
out of every device. Run it once right after the first deploy: a fresh
production server creates the admin with a random password nobody knows.
"""
import getpass
import hashlib
import json
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
USERS = os.path.join(HERE, "users.json")
SESSIONS = os.path.join(HERE, "auth_sessions.json")


def _hash_pw(password: str, salt: str) -> str:        # same scheme as server.py
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "admin"
    try:
        with open(USERS, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        sys.exit("users.json not found — start the server once first (systemctl start farhanfx)")
    user = next((u for u in data.get("users", []) if u["username"] == username), None)
    if not user:
        sys.exit(f"No account named '{username}'")
    pw = getpass.getpass(f"New password for {username} (8+ characters): ")
    if len(pw) < 8:
        sys.exit("Too short — use at least 8 characters")
    if getpass.getpass("Repeat it: ") != pw:
        sys.exit("The two passwords do not match")
    user["salt"] = secrets.token_hex(16)
    user["password_hash"] = _hash_pw(pw, user["salt"])
    tmp = USERS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, USERS)
    try:
        with open(SESSIONS, encoding="utf-8") as f:
            sessions = json.load(f)
        sessions = {t: s for t, s in sessions.items() if s.get("username") != username}
        with open(SESSIONS, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2)
    except FileNotFoundError:
        pass
    print(f"Password for '{username}' updated. Restart to sign out open sessions: systemctl restart farhanfx")


if __name__ == "__main__":
    main()
