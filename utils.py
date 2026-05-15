# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
utils.py — Utilitaires AudioShapePRO v6.0.

  - Graph Editor context override (force le bake)
  - Sauvegarde / restauration du contexte utilisateur
  - Matching tolérant des noms de shape keys
  - Helpers F-curve / shape keys
  - Formatage Hz / ms
"""

from __future__ import annotations

import bpy
from . import compat as _compat


# ─────────────────────────────────────────────────────────────────────────────
# Contexte Graph Editor
# ─────────────────────────────────────────────────────────────────────────────

def setup_graph_editor_context(
    context: bpy.types.Context,
) -> tuple[bpy.types.Area | None, str | None]:
    """Trouve ou crée temporairement un Graph Editor."""
    for area in context.screen.areas:
        if area.type == "GRAPH_EDITOR":
            return area, None

    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "GRAPH_EDITOR":
                return area, None

    for area in context.screen.areas:
        if area.type == "VIEW_3D":
            original = area.type
            area.type = "GRAPH_EDITOR"
            return area, original

    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                original = area.type
                area.type = "GRAPH_EDITOR"
                return area, original

    for area in context.screen.areas:
        if area.type not in ("GRAPH_EDITOR", "INFO", "TOPBAR", "STATUSBAR"):
            original = area.type
            area.type = "GRAPH_EDITOR"
            return area, original

    return None, None


def restore_graph_editor_context(
    original_area_type: str | None,
    context: bpy.types.Context,
) -> None:
    if original_area_type is None:
        return
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "GRAPH_EDITOR":
                area.type = original_area_type
                return


# ─────────────────────────────────────────────────────────────────────────────
# Préservation contexte utilisateur (le bake n'écrase pas l'état)
# ─────────────────────────────────────────────────────────────────────────────

def save_user_context(context: bpy.types.Context) -> dict:
    state: dict = {}
    try:
        state["frame_current"] = context.scene.frame_current
    except Exception:  # noqa: BLE001
        pass
    try:
        obj = context.object
        if obj and getattr(obj, "data", None) and hasattr(obj.data, "shape_keys"):
            sk = obj.data.shape_keys
            if sk and sk.animation_data and sk.animation_data.action:
                selected = []
                for fc in _compat.action_fcurves(sk.animation_data.action):
                    if fc.select:
                        selected.append(fc.data_path)
                state["selected_fcurves"] = selected
    except Exception:  # noqa: BLE001
        pass
    return state


def restore_user_context(context: bpy.types.Context, state: dict) -> None:
    try:
        if "frame_current" in state:
            context.scene.frame_current = state["frame_current"]
    except Exception:  # noqa: BLE001
        pass
    try:
        obj = context.object
        if obj and getattr(obj, "data", None) and hasattr(obj.data, "shape_keys"):
            sk = obj.data.shape_keys
            if sk and sk.animation_data and sk.animation_data.action:
                selected = set(state.get("selected_fcurves", []))
                for fc in _compat.action_fcurves(sk.animation_data.action):
                    fc.select = fc.data_path in selected
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Matching tolérant des shape keys
# ─────────────────────────────────────────────────────────────────────────────

# Variantes courantes utilisées par les artistes : Mouth_A, vis_AH, etc.
# v13 — aliases mis à jour : OUE regroupe O/U/E, RL = liquides R/L
_PHONEME_ALIASES: dict[str, tuple[str, ...]] = {
    "A":   ("a", "ah", "aa", "vowela", "voya", "shapea", "moutha", "visa", "visaa", "visah"),
    "OUE": (
        # Groupe O/U/E — toutes les variantes possibles
        "oue", "o", "oh", "oo", "u", "uh", "uu", "e", "eh", "ee",
        "vowelo", "vowelu", "vowele", "voyo", "voyu", "voye",
        "shapeo", "shapeu", "shapee", "moutho", "mouthu", "mouthe",
        "viso", "visoh", "visoo", "visu", "visuh", "vise", "viseh",
        "rounded", "round", "pucker", "lips_round", "lipsround",
    ),
    "I":   ("i", "ih", "ii", "voweli", "voyi", "shapei", "mouthi", "visi", "visih",
            "smile", "wide", "spread"),
    "RL":  (
        # Liquides R/L — variantes artistes
        "rl", "r", "l", "rr", "ll", "liquid", "liquide",
        "visr", "visl", "visrl", "shaperl", "mouthr", "mouthl",
        "lateral", "rhotic",
    ),
    "FV":  ("fv", "fff", "f", "v", "visfv", "visff", "shapefv", "lipbite", "tooth"),
    "MBP": ("mbp", "mbm", "mb", "m", "b", "p", "vismbp", "vism", "shapembp",
            "closed", "close", "mouthclose", "lipsclosed", "lipclose", "rest", "neutral"),
    "SIL": ("sil", "silence", "rest", "neutral", "closed", "mouthclosed", "lipsclosed",
            "default"),
}


def _normalize(name: str) -> str:
    return name.lower().replace(" ", "").replace("_", "").replace("-", "").replace(".", "")


def resolve_shape_key(
    obj: bpy.types.Object | None,
    requested_name: str,
    phoneme_code: str = "",
    fuzzy: bool = True,
) -> str | None:
    """Résout le nom d'une shape key. Voir tests dans v5.3."""
    if obj is None or not hasattr(getattr(obj, "data", None), "shape_keys"):
        return None
    if obj.data.shape_keys is None:
        return None

    blocks = obj.data.shape_keys.key_blocks
    available = [kb.name for kb in blocks if kb.name != "Basis"]

    # 1. Match exact
    if requested_name and requested_name in blocks:
        return requested_name

    if not fuzzy:
        return None

    # 2. Insensible à la casse / séparateurs
    if requested_name:
        target = _normalize(requested_name)
        for name in available:
            if _normalize(name) == target:
                return name

    # 3. Alias du phonème
    if phoneme_code:
        aliases = (phoneme_code.lower(),) + _PHONEME_ALIASES.get(phoneme_code, ())
        normalized_aliases = [_normalize(a) for a in aliases]

        for name in available:
            n = _normalize(name)
            if n in normalized_aliases:
                return name

        best_match: str | None = None
        best_len = 0
        for name in available:
            n = _normalize(name)
            for alias in normalized_aliases:
                if len(alias) >= 2 and alias in n and len(alias) > best_len:
                    best_match = name
                    best_len = len(alias)
        if best_match is not None:
            return best_match

        # Dernier recours : le nom de la shape key se TERMINE par le code phonème
        # ex. "MouthA" → "A", "vis_O" → "O"
        if phoneme_code:
            ph_lower = phoneme_code.lower()
            for name in available:
                n = _normalize(name)
                if n.endswith(ph_lower) or n.startswith(ph_lower):
                    return name

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Auto-mapping de toutes les shape keys (NOUVEAU v6.0 : One-Click Setup)
# ─────────────────────────────────────────────────────────────────────────────

def auto_map_shape_keys(obj: bpy.types.Object, props) -> dict[str, str]:
    """Tente de mapper automatiquement TOUS les phonèmes vers les shape keys.

    Retourne un dict {phoneme: shape_key_name} pour les mappings réussis.
    Modifie aussi props.shape_key_<phoneme> pour l'UI.
    """
    if obj is None or not hasattr(getattr(obj, "data", None), "shape_keys"):
        return {}
    if obj.data.shape_keys is None:
        return {}

    # v13 — phonèmes restructurés : OUE, RL
    mapping: dict[str, str] = {}
    for ph in ("A", "OUE", "I", "MBP", "FV", "RL", "SIL"):
        sk = resolve_shape_key(obj, getattr(props, f"shape_key_{ph}", ph), ph, fuzzy=True)
        if sk is not None:
            mapping[ph] = sk
            try:
                setattr(props, f"shape_key_{ph}", sk)
            except Exception:  # noqa: BLE001
                pass
    return mapping


# ─────────────────────────────────────────────────────────────────────────────
# F-curve management
# ─────────────────────────────────────────────────────────────────────────────

def ensure_fcurve_active(
    obj: bpy.types.Object,
    sk_name: str,
) -> bpy.types.FCurve | None:
    if obj is None:
        return None
    data = obj.data
    if data is None or not hasattr(data, "shape_keys") or data.shape_keys is None:
        return None

    sk = data.shape_keys
    if sk.animation_data is None:
        sk.animation_data_create()

    anim = sk.animation_data
    if anim.action is None:
        action = bpy.data.actions.new(name=f"ASP_{obj.name}_ShapeKeys")
        anim.action = action

    action = anim.action
    dp = f'key_blocks["{sk_name}"].value'

    target_fc: bpy.types.FCurve | None = None
    for fc in _compat.action_fcurves(action):
        if fc.data_path == dp and fc.array_index == 0:
            target_fc = fc
            break

    if target_fc is None:
        target_fc = _compat.action_fcurves(action).new(data_path=dp, index=0)
        target_fc.keyframe_points.insert(frame=1, value=0.0)
        target_fc.keyframe_points.update()

    for fc in _compat.action_fcurves(action):
        fc.select = False
    target_fc.select = True
    return target_fc


def clear_shape_key_keyframes(obj: bpy.types.Object, sk_name: str) -> int:
    if obj is None or not hasattr(getattr(obj, "data", None), "shape_keys"):
        return 0
    if obj.data.shape_keys is None or obj.data.shape_keys.animation_data is None:
        return 0
    action = obj.data.shape_keys.animation_data.action
    if action is None:
        return 0

    dp = f'key_blocks["{sk_name}"].value'
    removed = 0
    for fc in list(_compat.action_fcurves(action)):
        if fc.data_path == dp:
            removed += len(fc.keyframe_points)
            _compat.action_fcurves(action).remove(fc)
    return removed


def count_keyframes_for_shape_key(obj: bpy.types.Object, sk_name: str) -> int:
    if obj is None:
        return 0
    data = obj.data
    if not data or not hasattr(data, "shape_keys") or data.shape_keys is None:
        return 0
    anim_data = data.shape_keys.animation_data
    if anim_data is None or anim_data.action is None:
        return 0
    dp_target = f'key_blocks["{sk_name}"].value'
    for fcurve in _compat.action_fcurves(anim_data.action):
        if fcurve.data_path == dp_target:
            return len(fcurve.keyframe_points)
    return 0


def get_all_shape_key_names(obj: bpy.types.Object | None) -> list[str]:
    if obj is None:
        return []
    data = obj.data
    if not data or not hasattr(data, "shape_keys") or data.shape_keys is None:
        return []
    return [kb.name for kb in data.shape_keys.key_blocks if kb.name != "Basis"]


def get_object_summary(obj: bpy.types.Object | None) -> str:
    """Affichage compact pour l'UI."""
    if obj is None:
        return "Aucun objet"
    n = len(get_all_shape_key_names(obj))
    return f"{obj.name} • {n} shape key{'s' if n != 1 else ''}"


# ─────────────────────────────────────────────────────────────────────────────
# Formatage
# ─────────────────────────────────────────────────────────────────────────────

def fmt_hz(freq: float) -> str:
    if freq >= 1000.0:
        return f"{freq / 1000:.2f} kHz"
    return f"{freq:.0f} Hz"


def fmt_ms(duration_s: float) -> str:
    if duration_s < 10.0:
        return f"{duration_s * 1000:.0f} ms"
    return f"{duration_s:.2f} s"


def fmt_frames(n: int, fps: float = 24.0) -> str:
    """Affiche frames ET équivalent en ms."""
    ms = (n / max(fps, 1.0)) * 1000.0
    return f"{n} fr ({ms:.0f} ms)"
