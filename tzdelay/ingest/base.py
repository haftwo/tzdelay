"""Ingest plug-in interface.

An ingest's only job is to get audio into the SegmentBuffer as timestamped
segments. Add a new input type (sound card, Livewire/AES67, SRT...) by
subclassing Ingest and registering it in ingest/__init__.py.
"""
import threading
import time


class Ingest(threading.Thread):
    def __init__(self, channel_id: str, buffer, options: dict, log):
        super().__init__(daemon=True, name=f"ingest-{channel_id}")
        self.channel_id = channel_id
        self.buffer = buffer
        self.options = options
        self.log = log
        self.stop_event = threading.Event()
        # Health fields shown on the status page
        self.last_segment_at: float | None = None
        self.last_error: str | None = None
        self.state = "starting"

    def mark_segment(self):
        self.last_segment_at = time.time()
        self.state = "ok"

    def mark_error(self, msg: str):
        self.last_error = f"{time.strftime('%H:%M:%S')} {msg}"
        self.state = "error"
        self.log.warning("[%s] %s", self.channel_id, msg)

    def stop(self):
        self.stop_event.set()

    def run(self):  # implemented by subclasses
        raise NotImplementedError
