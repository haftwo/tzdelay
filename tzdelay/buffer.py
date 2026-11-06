"""Delay buffer: segment files on disk plus a SQLite index.

Every ingest type (HLS, sound card, Livewire) ends up calling
SegmentBuffer.add(), so everything downstream is input-agnostic.

Each segment gets a "timeline" timestamp (`ts`). Contiguous segments are
stamped back-to-back (previous ts + previous duration) so the delayed
output plays smoothly; after a gap the timeline re-syncs to wall clock and
the segment is flagged as a discontinuity.
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- our own output sequence
    ts         REAL    NOT NULL,                   -- timeline start (unix secs)
    duration   REAL    NOT NULL,
    filename   TEXT    NOT NULL,
    discont    INTEGER NOT NULL DEFAULT 0,
    map_file   TEXT,                               -- fMP4 init segment, if any
    source_key TEXT                                -- dedupe key from the ingest
);
CREATE INDEX IF NOT EXISTS idx_ts ON segments(ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source ON segments(source_key);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v REAL);
INSERT OR IGNORE INTO meta VALUES ('pruned_discont', 0);
"""

# If a new segment arrives more than this far from where the timeline
# expects it, treat it as a gap and re-sync to wall clock.
RESYNC_TOLERANCE = 10.0


class SegmentBuffer:
    def __init__(self, directory: str, retain_seconds: float):
        self.dir = directory
        self.retain = retain_seconds
        os.makedirs(self.dir, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(os.path.join(self.dir, "index.db"),
                                  check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    # ---------- writing ----------
    def has(self, source_key: str) -> bool:
        with self._lock:
            row = self.db.execute("SELECT 1 FROM segments WHERE source_key=?",
                                  (source_key,)).fetchone()
        return row is not None

    def add(self, data: bytes, duration: float, ext: str, source_key: str,
            map_file: str | None = None, discontinuity: bool = False,
            wallclock: float | None = None) -> None:
        """Store one segment. `wallclock` is when the END of this audio was
        live (best estimate from the ingest); defaults to now."""
        now = wallclock if wallclock is not None else time.time()
        with self._lock:
            last = self.db.execute(
                "SELECT ts, duration FROM segments ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            if last is None:
                ts, discontinuity = now - duration, True
            else:
                expected = last[0] + last[1]
                # Expected ts is (arrival - duration); compare against that.
                if discontinuity or abs((now - duration) - expected) > RESYNC_TOLERANCE:
                    ts, discontinuity = max(now - duration, expected), True
                else:
                    ts = expected
            name = f"{int(ts * 1000)}{ext}"
            tmp = os.path.join(self.dir, name + ".part")
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(self.dir, name))  # atomic
            self.db.execute(
                "INSERT INTO segments (ts, duration, filename, discont, map_file, source_key)"
                " VALUES (?,?,?,?,?,?)",
                (ts, duration, name, int(discontinuity), map_file, source_key))
            self.db.commit()

    def save_map(self, data: bytes, name: str) -> str:
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(data)
        return name

    def prune(self) -> int:
        """Delete segments older than the retention window."""
        cutoff = time.time() - self.retain
        with self._lock:
            rows = self.db.execute(
                "SELECT seq, filename FROM segments WHERE ts < ?", (cutoff,)).fetchall()
            pruned_discont = self.db.execute(
                "SELECT COUNT(*) FROM segments WHERE ts < ? AND discont=1",
                (cutoff,)).fetchone()[0]
            self.db.execute("UPDATE meta SET v = v + ? WHERE k='pruned_discont'",
                            (pruned_discont,))
            for seq, name in rows:
                try:
                    os.remove(os.path.join(self.dir, name))
                except FileNotFoundError:
                    pass
            self.db.execute("DELETE FROM segments WHERE ts < ?", (cutoff,))
            self.db.commit()
        return len(rows)

    # ---------- reading ----------
    def window(self, playhead: float, count: int):
        """The `count` segments ending at the playhead (now - delay)."""
        with self._lock:
            rows = self.db.execute(
                "SELECT seq, ts, duration, filename, discont, map_file FROM segments"
                " WHERE ts + duration <= ? ORDER BY seq DESC LIMIT ?",
                (playhead, count)).fetchall()
        return list(reversed(rows))

    def discontinuities_before(self, seq: int) -> int:
        """Needed for EXT-X-DISCONTINUITY-SEQUENCE in the output playlist."""
        with self._lock:
            live = self.db.execute(
                "SELECT COUNT(*) FROM segments WHERE discont=1 AND seq < ?",
                (seq,)).fetchone()[0]
            pruned = self.db.execute(
                "SELECT v FROM meta WHERE k='pruned_discont'").fetchone()[0]
            return live + int(pruned)

    def stats(self) -> dict:
        with self._lock:
            row = self.db.execute(
                "SELECT COUNT(*), MIN(ts), MAX(ts + duration),"
                " SUM(discont) FROM segments").fetchone()
        count, first, last, gaps = row
        return {"segments": count, "oldest": first, "newest": last,
                "depth_seconds": (last - first) if count else 0,
                "discontinuities": gaps or 0}
