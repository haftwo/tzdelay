"""Linear audio ingest (sound card, Livewire/AES67, test tone) via FFmpeg.

FFmpeg captures the input and encodes it into short HLS segments in a
staging folder. This thread watches that folder and moves finished segments
into the delay buffer, so the rest of the app is identical to the HLS path.

Input types (set "input" in the channel's ingest options):
  test   - 1 kHz tone, for trying things out with no hardware
  alsa   - a Linux sound card, e.g. "device": "hw:1,0" (AES3 card, line in)
  aes67  - Livewire+/AES67/Ravenna multicast, described by an SDP file
           ("sdp": "/config/studio1.sdp"). Requires the host to be on the
           audio VLAN; for sample-accurate sync also run a PTP daemon.
  custom - your own FFmpeg input arguments in "ffmpeg_input": [...]
"""
import os
import shutil
import subprocess
import time

from .base import Ingest
from ..m3u8 import parse_media_playlist


def input_args(opts: dict) -> list[str]:
    kind = opts.get("input", "test")
    if kind == "test":
        return ["-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]
    if kind == "alsa":
        return ["-f", "alsa", "-channels", str(opts.get("channels", 2)),
                "-sample_rate", str(opts.get("sample_rate", 48000)),
                "-i", opts.get("device", "default")]
    if kind == "aes67":
        return ["-protocol_whitelist", "file,udp,rtp",
                "-i", opts["sdp"]]
    if kind == "custom":
        return list(opts["ffmpeg_input"])
    raise ValueError(f"unknown capture input type: {kind}")


class CaptureIngest(Ingest):
    def run(self):
        staging = os.path.join(self.buffer.dir, "_staging")
        seg_len = float(self.options.get("segment_seconds", 6))
        bitrate = self.options.get("bitrate", "256k")
        while not self.stop_event.is_set():
            shutil.rmtree(staging, ignore_errors=True)
            os.makedirs(staging)
            cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
                   + input_args(self.options)
                   + ["-c:a", "aac", "-b:a", bitrate,
                      "-f", "hls", "-hls_time", str(seg_len),
                      "-hls_list_size", "10", "-hls_flags", "delete_segments",
                      "-hls_segment_filename", os.path.join(staging, "s%09d.ts"),
                      os.path.join(staging, "live.m3u8")])
            self.log.info("[%s] starting capture: %s", self.channel_id, " ".join(cmd))
            proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True)
            # A restart means a real gap in the audio, so flag it downstream
            self.watch(proc, staging, seg_len, first_discont=True)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(5)
            err = (proc.stderr.read() or "").strip()[-300:]
            if not self.stop_event.is_set():
                self.mark_error(f"ffmpeg exited ({proc.returncode}) {err}; restarting")
                self.stop_event.wait(2)

    def watch(self, proc, staging, seg_len, first_discont):
        playlist = os.path.join(staging, "live.m3u8")
        seen = set()
        discont = first_discont
        while proc.poll() is None and not self.stop_event.is_set():
            if os.path.exists(playlist):
                with open(playlist) as f:
                    pl = parse_media_playlist(f.read(), staging + "/")
                for s in pl["segments"]:
                    name = os.path.basename(s["uri"])
                    if name in seen:
                        continue
                    path = os.path.join(staging, name)
                    try:
                        with open(path, "rb") as f:
                            data = f.read()
                    except FileNotFoundError:
                        continue
                    seen.add(name)
                    key = f"{self.channel_id}:cap:{int(time.time()*1000)}:{name}"
                    # Segment is listed once FFmpeg has finished it, so its
                    # audio ended being live at about now.
                    self.buffer.add(data, s["duration"], ".ts", key,
                                    discontinuity=discont, wallclock=time.time())
                    discont = False
                    self.mark_segment()
            self.stop_event.wait(min(1.0, seg_len / 4))
