#!/usr/bin/env python3
"""
Bootstrap NotebookLM auth by extracting Google cookies from Brave/Chrome.
Eliminates the manual browser login step.

Usage: python scripts/bootstrap_auth.py [--browser brave|chrome]
"""

import json
import sys
import sqlite3
import argparse
import base64
from pathlib import Path

# Add skill scripts dir to path
sys.path.insert(0, str(Path(__file__).parent))
from config import STATE_FILE, BROWSER_STATE_DIR, AUTH_INFO_FILE, DATA_DIR

GOOGLE_DOMAINS = [
    ".google.com", "google.com",
    "accounts.google.com", ".accounts.google.com",
    ".notebooklm.google.com", "notebooklm.google.com",
    ".google.pt", "accounts.google.pt",
]

BRAVE_PROFILE = Path.home() / "AppData/Local/BraveSoftware/Brave-Browser/User Data/Default"
CHROME_PROFILE = Path.home() / "AppData/Local/Google/Chrome/User Data/Default"


def _ensure_pywin32():
    """Import win32crypt, installing pywin32 and registering its DLL dir if needed.

    After `pip install pywin32`, the win32 DLLs live in site-packages/pywin32_system32
    and are NOT importable until that dir is on the DLL search path — so a same-process
    import right after install fails. Add the dir explicitly (os.add_dll_directory)."""
    import os, importlib, subprocess
    def _add_dll_dir():
        for sp in sys.path:
            cand = Path(sp) / "pywin32_system32"
            if cand.is_dir():
                try:
                    os.add_dll_directory(str(cand))
                except Exception:
                    pass
                os.environ["PATH"] = str(cand) + os.pathsep + os.environ.get("PATH", "")
                return
    _add_dll_dir()
    try:
        return importlib.import_module("win32crypt")
    except ImportError:
        print("Installing pywin32...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pywin32"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _add_dll_dir()
        return importlib.import_module("win32crypt")


def get_encryption_key(local_state_path: Path) -> bytes:
    """Extract and decrypt the AES key from browser Local State."""
    win32crypt = _ensure_pywin32()

    # Local State is in "User Data", two levels up from "Network/Cookies"
    local_state_file = local_state_path.parent.parent.parent / "Local State"
    with open(local_state_file, "r", encoding="utf-8") as f:
        local_state = json.load(f)

    encrypted_key_b64 = local_state["os_crypt"]["encrypted_key"]
    encrypted_key = base64.b64decode(encrypted_key_b64)
    # Strip DPAPI prefix "DPAPI" (5 bytes)
    encrypted_key = encrypted_key[5:]
    key = win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]
    return key


def decrypt_cookie(encrypted_value: bytes, key: bytes) -> str:
    """Decrypt a Chrome v10/v20 encrypted cookie value."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "cryptography"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not encrypted_value or len(encrypted_value) < 15:
        return ""

    version = encrypted_value[:3]
    if version not in (b"v10", b"v20"):
        # Unencrypted (old format)
        try:
            return encrypted_value.decode("utf-8")
        except Exception:
            return ""

    nonce = encrypted_value[3:15]
    ciphertext = encrypted_value[15:]

    try:
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode("utf-8")
    except Exception:
        return ""


def extract_cookies(profile_dir: Path, domains: list[str]) -> list[dict]:
    """Extract and decrypt Google cookies from browser profile."""
    cookies_db = profile_dir / "Network" / "Cookies"
    if not cookies_db.exists():
        raise FileNotFoundError(f"Cookies database not found: {cookies_db}")

    local_state_path = profile_dir / "Network" / "Cookies"  # path used for key lookup
    key = get_encryption_key(local_state_path)

    # Read the cookie DB while the browser may still be running. Chromium holds an
    # exclusive lock, so we try, in order:
    #   1) win32file copy with full share access (works on older Chromium),
    #   2) SQLite read-only `immutable=1` connection directly on the file — bypasses all
    #      locking (reads the committed DB; ignores the -wal, which is fine for stable auth
    #      cookies). This is what lets bootstrap work without closing the browser.
    import tempfile
    tmp_path = None
    conn = None
    try:
        import win32file, win32con
        tmp_path = Path(tempfile.mktemp(suffix=".db"))
        handle = win32file.CreateFile(
            str(cookies_db),
            win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_ATTRIBUTE_NORMAL,
            None
        )
        data = win32file.ReadFile(handle, cookies_db.stat().st_size)[1]
        win32file.CloseHandle(handle)
        tmp_path.write_bytes(data)
        conn = sqlite3.connect(str(tmp_path))
    except Exception:
        # Fallback: read-only immutable connection (bypasses Chromium's exclusive lock)
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
            tmp_path = None
        uri = cookies_db.as_uri() + "?mode=ro&immutable=1"
        conn = sqlite3.connect(uri, uri=True)

    results = []
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        placeholders = ",".join("?" * len(domains))
        cur.execute(
            f"SELECT name, encrypted_value, host_key, path, expires_utc, is_httponly, is_secure, samesite "
            f"FROM cookies WHERE host_key IN ({placeholders})",
            domains
        )

        app_bound_failures = 0
        for row in cur.fetchall():
            ev = row["encrypted_value"]
            value = decrypt_cookie(ev, key)
            if not value:
                # v20 = Chromium "app-bound encryption": the cookie key is sealed under a
                # SYSTEM-context DPAPI blob and cannot be unwrapped by a normal user process.
                if ev and bytes(ev[:3]) == b"v20":
                    app_bound_failures += 1
                continue

            # Chrome epoch → Unix: Chrome uses microseconds since 1601-01-01
            expires_utc = row["expires_utc"]
            if expires_utc > 0:
                # Convert from Chrome microseconds (since 1601) to Unix seconds (since 1970)
                CHROME_EPOCH_OFFSET = 11644473600  # seconds between 1601 and 1970
                expires_unix = (expires_utc / 1_000_000) - CHROME_EPOCH_OFFSET
            else:
                expires_unix = -1

            samesite_map = {-1: "Unspecified", 0: "None", 1: "Lax", 2: "Strict"}
            results.append({
                "name": row["name"],
                "value": value,
                "domain": row["host_key"],
                "path": row["path"],
                "expires": expires_unix,
                "httpOnly": bool(row["is_httponly"]),
                "secure": bool(row["is_secure"]),
                "sameSite": samesite_map.get(row["samesite"], "None"),
            })

        conn.close()
        if not results and app_bound_failures:
            raise RuntimeError(
                f"found {app_bound_failures} Google cookies but all are app-bound "
                "encrypted (v20) — these cannot be decrypted by a user process. "
                "Cookie extraction won't work on this browser. Use interactive login "
                "instead:  python scripts/run.py auth_manager.py setup"
            )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    return results


def main():
    parser = argparse.ArgumentParser(description="Bootstrap NotebookLM auth from browser cookies")
    parser.add_argument("--browser", choices=["brave", "chrome"], default="brave")
    args = parser.parse_args()

    profile_dir = BRAVE_PROFILE if args.browser == "brave" else CHROME_PROFILE
    if not profile_dir.exists():
        print(f"Browser profile not found: {profile_dir}")
        sys.exit(1)

    print(f"Extracting Google cookies from {args.browser.title()}...")

    try:
        cookies = extract_cookies(profile_dir, GOOGLE_DOMAINS)
    except Exception as e:
        print(f"Failed to extract cookies: {e}")
        sys.exit(1)

    if not cookies:
        print("No Google cookies found. Make sure you're logged into Google in Brave.")
        sys.exit(1)

    print(f"Found {len(cookies)} Google cookies.")

    # Write Playwright storage state format
    BROWSER_STATE_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    state = {"cookies": cookies, "origins": []}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

    print(f"Saved state to: {STATE_FILE}")

    # Write auth_info
    from datetime import datetime, timezone
    auth_info = {
        "authenticated": True,
        "last_auth": datetime.now(timezone.utc).isoformat(),
        "method": f"cookie_extraction_{args.browser}",
        "email": "extracted_from_browser"
    }
    with open(AUTH_INFO_FILE, "w") as f:
        json.dump(auth_info, f, indent=2)

    print("Auth bootstrapped. Run auth_manager.py status to verify.")


if __name__ == "__main__":
    main()
