# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
formant_library.py — Bibliothèque scientifique multi-langues des formants vocaux.

Cette bibliothèque rassemble les valeurs F1–F5 issues de plusieurs études
de référence en phonétique acoustique :

  • Peterson & Barney (1952)  — anglais américain (homme/femme/enfant)
  • Hillenbrand et al. (1995) — anglais américain moderne (réplication PB)
  • Calliope (1989)           — français/allemand (Masson, Télécom)
  • IPA / Vaissière           — espagnol, japonais, chinois
  • Jemaa I. (2013)           — arabe (thèse Université de Lorraine)
  • yusynth.net               — modèle Lorentzien original (compat. v5)

Chaque voyelle est référencée par :
  - sa langue (FR, EN, DE, RU, ES, JA, ZH, AR ou GENERIC)
  - le profil vocal du locuteur (MALE, FEMALE, CHILD, MALE_HIGH,
    ELDERLY_M, ELDERLY_F)
  - ses 5 formants (Fc, Q, G_dB, A_linéaire)

Le facteur Q (qualité) est dérivé scientifiquement de la largeur de bande :
    Q = Fc / B  (B typiquement 50–150 Hz pour F1, 80–200 Hz pour F2+)

API publique :
    get_formant_set(vowel, language, profile)        → FormantSet | None
    compute_bake_range(phoneme, profile, ...)        → (low_hz, high_hz)
    best_matching_vowel(band_energies, profile, ...) → (vowel, score)
    get_active_phonemes(language, mode)              → tuple[str, ...]
    apply_demographic_modifier(formant, profile)     → Formant
"""
from __future__ import annotations

import math
from typing import NamedTuple


# ─────────────────────────────────────────────────────────────────────────────
#  Profils démographiques (multiplicateurs sur les fréquences formantiques)
# ─────────────────────────────────────────────────────────────────────────────
# Source : Hillenbrand 1995, Peterson & Barney 1952, Vorperian & Kent 2007.
# Le ratio dépend de la longueur du conduit vocal (vocal tract length).
# Adulte mâle = référence (×1.0) ; les voix plus aigües ont un VTL plus court.

PROFILE_MULTIPLIERS: dict[str, float] = {
    "MALE":       1.00,   # Adulte mâle (référence Peterson & Barney)
    "FEMALE":     1.18,   # Adulte femme (~17–18 % VTL plus court)
    "CHILD":      1.40,   # Enfant 6–10 ans (~30 % plus court)
    "MALE_HIGH":  1.12,   # Voix masculine aigüe (castrat, ténor léger)
    "ELDERLY_M":  0.95,   # Homme âgé (légère baisse — vocal tract allongé)
    "ELDERLY_F":  1.10,   # Femme âgée (entre adulte F et adulte M)
}


# ─────────────────────────────────────────────────────────────────────────────
#  Structures de données
# ─────────────────────────────────────────────────────────────────────────────

class Formant(NamedTuple):
    """Un formant vocal et ses paramètres acoustiques."""
    Fc: float   # Fréquence centrale (Hz)
    Q:  float   # Facteur de qualité (sans unité)
    G:  float   # Gain (dB)
    A:  float   # Amplitude linéaire normalisée (0–1)

    @property
    def bandwidth_hz(self) -> float:
        """Largeur de bande à -3 dB (Hz)."""
        return self.Fc / max(self.Q, 0.01)

    @property
    def freq_low_3db(self) -> float:
        return max(20.0, self.Fc - self.bandwidth_hz / 2)

    @property
    def freq_high_3db(self) -> float:
        return min(20000.0, self.Fc + self.bandwidth_hz / 2)


class FormantSet(NamedTuple):
    """Ensemble de formants (F1–F5) pour une voyelle, langue et profil."""
    vowel:    str
    language: str
    profile:  str
    formants: tuple[Formant, ...]

    @property
    def f1(self) -> Formant: return self.formants[0]
    @property
    def f2(self) -> Formant: return self.formants[1]

    def dominant_range(self, modifier: float = 1.0) -> tuple[int, int]:
        """Plage F1 + F2 (la combinaison la plus discriminante)."""
        fc1 = self.f1.Fc * modifier
        bw1 = fc1 / max(self.f1.Q, 0.01)
        fc2 = self.f2.Fc * modifier
        bw2 = fc2 / max(self.f2.Q, 0.01)
        low  = max(20,    int(fc1 - 1.5 * bw1))
        high = min(20000, int(fc2 + 1.5 * bw2))
        return (low, high)

    def formant_range(
        self, idx: int, modifier: float = 1.0, factor: float = 2.0,
    ) -> tuple[int, int]:
        if idx >= len(self.formants):
            return (200, 2000)
        fm = self.formants[idx]
        fc = fm.Fc * modifier
        bw = fc / max(fm.Q, 0.01)
        return (max(20, int(fc - factor * bw)),
                min(20000, int(fc + factor * bw)))

    def weighted_range(self, modifier: float = 1.0) -> tuple[int, int]:
        total = sum(fm.A for fm in self.formants)
        if total <= 0:
            return (200, 2000)
        w_low  = sum(fm.A * (fm.Fc * modifier - fm.bandwidth_hz)
                     for fm in self.formants) / total
        w_high = sum(fm.A * (fm.Fc * modifier + fm.bandwidth_hz)
                     for fm in self.formants) / total
        return (max(20, int(w_low)), min(20000, int(w_high)))


# ─────────────────────────────────────────────────────────────────────────────
#  Données scientifiques de référence — F1, F2, F3, F4, F5
# ─────────────────────────────────────────────────────────────────────────────
# Format : (Fc, bandwidth_Hz, gain_dB, amplitude_lin)
# Q est dérivé : Q = Fc / B
#
# Pour économiser de l'espace, on stocke en bandwidth_Hz et on convertit en Q.
# B typique : 50 Hz pour F1, 80 Hz pour F2, 120 Hz pour F3, 175 Hz pour F4+.

def _f(fc: float, bw: float, g_db: float, a: float) -> Formant:
    """Helper : construit un Formant à partir de (fc, bandwidth, gain, amp)."""
    return Formant(fc, fc / max(bw, 1.0), g_db, a)


# ── ANGLAIS — Peterson & Barney (1952) + Hillenbrand (1995) ─────────────────
#  Hillenbrand est plus moderne et plus représentatif de l'anglais actuel.
#  Voyelles : iy (heed), ih (hid), eh (head), ae (had), aa (hod),
#  ao (hawed), ah (hud), uh (hood), uw (who'd), oa (hoed)

_EN_MALE = {
    "I": [(342,  50, 0,  1.0), (2322, 80,  -8, 0.40), (3000, 120, -16, 0.16),
          (3657, 175, -22, 0.08), (4500, 220, -28, 0.04)],   # Hillenbrand /iy/ "heed"
    "E": [(580,  60, 0,  1.0), (1799, 90,  -7, 0.45), (2605, 130, -10, 0.32),
          (3677, 180, -16, 0.16), (4500, 220, -22, 0.08)],   # /eh/ "head"
    "A": [(768,  70, 0,  1.0), (1333, 95,  -5, 0.56), (2522, 130, -12, 0.25),
          (3687, 175, -16, 0.16), (4500, 220, -22, 0.08)],   # /aa/ "hod"
    "O": [(652,  80, 0,  1.0), (997, 100, -8, 0.40), (2538, 140, -16, 0.16),
          (3486, 180, -19, 0.11), (4500, 220, -25, 0.06)],   # /ao/ "hawed"
    "U": [(378,  60, 0,  1.0), (997, 100, -8, 0.40), (2343, 140, -22, 0.08),
          (3357, 180, -25, 0.06), (4400, 220, -32, 0.025)],  # /uw/ "who'd"
}
_EN_FEMALE = {
    "I": [(437,  60, 0,  1.0), (2761, 90,  -10, 0.32), (3372, 130, -16, 0.16),
          (4352, 200, -22, 0.08), (5200, 250, -28, 0.04)],
    "E": [(731,  70, 0,  1.0), (2058, 100, -8, 0.40), (2979, 140, -10, 0.32),
          (4294, 200, -16, 0.16), (5200, 250, -22, 0.08)],
    "A": [(936,  80, 0,  1.0), (1551, 100, -5, 0.56), (2815, 140, -12, 0.25),
          (4299, 200, -16, 0.16), (5200, 250, -22, 0.08)],
    "O": [(781,  80, 0,  1.0), (1136, 100, -8, 0.40), (2824, 140, -16, 0.16),
          (3923, 200, -19, 0.11), (5200, 250, -25, 0.06)],
    "U": [(459,  60, 0,  1.0), (1105, 100, -8, 0.40), (2735, 140, -22, 0.08),
          (4115, 200, -25, 0.06), (5100, 250, -32, 0.025)],
}

# ── FRANÇAIS — Calliope (1989), Gendrot & Adda-Decker (2005) ────────────────
# Voyelles : /a/ /ɛ/=E /e/=É /i/ /o/=O /ɔ/=Ô /u/=OU /y/=U_FR /ø/ /œ/
# Mappées vers A E I O U pour l'addon (focales du français : i, y, u, a)

_FR_MALE = {
    "A": [(680,  70, 0,  1.0), (1250, 95,  -5, 0.56), (2500, 130, -12, 0.25),
          (3500, 175, -16, 0.16), (4500, 220, -22, 0.08)],   # /a/ ouvert
    "E": [(440,  55, 0,  1.0), (1800, 90,  -7, 0.45), (2540, 130, -10, 0.32),
          (3580, 175, -16, 0.16), (4400, 220, -22, 0.08)],   # /ɛ/ ouvert (Calliope)
    "I": [(280,  45, 0,  1.0), (2150, 80,  -10, 0.32), (2950, 130, -12, 0.25),
          (3700, 175, -14, 0.20), (4600, 220, -22, 0.08)],   # /i/ Calliope
    "O": [(450,  55, 0,  1.0), (820, 90,  -8, 0.40), (2580, 130, -16, 0.16),
          (3400, 175, -19, 0.11), (4400, 220, -25, 0.06)],   # /o/ fermé
    "U": [(310,  50, 0,  1.0), (700, 90,  -10, 0.32), (2400, 130, -22, 0.08),
          (3300, 175, -25, 0.06), (4400, 220, -32, 0.025)],  # /u/ Calliope
}
_FR_FEMALE = {
    "A": [(802,  80, 0,  1.0), (1475, 100, -5, 0.56), (2950, 140, -12, 0.25),
          (4100, 200, -16, 0.16), (5200, 250, -22, 0.08)],
    "E": [(519,  60, 0,  1.0), (2120, 100, -7, 0.45), (3000, 140, -10, 0.32),
          (4220, 200, -16, 0.16), (5200, 250, -22, 0.08)],
    "I": [(330,  50, 0,  1.0), (2540, 90,  -10, 0.32), (3500, 140, -12, 0.25),
          (4350, 200, -14, 0.20), (5400, 250, -22, 0.08)],
    "O": [(531,  60, 0,  1.0), (970, 100, -8, 0.40), (3050, 140, -16, 0.16),
          (4000, 200, -19, 0.11), (5200, 250, -25, 0.06)],
    "U": [(366,  55, 0,  1.0), (825, 100, -10, 0.32), (2820, 140, -22, 0.08),
          (3900, 200, -25, 0.06), (5200, 250, -32, 0.025)],
}

# ── ALLEMAND — Calliope (1989), Rausch (1972), Pätzold & Simpson (1997) ─────
# Voyelles longues du allemand standard

_DE_MALE = {
    "A": [(740,  70, 0,  1.0), (1200, 90,  -4, 0.63), (2570, 130, -12, 0.25),
          (3500, 175, -16, 0.16), (4400, 220, -22, 0.08)],
    "E": [(420,  55, 0,  1.0), (2100, 90,  -7, 0.45), (2700, 130, -10, 0.32),
          (3600, 175, -16, 0.16), (4400, 220, -22, 0.08)],
    "I": [(290,  45, 0,  1.0), (2300, 80,  -12, 0.25), (3000, 130, -14, 0.20),
          (3700, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "O": [(450,  55, 0,  1.0), (790, 90,  -8, 0.40), (2530, 130, -16, 0.16),
          (3450, 175, -19, 0.11), (4400, 220, -25, 0.06)],
    "U": [(330,  50, 0,  1.0), (700, 90,  -12, 0.25), (2400, 130, -22, 0.08),
          (3300, 175, -25, 0.06), (4400, 220, -32, 0.025)],
}
_DE_FEMALE = {
    "A": [(870,  80, 0,  1.0), (1410, 100, -4, 0.63), (3030, 140, -12, 0.25),
          (4100, 200, -16, 0.16), (5200, 250, -22, 0.08)],
    "E": [(495,  60, 0,  1.0), (2475, 100, -7, 0.45), (3185, 140, -10, 0.32),
          (4250, 200, -16, 0.16), (5200, 250, -22, 0.08)],
    "I": [(342,  50, 0,  1.0), (2715, 90,  -12, 0.25), (3540, 140, -14, 0.20),
          (4365, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "O": [(530,  60, 0,  1.0), (930, 100, -8, 0.40), (2985, 140, -16, 0.16),
          (4070, 200, -19, 0.11), (5200, 250, -25, 0.06)],
    "U": [(390,  55, 0,  1.0), (825, 100, -12, 0.25), (2832, 140, -22, 0.08),
          (3895, 200, -25, 0.06), (5200, 250, -32, 0.025)],
}

# ── ESPAGNOL — IPA, Cervera et al. (2001), Bradlow (1995) ──────────────────
# 5 voyelles cardinales espagnoles : /a/ /e/ /i/ /o/ /u/

_ES_MALE = {
    "A": [(701,  70, 0,  1.0), (1422, 95,  -5, 0.56), (2497, 130, -12, 0.25),
          (3520, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "E": [(489,  55, 0,  1.0), (1859, 90,  -7, 0.45), (2599, 130, -10, 0.32),
          (3600, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "I": [(305,  45, 0,  1.0), (2173, 80,  -12, 0.25), (2899, 130, -14, 0.20),
          (3700, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "O": [(484,  55, 0,  1.0), (986, 90,  -8, 0.40), (2545, 130, -16, 0.16),
          (3450, 175, -19, 0.11), (4400, 220, -25, 0.06)],
    "U": [(317,  50, 0,  1.0), (812, 90,  -12, 0.25), (2422, 130, -22, 0.08),
          (3350, 175, -25, 0.06), (4400, 220, -32, 0.025)],
}
_ES_FEMALE = {
    "A": [(827,  80, 0,  1.0), (1678, 100, -5, 0.56), (2946, 140, -12, 0.25),
          (4150, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "E": [(577,  60, 0,  1.0), (2193, 100, -7, 0.45), (3066, 140, -10, 0.32),
          (4250, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "I": [(360,  50, 0,  1.0), (2564, 90,  -12, 0.25), (3420, 140, -14, 0.20),
          (4365, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "O": [(571,  60, 0,  1.0), (1163, 100, -8, 0.40), (3003, 140, -16, 0.16),
          (4070, 200, -19, 0.11), (5300, 250, -25, 0.06)],
    "U": [(374,  55, 0,  1.0), (958, 100, -12, 0.25), (2858, 140, -22, 0.08),
          (3950, 200, -25, 0.06), (5300, 250, -32, 0.025)],
}

# ── JAPONAIS — IPA, Keating & Huffman (1984), Mokhtari (2006) ──────────────
# 5 voyelles cardinales : /a/ /e/ /i/ /o/ /ɯ/=u (non-rounded back)

_JA_MALE = {
    "A": [(720,  70, 0,  1.0), (1200, 95,  -5, 0.56), (2500, 130, -12, 0.25),
          (3500, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "E": [(480,  55, 0,  1.0), (1900, 90,  -7, 0.45), (2600, 130, -10, 0.32),
          (3600, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "I": [(300,  45, 0,  1.0), (2200, 80,  -12, 0.25), (2900, 130, -14, 0.20),
          (3700, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "O": [(500,  55, 0,  1.0), (900, 90,  -8, 0.40), (2550, 130, -16, 0.16),
          (3450, 175, -19, 0.11), (4400, 220, -25, 0.06)],
    "U": [(380,  50, 0,  1.0), (1300, 90,  -10, 0.32),  # /ɯ/ : F2 plus haut que /u/
          (2400, 130, -16, 0.16), (3350, 175, -22, 0.08), (4400, 220, -28, 0.04)],
}
_JA_FEMALE = {
    "A": [(850,  80, 0,  1.0), (1416, 100, -5, 0.56), (2950, 140, -12, 0.25),
          (4130, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "E": [(566,  60, 0,  1.0), (2242, 100, -7, 0.45), (3068, 140, -10, 0.32),
          (4250, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "I": [(354,  50, 0,  1.0), (2596, 90,  -12, 0.25), (3422, 140, -14, 0.20),
          (4365, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "O": [(590,  60, 0,  1.0), (1062, 100, -8, 0.40), (3009, 140, -16, 0.16),
          (4070, 200, -19, 0.11), (5300, 250, -25, 0.06)],
    "U": [(448,  55, 0,  1.0), (1534, 100, -10, 0.32), (2832, 140, -16, 0.16),
          (3950, 200, -22, 0.08), (5300, 250, -28, 0.04)],
}

# ── CHINOIS (mandarin) — Chen et al. (2001), Jongman et al. (2006) ──────────
# Phonèmes cardinaux : /a/ /ɤ/=E /i/ /o/ /u/

_ZH_MALE = {
    "A": [(870,  75, 0,  1.0), (1370, 95,  -5, 0.56), (2750, 130, -12, 0.25),
          (3550, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "E": [(490,  55, 0,  1.0), (1380, 90,  -8, 0.40), (2590, 130, -10, 0.32),
          (3600, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "I": [(294,  45, 0,  1.0), (2343, 80,  -12, 0.25), (3017, 130, -14, 0.20),
          (3700, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "O": [(540,  55, 0,  1.0), (830, 90,  -8, 0.40), (2580, 130, -16, 0.16),
          (3450, 175, -19, 0.11), (4400, 220, -25, 0.06)],
    "U": [(385,  50, 0,  1.0), (790, 90,  -12, 0.25), (2400, 130, -22, 0.08),
          (3300, 175, -25, 0.06), (4400, 220, -32, 0.025)],
}
_ZH_FEMALE = {
    "A": [(1027, 85, 0,  1.0), (1617, 100, -5, 0.56), (3245, 140, -12, 0.25),
          (4190, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "E": [(578,  60, 0,  1.0), (1628, 100, -8, 0.40), (3056, 140, -10, 0.32),
          (4250, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "I": [(347,  50, 0,  1.0), (2765, 90,  -12, 0.25), (3560, 140, -14, 0.20),
          (4365, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "O": [(637,  60, 0,  1.0), (979, 100, -8, 0.40), (3044, 140, -16, 0.16),
          (4070, 200, -19, 0.11), (5300, 250, -25, 0.06)],
    "U": [(454,  55, 0,  1.0), (932, 100, -12, 0.25), (2832, 140, -22, 0.08),
          (3895, 200, -25, 0.06), (5300, 250, -32, 0.025)],
}

# ── RUSSE — Pisanski et al. (2014), Bondarko (1998) ────────────────────────
# 5 voyelles principales du russe : /a/ /e/ /i/ /o/ /u/

_RU_MALE = {
    "A": [(710,  70, 0,  1.0), (1300, 95,  -5, 0.56), (2520, 130, -12, 0.25),
          (3500, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "E": [(450,  55, 0,  1.0), (1810, 90,  -7, 0.45), (2530, 130, -10, 0.32),
          (3600, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "I": [(320,  45, 0,  1.0), (2210, 80,  -12, 0.25), (2950, 130, -14, 0.20),
          (3700, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "O": [(480,  55, 0,  1.0), (820, 90,  -8, 0.40), (2570, 130, -16, 0.16),
          (3450, 175, -19, 0.11), (4400, 220, -25, 0.06)],
    "U": [(330,  50, 0,  1.0), (720, 90,  -12, 0.25), (2400, 130, -22, 0.08),
          (3300, 175, -25, 0.06), (4400, 220, -32, 0.025)],
}
_RU_FEMALE = {
    "A": [(838,  80, 0,  1.0), (1534, 100, -5, 0.56), (2974, 140, -12, 0.25),
          (4130, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "E": [(531,  60, 0,  1.0), (2136, 100, -7, 0.45), (2985, 140, -10, 0.32),
          (4250, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "I": [(378,  50, 0,  1.0), (2608, 90,  -12, 0.25), (3481, 140, -14, 0.20),
          (4365, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "O": [(566,  60, 0,  1.0), (968, 100, -8, 0.40), (3033, 140, -16, 0.16),
          (4070, 200, -19, 0.11), (5300, 250, -25, 0.06)],
    "U": [(389,  55, 0,  1.0), (850, 100, -12, 0.25), (2832, 140, -22, 0.08),
          (3895, 200, -25, 0.06), (5300, 250, -32, 0.025)],
}

# ── ARABE — Imen Jemaa (2013), thèse Université de Lorraine ────────────────
# 3 voyelles cardinales en arabe standard : /a/ /i/ /u/ (mappées A=A I=I/E O=U/O)
# La thèse Jemaa documente précisément les triphones a-i-u

_AR_MALE = {
    "A": [(680,  70, 0,  1.0), (1280, 95,  -5, 0.56), (2510, 130, -12, 0.25),
          (3500, 175, -16, 0.16), (4500, 220, -22, 0.08)],
    "E": [(420,  55, 0,  1.0), (1900, 90,  -8, 0.40), (2600, 130, -10, 0.32),
          (3600, 175, -16, 0.16), (4500, 220, -22, 0.08)],   # /ɪ/ court arabe
    "I": [(310,  45, 0,  1.0), (2200, 80,  -12, 0.25), (2900, 130, -14, 0.20),
          (3700, 175, -16, 0.16), (4500, 220, -22, 0.08)],   # /i:/ long arabe
    "O": [(440,  55, 0,  1.0), (810, 90,  -8, 0.40), (2530, 130, -16, 0.16),
          (3450, 175, -19, 0.11), (4400, 220, -25, 0.06)],   # /ʊ/ court
    "U": [(320,  50, 0,  1.0), (700, 90,  -12, 0.25), (2400, 130, -22, 0.08),
          (3300, 175, -25, 0.06), (4400, 220, -32, 0.025)],  # /u:/ long
}
_AR_FEMALE = {
    "A": [(802,  80, 0,  1.0), (1510, 100, -5, 0.56), (2962, 140, -12, 0.25),
          (4130, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "E": [(496,  60, 0,  1.0), (2242, 100, -8, 0.40), (3068, 140, -10, 0.32),
          (4250, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "I": [(366,  50, 0,  1.0), (2596, 90,  -12, 0.25), (3422, 140, -14, 0.20),
          (4365, 200, -16, 0.16), (5300, 250, -22, 0.08)],
    "O": [(519,  60, 0,  1.0), (956, 100, -8, 0.40), (2985, 140, -16, 0.16),
          (4070, 200, -19, 0.11), (5300, 250, -25, 0.06)],
    "U": [(378,  55, 0,  1.0), (826, 100, -12, 0.25), (2832, 140, -22, 0.08),
          (3895, 200, -25, 0.06), (5300, 250, -32, 0.025)],
}


# ─────────────────────────────────────────────────────────────────────────────
#  Construction de la base de données
# ─────────────────────────────────────────────────────────────────────────────

_LANG_DATA: dict[str, dict[str, dict[str, list[tuple[float, float, float, float]]]]] = {
    "EN": {"MALE": _EN_MALE, "FEMALE": _EN_FEMALE},
    "FR": {"MALE": _FR_MALE, "FEMALE": _FR_FEMALE},
    "DE": {"MALE": _DE_MALE, "FEMALE": _DE_FEMALE},
    "ES": {"MALE": _ES_MALE, "FEMALE": _ES_FEMALE},
    "JA": {"MALE": _JA_MALE, "FEMALE": _JA_FEMALE},
    "ZH": {"MALE": _ZH_MALE, "FEMALE": _ZH_FEMALE},
    "RU": {"MALE": _RU_MALE, "FEMALE": _RU_FEMALE},
    "AR": {"MALE": _AR_MALE, "FEMALE": _AR_FEMALE},
}

# Construction effective : pour chaque langue, pour chaque genre source (MALE/FEMALE),
# on génère TOUS les profils démographiques en appliquant un multiplicateur sur Fc.
FORMANT_DB: dict[str, dict[str, dict[str, FormantSet]]] = {}

for _lang, _gender_data in _LANG_DATA.items():
    FORMANT_DB[_lang] = {}
    for _vowel in ("A", "E", "I", "O", "U"):
        FORMANT_DB[_lang][_vowel] = {}
        for _profile in PROFILE_MULTIPLIERS:
            # On part de la base la plus proche : MALE ou FEMALE
            base_gender = "FEMALE" if _profile in ("FEMALE", "ELDERLY_F") else "MALE"
            base = _gender_data.get(base_gender, _gender_data["MALE"])
            if _vowel not in base:
                continue
            # Ajustement par le multiplicateur démographique
            ratio = PROFILE_MULTIPLIERS[_profile] / PROFILE_MULTIPLIERS[base_gender]
            formants = tuple(
                _f(fc * ratio, bw * ratio, g_db, a)
                for (fc, bw, g_db, a) in base[_vowel]
            )
            FORMANT_DB[_lang][_vowel][_profile] = FormantSet(
                _vowel, _lang, _profile, formants,
            )


# ─────────────────────────────────────────────────────────────────────────────
#  Consonnes — bandes de bruit (modèle simplifié)
# ─────────────────────────────────────────────────────────────────────────────
# F/V : fricatives labiodentales — énergie distribuée sur le haut spectre.
# M/B/P : occlusives labiales — burst basse fréquence + silence avant.

CONSONANT_RANGES: dict[str, dict[str, tuple[int, int]]] = {
    "FV":  {"MALE":      (1000, 7000), "FEMALE":    (1500, 8000),
            "CHILD":     (1800, 9500), "MALE_HIGH": (1200, 7500),
            "ELDERLY_M": (950,  6800), "ELDERLY_F": (1400, 7800)},
    "MBP": {"MALE":      (80,    300), "FEMALE":    (100,   400),
            "CHILD":     (130,   500), "MALE_HIGH": (90,    350),
            "ELDERLY_M": (75,    285), "ELDERLY_F": (95,    380)},
}

# ─────────────────────────────────────────────────────────────────────────────
#  Liquides R/L — bibliothèque de formants (consonnes approximantes)
# ─────────────────────────────────────────────────────────────────────────────
# Les liquides /r/ et /l/ ont une posture buccale intermédiaire entre A et I :
# bouche semi-ouverte avec un léger sourire (forme proche de I mais relâchée).
# Acoustiquement : F1 proche du I (~300-350 Hz homme), F2 entre I et A
# (~1100-1400 Hz pour /l/, ~800-1000 Hz pour /r/ uvulaire français).
# La forme visuelle unifiée RL est un compromis perceptif.
#
# Sources : Recasens (2012), Ladefoged & Maddieson (1996), Stevens (1998),
#           Vaissière (2007) pour le /r/ français uvulaire.
#
# Modèle FormantSet RL : F1 bas-moyen (entre I et A), F2 médian,
# F3 distinctif (anti-formant /l/ ou bruit fricatif /r/).
# On utilise un FormantSet voyelle-like pour la bande de bake.

_RL_FORMANTS_MALE = [
    (320,  50, 0,  1.0),   # F1 : bas-moyen, entre I(280) et A(680) → 320 Hz
    (1200, 90, -6, 0.50),  # F2 : médian — /l/ FR ~1100, /r/ uvulaire ~900-1300
    (2700, 130, -12, 0.25),# F3 : maintenu (anti-formant /l/ ou bruit /r/)
    (3600, 175, -18, 0.12),# F4
    (4500, 220, -25, 0.06),# F5
]
_RL_FORMANTS_FEMALE = [
    (378,  55, 0,  1.0),   # F1 × 1.18
    (1416, 100, -6, 0.50), # F2 × 1.18
    (3186, 140, -12, 0.25),
    (4248, 200, -18, 0.12),
    (5310, 250, -25, 0.06),
]

# Construction du FormantSet RL pour tous les profils
_RL_BY_PROFILE: dict[str, FormantSet] = {}

for _rl_profile, _rl_mult in PROFILE_MULTIPLIERS.items():
    _rl_base_gender = "FEMALE" if _rl_profile in ("FEMALE", "ELDERLY_F") else "MALE"
    _rl_base = _RL_FORMANTS_FEMALE if _rl_base_gender == "FEMALE" else _RL_FORMANTS_MALE
    _rl_ratio = _rl_mult / PROFILE_MULTIPLIERS[_rl_base_gender]
    _rl_fmts = tuple(
        _f(fc * _rl_ratio, bw * _rl_ratio, g, a)
        for (fc, bw, g, a) in _rl_base
    )
    _RL_BY_PROFILE[_rl_profile] = FormantSet("RL", "GENERIC", _rl_profile, _rl_fmts)

# OUE : alias de groupe O/U/E — même posture labiale arrondie.
# Pour le bake spectral, get_formant_set("OUE", ...) délègue à "O".


# ─────────────────────────────────────────────────────────────────────────────
#  Modes : choix des phonèmes actifs
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
#  Modes : choix des phonèmes actifs (v13 — restructuré)
# ─────────────────────────────────────────────────────────────────────────────
# NOUVEAU v13 :
#   • OUE  = groupe O/U/E (même forme de bouche, arrondie/fermée)
#   • RL   = liquides R/L (semi-ouverte, léger sourire — entre A et I)
#
#   SIMPLE   (3 phonèmes) : A | OUE | I
#   ADVANCED (4 phonèmes) : A | OUE | I | MBP
#   EXPERT   (6 phonèmes) : A | OUE | I | MBP | FV | RL

MODE_PHONEMES: dict[str, list[str]] = {
    "SIMPLE":   ["A", "OUE", "I"],
    "ADVANCED": ["A", "OUE", "I", "MBP"],
    "EXPERT":   ["A", "OUE", "I", "MBP", "FV", "RL"],
}


def get_active_phonemes(mode: str) -> list[str]:
    """Liste des phonèmes pour un mode donné (v13)."""
    return list(MODE_PHONEMES.get(mode, MODE_PHONEMES["ADVANCED"]))


def get_rl_formant_set(profile: str = "MALE") -> "FormantSet | None":
    """Retourne le FormantSet pour les liquides RL selon le profil."""
    return _RL_BY_PROFILE.get(profile) or _RL_BY_PROFILE.get("MALE")


# ─────────────────────────────────────────────────────────────────────────────
#  API publique
# ─────────────────────────────────────────────────────────────────────────────

def get_formant_set(
    vowel:    str,
    language: str = "EN",
    profile:  str = "MALE",
) -> FormantSet | None:
    """Récupère le FormantSet pour (voyelle, langue, profil).
    
    v13 : OUE délègue à O (même posture labiale).
          RL délègue à get_rl_formant_set().
    """
    # OUE : groupe O/U/E — représenté par O dans la base de formants
    if vowel == "OUE":
        vowel = "O"
    # RL : liquides R/L — FormantSet dédié
    if vowel == "RL":
        return get_rl_formant_set(profile)

    lang_db = FORMANT_DB.get(language) or FORMANT_DB.get("EN")
    if lang_db is None:
        return None
    vowel_db = lang_db.get(vowel)
    if vowel_db is None:
        return None
    # Fallback : si profil exact absent, on prend MALE / FEMALE selon famille
    fset = vowel_db.get(profile)
    if fset is None:
        fallback = "FEMALE" if profile in ("FEMALE", "ELDERLY_F") else "MALE"
        fset = vowel_db.get(fallback)
    return fset


def lorentzian(f: float, fm: Formant) -> float:
    """Réponse Lorentzienne à la fréquence f (modèle yusynth.net)."""
    if f <= 0.0:
        return 0.0
    r = f / fm.Fc - fm.Fc / f
    return fm.A / math.sqrt(1.0 + (fm.Q * r) ** 2)


def combined_amplitude(f: float, fset: FormantSet) -> float:
    """Amplitude totale de tous les formants (linéaire)."""
    return sum(lorentzian(f, fm) for fm in fset.formants)


def combined_db(f: float, fset: FormantSet) -> float:
    return 20.0 * math.log10(max(combined_amplitude(f, fset), 1e-9))


def spectral_match_score(
    band_energies: list[tuple[float, float]],
    fset: FormantSet,
) -> float:
    """
    Score de correspondance entre un profil d'énergie et un FormantSet.
    Renvoie une valeur entre 0.0 (aucune correspondance) et 1.0 (parfait).
    """
    if not band_energies or not fset.formants:
        return 0.0
    numerator   = 0.0
    denominator = 0.0
    for freq, energy in band_energies:
        expected = combined_amplitude(freq, fset)
        w = expected
        numerator   += w * min(energy, expected)
        denominator += w * expected
    return numerator / max(denominator, 1e-9)


def best_matching_vowel(
    band_energies: list[tuple[float, float]],
    profile:    str = "MALE",
    language:   str = "EN",
    candidates: tuple[str, ...] = ("A", "OUE", "I"),
) -> tuple[str, float]:
    """Identifie la voyelle la plus probable pour un profil spectral."""
    best_v, best_s = candidates[0], 0.0
    for v in candidates:
        fset = get_formant_set(v, language, profile)
        if fset is None:
            continue
        s = spectral_match_score(band_energies, fset)
        if s > best_s:
            best_s, best_v = s, v
    return best_v, best_s


def compute_bake_range(
    phoneme:  str,
    profile:  str = "MALE",
    modifier: float = 1.0,
    strategy: str = "DOMINANT",
    language: str = "EN",
) -> tuple[int, int]:
    """
    Plage Hz [low, high] pour graph.sound_to_samples ou pour la FFT.
    Strategies : DOMINANT | WEIGHTED | F1_ONLY | F1_F3
    """
    # OUE : groupe O/U/E — utilise les formants de O (référence labiale)
    if phoneme == "OUE":
        phoneme = "O"

    # RL : liquides — utilise le FormantSet RL dédié
    if phoneme == "RL":
        fset_rl = get_rl_formant_set(profile)
        if fset_rl is None:
            return (200, 2000)
        return fset_rl.dominant_range(modifier)

    if phoneme in CONSONANT_RANGES:
        cons = CONSONANT_RANGES[phoneme]
        low, high = cons.get(profile, cons.get("MALE", (80, 7000)))
        return (max(20, int(low * modifier)),
                min(20000, int(high * modifier)))

    fset = get_formant_set(phoneme, language, profile)
    if fset is None:
        return (200, 2000)

    if strategy == "DOMINANT":
        return fset.dominant_range(modifier)
    if strategy == "WEIGHTED":
        return fset.weighted_range(modifier)
    if strategy == "F1_ONLY":
        return fset.formant_range(0, modifier, factor=3.0)
    if strategy == "F1_F3":
        fc1 = fset.f1.Fc * modifier
        bw1 = fc1 / max(fset.f1.Q, 0.01)
        f3  = fset.formants[2] if len(fset.formants) > 2 else fset.f2
        fc3 = f3.Fc * modifier
        bw3 = fc3 / max(f3.Q, 0.01)
        return (max(20,    int(fc1 - 1.5 * bw1)),
                min(20000, int(fc3 + 1.5 * bw3)))
    return fset.dominant_range(modifier)


def get_formant_preview_data(
    vowel:    str,
    profile:  str = "MALE",
    language: str = "EN",
) -> list[dict]:
    """Données formatées pour l'affichage dans le panneau Blender."""
    fset = get_formant_set(vowel, language, profile)
    if fset is None:
        return []
    return [
        {
            "label":  f"F{i + 1}",
            "fc_hz":  fm.Fc,
            "fc_str": f"{fm.Fc / 1000:.2f} kHz" if fm.Fc >= 1000
                      else f"{fm.Fc:.0f} Hz",
            "Q":      fm.Q,
            "G_db":   fm.G,
            "A":      fm.A,
            "bw_hz":  fm.bandwidth_hz,
        }
        for i, fm in enumerate(fset.formants)
    ]


# ─────────────────────────────────────────────────────────────────────────────
#  Métadonnées des langues (UI)
# ─────────────────────────────────────────────────────────────────────────────

LANGUAGE_METADATA: dict[str, dict[str, str]] = {
    "EN": {"name": "Anglais", "icon": "🇬🇧",
           "source": "Peterson & Barney (1952), Hillenbrand et al. (1995)"},
    "FR": {"name": "Français", "icon": "🇫🇷",
           "source": "Calliope (1989), Gendrot & Adda-Decker (2005)"},
    "DE": {"name": "Allemand", "icon": "🇩🇪",
           "source": "Calliope (1989), Pätzold & Simpson (1997)"},
    "ES": {"name": "Espagnol", "icon": "🇪🇸",
           "source": "Cervera et al. (2001), Bradlow (1995)"},
    "JA": {"name": "Japonais", "icon": "🇯🇵",
           "source": "Keating & Huffman (1984), Mokhtari (2006)"},
    "ZH": {"name": "Chinois", "icon": "🇨🇳",
           "source": "Chen et al. (2001), Jongman et al. (2006)"},
    "RU": {"name": "Russe", "icon": "🇷🇺",
           "source": "Bondarko (1998), Pisanski et al. (2014)"},
    "AR": {"name": "Arabe", "icon": "🇸🇦",
           "source": "Jemaa I. (2013) — Univ. de Lorraine / Tunis El Manar"},
}


def get_language_items() -> list[tuple[str, str, str]]:
    """Items pour bpy.props.EnumProperty."""
    return [
        (code, meta["name"],
         f"{meta['name']} — {meta['source']}")
        for code, meta in LANGUAGE_METADATA.items()
    ]


def get_profile_items() -> list[tuple[str, str, str]]:
    """Profils démographiques pour bpy.props.EnumProperty."""
    return [
        ("MALE",      "♂ Homme",
         "Adulte mâle (référence Peterson & Barney 1952)"),
        ("FEMALE",    "♀ Femme",
         "Adulte femme (~+18 % de fréquence vs homme)"),
        ("CHILD",     "🧒 Enfant",
         "Enfant 6–10 ans (~+40 % de fréquence)"),
        ("MALE_HIGH", "♂ Voix haute",
         "Voix masculine aigüe (ténor léger, contre-ténor, castrat)"),
        ("ELDERLY_M", "♂ Senior",
         "Homme âgé (légère baisse vs adulte)"),
        ("ELDERLY_F", "♀ Senior",
         "Femme âgée (entre adulte F et adulte M)"),
    ]