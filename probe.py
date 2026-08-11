#!/usr/bin/env python3
"""Diagnose stale reads.  python3 probe.py +15551234567"""
import sqlite3, sys, time
from datetime import datetime
from pathlib import Path

DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE = 978307200
want = "".join(c for c in (sys.argv[1] if len(sys.argv) > 1 else "") if c.isdigit())[-10:]
if not want:
    sys.exit("Usage: python3 probe.py +15551234567")
# ── contacts diagnostic: python3 probe.py +15551234567 --contacts ────────────
def contacts_report(number):
    """Dump every Contacts row whose digits match, with all populated fields.

    Schema-agnostic on purpose: introspects columns rather than assuming, so a
    name living in an unexpected field still shows up.
    """
    import glob
    digits = "".join(c for c in number if c.isdigit())[-10:]
    root = Path.home() / "Library" / "Application Support" / "AddressBook"
    paths = sorted(set(glob.glob(str(root / "AddressBook-v22.abcddb")) +
                       glob.glob(str(root / "**" / "AddressBook-v22.abcddb"), recursive=True)))
    print(f"\nsearching {len(paths)} contacts database(s) for ...{digits}")
    if not paths:
        print("  none found — is Full Disk Access granted?")
        return
    hit = False
    for path in paths:
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            print(f"  cannot open {path}: {e}")
            continue
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        print(f"\n  {path}")
        print(f"    {len(tables)} tables")
        for tbl in tables:
            try:
                cols = [c[1] for c in con.execute(f"PRAGMA table_info({tbl})")]
            except sqlite3.Error:
                continue
            text_cols = [c for c in cols if c.upper().startswith("Z")]
            if not text_cols:
                continue
            where = " OR ".join(
                f"REPLACE(REPLACE(REPLACE(REPLACE(IFNULL({c},''),' ',''),'-',''),'(',''),')','') "
                f"LIKE '%{digits}%'" for c in text_cols)
            try:
                rows = con.execute(f"SELECT * FROM {tbl} WHERE {where} LIMIT 5").fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                hit = True
                print(f"    [{tbl}] " + ", ".join(
                    f"{k}={r[k]!r}" for k in r.keys() if r[k] not in (None, "", 0)))
                owner = r["ZOWNER"] if "ZOWNER" in r.keys() else None
                if owner:
                    try:
                        rec = con.execute(
                            "SELECT * FROM ZABCDRECORD WHERE Z_PK=?", (owner,)).fetchone()
                        if rec:
                            print("      -> owner record: " + ", ".join(
                                f"{k}={rec[k]!r}" for k in rec.keys()
                                if rec[k] not in (None, "", 0) and not k.startswith("Z_")))
                    except sqlite3.Error:
                        pass
        con.close()
    if not hit:
        print("\n  No row anywhere contains those digits.")
        print("  The contact likely lives in an iCloud record not cached locally.")
        print('  Use "+ name" in Browse to label it by hand.')



def contacts_summary():
    """What can we actually read? Paths, readability, row counts, samples."""
    import glob
    root = Path.home() / "Library" / "Application Support" / "AddressBook"
    print(f"\nAddressBook root: {root}")
    print(f"  exists: {root.exists()}")
    if root.exists():
        try:
            kids = sorted(p.name for p in root.iterdir())
            print(f"  contents: {', '.join(kids[:12])}{' …' if len(kids) > 12 else ''}")
        except OSError as e:
            print(f"  cannot list: {e}")

    paths = sorted(set(glob.glob(str(root / "**" / "*.abcddb"), recursive=True) +
                       glob.glob(str(root / "*.abcddb"))))
    print(f"\n{len(paths)} database file(s) found")
    total = 0
    for p in paths:
        size = Path(p).stat().st_size / 1e6
        rel = str(Path(p).relative_to(root)) if str(p).startswith(str(root)) else p
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            n_rec = con.execute("SELECT COUNT(*) c FROM ZABCDRECORD").fetchone()["c"]
            n_tel = con.execute("SELECT COUNT(*) c FROM ZABCDPHONENUMBER").fetchone()["c"]
            print(f"\n  {rel}  ({size:.1f} MB)")
            print(f"    records: {n_rec}   phone rows: {n_tel}")
            total += n_tel
            cols = [c[1] for c in con.execute("PRAGMA table_info(ZABCDRECORD)")]
            namecols = [c for c in cols if any(k in c.upper() for k in
                        ("NAME", "NICK", "ORGAN", "DISPLAY"))]
            print(f"    name-ish columns: {', '.join(namecols) or 'none'}")
            for r in con.execute("""
                SELECT r.ZFIRSTNAME f, r.ZLASTNAME l, p.ZFULLNUMBER n
                FROM ZABCDPHONENUMBER p JOIN ZABCDRECORD r ON r.Z_PK=p.ZOWNER LIMIT 3"""):
                print(f"      sample: {r['f']!r} {r['l']!r} -> {r['n']!r}")
            con.close()
        except sqlite3.Error as e:
            print(f"\n  {rel}  ({size:.1f} MB)")
            print(f"    UNREADABLE: {e}")
    print(f"\ntotal phone rows readable: {total}")
    if not total:
        print("  Nothing readable. Either Contacts data isn't cached locally,")
        print("  or the terminal lacks Full Disk Access.")


if "--contacts-summary" in sys.argv:
    contacts_summary()
    sys.exit(0)

if "--contacts" in sys.argv:
    contacts_report(sys.argv[1])
    sys.exit(0)



def when(v):
    return datetime.fromtimestamp(v / 1e9 + APPLE).strftime("%b %d %-I:%M:%S %p")


print(f"\ndb   {DB}")
for sfx in ("", "-wal", "-shm"):
    p = Path(str(DB) + sfx)
    if p.exists():
        age = time.time() - p.stat().st_mtime
        print(f"     chat.db{sfx:<5} {p.stat().st_size/1e6:8.1f} MB   modified {age:6.0f}s ago")
    else:
        print(f"     chat.db{sfx:<5} missing")

print("\nread paths")
results = {}
for name, uri in [("mode=ro (reads WAL)", f"file:{DB}?mode=ro"),
                  ("immutable=1 (ignores WAL)", f"file:{DB}?mode=ro&immutable=1")]:
    try:
        c = sqlite3.connect(uri, uri=True)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT MAX(ROWID) x, COUNT(*) n FROM message").fetchone()
        top = c.execute("SELECT ROWID, text, date, is_from_me FROM message "
                        "ORDER BY ROWID DESC LIMIT 1").fetchone()
        results[name] = (c, r["x"], r["n"])
        print(f"  OK   {name}")
        print(f"       {r['n']:,} messages, newest rowid {r['x']} at {when(top['date'])}")
    except Exception as e:
        print(f"  FAIL {name}: {type(e).__name__}: {e}")

if len(results) == 2:
    a = list(results.values())[0][1]
    b = list(results.values())[1][1]
    if a > b:
        print(f"\n  >> WAL holds {a-b} message(s) the immutable reader cannot see.")
        print("     If the app is stale, it is still running the old code. Restart it.")
    else:
        print("\n  >> Both readers agree; the WAL is checkpointed. Staleness is elsewhere.")

con = list(results.values())[0][0] if results else sys.exit("\nNo readable path. Check Full Disk Access.")
ids = [r["ROWID"] for r in con.execute("SELECT ROWID, id FROM handle")
       if "".join(c for c in r["id"] if c.isdigit())[-10:] == want]
print(f"\nhandle rows for ...{want}: {ids or 'NONE — number not found'}")
if not ids:
    sys.exit(1)

q = ",".join("?" * len(ids))
print("\nlast 6 in the 1:1 thread (what the app sees)")
for r in con.execute(f"""
    SELECT m.ROWID, m.text, m.attributedBody, m.is_from_me, m.date FROM message m
    WHERE m.handle_id IN ({q}) AND m.associated_message_guid IS NULL
      AND EXISTS (SELECT 1 FROM chat_message_join j JOIN chat c ON c.ROWID=j.chat_id
                  WHERE j.message_id=m.ROWID AND c.room_name IS NULL)
    ORDER BY m.ROWID DESC LIMIT 6""", ids):
    body = r["text"] or ("<attachment / no text>" if not r["attributedBody"] else "<encoded>")
    print(f"  {r['ROWID']:>7} {'me  ' if r['is_from_me'] else 'them'} {when(r['date'])}  {body[:52]}")

print("\nlast 6 from this handle ignoring the 1:1 filter")
for r in con.execute(f"""
    SELECT m.ROWID, m.text, m.is_from_me, m.date, c.room_name FROM message m
    LEFT JOIN chat_message_join j ON j.message_id=m.ROWID
    LEFT JOIN chat c ON c.ROWID=j.chat_id
    WHERE m.handle_id IN ({q}) ORDER BY m.ROWID DESC LIMIT 6""", ids):
    tag = "group" if r["room_name"] else ("1:1" if r["room_name"] is None else "?")
    print(f"  {r['ROWID']:>7} {'me  ' if r['is_from_me'] else 'them'} {when(r['date'])}  "
          f"[{tag}] {(r['text'] or '<no text>')[:44]}")
print()
