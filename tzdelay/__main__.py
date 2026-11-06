"""Entry point: python -m tzdelay --config config.json

config.json holds server settings. Channels are managed in the web
interface and saved to <data_dir>/channels.json. On first run, any channels
listed in config.json are used to seed that file.
"""
import argparse
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.request

from .buffer import SegmentBuffer
from .ingest import INGEST_TYPES
from .m3u8 import parse_master_playlist
from .server import serve

# Keep this much extra audio beyond the delay, so a delay increase or a
# quick restart doesn't immediately run out of buffer.
RETAIN_MARGIN = 15 * 60
MAX_DELAY = 48 * 3600


def parse_delay(value) -> float:
    """Accept seconds (10800) or "H:MM:SS" / "H:MM" strings."""
    if isinstance(value, (int, float)):
        return float(value)
    parts = [float(p) for p in str(value).split(":")]
    while len(parts) < 3:
        parts.append(0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def check_hls_url(url: str) -> None:
    """Fetch the URL once so a typo is caught when adding, not hours later."""
    if not re.match(r"^https?://", url or ""):
        raise ValueError("URL must start with http:// or https://")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tzdelay/0.1"})
        with urllib.request.urlopen(req, timeout=8) as r:
            text = r.read(200_000).decode("utf-8", "replace")
    except Exception as e:
        raise ValueError(f"could not fetch the URL ({e})")
    if "#EXTM3U" not in text:
        raise ValueError("that URL didn't return an HLS playlist (.m3u8)")
    if "#EXT-X-STREAM-INF" in text and not parse_master_playlist(text, url):
        raise ValueError("master playlist has no streams")


class Channel:
    def __init__(self, cfg, data_dir, log):
        self.cfg = cfg
        self.id = cfg["id"]
        self.name = cfg.get("name", self.id)
        self.delay = parse_delay(cfg["delay"])
        self.log = log
        self.buffer = SegmentBuffer(os.path.join(data_dir, self.id),
                                    retain_seconds=self.delay + RETAIN_MARGIN)
        self.ingest = None
        self.start_ingest()

    @property
    def ingest_type(self):
        return self.cfg["ingest"]["type"]

    def start_ingest(self, continue_from_seq=None):
        ing = dict(self.cfg["ingest"])
        if continue_from_seq is not None:
            ing["_continue_from_seq"] = continue_from_seq
        self.ingest = INGEST_TYPES[ing["type"]](self.id, self.buffer, ing, self.log)
        self.ingest.start()

    def stop_ingest(self):
        if self.ingest:
            self.ingest.stop()
            self.ingest.join(timeout=10)


class App:
    def __init__(self, config_path):
        # The config file is optional: with none, run on defaults and manage
        # everything in the web page. Environment variables override it
        # (handy for Docker / Unraid).
        try:
            with open(config_path) as f:
                self.cfg = json.load(f)
            found = True
        except FileNotFoundError:
            self.cfg, found = {}, False
        for key, env in (("data_dir", "TZDELAY_DATA_DIR"), ("port", "TZDELAY_PORT"),
                         ("log_level", "TZDELAY_LOG_LEVEL")):
            if os.environ.get(env):
                self.cfg[key] = os.environ[env]
        logging.basicConfig(level=self.cfg.get("log_level", "INFO"),
                            format="%(asctime)s %(levelname)s %(message)s")
        self.log = logging.getLogger("tzdelay")
        if not found:
            self.log.info("no config file at %s; using defaults", config_path)
        self.data_dir = self.cfg.get("data_dir", "./data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.channels_path = os.path.join(self.data_dir, "channels.json")
        self.lock = threading.RLock()
        self.channels: dict[str, Channel] = {}
        for c in self.load_channel_configs():
            self.channels[c["id"]] = Channel(c, self.data_dir, self.log)
        self.save()

    def load_channel_configs(self) -> list[dict]:
        try:
            with open(self.channels_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            # First run: seed from config.json, honouring delays changed by
            # the older state.json if present
            seeds = [dict(c) for c in self.cfg.get("channels", [])]
            try:
                with open(os.path.join(self.data_dir, "state.json")) as f:
                    state = json.load(f)
                for c in seeds:
                    c["delay"] = state.get(c["id"], {}).get("delay", c["delay"])
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            return seeds

    def save(self):
        with self.lock:
            data = [dict(ch.cfg, delay=ch.delay, name=ch.name)
                    for ch in self.channels.values()]
        tmp = self.channels_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.channels_path)  # survives restarts

    def list_channels(self) -> list[Channel]:
        with self.lock:
            return list(self.channels.values())

    # ---------- changes from the web interface ----------
    @staticmethod
    def _validate(name, delay):
        if not (name or "").strip():
            raise ValueError("give the channel a name")
        if not 0 <= delay <= MAX_DELAY:
            raise ValueError("delay must be between 0 and 48 hours")

    def _new_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "channel"
        cid, n = base, 2
        while cid in self.channels or os.path.exists(os.path.join(self.data_dir, cid)):
            cid, n = f"{base}-{n}", n + 1
        return cid

    def add_channel(self, name: str, url: str, delay: float) -> Channel:
        self._validate(name, delay)
        check_hls_url(url)
        with self.lock:
            cfg = {"id": self._new_id(name), "name": name.strip(), "delay": delay,
                   "ingest": {"type": "hls", "url": url.strip()}}
            ch = Channel(cfg, self.data_dir, self.log)
            self.channels[ch.id] = ch
        self.log.info("[%s] added: %s, delay %ss", ch.id, url, delay)
        self.save()
        return ch

    def update_channel(self, cid: str, name=None, url=None, delay=None) -> Channel:
        ch = self.channels[cid]
        new_name = ch.name if name is None else name
        new_delay = ch.delay if delay is None else float(delay)
        self._validate(new_name, new_delay)
        url_changed = (url is not None and ch.ingest_type == "hls"
                       and url.strip() != ch.cfg["ingest"].get("url"))
        if url_changed:
            check_hls_url(url)
        with self.lock:
            ch.name = new_name.strip()
            if new_delay != ch.delay:
                # Shorter = audio jumps forward; longer = a section repeats
                # (or the output waits until the buffer catches up).
                self.log.info("[%s] delay %s -> %s seconds", cid, ch.delay, new_delay)
                ch.delay = new_delay
                ch.buffer.retain = new_delay + RETAIN_MARGIN
            if url_changed:
                # Keep the buffered audio; the new source starts after a
                # discontinuity so players handle the join cleanly.
                self.log.info("[%s] source -> %s", cid, url)
                ch.stop_ingest()
                ch.cfg["ingest"]["url"] = url.strip()
                ch.start_ingest(continue_from_seq=getattr(ch.ingest, "last_seq", None))
        self.save()
        return ch

    def remove_channel(self, cid: str):
        with self.lock:
            ch = self.channels.pop(cid)
        ch.stop_ingest()
        ch.buffer.close()
        shutil.rmtree(ch.buffer.dir, ignore_errors=True)
        self.log.info("[%s] removed", cid)
        self.save()

    # kept for the original API endpoint
    def set_delay(self, cid, seconds):
        self.update_channel(cid, delay=seconds)

    # ---------- background ----------
    def housekeeping(self):
        while True:
            for ch in self.list_channels():
                try:
                    n = ch.buffer.prune()
                    if n:
                        self.log.debug("[%s] pruned %d segments", ch.id, n)
                except Exception as e:  # channel removed mid-prune
                    self.log.debug("prune skipped: %s", e)
            time.sleep(60)

    def run(self):
        threading.Thread(target=self.housekeeping, daemon=True).start()
        serve(self, self.cfg.get("listen", "0.0.0.0"), int(self.cfg.get("port", 8080)))


def main():
    ap = argparse.ArgumentParser(description="Timezone delay for radio streams")
    ap.add_argument("--config", default=os.environ.get("TZDELAY_CONFIG", "config.json"))
    App(ap.parse_args().config).run()


if __name__ == "__main__":
    main()
