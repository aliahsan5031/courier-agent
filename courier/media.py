"""Audio and video: ffmpeg extraction, timestamped transcripts, subtitles, chapters, frame sampling.

Speech recognition itself is pluggable (default: faster-whisper via voice.py). ffmpeg/ffprobe must be installed.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}


class MediaError(Exception):
    pass


def _need_ffmpeg():
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise MediaError("ffmpeg is not installed (needed for audio/video). Install it from ffmpeg.org or your package manager.")


def probe(path: Path) -> dict:
    _need_ffmpeg()
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise MediaError(f"cannot read media file: {r.stderr.strip()[:150]}")
    d = json.loads(r.stdout)
    kinds = {s["codec_type"] for s in d.get("streams", [])}
    return {"duration_s": float(d.get("format", {}).get("duration", 0) or 0), "audio": "audio" in kinds, "video": "video" in kinds}


def extract_audio(path: Path, out: Path):
    _need_ffmpeg()
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", str(out)],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not out.exists():
        raise MediaError(f"audio extraction failed: {r.stderr.strip()[:150]}")


def sample_frames(path: Path, out_dir: Path, every_s=30, max_frames=8):
    _need_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("frame_*.jpg"):
        old.unlink()
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf", f"fps=1/{max(1, int(every_s))}", "-frames:v", str(max_frames),
                        str(out_dir / "frame_%03d.jpg")], capture_output=True, text=True, timeout=1800)
    frames = sorted(out_dir.glob("frame_*.jpg"))
    if r.returncode != 0 or not frames:
        raise MediaError(f"frame extraction failed: {r.stderr.strip()[:150]}")
    return [(f, i * max(1, int(every_s))) for i, f in enumerate(frames)]


def _ts(sec, comma=False):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02}:{m:02}:{s:02}{',' if comma else '.'}{ms:03}"


def to_srt(segments):
    return "\n".join(f"{i}\n{_ts(a, True)} --> {_ts(b, True)}\n{t.strip()}\n" for i, (a, b, t) in enumerate(segments, 1))


def to_markdown(title, segments):
    return f"# Transcript: {title}\n\n" + "\n".join(f"[{_ts(a)[:8]}] {t.strip()}" for a, b, t in segments) + "\n"


def chapters(segments, window_s=300):
    """Group segments into fixed windows; title = first sentence in the window. A starting point, not a summary."""
    out, cur_start, cur_text = [], None, []
    for a, b, t in segments:
        if cur_start is None:
            cur_start = a
        if a - cur_start >= window_s and cur_text:
            out.append((cur_start, cur_text))
            cur_start, cur_text = a, []
        cur_text.append(t.strip())
    if cur_text:
        out.append((cur_start or 0, cur_text))
    res = []
    for start, texts in out:
        first = re.split(r"(?<=[.!?])\s", " ".join(texts), 1)[0]
        res.append({"start": _ts(start)[:8], "title": first[:70] + ("…" if len(first) > 70 else "")})
    return res


def transcribe_media(path: Path, outdir: Path, transcriber, language=None):
    """-> dict with paths of transcript (.md) and subtitles (.srt), duration and chapters."""
    ext = path.suffix.lower()
    if ext not in AUDIO_EXT | VIDEO_EXT:
        raise MediaError(f"unsupported media type {ext}")
    info = probe(path)
    if not info["audio"]:
        raise MediaError("file has no audio track")
    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        extract_audio(path, wav)
        segments = transcriber(str(wav), language)
    if not segments:
        raise MediaError("no speech recognised")
    outdir.mkdir(parents=True, exist_ok=True)
    md, srt = outdir / f"{path.stem}.transcript.md", outdir / f"{path.stem}.srt"
    md.write_text(to_markdown(path.name, segments))
    srt.write_text(to_srt(segments))
    return {"duration_s": round(info["duration_s"], 1), "transcript": md, "subtitles": srt, "chapters": chapters(segments),
            "preview": " ".join(t.strip() for _, _, t in segments)[:1200]}
