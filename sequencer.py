# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
sequencer.py — Séquenceur AudioShapePRO (LipSync2D-compatible).
Pipeline : Vosk (mots + temps) → si phonemizer dispo : style LipSyncBlender (IPA → visèmes)
         sinon G2P français embarqué → répartition sur la durée du mot.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import bpy
from . import audio_core
from . import compat as _compat
from . import lsb_sequencer as _lsb
from . import vosk_helper
from .formant_library import (
    CONSONANT_RANGES,
    best_matching_vowel,
    compute_bake_range,
    get_formant_set,
)

# Constantes
_VAD_PEAK_RATIO = 0.04
_VAD_FLOOR = 0.005

# Matrice grammaticale française (inchangée)
FRENCH_VISEME_FREQ = {
    "A": 0.172, "OUE": 0.434, "I": 0.120,
    "MBP": 0.090, "FV": 0.072, "RL": 0.080, "SIL": 0.112,
}
FRENCH_BIGRAM_PROB = {
    "A": {"OUE": 0.25, "I": 0.22, "RL": 0.18, "MBP": 0.12, "FV": 0.10, "SIL": 0.13},
    "OUE": {"A": 0.28, "I": 0.22, "RL": 0.18, "MBP": 0.12, "FV": 0.08, "SIL": 0.12},
    "I": {"OUE": 0.30, "A": 0.25, "RL": 0.15, "MBP": 0.10, "FV": 0.08, "SIL": 0.12},
    "RL": {"A": 0.30, "OUE": 0.25, "I": 0.22, "MBP": 0.10, "FV": 0.07, "SIL": 0.06},
    "FV": {"A": 0.30, "OUE": 0.28, "I": 0.18, "RL": 0.12, "MBP": 0.06, "SIL": 0.06},
    "MBP": {"A": 0.28, "OUE": 0.22, "I": 0.20, "RL": 0.15, "FV": 0.08, "SIL": 0.07},
    "SIL": {"A": 0.22, "OUE": 0.20, "MBP": 0.18, "I": 0.15, "RL": 0.12, "FV": 0.08, "SIL": 0.05},
}
_CONFLICT_PAIRS = {
    frozenset({"MBP", "FV"}), frozenset({"I", "RL"}), frozenset({"A", "RL"}),
}
_NASAL_FRICATIVE = {"MBP", "FV"}

def _has_phoneme_conflict(seq: list, new_ph: str) -> bool:
    if not seq:
        return False
    last = seq[-1]
    if last == new_ph and new_ph in _NASAL_FRICATIVE:
        return True
    if frozenset({last, new_ph}) in _CONFLICT_PAIRS:
        return True
    if len(seq) >= 2 and seq[-2] == seq[-1] == new_ph:
        return True
    return False

def _apply_grammatical_matrix(phoneme_seq: list, available: list) -> list:
    if not phoneme_seq:
        return phoneme_seq
    corrected = []
    for ph in phoneme_seq:
        if _has_phoneme_conflict(corrected, ph):
            prev = corrected[-1] if corrected else "SIL"
            bigrams = FRENCH_BIGRAM_PROB.get(prev, {})
            candidates = sorted(
                [(p, prob) for p, prob in bigrams.items()
                 if p in available and p != ph and not _has_phoneme_conflict(corrected, p)],
                key=lambda x: -x[1],
            )
            if candidates:
                corrected.append(candidates[0][0])
        corrected.append(ph)
    result, streak, last_ph = [], 0, None
    for ph in corrected:
        streak = streak + 1 if ph == last_ph else 1
        last_ph = ph
        if streak <= 2:
            result.append(ph)
    return result if result else phoneme_seq

# --- Structures ---
@dataclass(slots=True)
class SequencerParams:
    inbetween_fr: int
    silence_fr: int
    close_fr: int
    silence_stretch: float
    gate_db: float
    profile: str
    language: str
    mode: str
    use_vosk: bool = True
    smooth_curves: bool = False
    anticipation: float = 0.18
    release: float = 0.22
    # NOUVEAUX PARAMÈTRES (LIPSYNC2D)
    silence_frame_threshold: int = 5
    in_between_frame_threshold: int = 2

@dataclass(slots=True)
class DetectedWord:
    frame_start: int
    frame_end: int
    phoneme_seq: list[str]
    avg_score: float
    vosk_text: str = ""

    def as_text(self) -> str:
        if self.vosk_text:
            return self.vosk_text
        return "".join(p.lower() for p in self.phoneme_seq if p not in ("MBP", "FV"))

@dataclass(slots=True)
class SequencerResult:
    n_speech_segments: int = 0
    n_silences: int = 0
    total_keyframes: int = 0
    duration_s: float = 0.0
    detected_words: list[DetectedWord] = field(default_factory=list)
    analysis_mode: str = "VOSK_G2P"

# --- G2P Français (inchangé) ---
def french_g2p_to_asp_visemes(word: str, phonemes: list[str]) -> list[str]:
    VOWEL_VISEMES = {"A", "OUE", "I", "RL"}
    CONS_VISEMES = {"FV", "MBP"}
    oue_avail = "OUE" in phonemes
    fv_avail = "FV" in phonemes
    mbp_avail = "MBP" in phonemes
    rl_avail = "RL" in phonemes
    vowels_avail = [p for p in ("A", "OUE", "I") if p in phonemes]
    word = word.lower().strip()
    out = []
    i = 0
    while i < len(word):
        tri = word[i:i+3]
        bi = word[i:i+2]
        c = word[i]
        if tri in ("eau", "eua", "oua", "oue", "oui", "ain", "ein", "oin"):
            if oue_avail:
                out.append("OUE")
            i += 3
            continue
        if bi in ("ou", "oû", "où", "au", "ao", "ai", "aî", "ei", "oî", "eu", "œu", "oi", "oy"):
            if oue_avail:
                out.append("OUE")
            elif bi in ("oi", "oy") and "A" in phonemes:
                out.append("A")
            i += 2
            continue
        if c in "aàâ" and "A" in phonemes:
            out.append("A")
        elif c in "eéèêëœoôõuûù" and oue_avail:
            out.append("OUE")
        elif c in "iîïy" and "I" in phonemes:
            out.append("I")
        elif c in "rl" and rl_avail:
            out.append("RL")
        elif c in "fv" and fv_avail:
            out.append("FV")
        elif c in "mbp" and mbp_avail:
            out.append("MBP")
        i += 1
    deduped = []
    for v in out:
        if not deduped or deduped[-1] != v:
            deduped.append(v)
    if not deduped:
        if vowels_avail:
            deduped = [vowels_avail[0]]
        elif fv_avail:
            deduped = ["FV"]
        elif mbp_avail:
            deduped = ["MBP"]
        elif phonemes:
            deduped = [phonemes[0]]
    return deduped

def _build_words_from_vosk_g2p(
    vosk_words, fps, frame_start, frame_end, phonemes, silence_fr
) -> list[dict]:
    words = []
    for vw in vosk_words:
        if not vw.text:
            continue
        f_start = frame_start + int(round(vw.start_s * fps))
        f_end = frame_start + int(round(vw.end_s * fps))
        f_start = max(frame_start, min(frame_end, f_start))
        f_end = max(frame_start, min(frame_end, f_end))
        if f_end <= f_start:
            f_end = f_start + 1
        duration = f_end - f_start
        vis_seq = french_g2p_to_asp_visemes(vw.text, phonemes)
        n_vis = len(vis_seq)
        if n_vis > 0 and duration > 0:
            parts = duration / n_vis
            visemes = []
            for idx, ph in enumerate(vis_seq):
                frame = f_start + round(parts * idx)
                frame = max(f_start, min(f_end, frame))
                visemes.append((frame, ph))
        else:
            visemes = [(f_start, vis_seq[0] if vis_seq else phonemes[0] if phonemes else "A")]
        if visemes:
            words.append({
                "word_frame_start": f_start,
                "word_frame_end": f_end,
                "visemes": visemes,
            })
    return words


def _apply_anticipation_release_to_words(
    words: list[dict],
    anticipation: float,
    release: float,
    frame_start: int,
    frame_end: int,
) -> None:
    """
    Ajuste in-place : recule légèrement les visèmes (anticipation) dans la marge
    avant le mot, et prolonge la fin du mot (relâchement) en frames.
    """
    if not words or (anticipation <= 0.0001 and release <= 0.0001):
        return
    prev_end = frame_start
    for w in words:
        fs0 = int(w["word_frame_start"])
        fe0 = int(w["word_frame_end"])
        dur = max(1, fe0 - fs0)
        ant = int(round(anticipation * dur)) if anticipation > 0 else 0
        rel = int(round(release * dur)) if release > 0 else 0
        gap = max(0, fs0 - prev_end - 1)
        ant_use = min(ant, gap)
        new_vis = [
            (max(frame_start, int(vf) - ant_use), ph)
            for vf, ph in w["visemes"]
        ]
        w["visemes"] = new_vis
        w["word_frame_start"] = max(frame_start, fs0 - ant_use)
        w["word_frame_end"] = min(frame_end, fe0 + rel)
        prev_end = w["word_frame_end"]


MODE_PHONEMES = {
    "SIMPLE": ["A", "OUE", "I"],
    "ADVANCED": ["A", "OUE", "I", "MBP"],
    "EXPERT": ["A", "OUE", "I", "MBP", "FV", "RL"],
}

# --- Analyse audio (inchangée) ---
def get_phoneme_band(phoneme: str, profile: str, language: str = "EN") -> tuple[int, int]:
    if phoneme in CONSONANT_RANGES:
        cons = CONSONANT_RANGES[phoneme]
        low, high = cons.get(profile, cons.get("MALE", (80, 7000)))
        return (max(20, int(low)), min(20000, int(high)))
    fset = get_formant_set(phoneme, language, profile)
    if fset is None:
        return (200, 2000)
    return fset.dominant_range(1.0)

def analyze_wav(wav_path, fps_num, fps_base, frame_start, frame_end, phonemes_with_bands):
    try:
        import wave
        import numpy as np
    except ImportError:
        return None
    try:
        with wave.open(wav_path, "rb") as wf:
            sr = wf.getframerate()
            n_ch = wf.getnchannels()
            sw = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
    except Exception:
        return None
    if sw not in (1, 2, 4):
        return None
    dtype = {1: np.int8, 2: np.int16, 4: np.int32}[sw]
    max_val = float(2 ** (sw * 8 - 1))
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float32) / max_val
    if n_ch > 1:
        samples = samples.reshape(-1, n_ch).mean(axis=1)
    real_fps = fps_num / max(fps_base, 0.001)
    samples_per_fr = sr / real_fps
    total = len(samples)
    win_size = max(256, int(samples_per_fr))
    hann = np.hanning(win_size).astype(np.float32)
    bin_ranges = {}
    for ph, low_hz, high_hz in phonemes_with_bands:
        b_low = max(0, int(low_hz * win_size / sr))
        b_high = min(win_size // 2, int(high_hz * win_size / sr))
        if b_high <= b_low:
            b_high = b_low + 1
        bin_ranges[ph] = (b_low, b_high)
    log_bands_hz = [int(200 * (5000 / 200) ** (i / 7.0)) for i in range(8)]
    log_bands_bins = [max(0, min(win_size // 2 - 1, int(f * win_size / sr))) for f in log_bands_hz]
    amp_total = {}
    amp_per_ph = {}
    band_profile = {}
    for f in range(frame_start, frame_end + 1):
        t_center = (f - frame_start + 0.5) * samples_per_fr
        s0 = max(0, int(t_center - win_size / 2))
        s1 = min(total, s0 + win_size)
        if s1 - s0 < 32:
            amp_total[f] = 0.0
            amp_per_ph[f] = {ph: 0.0 for ph, *_ in phonemes_with_bands}
            band_profile[f] = []
            continue
        chunk = samples[s0:s1]
        if len(chunk) < win_size:
            chunk = np.pad(chunk, (0, win_size - len(chunk)))
        amp_total[f] = float(np.sqrt(np.mean(chunk * chunk)))
        spec = np.abs(np.fft.rfft(chunk * hann))
        per_ph = {}
        for ph, *_ in phonemes_with_bands:
            b_low, b_high = bin_ranges[ph]
            band = spec[b_low:b_high]
            per_ph[ph] = float(np.mean(band)) if band.size else 0.0
        amp_per_ph[f] = per_ph
        peak_e = float(spec.max()) if spec.size else 1.0
        if peak_e <= 1e-9:
            peak_e = 1.0
        band_profile[f] = [(float(log_bands_hz[i]), float(spec[log_bands_bins[i]]) / peak_e) for i in range(len(log_bands_hz))]
    return amp_total, amp_per_ph, band_profile

def detect_speech_segments(amp_total, frame_start, frame_end, silence_fr):
    if not amp_total:
        return [], []
    peak = max(amp_total.values()) or 1.0
    threshold = max(_VAD_FLOOR, peak * _VAD_PEAK_RATIO)
    voiced = {f for f, v in amp_total.items() if v > threshold}
    silence_runs = []
    in_sil, sil_s = False, -1
    for f in range(frame_start, frame_end + 1):
        if f not in voiced:
            if not in_sil:
                sil_s, in_sil = f, True
        elif in_sil:
            silence_runs.append((sil_s, f - 1))
            in_sil = False
    if in_sil:
        silence_runs.append((sil_s, frame_end))
    real_silence = set()
    for s, e in silence_runs:
        if (e - s + 1) >= silence_fr:
            real_silence.update(range(s, e + 1))
    speech_segs = []
    in_sp, sp_s = False, -1
    for f in range(frame_start, frame_end + 1):
        is_sp = f not in real_silence
        if is_sp and not in_sp:
            sp_s, in_sp = f, True
        elif not is_sp and in_sp:
            speech_segs.append((sp_s, f - 1))
            in_sp = False
    if in_sp:
        speech_segs.append((sp_s, frame_end))
    return speech_segs, silence_runs

# --- NOUVELLE VERSION DE write_lipsync_keyframes (LIPSYNC2D) ---
def _insert_kp(fc: bpy.types.FCurve, frame: int, value: float) -> None:
    kp = fc.keyframe_points.insert(frame, value, options={"FAST"})
    kp.interpolation = "LINEAR"
    kp.handle_left_type = "VECTOR"
    kp.handle_right_type = "VECTOR"

def _apply_smooth_bezier(all_fcs: "list[bpy.types.FCurve]") -> None:
    for fc in all_fcs:
        if fc is None:
            continue
        for kp in fc.keyframe_points:
            kp.interpolation = "BEZIER"
            kp.handle_left_type = "AUTO_CLAMPED"
            kp.handle_right_type = "AUTO_CLAMPED"
        fc.update()

def _setup_action(obj: bpy.types.Object) -> bpy.types.Action:
    sk_data = obj.data.shape_keys
    if sk_data.animation_data is None:
        sk_data.animation_data_create()
    anim = sk_data.animation_data
    if _compat.BL_GTE_50:
        if anim.action is None:
            action = bpy.data.actions.new(f"ASP_{obj.name}_Seq")
            slot = action.slots.new(id_type="KEY")
            anim.action = action
            try:
                anim.action_slot = slot
            except Exception:
                pass
        else:
            action = anim.action
            if not action.slots:
                slot = action.slots.new(id_type="KEY")
                try:
                    anim.action_slot = slot
                except Exception:
                    pass
        return anim.action
    else:
        if anim.action is None:
            anim.action = bpy.data.actions.new(f"ASP_{obj.name}_Seq")
        return anim.action

def _clear_target_fcurves(action: bpy.types.Action, target_dps: set[str]) -> None:
    for fc in list(_compat.action_fcurves(action)):
        if fc.data_path in target_dps:
            _compat.action_fcurves(action).remove(fc)

def write_lipsync_keyframes(
    fcurves: dict[str, bpy.types.FCurve],
    sil_fc: "bpy.types.FCurve | None",
    words: list[dict],
    frame_start: int,
    frame_end: int,
    close_fr: int,
    inbetween_fr: int,
    silence_fr: int,
    silence_frame_threshold: int,
    in_between_frame_threshold: int,
) -> None:
    """
    Version 100% compatible LIPSYNC2D_ShapeKeysAnimator.
    """
    all_fcs = list(fcurves.values()) + ([sil_fc] if sil_fc else [])

    # 1. Initialisation
    for fc in fcurves.values():
        _insert_kp(fc, frame_start, 0.0)
    if sil_fc is not None:
        is_first_word_silent = not words or (words[0]["word_frame_start"] - frame_start) >= silence_frame_threshold
        _insert_kp(sil_fc, frame_start, 1.0 if is_first_word_silent else 0.0)

    if not words:
        if sil_fc is not None:
            _insert_kp(sil_fc, frame_end, 1.0)
        for fc in fcurves.values():
            _insert_kp(fc, frame_end, 0.0)
        for fc in all_fcs:
            fc.keyframe_points.sort()
            fc.update()
        return

    last_kf_frame: int = -10**9
    last_phoneme: str | None = None
    last_shape_key: str | None = None

    for w_idx, word in enumerate(words):
        sp_start = word["word_frame_start"]
        sp_end = word["word_frame_end"]
        visemes = word["visemes"]
        is_first = (w_idx == 0)
        is_last = (w_idx == len(words) - 1)

        # SIL avant le 1er mot
        if sil_fc is not None and is_first:
            open_frame = max(frame_start + 1, sp_start - max(1, close_fr))
            if open_frame < sp_start:
                _insert_kp(sil_fc, open_frame, 1.0)
                for fc in fcurves.values():
                    _insert_kp(fc, open_frame, 0.0)

        # Insertion des visèmes
        for v_idx, (v_frame, v_phoneme) in enumerate(visemes):
            current_shape_key = fcurves.get(v_phoneme)
            if current_shape_key is not None and last_shape_key == current_shape_key.data_path:
                continue
            if (
                v_idx > 0
                and last_kf_frame > -10**8
                and (v_frame - last_kf_frame) <= max(0, in_between_frame_threshold)
            ):
                continue
            if v_frame == last_kf_frame:
                continue
            for ph, fc in fcurves.items():
                _insert_kp(fc, v_frame, 1.0 if ph == v_phoneme else 0.0)
            if sil_fc is not None:
                _insert_kp(sil_fc, v_frame, 0.0)
            last_kf_frame = v_frame
            last_phoneme = v_phoneme
            last_shape_key = current_shape_key.data_path if current_shape_key else None

        # SIL après le mot (avec réouverture)
        if sil_fc is not None:
            if is_last:
                close_frame = min(frame_end, sp_end + max(1, close_fr))
                if close_frame > last_kf_frame:
                    _insert_kp(sil_fc, close_frame, 1.0)
                    for fc in fcurves.values():
                        _insert_kp(fc, close_frame, 0.0)
            else:
                next_start = words[w_idx + 1]["word_frame_start"]
                gap = next_start - sp_end
                if gap >= silence_frame_threshold:
                    close_frame = sp_end + max(1, close_fr)
                    if close_frame > last_kf_frame:
                        _insert_kp(sil_fc, close_frame, 1.0)
                        for fc in fcurves.values():
                            _insert_kp(fc, close_frame, 0.0)
                    reopen_frame = next_start - max(1, in_between_frame_threshold)
                    if reopen_frame > close_frame and reopen_frame < next_start:
                        _insert_kp(sil_fc, reopen_frame, 0.0)
                        for fc in fcurves.values():
                            _insert_kp(fc, reopen_frame, 0.0)

    # Verrou final
    for fc in fcurves.values():
        _insert_kp(fc, frame_end, 0.0)
    if sil_fc is not None:
        _insert_kp(sil_fc, frame_end, 1.0)
    for fc in all_fcs:
        fc.keyframe_points.sort()
        fc.update()
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"

# --- run_sequencer : Vosk + (LSB phonemizer | G2P) + keyframes ---
def run_sequencer(
    operator,
    context: bpy.types.Context,
    obj: bpy.types.Object,
    sk_map: dict[str, str],
    sil_sk_name: str,
    params: SequencerParams,
    src_filepath: str,
    use_highpass: bool, highpass_freq: float,
    use_lowpass: bool, lowpass_freq: float,
) -> SequencerResult | None:
    # 1. Pré-traitement audio
    _vosk_ready = vosk_helper.is_vosk_available() and vosk_helper.is_model_ready()
    _apply_gate = not (params.use_vosk and _vosk_ready)
    wav_path = audio_core.prepare_wav_for_bake(
        src_filepath,
        use_highpass=use_highpass, highpass_freq=highpass_freq,
        use_lowpass=use_lowpass, lowpass_freq=lowpass_freq,
        apply_gate=_apply_gate, gate_db=params.gate_db,
    )
    if wav_path is None:
        operator.report({"ERROR"}, "Impossible d'exporter le WAV pour analyse")
        return None

    # 2. Bandes de formants
    bands = []
    for ph in sk_map.keys():
        low, high = get_phoneme_band(ph, params.profile, params.language)
        bands.append((ph, low, high))

    # 3. Préparation
    scene = context.scene
    frame_start, frame_end = scene.frame_start, scene.frame_end
    real_fps = scene.render.fps / max(scene.render.fps_base, 0.001)
    _vosk_ok = vosk_helper.is_vosk_available() and vosk_helper.is_model_ready()

    # 4. SEGMENTATION DES MOTS (FORCE VOSK)
    if not params.use_vosk or not _vosk_ok:
        operator.report({"ERROR"}, "Vosk non disponible — impossible de matcher LIPSYNC2D")
        return None

    vosk_words_raw = vosk_helper.recognize_words(wav_path) or []
    if not vosk_words_raw:
        operator.report({"ERROR"}, "Vosk n'a reconnu aucun mot")
        return None

    speech_segs = vosk_helper.words_to_speech_segments(
        vosk_words_raw, real_fps, frame_start, frame_end,
        silence_fr=params.silence_fr, merge_close=True,
    )
    if not speech_segs:
        operator.report({"ERROR"}, "Aucun segment de parole détecté")
        return None

    # 5. Conversion en words+visemes : LipSyncBlender (phonemizer) si dispo, sinon G2P
    phonemes = list(sk_map.keys())
    props = context.scene.audioshape_props
    vosk_lang = str(getattr(props, "vosk_language", "fr") or "fr")
    if vosk_lang == "none":
        vosk_lang = "fr"

    used_lsb = False
    words: list[dict] = []
    if _lsb.is_available():
        words = _lsb.build_words(
            vosk_words_raw, real_fps, frame_start, frame_end, vosk_lang, phonemes
        )
        used_lsb = bool(words)
    if not words:
        words = _build_words_from_vosk_g2p(
            vosk_words_raw, real_fps, frame_start, frame_end, phonemes, params.silence_fr
        )
    if not words:
        operator.report({"ERROR"}, "Aucun mot généré")
        return None

    _apply_anticipation_release_to_words(
        words, params.anticipation, params.release, frame_start, frame_end
    )

    analysis_mode = "VOSK_LSB" if used_lsb else "VOSK_G2P"
    # 6. Préparation des F-curves
    use_sil = bool(sil_sk_name) and sil_sk_name in obj.data.shape_keys.key_blocks
    action = _setup_action(obj)
    target_dps = {f'key_blocks["{sk}"].value' for sk in sk_map.values()}
    if use_sil:
        target_dps.add(f'key_blocks["{sil_sk_name}"].value')
    _clear_target_fcurves(action, target_dps)

    def _get_or_create_fc(action: bpy.types.Action, data_path: str) -> bpy.types.FCurve:
        fc = _compat.action_fcurves(action).find(data_path, index=0)
        if fc is not None:
            while fc.keyframe_points:
                fc.keyframe_points.remove(fc.keyframe_points[0])
            return fc
        return _compat.action_fcurves(action).new(data_path=data_path, index=0)

    fcurves = {ph: _get_or_create_fc(action, f'key_blocks["{sk}"].value') for ph, sk in sk_map.items()}
    sil_fc = _get_or_create_fc(action, f'key_blocks["{sil_sk_name}"].value') if use_sil else None

    # 7. Insertion des keyframes (NOUVEAUX PARAMÈTRES)
    write_lipsync_keyframes(
        fcurves=fcurves,
        sil_fc=sil_fc,
        words=words,
        frame_start=frame_start,
        frame_end=frame_end,
        close_fr=params.close_fr,
        inbetween_fr=params.inbetween_fr,
        silence_fr=params.silence_fr,
        silence_frame_threshold=params.silence_frame_threshold,
        in_between_frame_threshold=params.in_between_frame_threshold,
    )

    # 8. Interpolation : Bézier si demandé, sinon LINEAR (LipSync2D)
    all_fcs = list(fcurves.values()) + ([sil_fc] if sil_fc else [])
    if params.smooth_curves:
        _apply_smooth_bezier(all_fcs)
    else:
        for fc in all_fcs:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

    # 9. Rapport
    result = SequencerResult(
        n_speech_segments=len(speech_segs),
        n_silences=len([w for w in words if w["word_frame_end"] - w["word_frame_start"] >= params.silence_fr]),
        total_keyframes=sum(len(w["visemes"]) for w in words),
        duration_s=(frame_end - frame_start) / real_fps,
        detected_words=[DetectedWord(
            frame_start=w["word_frame_start"],
            frame_end=w["word_frame_end"],
            phoneme_seq=[ph for _, ph in w["visemes"]],
            avg_score=1.0,
            vosk_text=vw.text if (vw := next((vw for vw in vosk_words_raw if abs(vw.start_s - (w["word_frame_start"] - frame_start)/real_fps) < 0.1), None)) else "",
        ) for w in words],
        analysis_mode=analysis_mode,
    )
    return result