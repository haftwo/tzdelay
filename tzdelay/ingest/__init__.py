from .hls import HlsIngest
from .capture import CaptureIngest

# Register new input types here
INGEST_TYPES = {
    "hls": HlsIngest,
    "capture": CaptureIngest,
}
