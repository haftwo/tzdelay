"""HLS ingest: polls a live .m3u8 and copies new segments into the buffer.

No re-encoding: segments are stored byte-for-byte, so the delayed output is
identical to the source.
"""
import hashlib
import time
import urllib.parse
import urllib.request

from .base import Ingest
from ..m3u8 import parse_media_playlist, parse_master_playlist

USER_AGENT = "tzdelay/0.1"


def fetch(url: str, timeout: float = 10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class HlsIngest(Ingest):
    last_seq = None    # newest media sequence number taken from the source
    joined = False     # set once the first segment from this source is in

    def run(self):
        url = self.options["url"]
        backoff = 1.0
        media_url = None
        while not self.stop_event.is_set():
            try:
                if media_url is None:
                    media_url = self.resolve_media_playlist(url)
                    self.log.info("[%s] using media playlist %s", self.channel_id, media_url)
                target = self.poll_once(media_url)
                backoff = 1.0
                # Poll at half the target duration (HLS spec guidance)
                self.stop_event.wait(max(1.0, target / 2))
            except Exception as e:  # network blips, 404s, bad playlists
                self.mark_error(f"{type(e).__name__}: {e}")
                media_url = None  # re-resolve in case the CDN moved us
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 30)

    def resolve_media_playlist(self, url: str) -> str:
        """If given a master playlist, pick one rendition."""
        text = fetch(url).decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" not in text:
            return url
        variants = parse_master_playlist(text, url)
        prefer = self.options.get("prefer_bandwidth", "highest")
        variants.sort(key=lambda v: v["bandwidth"])
        chosen = variants[-1] if prefer == "highest" else variants[0]
        return chosen["uri"]

    def poll_once(self, media_url: str) -> float:
        polled_at = time.time()
        pl = parse_media_playlist(fetch(media_url).decode("utf-8", "replace"), media_url)
        if pl["encrypted"]:
            raise RuntimeError("encrypted HLS (EXT-X-KEY) is not supported yet")

        seq_of = pl["media_sequence_of"]
        segs = pl["segments"]
        if not segs:
            return pl["target_duration"]
        newest = seq_of[segs[-1]["uri"]]
        hint = self.options.get("_continue_from_seq")
        if self.last_seq is None and hint is not None and hint - 10 <= newest <= hint + 10:
            # URL changed to a mirror of the same stream (same numbering):
            # carry on exactly where the previous source left off
            self.last_seq = hint
            self.joined = True
        if self.last_seq is None or newest < self.last_seq - 5:
            # First poll, or the source restarted its numbering: start near
            # the live edge rather than replaying the whole playlist window
            if self.last_seq is not None:
                self.mark_error("source sequence numbers reset; re-syncing")
            # New channel: take the newest 3 to get going. Joining a channel
            # that already has audio (URL changed, app restarted): take only
            # the newest, so the join doesn't repeat audio already buffered.
            take = 3 if self.buffer.stats()["segments"] == 0 else 1
            self.last_seq = seq_of[segs[max(0, len(segs) - take)]["uri"]] - 1
        # Only ever move forward - never backfill older segments
        segs = [s for s in segs if seq_of[s["uri"]] > self.last_seq]
        if not segs:
            return pl["target_duration"]

        # Estimate when each segment's audio ENDED being live. The last
        # segment in a live playlist ends at roughly "now"; earlier ones end
        # earlier by the durations of the segments after them. If the stream
        # carries EXT-X-PROGRAM-DATE-TIME we prefer that.
        remaining = 0.0
        for s in reversed(segs):
            s["est_end"] = polled_at - remaining
            remaining += s["duration"]

        # Key includes the source URL so a changed URL never collides
        src = hashlib.sha1(media_url.encode()).hexdigest()[:8]
        for s in segs:
            seq = seq_of[s["uri"]]
            key = f"{self.channel_id}:{src}:{seq}"
            if self.buffer.has(key):
                continue
            map_name = None
            if s.get("map_uri"):
                map_name = self.buffer.save_map(
                    fetch(s["map_uri"]), "init-" + hashlib.sha1(s["map_uri"].encode()).hexdigest()[:12] + ".mp4")
            data = fetch(s["uri"])
            wall = s["pdt"] + s["duration"] if s.get("pdt") else s["est_end"]
            ext = "." + urllib.parse.urlparse(s["uri"]).path.rsplit(".", 1)[-1][:5]
            self.buffer.add(data, s["duration"], ext, key, map_file=map_name,
                            # first segment from a (new) source is a join
                            discontinuity=s["discontinuity"] or not self.joined,
                            wallclock=wall)
            self.joined = True
            self.last_seq = seq
            self.mark_segment()
        return pl["target_duration"]
