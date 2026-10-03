"""yt-dlp for her computers (Quasar step 3), shipped by Aurelia herself.

YouTube changes often and yt-dlp follows it, so the version a computer uses is the one pinned in this
service's own lock file: tested by Chrysalis before every release, and updated by updating that pin.
A computer fetches it with its node key (GET /nodes/ytdlp for the version and checksum, then
/nodes/ytdlp.zip) and checks the checksum before using it. It's the yt_dlp package and its
yt-dlp-ejs component (the scripts YouTube's JavaScript challenges need) in one zip, which the agent
runs as a Python package (python -m yt_dlp with the zip on PYTHONPATH): nothing is installed and no
downloaded program is run. The zip is built the same way every time (sorted, fixed dates), so its
checksum only changes when the pin does.
"""
import hashlib
import io
import zipfile
from importlib import metadata
from pathlib import Path

PACKAGES = ("yt-dlp", "yt-dlp-ejs")
_cache: dict | None = None


def _files(dist: metadata.Distribution) -> list[tuple[str, Path]]:
    out = []
    for f in dist.files or []:
        name = str(f)
        if name.startswith("..") or "__pycache__" in name or name.endswith((".pyc", "RECORD", "INSTALLER")):
            continue                                             # console scripts live outside site-packages
        path = Path(dist.locate_file(f))
        if path.is_file():
            out.append((name, path))
    return out


def build() -> dict:
    """{"zip": bytes, "sha256", "version"}; built once per process."""
    global _cache
    if _cache is not None:
        return _cache
    entries: list[tuple[str, Path]] = []
    versions = []
    for pkg in PACKAGES:
        dist = metadata.distribution(pkg)
        versions.append(f"{pkg} {dist.version}")
        entries += _files(dist)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, path.read_bytes())
    data = buf.getvalue()
    _cache = {"zip": data, "sha256": hashlib.sha256(data).hexdigest(), "version": ", ".join(versions)}
    return _cache
