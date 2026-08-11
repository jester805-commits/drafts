#!/usr/bin/env python3
"""
imsg_assist.py — single-contact iMessage draft-and-approve assistant.

Reads ~/Library/Messages/chat.db READ-ONLY, learns your writing style from your
own sent messages, drafts replies with the Anthropic API, and sends only after
you press a key.

Hard safety rails:
  * The database is opened read-only. Nothing is ever written to it.
  * One contact per run. Every read and every send is filtered to that handle.
  * No message is ever sent without an explicit keypress from you.

Usage:
    python3 imsg_assist.py doctor --to +1XXXXXXXXXX   # check access, preview thread
    python3 imsg_assist.py style  --to +1XXXXXXXXXX   # learn your voice (add --global if thin)
    python3 imsg_assist.py watch  --to +1XXXXXXXXXX   # draft-and-approve loop

Set IMSG_TO in your shell to skip --to. Style profiles are stored per contact,
so switching numbers never reuses the wrong person's voice.
"""

import json
import os
import sqlite3
import subprocess
import sys
import termios
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def prompt(msg):
    """Read a line, discarding anything typed before the prompt appeared.

    Without this, keys pressed while watch was idle sit in the buffer and get
    consumed by the next prompt — an 'a' typed minutes earlier could approve
    and send a draft the user never saw.
    """
    try:
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (termios.error, ValueError, OSError):
        pass  # not a tty (piped input) — nothing buffered to worry about
    return input(msg).strip()

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG — edit these two lines, that's it.
# ─────────────────────────────────────────────────────────────────────────────

# Optional default so you can omit --to. Leave as None to always pass it.
DEFAULT_HANDLE = os.environ.get("IMSG_TO") or None
MODEL = os.environ.get("IMSG_MODEL", "claude-sonnet-5")
# Provider is chosen at call time; see provider() / base_url().

HANDLE = None  # set at startup from --to / IMSG_TO / DEFAULT_HANDLE

# ─────────────────────────────────────────────────────────────────────────────

DB = Path.home() / "Library" / "Messages" / "chat.db"
ROOT = Path.home() / ".imsg_assist"
APPLE_EPOCH = 978307200  # 2001-01-01 in Unix seconds
POLL_SECONDS = 10
STYLE_SAMPLE = 400       # how many of your own messages to learn from
CONTEXT_TURNS = 20       # how much recent thread the drafter sees


# ── database ────────────────────────────────────────────────────────────────

_SNAP = {}


def _snapshot():
    """Copy the db plus its write-ahead log somewhere we can open normally.

    Uses ONE fixed path under ~/.imsg_assist, not a fresh temp dir per process.
    mkdtemp() per start leaked a full copy of chat.db on every restart, which
    fills the disk fast. Refreshed whenever the -wal changes so polling stays live.
    """
    import shutil
    d = ROOT / "snapshot"
    d.mkdir(parents=True, exist_ok=True)
    wal = Path(str(DB) + "-wal")
    stamp = (wal.stat().st_mtime if wal.exists() else 0, DB.stat().st_mtime)
    if _SNAP.get("stamp") != stamp:
        free = shutil.disk_usage(d).free
        need = DB.stat().st_size * 1.3
        if free < need:
            raise OSError(f"needs ~{need/1e9:.1f} GB free to copy the database, "
                          f"only {free/1e9:.1f} GB available")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(DB) + suffix)
            dst = d / ("chat.db" + suffix)
            if src.exists():
                shutil.copy2(src, dst)
            elif dst.exists():
                dst.unlink()  # stale log from a previous copy would corrupt the read
        _SNAP["stamp"] = stamp
    return d / "chat.db"


def _sweep_old_temp():
    """Remove snapshot dirs leaked by earlier versions.

    Entirely best-effort. gettempdir() itself raises when no temp directory is
    usable, so this must never be allowed to take down connect().
    """
    try:
        import glob
        import shutil
        import tempfile
        for p in glob.glob(str(Path(tempfile.gettempdir()) / "imsg_*")):
            shutil.rmtree(p, ignore_errors=True)
    except Exception:  # noqa: BLE001 — cleanup is optional, connecting is not
        pass


_SWEPT = False


def connect():
    global _SWEPT
    if not _SWEPT:
        _SWEPT = True
        _sweep_old_temp()
    if not DB.exists():
        die(f"No Messages database at {DB}. Is this a Mac with Messages set up?")

    # Plain read-only first: it reads the write-ahead log, so it sees messages
    # that arrived seconds ago. immutable=1 would ignore the WAL and quietly
    # serve a stale view — the reason live watching appears to do nothing.
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        con.execute("SELECT COUNT(*) FROM message").fetchone()
        return con
    except sqlite3.OperationalError:
        pass

    try:
        con = sqlite3.connect(_snapshot())
        con.row_factory = sqlite3.Row
        con.execute("SELECT COUNT(*) FROM message").fetchone()
        return con
    except OSError as e:
        import shutil
        free = shutil.disk_usage(Path.home()).free / 1e9
        die(f"Couldn't copy the Messages database: {e}\n"
            f"  Free space on this disk: {free:.1f} GB\n"
            f"  If that's low, clear space and retry. Old copies from earlier\n"
            f"  versions may be left in $TMPDIR — remove them with:\n"
            f"      rm -rf $TMPDIR/imsg_*")
    except sqlite3.OperationalError as e:
        die(f"Could not read the Messages database: {e}\n"
            f"  Grant Full Disk Access to your terminal in\n"
            f"  System Settings > Privacy & Security > Full Disk Access,\n"
            f"  then fully quit and reopen Terminal.")


def decode_body(text, blob):
    """Since Ventura, message text often lives in attributedBody as a typedstream."""
    if text:
        return text
    if not blob:
        return None
    idx = blob.find(b"NSString")
    if idx == -1:
        return None
    p = blob.find(b"\x2b", idx)  # '+' precedes the length-prefixed string
    if p == -1:
        return None
    p += 1
    if p >= len(blob):
        return None
    n = blob[p]
    p += 1
    if n == 0x81:
        n = int.from_bytes(blob[p:p + 2], "little"); p += 2
    elif n == 0x82:
        n = int.from_bytes(blob[p:p + 4], "little"); p += 4
    chunk = blob[p:p + n]
    if not chunk:
        return None
    return chunk.decode("utf-8", errors="replace")


def normalize(h):
    return "".join(c for c in (h or "") if c.isdigit() or c == "+")[-10:]


def state_dir():
    """Per-contact state. Shared state would leak one person's voice into another's drafts."""
    slug = "".join(c for c in HANDLE if c.isalnum()) or "unknown"
    d = ROOT / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def style_file():
    return state_dir() / "style.md"


def seen_file():
    return state_dir() / "last_seen.txt"


_HANDLE_IDS = None


def handle_ids(con):
    """ROWIDs in the handle table matching HANDLE (Apple stores several formats)."""
    global _HANDLE_IDS
    if _HANDLE_IDS is None:
        want = normalize(HANDLE)
        _HANDLE_IDS = [r["ROWID"] for r in con.execute("SELECT ROWID, id FROM handle")
                       if normalize(r["id"]) == want]
    return _HANDLE_IDS


def fetch(con, limit=200, after_rowid=None, everyone=False):
    """Messages for HANDLE's 1:1 thread only, oldest-first.

    Filtering happens in SQL. Doing it in Python meant a busy inbox could push
    the whole thread out of the fetched window.
    """
    where = ["m.associated_message_guid IS NULL"]
    args = []

    if not everyone:
        ids = handle_ids(con)
        if not ids:
            return []
        # Restrict to the direct thread: excludes group chats this person is in,
        # so we never draft a group reply and send it as a DM.
        where.append(f"""m.handle_id IN ({','.join('?' * len(ids))})
            AND EXISTS (
                SELECT 1 FROM chat_message_join cmj
                JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE cmj.message_id = m.ROWID AND c.room_name IS NULL
            )""")
        args.extend(ids)

    if after_rowid is not None:
        where.append("m.ROWID > ?")
        args.append(after_rowid)

    sql = f"""
        SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date
        FROM message m
        WHERE {' AND '.join(where)}
        ORDER BY m.ROWID DESC LIMIT ?
    """
    args.append(limit)

    out = []
    for r in con.execute(sql, args):
        body = decode_body(r["text"], r["attributedBody"])
        if not body or not body.strip():
            continue
        out.append({
            "rowid": r["ROWID"],
            "from_me": bool(r["is_from_me"]),
            "text": body.strip(),
            "when": datetime.fromtimestamp(
                r["date"] / 1e9 + APPLE_EPOCH, tz=timezone.utc
            ).astimezone(),
        })
    return list(reversed(out))


def transcript(msgs):
    return "\n".join(
        f"[{m['when']:%b %d %-I:%M %p}] {'Me' if m['from_me'] else 'Them'}: {m['text']}"
        for m in msgs
    )


# ── anthropic api ───────────────────────────────────────────────────────────

def provider():
    """anthropic (default) or openai. Most providers speak the OpenAI shape."""
    p = os.environ.get("IMSG_PROVIDER", "").strip().lower()
    if p in ("anthropic", "openai"):
        return p
    # An explicit key is the strongest hint. A custom base URL alone usually
    # means an OpenAI-compatible server, but must not override an Anthropic key.
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("IMSG_BASE_URL"):
        return "openai"
    return "anthropic"


def api_key(kind):
    """IMSG_API_KEY wins, then the provider's usual variable."""
    for var in ("IMSG_API_KEY",
                "ANTHROPIC_API_KEY" if kind == "anthropic" else "OPENAI_API_KEY"):
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    return None


def base_url(kind):
    b = os.environ.get("IMSG_BASE_URL", "").strip().rstrip("/")
    if b:
        return b
    return ("https://api.anthropic.com" if kind == "anthropic"
            else "https://api.openai.com")


def _is_local(url):
    return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"))


def call_api(system, user, max_tokens=1000):
    kind = provider()
    url = base_url(kind)
    key = api_key(kind)

    # Local servers (Ollama, LM Studio, llama.cpp) usually need no key.
    if not key and not _is_local(url):
        want = "ANTHROPIC_API_KEY" if kind == "anthropic" else "OPENAI_API_KEY"
        die(f"No API key found. Set one:  export {want}=...\n"
            f"  (provider={kind}, endpoint={url})")

    if kind == "anthropic":
        endpoint = f"{url}/v1/messages"
        payload = {
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": key or "",
            "anthropic-version": "2023-06-01",
        }
    else:
        endpoint = f"{url}/v1/chat/completions"
        payload = {
            "model": MODEL,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"

    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(),
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        die(f"API error {e.code} from {endpoint}: {e.read().decode()[:400]}")
    except urllib.error.URLError as e:
        die(f"Network error reaching {endpoint}: {e.reason}")

    try:
        if kind == "anthropic":
            text = "".join(b.get("text", "") for b in data.get("content", []))
        else:
            text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        die(f"Unexpected response shape from {endpoint}: {json.dumps(data)[:300]}")
    return text.strip()


# ── sending ─────────────────────────────────────────────────────────────────

SEND_SCRIPT = """on run {targetHandle, msgText}
    tell application "Messages"
        set svc to 1st service whose service type = iMessage
        send msgText to buddy targetHandle of svc
    end tell
end run"""


def send(text):
    """Send to HANDLE and nowhere else. Text passed as argv, never interpolated."""
    r = subprocess.run(
        ["osascript", "-e", SEND_SCRIPT, HANDLE, text],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  ✗ Send failed: {r.stderr.strip()}")
        return False
    return True


# ── commands ────────────────────────────────────────────────────────────────

def cmd_doctor():
    con = connect()
    total = con.execute("SELECT COUNT(*) c FROM message").fetchone()["c"]
    print(f"✓ Database readable — {total:,} messages total\n")

    want = normalize(HANDLE)
    print(f"Looking for handle matching {HANDLE} ...")
    hits = [r["id"] for r in con.execute("SELECT DISTINCT id FROM handle")
            if normalize(r["id"]) == want]
    if not hits:
        print(f"✗ No conversation found for {HANDLE}.")
        print("\n  Handles with recent activity, so you can copy the exact format:")
        for r in con.execute("""
            SELECT h.id, COUNT(*) c FROM message m JOIN handle h ON m.handle_id=h.ROWID
            GROUP BY h.id ORDER BY MAX(m.date) DESC LIMIT 15
        """):
            print(f"    {r['id']:<32} {r['c']:>6} messages")
        return

    print(f"✓ Matched: {', '.join(hits)}")
    msgs = fetch(con, limit=8)
    mine = sum(1 for m in fetch(con, limit=2000) if m["from_me"])
    print(f"✓ {mine} messages from you in this thread"
          f"{' — plenty to learn from' if mine >= 50 else ' — thin, style may be rough'}\n")
    print("Last few messages:")
    print(transcript(msgs))


def cmd_style():
    con = connect()
    broad = "--global" in sys.argv
    mine = [m["text"] for m in fetch(con, limit=20000, everyone=broad)
            if m["from_me"]][-STYLE_SAMPLE:]
    if len(mine) < 20:
        die(f"Only {len(mine)} messages from you. Try:  python3 imsg_assist.py style --global")

    scope = "all your conversations" if broad else "this thread"
    print(f"Analyzing {len(mine)} of your messages from {scope} ...")
    profile = call_api(
        system=(
            "You analyze a person's texting style so it can be imitated in drafts they "
            "will review before sending. Be concrete and specific — cite actual patterns "
            "you observe, not generic advice. Output a compact markdown style guide."
        ),
        user=(
            "Here are messages I sent to one person. Describe how I write to them: "
            "typical message length, punctuation and capitalization habits, emoji use, "
            "how I open and close, filler words and verbal tics, level of formality, "
            "how I handle questions vs. logistics vs. jokes. Include 5-8 short verbatim "
            "examples that are representative.\n\n" + "\n".join(f"- {t}" for t in mine)
        ),
        max_tokens=1600,
    )
    style_file().write_text(profile)
    print(f"\n{profile}\n")
    print(f"✓ Saved to {style_file()} — edit it by hand anytime to correct the drafts.")


RISK_PATTERNS = [
    (r"\b\d{1,2}:\d{2}\s*(am|pm)?\b|\b\d{1,2}\s*(am|pm)\b", "a specific time"),
    (r"\b(mon|tue|wed|thu|fri|sat|sun)(day|s)?\b|\btomorrow\b|\btonight\b|\bnext week\b",
     "a day or plan"),
    (r"\$\s?\d|\b\d+\s?(k|dollars|bucks)\b", "an amount of money"),
    (r"\b\d{3,}\b", "a specific number"),
    (r"\b(i'?ll|i will|i can|i'?m down|sounds good|see you|see u|deal|yes|yeah sure|"
     r"count me in|on my way|omw)\b", "a commitment"),
    (r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", "a phone number"),
    (r"\b\d+\s+[A-Z][a-z]+\s+(st|street|ave|avenue|rd|road|blvd|dr|drive)\b", "an address"),
]


def risky(text):
    """Reasons a draft shouldn't be sent unreviewed.

    Auto-send is fine for banter and bad for anything the model could invent:
    times, plans, numbers, and agreements. These get held for a human.
    """
    import re
    found = []
    for pattern, why in RISK_PATTERNS:
        if re.search(pattern, text, re.I):
            found.append(why)
    return found


def draft(msgs, style):
    return call_api(
        system=(
            "You draft text-message replies that the user will review, edit, or reject "
            "before anything is sent. Match their style precisely.\n\n"
            "Rules: output ONLY the message body, no preamble, no quotes, no options. "
            "Match their typical length — usually short. Never invent facts, "
            "commitments, times, or plans that aren't already in the thread; if a reply "
            "would require information you don't have, write a brief holding reply "
            "instead.\n\n=== THEIR STYLE ===\n" + style
        ),
        user=(
            "Recent thread (most recent last). Draft my next reply.\n\n"
            + transcript(msgs)
        ),
        max_tokens=400,
    )


def cmd_watch():
    if not style_file().exists():
        die("No style profile yet. Run:  python3 imsg_assist.py style")
    style = style_file().read_text()
    con = connect()

    # Start from the newest row in the whole database, not the thread. If the
    # thread is quiet, anchoring to its last message would replay old messages.
    last = con.execute("SELECT MAX(ROWID) m FROM message").fetchone()["m"] or 0
    if seen_file().exists():
        last = max(last, int(seen_file().read_text().strip() or 0))

    recent = verify_target(con)
    print(f"\nTarget: {HANDLE}")
    print(f"Last message — {recent[-1]['when']:%b %d %-I:%M %p} "
          f"{'you' if recent[-1]['from_me'] else 'them'}: {recent[-1]['text'][:60]}")
    if prompt("\nDraft replies to this person? [y/N] > ").lower() != "y":
        print("Cancelled.")
        return
    print(f"\nWatching {HANDLE}. Nothing sends without your approval. Ctrl-C to stop.\n")
    try:
        while True:
            fresh = [m for m in fetch(con, limit=50, after_rowid=last) if not m["from_me"]]
            if fresh:
                last = max(m["rowid"] for m in fresh)
                seen_file().write_text(str(last))
                for m in fresh:
                    print(f"\n← [{m['when']:%-I:%M %p}] {m['text']}")

                context = fetch(con, limit=CONTEXT_TURNS)
                text = draft(context, style)

                while True:
                    print(f"\n  Draft: {text}")
                    choice = prompt("  [a]pprove  [e]dit  [r]egenerate  [s]kip  [q]uit > ").lower()
                    if choice == "a":
                        if send(text):
                            print("  ✓ Sent")
                        break
                    if choice == "e":
                        edited = prompt("  Your version: ")
                        if edited:
                            text = edited
                        continue
                    if choice == "r":
                        text = draft(context, style)
                        continue
                    if choice == "s":
                        print("  — skipped")
                        break
                    if choice == "q":
                        return
            else:
                # advance the cursor past your own messages so they don't queue up
                own = fetch(con, limit=50, after_rowid=last)
                if own:
                    last = max(m["rowid"] for m in own)
                    seen_file().write_text(str(last))
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nStopped.")


def die(msg):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def parse_handle(argv):
    """--to NUMBER, else IMSG_TO env var, else DEFAULT_HANDLE."""
    if "--to" in argv:
        i = argv.index("--to")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            die("--to needs a number, e.g.  --to +15551234567")
        return argv[i + 1]
    if DEFAULT_HANDLE:
        return DEFAULT_HANDLE
    die("Who to? Pass  --to +1XXXXXXXXXX  (or set IMSG_TO in your shell).")


def verify_target(con):
    """Refuse to operate on a number with no real conversation behind it.

    The flag makes a typo cheap, and a typo that still resolves would mean
    drafting to a stranger. Prior two-way history is the cheapest proof
    that this is a thread the user actually has.
    """
    if not handle_ids(con):
        print(f"✗ No conversation found for {HANDLE}.\n")
        print("  Recent handles — copy one of these exactly:")
        for r in con.execute("""
            SELECT h.id, COUNT(*) c FROM message m JOIN handle h ON m.handle_id=h.ROWID
            GROUP BY h.id ORDER BY MAX(m.date) DESC LIMIT 15
        """):
            print(f"    {r['id']:<32} {r['c']:>6} messages")
        sys.exit(1)
    msgs = fetch(con, limit=400)
    if not any(m["from_me"] for m in msgs) or not any(not m["from_me"] for m in msgs):
        die(f"{HANDLE} has no two-way history. Check the number before drafting to it.")
    return msgs


if __name__ == "__main__":
    cmds = {"doctor": cmd_doctor, "style": cmd_style, "watch": cmd_watch}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print(__doc__)
        sys.exit(1)
    HANDLE = parse_handle(sys.argv)
    cmds[sys.argv[1]]()
