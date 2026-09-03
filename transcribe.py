"""Audio transcription using local Whisper."""
import os
import tempfile
import urllib.request

_model = None


def _get_model():
    global _model
    if _model is None:
        import whisper
        # "base" is a good balance of speed and accuracy; runs fine on CPU
        _model = whisper.load_model("base")
    return _model


def transcribe_slack_audio(file_url: str, bot_token: str) -> str | None:
    """
    Download a Slack audio file and transcribe it.
    Returns the transcript text, or None on failure.
    """
    try:
        # Download the file (Slack files need auth header)
        req = urllib.request.Request(file_url, headers={"Authorization": f"Bearer {bot_token}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_data = resp.read()

        # Save to temp file
        suffix = os.path.splitext(file_url.split("?")[0])[1] or ".mp4"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_data)
            tmp_path = tmp.name

        try:
            model = _get_model()
            result = model.transcribe(tmp_path, fp16=False)
            return result.get("text", "").strip()
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Transcription failed: {e}")
        return None
