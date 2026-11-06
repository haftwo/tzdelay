"""Minimal HLS playlist parser - just the tags a delay needs."""
import datetime
import urllib.parse


def _attrs(line: str) -> dict:
    """Parse KEY=VALUE,KEY="VALUE" attribute lists."""
    out, key, val, in_q, cur = {}, None, "", False, ""
    for ch in line.split(":", 1)[1] + ",":
        if ch == '"':
            in_q = not in_q
        elif ch == "=" and not in_q and key is None:
            key, cur = cur.strip(), ""
        elif ch == "," and not in_q:
            if key is not None:
                out[key.upper()] = cur.strip()
            key, cur = None, ""
        else:
            cur += ch
    return out


def parse_master_playlist(text: str, base: str) -> list[dict]:
    variants, pending = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = _attrs(line)
        elif line and not line.startswith("#") and pending is not None:
            variants.append({"uri": urllib.parse.urljoin(base, line),
                             "bandwidth": int(pending.get("BANDWIDTH", 0))})
            pending = None
    return variants


def parse_media_playlist(text: str, base: str) -> dict:
    target, seq, encrypted = 6.0, 0, False
    segments, media_sequence_of = [], {}
    cur = {"discontinuity": False}
    map_uri = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-TARGETDURATION"):
            target = float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            seq = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-KEY"):
            encrypted = _attrs(line).get("METHOD", "NONE") != "NONE"
        elif line.startswith("#EXT-X-MAP"):
            map_uri = urllib.parse.urljoin(base, _attrs(line)["URI"])
        elif line.startswith("#EXTINF"):
            cur["duration"] = float(line.split(":", 1)[1].split(",")[0])
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE"):
            cur["discontinuity"] = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME"):
            iso = line.split(":", 1)[1].replace("Z", "+00:00")
            try:
                cur["pdt"] = datetime.datetime.fromisoformat(iso).timestamp()
            except ValueError:
                pass
        elif line and not line.startswith("#"):
            uri = urllib.parse.urljoin(base, line)
            cur.update(uri=uri, map_uri=map_uri)
            cur.setdefault("duration", target)
            media_sequence_of[uri] = seq
            segments.append(cur)
            seq += 1
            cur = {"discontinuity": False}
    return {"target_duration": target, "segments": segments,
            "media_sequence_of": media_sequence_of, "encrypted": encrypted}
