#!/usr/bin/env python3
"""
imsg_web.py — local web UI for imsg_assist.

Run:
    python3 imsg_web.py

Opens a server on 127.0.0.1 only. It prints a URL containing a token stored
under ~/.imsg_assist; requests without it are refused, so another page in your
browser can't reach these endpoints. All message data stays on this machine except the style
sample and draft context, which go to the Anthropic API.

Requires imsg_assist.py in the same folder.
"""

import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import imsg_assist as A
except ImportError as e:
    found = sorted(p.name for p in HERE.glob("*.py") if p.name != Path(__file__).name)
    print(f"Couldn't load imsg_assist.py from {HERE}")
    print(f"  reason: {e}")
    print(f"  .py files here: {', '.join(found) if found else 'none'}")
    print("\nThe file must be named exactly imsg_assist.py — downloads often")
    print("arrive as 'imsg assist.py' or 'imsg_assist (1).py'. Rename it and retry.")
    sys.exit(1)

PORT = int(os.environ.get("IMSG_PORT", "8765"))
UI = HERE / "imsg_ui.html"


def _token():
    """Reuse one token across restarts so an open tab keeps working.

    Regenerating it every run silently 403s any tab you already had open.
    """
    f = A.ROOT / "token"
    try:
        if f.exists():
            t = f.read_text().strip()
            if t:
                return t
        A.ROOT.mkdir(parents=True, exist_ok=True)
        t = secrets.token_urlsafe(24)
        f.write_text(t)
        os.chmod(f, 0o600)
        return t
    except OSError:
        return secrets.token_urlsafe(24)


TOKEN = _token()


class Refuse(Exception):
    """Expected, reportable failure — becomes a 400 with a readable message."""


_OPEN = []


def _track(con):
    """Register a connection for close at end of request.

    Every handler used to open a connection and drop it on the floor. Under
    polling that exhausts the process file descriptor limit within minutes,
    which surfaces as Errno 24 and as bogus 'no usable temp directory' errors.
    """
    _OPEN.append(con)
    return con


def close_open():
    while _OPEN:
        try:
            _OPEN.pop().close()
        except Exception:  # noqa: BLE001
            pass


def use(handle):
    """Point the shared logic at one contact for the duration of a request."""
    if not handle or not handle.strip():
        raise Refuse("Enter a phone number or Apple ID first.")
    A.HANDLE = handle.strip()
    A._HANDLE_IDS = None
    try:
        return _track(A.connect())
    except SystemExit as e:
        raise Refuse(str(e) or "Can't read the Messages database. Check Full Disk Access.")


def require_two_way(con):
    """Never draft or send into a thread with no real back-and-forth."""
    if not A.handle_ids(con):
        raise Refuse(f"No conversation found for {A.HANDLE}.")
    msgs = A.fetch(con, limit=400)
    if not any(m["from_me"] for m in msgs) or not any(not m["from_me"] for m in msgs):
        raise Refuse(f"{A.HANDLE} has no two-way history. Check the number.")
    return msgs


def pack(msgs):
    return [{
        "id": m["rowid"],
        "mine": m["from_me"],
        "text": m["text"],
        "when": m["when"].strftime("%b %-d, %-I:%M %p"),
    } for m in msgs]


# ── endpoints ───────────────────────────────────────────────────────────────

def api_contacts(_):
    """Recent threads, so a new number can be picked rather than typed blind."""
    A.HANDLE = "+0"
    try:
        con = _track(A.connect())
    except SystemExit as e:
        raise Refuse(str(e) or "Can't read the Messages database.")
    rows = con.execute("""
        SELECT h.id AS handle, COUNT(*) AS n, MAX(m.date) AS last
        FROM message m JOIN handle h ON m.handle_id = h.ROWID
        GROUP BY h.id HAVING n > 10 ORDER BY last DESC LIMIT 25
    """).fetchall()
    known = {p.name for p in A.ROOT.glob("*") if p.is_dir()} if A.ROOT.exists() else set()
    saved = {x["handle"]: x["label"] for x in load_roster()}
    out = []
    for r in rows:
        h = r["handle"]
        out.append({
            "handle": h,
            "count": r["n"],
            "trained": "".join(c for c in h if c.isalnum()) in known,
            # A name you set yourself wins; a stored number does not.
            "name": (saved[h].strip() if is_custom(h, saved.get(h))
                     else (contact_name(h) or "")),
        })
    out.sort(key=lambda x: (not x["name"], x["name"].lower() or x["handle"]))
    return {"contacts": out,
            "named": sum(1 for x in out if x["name"]),
            "loaded": len(contact_map())}


def api_check(body):
    con = use(body.get("handle"))
    msgs = require_two_way(con)
    mine = sum(1 for m in msgs if m["from_me"])
    return {
        "handle": A.HANDLE,
        "name": display_name(A.HANDLE, {x["handle"]: x.get("label")
                                        for x in load_roster()}.get(A.HANDLE)),
        "total": len(msgs),
        "mine": mine,
        "thin": mine < 50,
        "hasStyle": A.style_file().exists(),
        "messages": pack(msgs[-14:]),
    }


def api_style_get(body):
    use(body.get("handle"))
    f = A.style_file()
    return {"profile": f.read_text() if f.exists() else ""}


def api_style_learn(body):
    con = use(body.get("handle"))
    require_two_way(con)
    broad = bool(body.get("broad"))
    mine = [m["text"] for m in A.fetch(con, limit=20000, everyone=broad)
            if m["from_me"]][-A.STYLE_SAMPLE:]
    if len(mine) < 20:
        raise Refuse(f"Only {len(mine)} messages from you. Try learning from all threads.")
    try:
        profile = A.call_api(
            system=("You analyze a person's texting style so it can be imitated in drafts "
                    "they will review before sending. Be concrete and specific — cite actual "
                    "patterns you observe, not generic advice. Output a compact markdown "
                    "style guide."),
            user=("Here are messages I sent. Describe how I write: typical length, "
                  "punctuation and capitalization habits, emoji use, how I open and close, "
                  "filler words and verbal tics, level of formality, how I handle questions "
                  "vs logistics vs jokes. Include 5-8 short verbatim examples.\n\n"
                  + "\n".join(f"- {t}" for t in mine)),
            max_tokens=2000,
        )
    except SystemExit as e:
        raise Refuse(str(e) or "API call failed.")
    A.style_file().write_text(profile)
    return {"profile": profile, "sampled": len(mine), "broad": broad}


def api_style_save(body):
    use(body.get("handle"))
    A.style_file().write_text(body.get("profile", ""))
    return {"ok": True}


def api_poll(body):
    con = use(body.get("handle"))
    since = body.get("since")
    if since is None:
        msgs = A.fetch(con, limit=A.CONTEXT_TURNS)
        top = con.execute("SELECT MAX(ROWID) m FROM message").fetchone()["m"] or 0
        return {"messages": pack(msgs), "cursor": top, "incoming": []}
    fresh = A.fetch(con, limit=50, after_rowid=int(since))
    cursor = max([m["rowid"] for m in fresh], default=int(since))
    top = con.execute("SELECT MAX(ROWID) m FROM message").fetchone()["m"] or cursor
    return {
        "messages": pack(fresh),
        "incoming": pack([m for m in fresh if not m["from_me"]]),
        "cursor": max(cursor, top),
    }


def api_draft(body):
    con = use(body.get("handle"))
    require_two_way(con)
    f = A.style_file()
    if not f.exists():
        raise Refuse("Learn this contact's voice first.")
    try:
        text = A.draft(A.fetch(con, limit=A.CONTEXT_TURNS), f.read_text())
    except SystemExit as e:
        raise Refuse(str(e) or "API call failed.")
    return {"draft": text, "risky": A.risky(text)}


AUTO_LOG = {}          # handle -> [timestamps]
AUTO_MIN_GAP = 25      # seconds between unattended sends
AUTO_PER_HOUR = 12     # ceiling per contact per hour


def check_auto_limits(handle):
    """Server-side ceiling. The browser enforces its own, but the send endpoint
    is the real gate, so the limit lives here too."""
    import time
    now = time.time()
    hits = [t for t in AUTO_LOG.get(handle, []) if now - t < 3600]
    if hits and now - hits[-1] < AUTO_MIN_GAP:
        raise Refuse(f"Slow down — {AUTO_MIN_GAP}s minimum between unattended sends.")
    if len(hits) >= AUTO_PER_HOUR:
        raise Refuse(f"Hourly cap reached ({AUTO_PER_HOUR} auto-sends). "
                     f"Switch to approving each one.")
    hits.append(now)
    AUTO_LOG[handle] = hits


def api_send(body):
    con = use(body.get("handle"))
    require_two_way(con)
    text = (body.get("text") or "").strip()
    if not text:
        raise Refuse("Nothing to send.")
    if body.get("auto"):
        check_auto_limits(A.HANDLE)
    if not A.send(text):
        raise Refuse("Messages refused the send. Is Messages open and signed in?")
    return {"sent": True}


ROSTER = lambda: A.ROOT / "roster.json"


def load_roster():
    try:
        return json.loads(ROSTER().read_text())
    except (OSError, ValueError):
        return []


def write_roster(items):
    A.ROOT.mkdir(parents=True, exist_ok=True)
    ROSTER().write_text(json.dumps(items, indent=2))


_NAMES = {"map": None, "sources": []}


def contact_map(force=False):
    """number -> name, built once from every Contacts database on this Mac.

    Scanning per lookup was slow and only covered iCloud-synced sources. Local
    contacts live in a database at the AddressBook root with no Sources dir.
    """
    if _NAMES["map"] is not None and not force:
        return _NAMES["map"]

    out, seen = {}, []
    root = Path.home() / "Library" / "Application Support" / "AddressBook"
    candidates = []
    try:
        candidates = sorted(set(list(root.glob("AddressBook-v22.abcddb")) +
                                list(root.glob("Sources/*/AddressBook-v22.abcddb")) +
                                list(root.glob("**/AddressBook-v22.abcddb"))))
    except OSError:
        pass

    for path in candidates:
        rows = []
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = con.execute("""
                SELECT COALESCE(NULLIF(TRIM(r.ZFIRSTNAME),''), r.ZNICKNAME),
                       r.ZLASTNAME, r.ZORGANIZATION, p.ZFULLNUMBER
                FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON r.Z_PK = p.ZOWNER
                WHERE p.ZFULLNUMBER IS NOT NULL
            """).fetchall()
            try:
                # iMessage handles are often an Apple ID rather than a number.
                rows += con.execute("""
                    SELECT COALESCE(NULLIF(TRIM(r.ZFIRSTNAME),''), r.ZNICKNAME),
                           r.ZLASTNAME, r.ZORGANIZATION, e.ZADDRESS
                    FROM ZABCDEMAILADDRESS e JOIN ZABCDRECORD r ON r.Z_PK = e.ZOWNER
                    WHERE e.ZADDRESS IS NOT NULL
                """).fetchall()
            except sqlite3.Error:
                pass
            con.close()
        except sqlite3.Error:
            seen.append({"path": str(path), "ok": False, "n": 0})
            continue
        n = 0
        for first, last, org, num in rows:
            raw = (num or "").strip()
            key = A.normalize(raw) if any(c.isdigit() for c in raw) else raw.lower()
            if not key:
                continue
            parts = [x.strip() for x in (first, last) if x and x.strip()]
            if len(parts) == 2 and parts[0].lower() == parts[1].lower():
                parts = parts[:1]
            name = " ".join(parts) or (org or "").strip()
            if name and key not in out:
                out[key] = name
                n += 1
        seen.append({"path": str(path), "ok": True, "n": n})

    _NAMES["map"], _NAMES["sources"] = out, seen
    return out


def contact_name(handle):
    """Best-effort. Contacts may be absent or schema-shifted; failure just means
    the user types their own nickname."""
    try:
        h = (handle or "").strip()
        key = A.normalize(h) if any(c.isdigit() for c in h) else h.lower()
        return contact_map().get(key) or None
    except Exception:  # noqa: BLE001
        return None


def api_names(body):
    """Diagnostics for the Contacts lookup, plus a way to force a rescan."""
    m = contact_map(force=bool(body.get("refresh")))
    h = (body.get("handle") or "").strip()
    return {
        "loaded": len(m),
        "sources": _NAMES["sources"],
        "match": contact_name(h) if h else None,
        "normalized": A.normalize(h) if h else None,
    }


def is_custom(handle, label):
    """A label that's just the number isn't a name.

    Early versions stored the handle as the label when Contacts lookup failed.
    Those stale labels would otherwise shadow the real name forever.
    """
    if not label:
        return False
    label = label.strip()
    if not label or label == handle:
        return False
    return A.normalize(label) != A.normalize(handle) or any(c.isalpha() for c in label)


def display_name(handle, label=None):
    if is_custom(handle, label):
        return label.strip()
    return contact_name(handle) or handle


def api_roster(_):
    items = load_roster()
    known = {p.name for p in A.ROOT.glob("*") if p.is_dir()} if A.ROOT.exists() else set()
    out = []
    for it in items:
        h = it["handle"]
        custom = is_custom(h, it.get("label"))
        out.append({
            "handle": h,
            "label": display_name(h, it.get("label")),
            "custom": custom,
            "pinned": bool(it.get("pinned")),
            "trained": "".join(c for c in h if c.isalnum()) in known,
        })
    out.sort(key=lambda x: x["label"].lower())
    return {"roster": out}


def api_roster_save(body):
    """Add or update one entry. Label falls back to Contacts, then the number."""
    handle = (body.get("handle") or "").strip()
    if not handle:
        raise Refuse("No number given.")
    items = [x for x in load_roster() if x["handle"] != handle]
    raw = (body.get("label") or "").strip()
    # Store nothing when it isn't a real name, so Contacts can fill it in later.
    entry = {"handle": handle, "pinned": bool(body.get("pinned"))}
    if is_custom(handle, raw):
        entry["label"] = raw[:40]
    items.append(entry)
    write_roster(items)
    return api_roster({})


def api_roster_remove(body):
    items = [x for x in load_roster() if x["handle"] != (body.get("handle") or "")]
    write_roster(items)
    return api_roster({})


def api_lookup(body):
    return {"name": contact_name(body.get("handle") or "")}


def preset_dir():
    d = A.ROOT / "presets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(n):
    n = "".join(c for c in (n or "").strip() if c.isalnum() or c in " -_").strip()
    if not n:
        raise Refuse("Give the preset a name.")
    return n[:48]


def api_presets(_):
    out = []
    for f in sorted(preset_dir().glob("*.md")):
        body = f.read_text()
        out.append({"name": f.stem, "chars": len(body),
                    "preview": body.strip().split("\n")[0][:60]})
    return {"presets": out}


def api_preset_save(body):
    """Snapshot a profile under a reusable name."""
    name = safe_name(body.get("name"))
    text = (body.get("profile") or "").strip()
    if len(text) < 40:
        raise Refuse("That profile looks empty — learn or write one first.")
    (preset_dir() / f"{name}.md").write_text(text)
    return {"saved": name}


def api_preset_apply(body):
    """Copy a preset onto a contact, so a new number skips the learning call."""
    use(body.get("handle"))
    f = preset_dir() / f"{safe_name(body.get('name'))}.md"
    if not f.exists():
        raise Refuse("No preset by that name.")
    text = f.read_text()
    A.style_file().write_text(text)
    return {"profile": text}


def api_preset_delete(body):
    f = preset_dir() / f"{safe_name(body.get('name'))}.md"
    if f.exists():
        f.unlink()
    return {"ok": True}


ROUTES = {
    "/api/roster": api_roster,
    "/api/roster/save": api_roster_save,
    "/api/roster/remove": api_roster_remove,
    "/api/lookup": api_lookup,
    "/api/names": api_names,
    "/api/presets": api_presets,
    "/api/presets/save": api_preset_save,
    "/api/presets/apply": api_preset_apply,
    "/api/presets/delete": api_preset_delete,
    "/api/contacts": api_contacts,
    "/api/check": api_check,
    "/api/style": api_style_get,
    "/api/style/learn": api_style_learn,
    "/api/style/save": api_style_save,
    "/api/poll": api_poll,
    "/api/draft": api_draft,
    "/api/send": api_send,
}


_UI_CACHE = {}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # the terminal is for status, not a request log

    def _send(self, code, payload, ctype="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self):
        if self.headers.get("X-Token") == TOKEN:
            return True
        q = urllib.parse.urlparse(self.path).query
        return urllib.parse.parse_qs(q).get("token", [None])[0] == TOKEN

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            if not self._authorized():
                return self._send(403, b"Open the URL printed in your terminal.", "text/plain")
            if not UI.exists():
                return self._send(500, b"imsg_ui.html is missing from this folder.", "text/plain")
            stamp = UI.stat().st_mtime
            if _UI_CACHE.get("stamp") != stamp:
                _UI_CACHE["stamp"], _UI_CACHE["body"] = stamp, UI.read_bytes()
            return self._send(200, _UI_CACHE["body"], "text/html; charset=utf-8")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # Read the body FIRST, always. Replying without draining it leaves the
        # bytes in the socket, and the next keep-alive request parses them as a
        # request line -- which surfaces as a confusing 501.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""

        if not self._authorized():
            return self._send(403, {"error": "STALE_TOKEN"})
        fn = ROUTES.get(path)
        if not fn:
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "Malformed request."})
        try:
            self._send(200, fn(body))
        except Refuse as e:
            self._send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001 — surface it rather than dying
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            close_open()


class Server(socketserver.TCPServer):
    allow_reuse_address = True


def raise_fd_limit():
    """Headroom in case anything else leaks."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, hard), hard))
    except Exception:  # noqa: BLE001
        pass


def main():
    raise_fd_limit()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("!  ANTHROPIC_API_KEY isn't set. Reading messages will work,")
        print("   but learning a voice and drafting will fail until you set it.\n")
    url = f"http://127.0.0.1:{PORT}/?token={TOKEN}"
    try:
        srv = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit(f"Port {PORT} is busy. Try:  IMSG_PORT=8766 python3 imsg_web.py")
    print("  Drafts  ·  running locally\n")
    print(f"  {url}\n")
    print("  This link is required and stays the same between restarts.")
    print("  Ctrl-C to stop.\n")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
