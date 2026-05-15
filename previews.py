# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
previews.py — Aperçu animé de la bouche (v7.1).

v7.1 — synchronisation stricte avec l'audio :
  - La preview est intrinsèquement liée à la lecture audio :
      * démarre quand l'audio démarre
      * s'arrête quand l'audio s'arrête (fin naturelle OU clic Stop)
  - Le _timer_tick interroge audio_core.is_audio_playing() (état réel
    du handle aud) plutôt que le flag UI playback.is_playing — ce qui
    permet de couper proprement la preview quand l'audio se termine
    de lui-même.
  - Le cycle de visèmes est construit dynamiquement à partir des
    paramètres Animation pour que l'utilisateur voie en temps réel
    l'effet de inbetween_ms / silence_ms / close_duration_ms /
    silence_stretch.
"""

from __future__ import annotations

import os
from typing import Optional

import bpy

try:
    import bpy.utils.previews as _bpy_previews
    _PREVIEWS_AVAILABLE = True
except (ImportError, AttributeError):
    _bpy_previews = None  # type: ignore[assignment]
    _PREVIEWS_AVAILABLE = False

# ─── État global ──────────────────────────────────────────────────────────────

_preview_collection = None  # type: ignore[assignment]
_animation_running: bool = False
_animation_index: int = 0
_driven_by_audio: bool = False  # True si c'est l'audio qui pilote la preview

DEMO_CYCLE: tuple[str, ...] = (
    "SIL", "A", "OUE", "I", "MBP", "FV", "RL", "SIL",
    "SIL", "A", "OUE", "I", "MBP", "FV", "SIL",
)

# ─────────────────────────────────────────────────────────────────────────────
# Icônes
# ─────────────────────────────────────────────────────────────────────────────

ICON_FILES: dict[str, str] = {
    "SIL":  "mouth_SIL.png",
    "A":    "mouth_A.png",
    "E":    "mouth_E.png",
    "I":    "mouth_I.png",
    "O":    "mouth_O.png",
    "U":    "mouth_U.png",
    "FV":   "mouth_FF.png",
    "MBP":  "mouth_PP.png",
    "PP":   "mouth_PP.png",
    "FF":   "mouth_FF.png",
    "TH":   "mouth_TH.png",
    "DD":   "mouth_DD.png",
    "kk":   "mouth_kk.png",
    "CH":   "mouth_CH.png",
    "SS":   "mouth_SS.png",
    "nn":   "mouth_nn.png",
    "RR":   "mouth_RR.png",
    "aa":   "mouth_A.png",
    "ih":   "mouth_I.png",
    "oh":   "mouth_O.png",
    "ou":   "mouth_U.png",
    "UNK":  "mouth_UNK.png",
}


def _icons_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "icons")


def _get_previews_module():
    if _bpy_previews is not None:
        return _bpy_previews
    try:
        if hasattr(bpy.utils, "previews"):
            return bpy.utils.previews
    except Exception:
        pass
    try:
        import bpy.utils.previews as _prev
        return _prev
    except (ImportError, AttributeError):
        pass
    return None


def _load_previews() -> None:
    global _preview_collection
    if _preview_collection is not None:
        return
    prev_mod = _get_previews_module()
    if prev_mod is None:
        print("AudioShapePRO previews: bpy.utils.previews non disponible.")
        return
    _preview_collection = prev_mod.new()
    icons_path = _icons_dir()
    for code, filename in ICON_FILES.items():
        full_path = os.path.join(icons_path, filename)
        if not os.path.isfile(full_path):
            continue
        try:
            _preview_collection.load(code, full_path, "IMAGE")
        except Exception as exc:
            print(f"AudioShapePRO previews: échec chargement {code} : {exc}")


def _unload_previews() -> None:
    global _preview_collection
    if _preview_collection is not None:
        try:
            prev_mod = _get_previews_module()
            if prev_mod is not None:
                prev_mod.remove(_preview_collection)
        except Exception:
            pass
        _preview_collection = None


def get_icon_id(viseme: str) -> int:
    # v13 alias : OUE → O icon, RL → I icon (forme la plus proche)
    _v13_alias = {"OUE": "O", "RL": "I"}
    viseme = _v13_alias.get(viseme, viseme)
    if _preview_collection is None:
        return 0
    if viseme not in _preview_collection:
        norm = viseme.upper()
        if norm in _preview_collection:
            viseme = norm
        else:
            return 0
    try:
        return _preview_collection[viseme].icon_id
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# Cycle dynamique — réagit à tous les paramètres animation
# ─────────────────────────────────────────────────────────────────────────────

def _build_dynamic_cycle(props) -> list[str]:
    """
    Construit un cycle de preview proportionnel aux paramètres animation :
      - inbetween_ms    → durée de chaque viseme actif (1 frame cycle)
      - silence_ms      → combien de frames-cycle pour les silences
      - silence_stretch → étire les silences
      - close_duration_ms → ajoute des MBP (fermeture) en transition
    """
    inb = float(getattr(props, "seq_inbetween_ms", 100.0))
    sil_ms = float(getattr(props, "seq_silence_ms", 220.0))
    close_ms = float(getattr(props, "seq_close_duration_ms", 200.0))
    stretch = float(getattr(props, "seq_silence_stretch", 1.0))

    sil_frames = max(1, min(8, round(sil_ms * stretch / max(inb, 1.0))))
    close_frames = max(1, min(4, round(close_ms / max(inb, 1.0))))

    cycle: list[str] = []
    # Intro silence
    cycle.extend(["SIL"] * sil_frames)
    # Premier mot
    for ph in ["A", "OUE", "I", "MBP", "FV", "RL"]:
        cycle.append(ph)
    # Fermeture
    cycle.extend(["MBP"] * close_frames)
    # Silence inter-mots
    cycle.extend(["SIL"] * sil_frames)
    # Deuxième mot
    for ph in ["A", "OUE", "I", "MBP", "FV"]:
        cycle.append(ph)
    # Fermeture finale
    cycle.extend(["MBP"] * close_frames)
    cycle.extend(["SIL"] * sil_frames)

    return cycle if cycle else list(DEMO_CYCLE)


# ─────────────────────────────────────────────────────────────────────────────
# Vitesse
# ─────────────────────────────────────────────────────────────────────────────

def cycle_speed_seconds(props) -> float:
    """Vitesse calée sur seq_inbetween_ms. Min 0.05s."""
    inb = float(getattr(props, "seq_inbetween_ms", 100.0))
    return max(0.05, inb / 1000.0)


def get_current_preview_viseme(scene) -> str:
    if not _animation_running:
        manual = scene.audioshape_props.preview_manual_viseme \
            if hasattr(scene, "audioshape_props") else "SIL"
        return manual or "SIL"
    try:
        props = scene.audioshape_props
        dyn = _build_dynamic_cycle(props)
    except Exception:
        dyn = list(DEMO_CYCLE)
    return dyn[_animation_index % len(dyn)]


def is_preview_animating() -> bool:
    return _animation_running


# ─────────────────────────────────────────────────────────────────────────────
# Timer
# ─────────────────────────────────────────────────────────────────────────────

def _timer_tick() -> Optional[float]:
    global _animation_index, _animation_running, _driven_by_audio

    if not _animation_running:
        return None

    # Source de vérité : l'état réel du handle audio (gère la fin naturelle).
    # Le flag scene.audio_playback.is_playing n'est mis à False que par les
    # opérateurs Stop/Play, jamais quand l'audio se termine tout seul → on
    # interroge audio_core directement.
    try:
        from . import audio_core
        audio_alive = audio_core.is_audio_playing()
    except Exception:
        audio_alive = False

    if not audio_alive:
        # Audio terminé (naturel ou manuel) : on coupe la preview ET on
        # remet le flag UI en cohérence pour que le bouton Play réapparaisse.
        _animation_running = False
        _driven_by_audio = False
        try:
            scene = bpy.context.scene
            if scene is not None and hasattr(scene, "audio_playback"):
                scene.audio_playback.is_playing = False
        except Exception:
            pass
        _force_redraw()
        return None

    try:
        props = bpy.context.scene.audioshape_props
        dyn = _build_dynamic_cycle(props)
        _animation_index = (_animation_index + 1) % len(dyn)
        next_delay = cycle_speed_seconds(props)
    except Exception:
        _animation_index = (_animation_index + 1) % len(DEMO_CYCLE)
        next_delay = 0.15

    _force_redraw()
    return next_delay


def _force_redraw() -> None:
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Démarrage / Arrêt
# ─────────────────────────────────────────────────────────────────────────────

def start_preview_animation(scene, driven_by_audio: bool = True) -> None:
    """
    Démarre le cycle de prévisualisation de la bouche.

    Depuis v7.1 la preview est TOUJOURS liée à la lecture audio :
    elle s'arrête automatiquement dès que le handle audio cesse de jouer
    (fin naturelle ou clic Stop). Le paramètre driven_by_audio est
    conservé pour compatibilité mais ignoré.
    """
    global _animation_running, _animation_index, _driven_by_audio

    if _animation_running:
        _driven_by_audio = True
        return

    _animation_running = True
    _driven_by_audio = True
    _animation_index = 0

    try:
        if not bpy.app.timers.is_registered(_timer_tick):
            try:
                props = scene.audioshape_props
                first_delay = cycle_speed_seconds(props)
            except Exception:
                first_delay = 0.15
            bpy.app.timers.register(_timer_tick, first_interval=first_delay)
    except Exception as exc:
        print(f"AudioShapePRO previews: timer register échoué : {exc}")
        _animation_running = False
        _driven_by_audio = False


def stop_preview_animation(scene) -> None:
    """Arrête le cycle (bouton manuel Stop)."""
    global _animation_running, _driven_by_audio
    _animation_running = False
    _driven_by_audio = False


# ─────────────────────────────────────────────────────────────────────────────
# Hooks audio — appelés par les opérateurs PlayAudio / StopAudio
# ─────────────────────────────────────────────────────────────────────────────

def on_audio_play(scene) -> None:
    """Appelé quand l'audio démarre. Lance la preview si l'aperçu est affiché."""
    try:
        props = scene.audioshape_props
        if not props.preview_show:
            return
    except Exception:
        return
    start_preview_animation(scene, driven_by_audio=True)


def on_audio_stop(scene) -> None:
    """Appelé quand l'audio s'arrête. Arrête la preview si elle était pilotée par l'audio."""
    global _animation_running, _driven_by_audio
    if _driven_by_audio:
        _animation_running = False
        _driven_by_audio = False


# ─────────────────────────────────────────────────────────────────────────────
# Labels
# ─────────────────────────────────────────────────────────────────────────────

def get_viseme_label(viseme: str) -> str:
    labels = {
        "SIL": "Silence",
        "A":   "voyelle A (ouverte)",
        "OUE": "voyelles O/U/E",
        "RL":  "liquides R/L",
        "I":   "voyelle I",
        "O":   "voyelle O",
        "U":   "voyelle U",
        "FV":  "F / V (consonne labio-dentale)",
        "MBP": "M / B / P (lèvres fermées)",
        "PP":  "M / B / P (lèvres fermées)",
        "FF":  "F / V (consonne labio-dentale)",
        "TH":  "TH (langue entre les dents)",
        "DD":  "T / D (langue derrière dents)",
        "kk":  "K / G (vélaire)",
        "CH":  "CH / J (affriquée)",
        "SS":  "S / Z (sibilante)",
        "nn":  "N / NG / L (nasale / latérale)",
        "RR":  "R (rhotique)",
        "aa":  "A (ouverte)",
        "ih":  "I (fermée antérieure)",
        "oh":  "O (mid-arrière)",
        "ou":  "OU (arrière fermée)",
        "UNK": "inconnu",
    }
    return labels.get(viseme, viseme)


# ─────────────────────────────────────────────────────────────────────────────
# Register / Unregister
# ─────────────────────────────────────────────────────────────────────────────

def register() -> None:
    _load_previews()


def unregister() -> None:
    global _animation_running, _driven_by_audio
    _animation_running = False
    _driven_by_audio = False
    try:
        if bpy.app.timers.is_registered(_timer_tick):
            bpy.app.timers.unregister(_timer_tick)
    except Exception:
        pass
    _unload_previews()
