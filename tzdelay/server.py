"""HTTP output: delayed playlists, segment files, status page, control API.

  GET    /                            web interface (add/edit channels, status)
  GET    /api/channels                JSON status of every channel (also /api/status)
  POST   /api/channels                {"name", "url", "delay_seconds"} add
  PUT    /api/channels/<id>           any of {"name", "url", "delay_seconds"} edit
  DELETE /api/channels/<id>           remove channel and its buffer
  POST   /api/channels/<id>/delay     {"seconds": 10800} change delay only
  GET  /channels/<id>/delayed.m3u8    the delayed stream (play this)
  GET  /channels/<id>/<segment>       segment files
"""
import json
import math
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAYLIST_LENGTH = 6  # segments listed in the output playlist

CONTENT_TYPES = {".ts": "video/mp2t", ".aac": "audio/aac", ".mp4": "audio/mp4",
                 ".m4s": "audio/mp4", ".mp3": "audio/mpeg"}


def fmt_hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def build_playlist(ch) -> str | None:
    playhead = time.time() - ch.delay
    rows = ch.buffer.window(playhead, PLAYLIST_LENGTH)
    if not rows:
        return None
    first_seq = rows[0][0]
    target = math.ceil(max(r[2] for r in rows))
    out = ["#EXTM3U", "#EXT-X-VERSION:6", f"#EXT-X-TARGETDURATION:{target}",
           f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
           f"#EXT-X-DISCONTINUITY-SEQUENCE:{ch.buffer.discontinuities_before(first_seq)}"]
    current_map = None
    for i, (seq, ts, dur, name, discont, map_file) in enumerate(rows):
        if discont and i > 0:
            out.append("#EXT-X-DISCONTINUITY")
        if map_file and map_file != current_map:
            out.append(f'#EXT-X-MAP:URI="{map_file}"')
            current_map = map_file
        # Program date-time = when this audio plays out at the delayed site
        play_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts + ch.delay))
        out.append(f"#EXT-X-PROGRAM-DATE-TIME:{play_at}.{int((ts % 1) * 1000):03d}Z")
        out.append(f"#EXTINF:{dur:.3f},")
        out.append(name)
    return "\n".join(out) + "\n"


def channel_status(ch) -> dict:
    st = ch.buffer.stats()
    now = time.time()
    ready = bool(st["oldest"]) and st["oldest"] <= now - ch.delay
    last = ch.ingest.last_segment_at
    stale = last is None or now - last > 30
    return {
        "id": ch.id, "name": ch.name, "delay_seconds": ch.delay,
        "delay": fmt_hms(ch.delay),
        "ingest_type": ch.ingest_type,
        "ingest_state": "stale" if (stale and ch.ingest.state == "ok") else ch.ingest.state,
        "last_segment_age": None if last is None else round(now - last, 1),
        "last_error": ch.ingest.last_error,
        "buffer_depth": fmt_hms(st["depth_seconds"]),
        "buffer_segments": st["segments"],
        "discontinuities": st["discontinuities"],
        "output_ready": ready,
        "time_until_ready": None if ready or not st["oldest"]
                            else fmt_hms(st["oldest"] + ch.delay - now),
        "output_url": f"/channels/{ch.id}/delayed.m3u8",
        "source_url": ch.cfg["ingest"].get("url"),
        "editable_source": ch.ingest_type == "hls",
    }


WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            app.log.debug("http: " + fmt, *args)

        def send(self, code, body: bytes, ctype, cache="no-cache"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, obj, code=200):
            self.send(code, json.dumps(obj, indent=2).encode(), "application/json")

        def error(self, code, msg):
            self.send_json({"error": msg}, code)

        def read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            return body

        def parts(self):
            return self.path.split("?")[0].strip("/").split("/")

        # ---------- reads ----------
        def do_GET(self):
            parts = self.parts()
            if parts in ([""], ["index.html"]):
                with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                    return self.send(200, f.read(), "text/html; charset=utf-8")
            if parts in (["logo.png"], ["favicon.png"]):
                with open(os.path.join(WEB_DIR, parts[0]), "rb") as f:
                    return self.send(200, f.read(), "image/png", cache="max-age=86400")
            if parts == ["favicon.ico"]:
                with open(os.path.join(WEB_DIR, "favicon.png"), "rb") as f:
                    return self.send(200, f.read(), "image/png", cache="max-age=86400")
            if parts in (["api", "status"], ["api", "channels"]):
                return self.send_json([channel_status(c) for c in app.list_channels()])
            if len(parts) == 3 and parts[0] == "channels":
                ch = app.channels.get(parts[1])
                if ch is None:
                    return self.error(404, "no such channel")
                if parts[2] == "delayed.m3u8":
                    pl = build_playlist(ch)
                    if pl is None:
                        return self.send(503, b"buffer still filling\n", "text/plain")
                    return self.send(200, pl.encode(), "application/vnd.apple.mpegurl")
                name = os.path.basename(parts[2])  # no path traversal
                path = os.path.join(ch.buffer.dir, name)
                if name != "index.db" and os.path.isfile(path):
                    with open(path, "rb") as f:
                        data = f.read()
                    ext = os.path.splitext(name)[1]
                    return self.send(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"),
                                     cache="max-age=86400")
            self.error(404, "not found")

        # ---------- changes ----------
        def do_POST(self):
            parts = self.parts()
            try:
                body = self.read_json()
                if parts == ["api", "channels"]:  # add a channel
                    ch = app.add_channel(body.get("name", ""), body.get("url", ""),
                                         float(body.get("delay_seconds", -1)))
                    return self.send_json(channel_status(ch), 201)
                if (len(parts) == 4 and parts[:2] == ["api", "channels"]
                        and parts[3] == "delay"):  # original endpoint
                    if parts[2] not in app.channels:
                        return self.error(404, "no such channel")
                    ch = app.update_channel(parts[2], delay=float(body["seconds"]))
                    return self.send_json(channel_status(ch))
            except (ValueError, KeyError, TypeError) as e:
                return self.error(400, str(e))
            self.error(404, "not found")

        def do_PUT(self):  # edit a channel
            parts = self.parts()
            if len(parts) != 3 or parts[:2] != ["api", "channels"]:
                return self.error(404, "not found")
            if parts[2] not in app.channels:
                return self.error(404, "no such channel")
            try:
                body = self.read_json()
                delay = body.get("delay_seconds")
                ch = app.update_channel(parts[2], name=body.get("name"), url=body.get("url"),
                                        delay=None if delay is None else float(delay))
            except (ValueError, KeyError, TypeError) as e:
                return self.error(400, str(e))
            self.send_json(channel_status(ch))

        def do_DELETE(self):
            parts = self.parts()
            if len(parts) == 3 and parts[:2] == ["api", "channels"] and parts[2] in app.channels:
                app.remove_channel(parts[2])
                return self.send_json({"deleted": parts[2]})
            self.error(404, "not found")

    return Handler


def serve(app, host, port):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    app.log.info("listening on http://%s:%d", host, port)
    httpd.serve_forever()
