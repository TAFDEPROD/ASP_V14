# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
audio_core.py — Pipeline DSP audio.

Responsabilités :
  - Initialisation et gestion du périphérique aud (Blender)
  - Lecture audio (source brut + version traitée)
  - Pipeline de pré-traitement avant analyse :
      load source → highpass → lowpass → export WAV → noise gate sample-level
  - Analyse spectrale : RMS par frame Blender, FFT par fenêtre

Le noise gate est inspiré du compresseur/gate des DAW pro (FL Studio Maximus,
Cubase Gate, Ableton Gate) : seuil en dB plein-échelle, knee dur, application
sample par sample. Le signal sous le seuil est ramené à 0 avant l'analyse,
ce qui rend la détection des silences absolument propre.
"""

from __future__ import annotations

import os
import bpy
import aud

from . import security


# ─── État global ─────────────────────────────────────────────────────────────
_audio_device: aud.Device | None = None
_audio_handle: aud.Handle | None = None
_processed_sound: aud.Sound | None = None
_processed_filepath: str = ""
_current_filepath: str = ""
_current_file_size: int = 0


# ─── Initialisation / nettoyage ──────────────────────────────────────────────

def initialize_audio() -> None:
    """Initialise le device aud (idempotent)."""
    global _audio_device
    if _audio_device is None:
        try:
            _audio_device = aud.Device()
        except Exception as exc:  # noqa: BLE001
            print(f"AudioShapePRO: init audio device : {exc}")


def cleanup_audio() -> None:
    """Stoppe la lecture, libère les ressources et le WAV temporaire."""
    global _audio_handle, _processed_sound, _processed_filepath, _current_file_size

    stop_audio()
    _audio_handle = None
    _processed_sound = None

    if _processed_filepath and os.path.isfile(_processed_filepath):
        try:
            os.remove(_processed_filepath)
        except Exception:  # noqa: BLE001
            pass
    _processed_filepath = ""

    if _current_file_size > 0:
        security.get_budget().free(_current_file_size)
        _current_file_size = 0

    _, errors = security.get_budget().cleanup_all_temps()
    for err in errors:
        print(f"AudioShapePRO [cleanup]: {err}")


# ─── Lecture ─────────────────────────────────────────────────────────────────

def stop_audio() -> None:
    global _audio_handle
    if _audio_handle is not None:
        try:
            _audio_handle.stop()
        except Exception:  # noqa: BLE001
            pass
        _audio_handle = None


def is_audio_playing() -> bool:
    if _audio_handle is None:
        return False
    try:
        return _audio_handle.status == aud.STATUS_PLAYING
    except AttributeError:
        try:
            return int(_audio_handle.status) == 1
        except Exception:  # noqa: BLE001
            return False


def resolve_filepath(filepath: str) -> str:
    """Résout un chemin relatif Blender (ex: //son.wav) en chemin absolu."""
    if not filepath:
        return filepath
    try:
        resolved = bpy.path.abspath(filepath)
        return resolved
    except Exception:  # noqa: BLE001
        return filepath


def play_audio(filepath: str, volume: float = 0.8) -> aud.Handle | None:
    global _audio_handle, _audio_device
    if not filepath:
        return None
    filepath = resolve_filepath(filepath)
    if _audio_device is None:
        initialize_audio()
    if _audio_device is None or not os.path.isfile(filepath):
        return None
    try:
        sound = aud.Sound.file(filepath).volume(max(0.0, min(1.0, volume)))
        _audio_handle = _audio_device.play(sound)
        return _audio_handle
    except Exception as exc:  # noqa: BLE001
        print(f"AudioShapePRO: erreur lecture — {exc}")
        return None


def play_processed_audio(volume: float = 0.8) -> aud.Handle | None:
    global _audio_handle, _audio_device
    if _processed_sound is None or _audio_device is None:
        return None
    try:
        sound = _processed_sound.volume(max(0.0, min(1.0, volume)))
        _audio_handle = _audio_device.play(sound)
        return _audio_handle
    except Exception as exc:  # noqa: BLE001
        print(f"AudioShapePRO: erreur lecture traitée — {exc}")
        return None


# ─── Pipeline de traitement avant analyse ────────────────────────────────────

def make_temp_wav_path() -> str:
    """Chemin du WAV temporaire dans bpy.app.tempdir."""
    tmpdir = bpy.app.tempdir or os.path.expanduser("~")
    return os.path.join(tmpdir, "audioshapepro_processed.wav")


def write_sound_to_wav(sound: aud.Sound, target_path: str) -> bool:
    """Exporte un aud.Sound en WAV mono PCM 16 bits (44.1 kHz)."""
    try:
        sound.write(
            target_path,
            rate=44100,
            channels=aud.CHANNELS_MONO,
            format=aud.FORMAT_S16,
            container=aud.CONTAINER_WAV,
            codec=aud.CODEC_PCM,
        )
        return os.path.isfile(target_path)
    except Exception as exc:  # noqa: BLE001
        print(f"AudioShapePRO: write WAV : {exc}")
        return False


def apply_noise_gate_inplace(wav_path: str, gate_db: float) -> bool:
    """
    Noise gate sample-level (knee dur).
    Tout échantillon dont |amplitude_normalisée| < 10**(gate_db/20) → 0.
    """
    try:
        import wave
        import numpy as np
    except ImportError:
        return False

    threshold = 10.0 ** (gate_db / 20.0)

    try:
        with wave.open(wav_path, "rb") as wf:
            params = wf.getparams()
            raw = wf.readframes(wf.getnframes())

        sw = params.sampwidth
        if sw not in (1, 2, 4):
            return False
        dtype = {1: np.int8, 2: np.int16, 4: np.int32}[sw]
        max_val = float(2 ** (sw * 8 - 1))

        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32) / max_val
        samples[np.abs(samples) < threshold] = 0.0
        out = (samples * max_val).clip(-max_val, max_val - 1).astype(dtype)

        with wave.open(wav_path, "wb") as wf:
            wf.setparams(params)
            wf.writeframes(out.tobytes())
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"AudioShapePRO: noise gate : {exc}")
        return False


def prepare_wav_for_bake(
    src_filepath: str,
    *,
    use_highpass: bool = False,
    highpass_freq: float = 90.0,
    use_lowpass: bool = False,
    lowpass_freq: float = 9400.0,
    apply_gate: bool = True,
    gate_db: float = -13.0,
) -> str | None:
    """
    Pipeline complet pour le Séquenceur :
        load → [highpass] → [lowpass] → export WAV → [noise gate]

    Le gate est TOUJOURS appliqué par défaut (key feature v5+).
    Renvoie le chemin du WAV traité, ou None.
    """
    global _processed_sound, _processed_filepath, _current_file_size, _current_filepath

    src_filepath = resolve_filepath(src_filepath)
    if not src_filepath or not os.path.isfile(src_filepath):
        return None

    try:
        sound = aud.Sound.file(src_filepath)

        if use_highpass:
            try:
                sound = sound.highpass(float(highpass_freq))
            except Exception as exc:  # noqa: BLE001
                print(f"AudioShapePRO: highpass : {exc}")

        if use_lowpass:
            try:
                sound = sound.lowpass(float(lowpass_freq))
            except Exception as exc:  # noqa: BLE001
                print(f"AudioShapePRO: lowpass : {exc}")

        temp_path = make_temp_wav_path()
        if not write_sound_to_wav(sound, temp_path):
            return None

        if apply_gate:
            apply_noise_gate_inplace(temp_path, gate_db)

        try:
            sound = aud.Sound.file(temp_path)
        except Exception:  # noqa: BLE001
            pass

        if _current_file_size > 0:
            security.get_budget().free(_current_file_size)
        try:
            new_size = os.path.getsize(temp_path)
            security.get_budget().try_allocate(new_size)
            _current_file_size = new_size
        except Exception:  # noqa: BLE001
            pass

        _current_filepath = src_filepath
        _processed_filepath = temp_path
        _processed_sound = sound
        return temp_path

    except Exception as exc:  # noqa: BLE001
        print(f"AudioShapePRO: erreur pipeline : {exc}")
        return None


def process_audio(filepath: str, props: object) -> aud.Sound | None:
    """Bouton 'Appliquer filtres' (preview audio)."""
    path = prepare_wav_for_bake(
        filepath,
        use_highpass=bool(getattr(props, "use_highpass", False)),
        highpass_freq=float(getattr(props, "highpass_freq", 90.0)),
        use_lowpass=bool(getattr(props, "use_lowpass", False)),
        lowpass_freq=float(getattr(props, "lowpass_freq", 9400.0)),
        apply_gate=True,
        gate_db=float(getattr(props, "noise_gate_db", -13.0)),
    )
    return _processed_sound if path else None


def get_processed_sound() -> aud.Sound | None:
    return _processed_sound


def get_processed_filepath() -> str:
    if _processed_filepath and os.path.isfile(_processed_filepath):
        return _processed_filepath
    return ""


def get_current_filepath() -> str:
    return _current_filepath