"""Optional voice I/O. UNTESTED on real hardware: needs a microphone and extra packages.

    pip install faster-whisper sounddevice numpy pyttsx3
Speech-to-text runs locally (faster-whisper). Urdu depends on the Whisper model size you choose and
should be checked on your own recordings. Wake-word detection is NOT implemented (push-to-talk only).
"""
from __future__ import annotations


class VoiceUnavailable(Exception):
    pass


def _need(mod, hint):
    try:
        return __import__(mod)
    except ImportError:
        raise VoiceUnavailable(f"{mod} is not installed. {hint}")


def transcribe_file(path, language=None, model_size="small"):
    fw = _need("faster_whisper", "pip install faster-whisper")
    model = fw.WhisperModel(model_size, compute_type="int8")
    segs, info = model.transcribe(str(path), language=language)
    return " ".join(s.text.strip() for s in segs), info.language


def record(seconds=6, rate=16000):
    sd = _need("sounddevice", "pip install sounddevice numpy")
    np = _need("numpy", "pip install numpy")
    try:
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="float32")
        sd.wait()
    except Exception as e:  # permission denied, no device, driver errors
        raise VoiceUnavailable(f"microphone unavailable ({e}). Check Windows Settings > Privacy > Microphone.")
    return np.squeeze(audio), rate


def listen(seconds=6, language=None):
    """Push-to-talk: record, save to a temp wav, transcribe. Returns text for the user to confirm/correct."""
    import tempfile
    import wave
    np = _need("numpy", "pip install numpy")
    audio, rate = record(seconds)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        with wave.open(f.name, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
            w.writeframes((audio * 32767).astype(np.int16).tobytes())
    text, lang = transcribe_file(f.name, language)
    if not text.strip():
        raise VoiceUnavailable("nothing recognised; try again closer to the microphone")
    return text


def speak(text, rate=175, volume=1.0):
    tts = _need("pyttsx3", "pip install pyttsx3")
    eng = tts.init()
    eng.setProperty("rate", rate)
    eng.setProperty("volume", volume)
    eng.say(text)
    eng.runAndWait()


def transcribe_segments(path, language=None, model_size="small"):
    """-> [(start_s, end_s, text)] with timestamps (used for transcripts and subtitles). Untested here."""
    fw = _need("faster_whisper", "pip install faster-whisper")
    model = fw.WhisperModel(model_size, compute_type="int8")
    segs, _ = model.transcribe(str(path), language=language)
    return [(s.start, s.end, s.text) for s in segs]


def wake_listen(keyword="hey_jarvis", threshold=0.5):
    """Block until the wake word is heard (openWakeWord). Untested: needs a microphone and downloaded models
    (`python -c "import openwakeword; openwakeword.utils.download_models()"`). Windows compatibility must be checked."""
    ow = _need("openwakeword", "pip install openwakeword sounddevice numpy")
    sd = _need("sounddevice", "pip install sounddevice numpy")
    from openwakeword.model import Model
    model = Model(wakeword_models=[keyword])
    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1280) as stream:
            while True:
                frame, _ = stream.read(1280)
                scores = model.predict(frame[:, 0])
                if any(v >= threshold for v in scores.values()):
                    return max(scores, key=scores.get)
    except Exception as e:
        raise VoiceUnavailable(f"wake word listening failed ({e}). Check the microphone permission.")
