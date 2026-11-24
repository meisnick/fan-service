#!/usr/bin/env python3
"""SQLite history store for fanctl: raw 10 s samples + 1-minute and 15-minute rollups (avg/min/max).

raw  : 7 days      m1 : 30 days      m15 : 2 years   (sizes ~ 50 / 60 / 90 MB with ~80 series)
WAL mode: the controller writes, HTTP threads read with their own connections."""
import glob, json, os, sqlite3, time

RET = {"raw": 7 * 86400, "m1": 30 * 86400, "m15": 730 * 86400}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS raw (s INTEGER NOT NULL, t INTEGER NOT NULL, v REAL NOT NULL, PRIMARY KEY (s, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS m1  (s INTEGER NOT NULL, t INTEGER NOT NULL, a REAL, lo REAL, hi REAL, PRIMARY KEY (s, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS m15 (s INTEGER NOT NULL, t INTEGER NOT NULL, a REAL, lo REAL, hi REAL, PRIMARY KEY (s, t)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS raw_t ON raw (t);
CREATE INDEX IF NOT EXISTS m1_t ON m1 (t);
CREATE INDEX IF NOT EXISTS m15_t ON m15 (t);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class HistDB:
    def __init__(self, path, own=None):
        self.path = path
        new = not os.path.exists(path)
        c = sqlite3.connect(path, timeout=30)
        if new:
            c.execute("PRAGMA auto_vacuum=INCREMENTAL")
        c.executescript(SCHEMA)
        c.execute("PRAGMA synchronous=NORMAL")
        c.commit()
        self.w = c                      # writer connection (controller thread only)
        self.ids = {n: i for i, n in c.execute("SELECT id, name FROM series")}
        self.new = new
        if own:
            for p in (path, path + "-wal", path + "-shm"):
                if os.path.exists(p):
                    own(p)
        self.last_clean = 0

    def sid(self, name):
        i = self.ids.get(name)
        if i is None:
            cur = self.w.execute("INSERT OR IGNORE INTO series (name) VALUES (?)", (name,))
            i = self.w.execute("SELECT id FROM series WHERE name=?", (name,)).fetchone()[0]
            self.ids[name] = i
        return i

    def meta(self, k, default=None):
        r = self.w.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def set_meta(self, k, v):
        self.w.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))

    def add(self, t, values):
        """values: {series_name: float}"""
        rows = [(self.sid(k), int(t), float(v)) for k, v in values.items() if isinstance(v, (int, float))]
        self.w.executemany("INSERT OR REPLACE INTO raw (s, t, v) VALUES (?, ?, ?)", rows)
        self.w.commit()

    def import_jsonl(self, folder):
        n = 0
        for f in sorted(glob.glob(os.path.join(folder, "*.jsonl"))):
            with open(f) as h:
                for line in h:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    vals = dict(r.get("s") or {})
                    for z, d in (r.get("d") or {}).items():
                        vals["duty%s" % z] = d
                    rows = [(self.sid(k), int(r["t"]), float(v)) for k, v in vals.items() if isinstance(v, (int, float))]
                    self.w.executemany("INSERT OR IGNORE INTO raw (s, t, v) VALUES (?, ?, ?)", rows)
                    n += 1
        self.w.commit()
        return n

    def rollup(self, now=None):
        """Aggregate closed minutes raw->m1 and closed quarter-hours m1->m15 (incremental, idempotent)."""
        now = int(now or time.time())
        start = int(self.meta("m1_done", 0) or 0)
        if not start:
            r = self.w.execute("SELECT MIN(t) FROM raw").fetchone()[0]
            start = (r // 60) * 60 if r else (now // 60) * 60
        end = (now // 60) * 60
        if end > start:
            self.w.execute("""INSERT OR REPLACE INTO m1 (s, t, a, lo, hi)
                              SELECT s, (t / 60) * 60, AVG(v), MIN(v), MAX(v) FROM raw
                              WHERE t >= ? AND t < ? GROUP BY s, t / 60""", (start, end))
            self.set_meta("m1_done", end)
        start = int(self.meta("m15_done", 0) or 0)
        if not start:
            r = self.w.execute("SELECT MIN(t) FROM m1").fetchone()[0]
            start = (r // 900) * 900 if r else (now // 900) * 900
        end = (now // 900) * 900
        if end > start:
            self.w.execute("""INSERT OR REPLACE INTO m15 (s, t, a, lo, hi)
                              SELECT s, (t / 900) * 900, AVG(a), MIN(lo), MAX(hi) FROM m1
                              WHERE t >= ? AND t < ? GROUP BY s, t / 900""", (start, end))
            self.set_meta("m15_done", end)
        self.w.commit()
        if now - self.last_clean >= 3600:
            for tbl, keep in RET.items():
                self.w.execute("DELETE FROM %s WHERE t < ?" % tbl, (now - keep,))
            self.w.commit()
            self.w.execute("PRAGMA incremental_vacuum(2000)")
            self.w.execute("PRAGMA wal_checkpoint(PASSIVE)")
            self.last_clean = now

    def stats(self):
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=10)
        try:
            out = {"series": c.execute("SELECT COUNT(*) FROM series").fetchone()[0],
                   "bytes": os.path.getsize(self.path) + (os.path.getsize(self.path + "-wal") if os.path.exists(self.path + "-wal") else 0)}
            for tbl in RET:
                n, lo, hi = c.execute("SELECT COUNT(*), MIN(t), MAX(t) FROM %s" % tbl).fetchone()
                out[tbl] = {"rows": n, "from": lo, "to": hi}
            return out
        finally:
            c.close()

    def query(self, minutes, names=None, points=720):
        """-> {"res": tier, "step": s, "rows": [{"t", "s": {name: avg}, "x": {name: max}}]} ~points rows."""
        now = int(time.time())
        since = now - int(minutes) * 60
        span = now - since
        tier = "raw" if span <= 2 * 86400 else ("m1" if span <= 30 * 86400 else "m15")
        base = {"raw": 10, "m1": 60, "m15": 900}[tier]
        step = max(base, (span // points // base) * base or base)
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=10)
        try:
            ids = {i: n for i, n in c.execute("SELECT id, name FROM series")}
            if names:
                want = [i for i, n in ids.items() if n in set(names)]
            else:
                want = list(ids)
            if not want:
                return {"res": tier, "step": step, "rows": []}
            q = ",".join("?" * len(want))
            if tier == "raw":
                sql = ("SELECT (t / ?) * ? AS b, s, AVG(v), MAX(v) FROM raw WHERE t >= ? AND s IN (%s) GROUP BY b, s ORDER BY b" % q)
            else:
                sql = ("SELECT (t / ?) * ? AS b, s, AVG(a), MAX(hi) FROM %s WHERE t >= ? AND s IN (%s) GROUP BY b, s ORDER BY b" % (tier, q))
            rows, cur = [], None
            for b, s, a, mx in c.execute(sql, [step, step, since] + want):
                if cur is None or cur["t"] != b:
                    cur = {"t": b, "s": {}, "x": {}}
                    rows.append(cur)
                n = ids[s]
                cur["s"][n] = round(a, 2)
                cur["x"][n] = round(mx, 2)
            for r in rows:                      # back-compat: duties as "d"
                r["d"] = {z: r["s"].pop("duty" + z) for z in ("0", "1") if "duty" + z in r["s"]}
                r["x"].pop("duty0", None)
                r["x"].pop("duty1", None)
            return {"res": tier, "step": step, "rows": rows}
        finally:
            c.close()

    def names(self):
        c = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=10)
        try:
            return sorted(n for (n,) in c.execute("SELECT name FROM series"))
        finally:
            c.close()
