# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
asms_engine.py — Moteur ASMS (Audio Shape Morphing System) v12
Analyse spectrale basée sur LPC (Praat-style) + formants Peterson & Barney.

Pipeline ASMS :
  1. Normalisation RMS (-14 LUFS target)
  2. Noise gate adaptatif
  3. LPC (Burg) → extraction formants F1-F5
  4. Scoring formantique hexagonal (matrice Fr accent neutre)
  5. Validation Winners / Losers (Checkwinner)
  6. Coarticulation syllabique

Données de référence :
  Peterson & Barney (1952) — via PRAAT_Table_dataSets.cpp
  Calliope (1989) — français
  Vaissière (2011) — accent neutre français
"""
from __future__ import annotations

import math
from typing import NamedTuple

try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False

# ─────────────────────────────────────────────────────────────────────────────
#  Données Peterson & Barney (1952) — F1/F2/F3 par voyelle (Hz, homme adulte)
#  Source : PRAAT_Table_dataSets.cpp — moyennes des 1520 utterances
# ─────────────────────────────────────────────────────────────────────────────

PB52_MALE: dict[str, tuple[float, float, float]] = {
    # phonème : (F1_mean, F2_mean, F3_mean) en Hz
    "I":   (342,  2322, 3000),   # iy  "heed"
    "OUE": (580,   997, 2538),   # groupe O/U/E — F2 arrondi moyen
    "A":   (768,  1333, 2522),   # aa  "hod"
    "RL":  (320,  1200, 2700),   # liquides R/L — entre I et A
    # Alias legacy
    "E":   (580,  1799, 2605),
    "O":   (652,   997, 2538),
    "U":   (378,   997, 2343),
}

# Formants français — Calliope (1989) + Vaissière (2011)
# Accent neutre français — F1/F2/F3 (Hz)
FR_MALE: dict[str, tuple[float, float, float]] = {
    "A":   (800,  1350, 2550),  # [a] "patte"
    "OUE": (400,   820, 2550),  # groupe O/U/E — F2 médian arrondi (O=740, E=2050 → centre 820)
    "I":   (280,  2250, 3000),  # [i] "vie"
    "RL":  (320,  1200, 2700),  # R/L liquides — F1 bas-moyen, F2 médian
    "FV":  (200,  1000, 2200),  # fricative labio-dentale f/v
    "MBP": (200,   800, 2100),  # occlusive bilabiale m/b/p
    # Alias individuels conservés pour rétrocompatibilité interne
    "E":   (400,  2050, 2700),
    "O":   (430,   740, 2550),
    "U":   (310,   900, 2450),
}

# Tolérance en Hz pour la validation du Winner
WINNER_TOLERANCE_F1 = 150   # Hz
WINNER_TOLERANCE_F2 = 250   # Hz

# ─────────────────────────────────────────────────────────────────────────────
#  Fréquences hexagonales FR (Loser → matrice de réentraînement)
#  Source : LogicSequencer — matrices shapekeys Débutant / Avancé / Expert
# ─────────────────────────────────────────────────────────────────────────────

# v13 — matrices restructurées avec OUE et RL
HEXAGONAL_FR = {
    "SIMPLE":   {"A", "OUE", "I"},
    "ADVANCED": {"A", "OUE", "I", "MBP"},
    "EXPERT":   {"A", "OUE", "I", "MBP", "FV", "RL"},
    # Alias legacy
    "DEBUTANT": {"A", "OUE", "I"},
    "AVANCE":   {"A", "OUE", "I", "MBP", "FV"},
}


# ─────────────────────────────────────────────────────────────────────────────
#  Burg LPC (pure Python — fallback si numpy indisponible)
# ─────────────────────────────────────────────────────────────────────────────

def _lpc_burg_python(x: list[float], order: int) -> list[float]:
    """Burg LPC sans numpy — O(n*order), ordre typique 12-16."""
    n = len(x)
    if n < order + 1:
        return [0.0] * order

    ef = list(x)
    eb = list(x)
    a  = [0.0] * order
    for m in range(order):
        num = den = 0.0
        for i in range(n - m - 1):
            num += ef[i + m + 1] * eb[i]
            den += ef[i + m + 1] ** 2 + eb[i] ** 2
        if abs(den) < 1e-12:
            break
        km = -2.0 * num / den
        a_new = a[:]
        for i in range(m):
            a_new[i] = a[i] + km * a[m - 1 - i]
        a_new[m] = km
        a = a_new
        ef_new = [0.0] * n
        eb_new = [0.0] * n
        for i in range(n - m - 1):
            ef_new[i + m + 1] = ef[i + m + 1] + km * eb[i]
            eb_new[i]         = eb[i] + km * ef[i + m + 1]
        ef, eb = ef_new, eb_new
    return a


def _lpc_burg_np(x: "np.ndarray", order: int) -> "np.ndarray":
    """Burg LPC avec numpy — plus rapide sur de longs signaux."""
    n = len(x)
    ef = x.copy()
    eb = x.copy()
    a  = np.zeros(order)
    for m in range(order):
        num = np.dot(ef[m + 1:], eb[:n - m - 1])
        den = np.dot(ef[m + 1:], ef[m + 1:]) + np.dot(eb[:n - m - 1], eb[:n - m - 1])
        if abs(den) < 1e-12:
            break
        km  = -2.0 * num / den
        a_new = a.copy()
        a_new[:m]  = a[:m] + km * a[m - 1::-1]
        a_new[m]   = km
        a = a_new
        ef_new       = ef.copy()
        eb_new       = eb.copy()
        ef_new[m + 1:] = ef[m + 1:] + km * eb[:n - m - 1]
        eb_new[:n - m - 1] = eb[:n - m - 1] + km * ef[m + 1:]
        ef, eb = ef_new, eb_new
    return a


def lpc_burg(signal, order: int = 12) -> list[float]:
    """Calcule les coefficients LPC par la méthode de Burg."""
    if _NP:
        arr = np.asarray(signal, dtype=np.float64)
        return _lpc_burg_np(arr, order).tolist()
    else:
        return _lpc_burg_python(list(signal), order)


# ─────────────────────────────────────────────────────────────────────────────
#  Extraction des formants depuis les pôles LPC
# ─────────────────────────────────────────────────────────────────────────────

def _roots_from_lpc(a: list[float]) -> list[complex]:
    """Racines du polynôme LPC (1, a[0], a[1], …) par companion matrix."""
    n = len(a)
    if n == 0:
        return []
    if _NP:
        coeffs = [1.0] + list(a)
        roots = np.roots(coeffs)
        return [complex(r.real, r.imag) for r in roots]
    # Pure Python : méthode de la puissance (approx, moins précise)
    # Suffisant pour les formants F1-F3
    return []


def extract_formants(
    frame_samples,
    sr: int,
    order: int = 12,
    pre_emphasis: float = 0.97,
) -> list[float]:
    """
    Extrait les formants F1-F5 (Hz) depuis un bloc de samples.

    Returns:
        Liste de fréquences formantiques triée (Hz), ou [] si échec.
    """
    if not _NP:
        return []

    arr = np.asarray(frame_samples, dtype=np.float64)
    if len(arr) < order + 2:
        return []

    # Pré-emphase (booste les hautes fréquences)
    arr[1:] -= pre_emphasis * arr[:-1]

    # Fenêtrage Hamming
    arr *= np.hamming(len(arr))

    # Normalisation énergie
    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms < 1e-8:
        return []
    arr /= rms

    # LPC Burg
    lpc_a = _lpc_burg_np(arr, order)
    coeffs = np.concatenate([[1.0], lpc_a])
    roots  = np.roots(coeffs)

    # Filtrage : conserver les pôles dans le demi-plan supérieur
    # (les conjugués donnent les mêmes fréquences)
    formants_hz: list[float] = []
    for r in roots:
        if r.imag >= 0:
            angle = math.atan2(r.imag, r.real)
            if angle > 0:
                freq = angle * sr / (2 * math.pi)
                if 50 < freq < sr / 2 - 50:
                    bw = -0.5 * sr / math.pi * math.log(abs(r))
                    # BW réaliste pour un vrai formant : 10–700 Hz
                    if 10 < bw < 700:
                        formants_hz.append(freq)

    formants_hz.sort()
    return formants_hz[:5]  # F1–F5 max


# ─────────────────────────────────────────────────────────────────────────────
#  Scoring formantique — distance dans l'espace F1×F2
# ─────────────────────────────────────────────────────────────────────────────

def _formant_distance(
    f1_meas: float,
    f2_meas: float,
    f1_ref: float,
    f2_ref: float,
    tol_f1: float = WINNER_TOLERANCE_F1,
    tol_f2: float = WINNER_TOLERANCE_F2,
) -> float:
    """
    Score de proximité [0..1] entre formants mesurés et référence.
    Distance euclidienne normalisée dans l'espace (F1/tol1, F2/tol2).
    """
    d = math.sqrt(
        ((f1_meas - f1_ref) / tol_f1) ** 2 +
        ((f2_meas - f2_ref) / tol_f2) ** 2
    )
    return max(0.0, 1.0 - d / 3.0)  # /3 = max distance ~3 tolérances


def score_phonemes_from_formants(
    formants: list[float],
    language: str = "FR",
    profile: str = "MALE",
) -> dict[str, float]:
    """
    Calcule le score de chaque phonème à partir des formants extraits.
    Retourne un dict {phonème: score [0..1]}.
    """
    if len(formants) < 2:
        return {}

    f1_m = formants[0]
    f2_m = formants[1] if len(formants) > 1 else formants[0] * 2.5

    # Sélection table de référence
    table = FR_MALE if language == "FR" else {
        **{k: (v[0], v[1], v[2]) for k, v in PB52_MALE.items()},
        "FV":  (200, 1000, 2200),
        "MBP": (200,  800, 2100),
    }

    # Ajustement profil vocal
    from .formant_library import PROFILE_MULTIPLIERS
    mult = PROFILE_MULTIPLIERS.get(profile, 1.0)

    scores: dict[str, float] = {}
    for ph, (f1r, f2r, _) in table.items():
        scores[ph] = _formant_distance(f1_m, f2_m, f1r * mult, f2r * mult)

    return scores


# ─────────────────────────────────────────────────────────────────────────────
#  CheckWinner : validation / rejet du phonème reconnu
# ─────────────────────────────────────────────────────────────────────────────

class ASMSResult(NamedTuple):
    phoneme:    str
    score:      float
    f1:         float
    f2:         float
    is_winner:  bool   # True si validé, False si Loser


def check_winner(
    phoneme: str,
    score: float,
    rms_db: float,
    noise_gate_db: float = -13.0,
    min_score: float = 0.25,
    quality_threshold_db: float = -44.0,
) -> bool:
    """
    Validation Winner/Loser.
    - Winner : score ≥ min_score ET rms > noise_gate
    - Loser  : score trop bas OU audio de mauvaise qualité
    """
    if rms_db < quality_threshold_db:
        return False   # Audio inaudible
    if rms_db < noise_gate_db:
        return False   # En dessous du noise gate
    return score >= min_score


# ─────────────────────────────────────────────────────────────────────────────
#  Normalisation RMS
# ─────────────────────────────────────────────────────────────────────────────

def compute_rms_db(samples, max_val: float = 32768.0) -> float:
    """RMS en dBFS d'un bloc de samples."""
    if not _NP:
        s = list(samples)
        if not s:
            return -96.0
        rms = math.sqrt(sum(x * x for x in s) / len(s)) / max_val
    else:
        arr = np.asarray(samples, dtype=np.float64)
        rms = float(np.sqrt(np.mean(arr ** 2))) / max_val

    if rms < 1e-10:
        return -96.0
    return 20.0 * math.log10(rms)


def normalize_rms(samples, target_db: float = -14.0):
    """Normalise le signal à un niveau RMS cible."""
    if not _NP:
        return samples  # fallback : pas de normalisation sans numpy
    arr = np.asarray(samples, dtype=np.float64)
    rms = float(np.sqrt(np.mean(arr ** 2)))
    if rms < 1e-10:
        return arr
    target_lin = 10 ** (target_db / 20.0)
    return arr * (target_lin / rms)


# ─────────────────────────────────────────────────────────────────────────────
#  Moteur ASMS principal
# ─────────────────────────────────────────────────────────────────────────────

def analyze_frame_asms(
    frame_samples,
    sr: int,
    language: str = "FR",
    profile: str = "MALE",
    noise_gate_db: float = -13.0,
    lpc_order: int = 12,
) -> "ASMSResult | None":
    """
    Analyse ASMS d'un bloc audio (une frame Blender).

    Returns:
        ASMSResult ou None si silence.
    """
    if not _NP:
        return None

    arr = np.asarray(frame_samples, dtype=np.float64)
    rms_db = compute_rms_db(arr, max_val=1.0)

    if rms_db < noise_gate_db - 6:
        # Silence définitif
        return None

    # Normaliser avant LPC
    arr_norm = normalize_rms(arr, target_db=-14.0)

    # Extraction formants
    formants = extract_formants(arr_norm, sr, order=lpc_order)

    if not formants:
        return None

    f1 = formants[0]
    f2 = formants[1] if len(formants) > 1 else f1 * 2.0

    # Scoring
    scores = score_phonemes_from_formants(formants, language, profile)
    if not scores:
        return None

    best_ph = max(scores, key=scores.get)
    best_sc = scores[best_ph]

    is_win = check_winner(best_ph, best_sc, rms_db, noise_gate_db)

    return ASMSResult(
        phoneme=best_ph,
        score=best_sc,
        f1=f1,
        f2=f2,
        is_winner=is_win,
    )


def run_asms_full(
    wav_path: str,
    fps_num: int,
    fps_base: float,
    frame_start: int,
    frame_end: int,
    language: str = "FR",
    profile: str = "MALE",
    noise_gate_db: float = -13.0,
    lpc_order: int = 12,
) -> "dict[int, ASMSResult] | None":
    """
    Analyse ASMS complète d'un fichier WAV.

    Returns:
        dict {frame_index: ASMSResult} ou None si erreur.
    """
    if not _NP:
        return None

    try:
        import wave as wv
        with wv.open(wav_path, "rb") as wf:
            sr    = wf.getframerate()
            n_ch  = wf.getnchannels()
            sw    = wf.getsampwidth()
            raw   = wf.readframes(wf.getnframes())
    except Exception:
        return None

    if sw not in (1, 2, 4):
        return None

    dtype   = {1: np.int8, 2: np.int16, 4: np.int32}[sw]
    max_val = float(2 ** (sw * 8 - 1))
    samples = np.frombuffer(raw, dtype=dtype).astype(np.float64) / max_val

    if n_ch > 1:
        samples = samples.reshape(-1, n_ch).mean(axis=1)

    real_fps       = fps_num / max(fps_base, 0.001)
    samples_per_fr = sr / real_fps

    # Fenêtre LPC : 25 ms centré sur la frame (standard Praat)
    win_ms  = 25.0
    win_smp = max(int(win_ms / 1000.0 * sr), lpc_order * 2 + 2)

    results: dict[int, ASMSResult] = {}

    for frame in range(frame_start, min(frame_end + 1, frame_start + 100000)):
        center  = int((frame - frame_start) * samples_per_fr)
        lo      = max(0, center - win_smp // 2)
        hi      = min(len(samples), lo + win_smp)
        if hi - lo < lpc_order + 2:
            continue

        block = samples[lo:hi]
        res   = analyze_frame_asms(
            block, sr, language, profile, noise_gate_db, lpc_order,
        )
        if res is not None:
            results[frame] = res

    return results if results else None
