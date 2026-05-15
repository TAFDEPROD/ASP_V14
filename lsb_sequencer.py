# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
lsb_sequencer.py — Séquençage type LipSyncBlender (Vosk → phonemizer / espeak → IPA → visèmes ASP).

Si phonemizer (+ backend espeak) est disponible, `build_words` produit une liste de mots
compatible `write_lipsync_keyframes` (même format que `_build_words_from_vosk_g2p`).
Sinon `is_available()` est False et le séquenceur retombe sur le G2P français embarqué.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
from typing import Callable, Optional

# ─────────────────────────────────────────────────────────────────────────────
# IPA → visème ASP (aligné sur french_g2p / preset v13)
# ─────────────────────────────────────────────────────────────────────────────

_IPA_TO_ASP: dict[str, str] = {
    "": "SIL",
    " ": "SIL",
    "ʔ": "SIL",
    "a": "A",
    "aː": "A",
    "æ": "A",
    "ä": "A",
    "ɐ": "A",
    "ɑ": "A",
    "ɑː": "A",
    "ɶ": "A",
    "e": "OUE",
    "eː": "OUE",
    "ɛ": "OUE",
    "ɛː": "OUE",
    "ə": "OUE",
    "ɘ": "OUE",
    "ɜ": "OUE",
    "ɜː": "OUE",
    "ɝ": "OUE",
    "o": "OUE",
    "oː": "OUE",
    "ɔ": "OUE",
    "ɔː": "OUE",
    "u": "OUE",
    "uː": "OUE",
    "ʉ": "OUE",
    "ʏ": "OUE",
    "y": "OUE",
    "yː": "OUE",
    "ø": "OUE",
    "øː": "OUE",
    "œ": "OUE",
    "ɵ": "OUE",
    "ʌ": "OUE",
    "ʊ": "OUE",
    "ɯ": "OUE",
    "ɑ̃": "OUE",
    "ɛ̃": "OUE",
    "œ̃": "OUE",
    "ɔ̃": "OUE",
    "i": "I",
    "iː": "I",
    "ɪ": "I",
    "ɨ": "I",
    "m": "MBP",
    "b": "MBP",
    "p": "MBP",
    "mː": "MBP",
    "pː": "MBP",
    "bː": "MBP",
    "f": "FV",
    "v": "FV",
    "fː": "FV",
    "vː": "FV",
    "r": "RL",
    "ɾ": "RL",
    "ʁ": "RL",
    "ʀ": "RL",
    "ɹ": "RL",
    "ɻ": "RL",
    "l": "RL",
    "ɫ": "RL",
    "ɭ": "RL",
    "ʎ": "RL",
    "w": "RL",
    "ɰ": "RL",
    "j": "I",
}

_VOWEL_FALLBACK_ORDER = ("A", "OUE", "I", "RL", "MBP", "FV")

_VOSK_TO_ESPEAK: dict[str, str] = {
    "fr": "fr-fr",
    "en": "en-us",
    "de": "de",
    "es": "es",
    "it": "it-it",
    "pt": "pt-br",
    "ru": "ru",
    "uk": "uk",
    "tr": "tr",
    "nl": "nl",
    "sv": "sv",
    "pl": "pl",
    "cs": "cs",
    "ca": "ca",
    "fa": "fa",
    "el": "el",
    "hi": "hi",
    "ja": "ja",
    "ko": "ko",
    "zh": "cmn",
}

_phonemize_fn: Optional[Callable[..., str]] = None
_phonemizer_ok = False
_init_done = False


def _try_import_phonemizer() -> bool:
    global _phonemize_fn, _phonemizer_ok
    try:
        from phonemizer import phonemize  # type: ignore

        _phonemize_fn = phonemize
        _phonemizer_ok = True
        return True
    except ImportError:
        return False


def _try_install_phonemizer() -> bool:
    addon_dir = pathlib.Path(__file__).parent
    wheels_dir = addon_dir / "wheels"
    wheel_path: Optional[pathlib.Path] = None
    if wheels_dir.is_dir():
        for whl in wheels_dir.glob("phonemizer*.whl"):
            wheel_path = whl
            break
    if wheel_path is not None:
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--quiet", str(wheel_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode == 0:
                return _try_import_phonemizer()
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "phonemizer", "--quiet", "--no-deps"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if r.returncode == 0:
            return _try_import_phonemizer()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def _ensure_init() -> None:
    global _init_done
    if _init_done:
        return
    _init_done = True
    if _try_import_phonemizer():
        print("[ASP-LSB] phonemizer disponible — mode LipSyncBlender actif.")
        return
    print("[ASP-LSB] phonemizer absent — tentative d'installation…")
    if _try_install_phonemizer():
        print("[ASP-LSB] phonemizer installé — mode LipSyncBlender actif.")
    else:
        print("[ASP-LSB] phonemizer non disponible — fallback G2P embarqué.")


def is_available() -> bool:
    _ensure_init()
    return _phonemizer_ok


def _vosk_lang_to_espeak(vosk_lang: str) -> str:
    if not vosk_lang or vosk_lang == "none":
        return "fr-fr"
    return _VOSK_TO_ESPEAK.get(vosk_lang, "en-us")


def _tokenize_ipa(ipa: str) -> list[str]:
    """Découpe une sortie phonemizer (souvent 'b ɑ̃ ʒ u') en tokens."""
    s = ipa.strip()
    if not s:
        return []
    parts = re.split(r"\s+", s)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        out.append(p)
    return out


def _ipa_tokens_to_visemes(tokens: list[str], available: list[str]) -> list[str]:
    avail = set(available)
    seq: list[str] = []
    for tok in tokens:
        _ipa_to_seq_one_token(tok, avail, seq)
    dedup: list[str] = []
    for v in seq:
        if not dedup or dedup[-1] != v:
            dedup.append(v)
    if not dedup:
        for cand in _VOWEL_FALLBACK_ORDER:
            if cand in avail:
                return [cand]
        if available:
            return [available[0]]
        return ["A"]
    return dedup


def _ipa_to_seq_one_token(tok: str, avail: set[str], seq: list[str]) -> None:
    """Ajoute les visèmes pour un token IPA (modifie seq in-place)."""
    asp = _IPA_TO_ASP.get(tok)
    if asp and asp != "SIL" and asp in avail:
        seq.append(asp)
        return
    i = 0
    n = len(tok)
    while i < n:
        matched = False
        for ln in range(min(6, n - i), 0, -1):
            sub = tok[i : i + ln]
            a = _IPA_TO_ASP.get(sub)
            if a and a != "SIL" and a in avail:
                seq.append(a)
                i += ln
                matched = True
                break
        if not matched:
            i += 1


def _phonemize_word(word: str, espeak_lang: str) -> str:
    if not _phonemize_fn:
        return ""
    last: Exception | None = None
    for backend in ("espeak", "espeak-ng"):
        try:
            return str(
                _phonemize_fn(
                    word,
                    language=espeak_lang,
                    backend=backend,
                    strip=True,
                    preserve_punctuation=False,
                )
            ).strip()
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    print(f"[ASP-LSB] phonemizer backends échoués ({word!r}): {last!r}")
    return ""


def build_words(
    vosk_words_raw: list,
    real_fps: float,
    frame_start: int,
    frame_end: int,
    vosk_lang: str,
    available_phonemes: list[str],
) -> list[dict]:
    """
    Construit la liste `words` au format séquenceur (visèmes répartis sur la durée du mot).
    Retourne [] si phonemizer indisponible.
    """
    if not is_available():
        return []
    espeak = _vosk_lang_to_espeak(vosk_lang)
    words: list[dict] = []
    for vw in vosk_words_raw:
        text = getattr(vw, "text", "") or ""
        if not text.strip():
            continue
        f_start = frame_start + int(round(float(vw.start_s) * real_fps))
        f_end = frame_start + int(round(float(vw.end_s) * real_fps))
        f_start = max(frame_start, min(frame_end, f_start))
        f_end = max(frame_start, min(frame_end, f_end))
        if f_end <= f_start:
            f_end = f_start + 1
        duration = f_end - f_start
        ipa = _phonemize_word(text, espeak)
        tokens = _tokenize_ipa(ipa)
        vis_seq = _ipa_tokens_to_visemes(tokens, available_phonemes)
        n_vis = len(vis_seq)
        if n_vis > 0 and duration > 0:
            parts = duration / n_vis
            visemes = []
            for idx, ph in enumerate(vis_seq):
                frame = f_start + round(parts * idx)
                frame = max(f_start, min(f_end, frame))
                visemes.append((frame, ph))
        else:
            fb = vis_seq[0] if vis_seq else (available_phonemes[0] if available_phonemes else "A")
            visemes = [(f_start, fb)]
        words.append(
            {
                "word_frame_start": f_start,
                "word_frame_end": f_end,
                "visemes": visemes,
            }
        )
    return words
