# SPDX-License-Identifier: MIT
"""
emotion_coarticulation.py v11 — Émotion + Coarticulation par manipulation
directe des keyframes et handles Bézier dans l'espace du Graph Editor.

Espace du Graph Editor :
  X = numéro de frame  (ex: 100.0)
  Y = valeur de la shape key (0.0 → 1.0)
  kp.co          = Vector2(frame, value)
  kp.handle_left = Vector2(frame_handle, value_handle)  ← ABSOLU, même espace
  kp.handle_right= Vector2(frame_handle, value_handle)  ← ABSOLU, même espace

  Pour modifier un handle, mettre kp.handle_left_type = 'FREE' d'abord,
  puis assigner kp.handle_left[0] = frame_absolu, kp.handle_left[1] = valeur.

ÉMOTION : plancher direct sur les valeurs kp.co.y (fiable toutes versions).
COARTICULATION : handles FREE avec anticipation, burst, décélération.
"""
from __future__ import annotations
import math
from typing import Optional
import bpy
from . import compat as _compat

# ── Tables ───────────────────────────────────────────────────────────────────
# (phonème_cible, valeur_plancher_max) pour chaque émotion
# v13 — OUE regroupe O/U/E, RL = liquides
EMOTION_FLOOR_MAP = {
    "NEUTRAL":   None,
    "HAPPY":     ("I",    0.80),  # sourire → I
    "SAD":       ("MBP",  0.80),  # lèvres serrées
    "ANGRY":     ("FV",   0.50),  # dents visibles
    "SURPRISED": ("OUE",  0.60),  # bouche arrondie → OUE
    "WHISPER":   None,            # traitement spécial : scaling
    "SINGING":   ("OUE",  0.50),  # tenue arrondie → OUE
}

# v13 — résistances coarticulatoires restructurées
COART_RESISTANCE = {
    "A":   0.15,  # voyelle ouverte — faible résistance
    "OUE": 0.22,  # groupe arrondi — résistance modérée
    "I":   0.25,  # voyelle fermée antérieure
    "RL":  0.20,  # liquides — intermédiaire entre A et I
    "MBP": 0.30,  # occlusives — plus résistantes
    "FV":  0.55,  # fricatives — très résistantes
    "SIL": 1.00,  # silence — imperméable
    # Alias legacy
    "E": 0.20, "O": 0.20, "U": 0.25,
}


# ── Utilitaires ───────────────────────────────────────────────────────────────

def _find_fcurve(action, sk_name: str) -> Optional[bpy.types.FCurve]:
    if not action:
        return None
    # Blender 4.x : action.fcurves standard
    for path in (f'key_blocks["{sk_name}"].value',
                 f"key_blocks['{sk_name}'].value"):
        fc = _compat.action_fcurves(action).find(path, index=0)
        if fc is None:
            fc = _compat.action_fcurves(action).find(path)
        if fc:
            return fc
    # Fallback : recherche partielle
    for fc in _compat.action_fcurves(action):
        if "key_blocks" in fc.data_path and sk_name in fc.data_path:
            return fc
    return None

def _get_action(obj) -> Optional[bpy.types.Action]:
    # Blender 4.4+ : animation_data peut être sur shape_keys
    sk_data = getattr(getattr(obj, "data", None), "shape_keys", None)
    if sk_data:
        ad = getattr(sk_data, "animation_data", None)
        if ad and ad.action:
            return ad.action
    ad = getattr(obj, "animation_data", None)
    return ad.action if ad else None

def _sk_ok(obj, name: str) -> bool:
    try:
        sk = getattr(getattr(obj, "data", None), "shape_keys", None)
        return sk is not None and sk.key_blocks.get(name) is not None
    except Exception:
        return False

def _refresh(fcs):
    for fc in fcs:
        try:
            fc.update()
        except Exception:
            pass
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    try:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type in ("GRAPH_EDITOR", "DOPESHEET_EDITOR"):
                    a.tag_redraw()
    except Exception:
        pass


# ── 1. Plancher émotionnel — modification directe kp.co.y ────────────────────

def apply_emotion_floor(obj, props, sk_map: dict) -> str:
    """
    Applique un plancher de valeur sur la shape key émotionnelle.

    Stratégie : modifier directement kp.co.y pour que la valeur ne descende
    jamais en dessous du plancher pendant les segments de parole.
    C'est la méthode la plus robuste et compatible avec toutes les versions.
    """
    emotion   = getattr(props, "emotion_preset",   "NEUTRAL")
    intensity = getattr(props, "emotion_intensity", 0.5)
    floor_info = EMOTION_FLOOR_MAP.get(emotion)
    if not floor_info or intensity <= 0.01:
        return ""

    target_ph, floor_max = floor_info
    floor_v = float(floor_max * intensity)
    sk_name = sk_map.get(target_ph, "")
    if not sk_name or not _sk_ok(obj, sk_name):
        return f"[{emotion}] '{target_ph}' non mappé"

    action = _get_action(obj)
    if not action:
        return "[Émotion] Pas d'action active"

    fc = _find_fcurve(action, sk_name)
    if not fc:
        return f"[Émotion] F-curve '{sk_name}' introuvable"

    n_modified = 0

    # ── Étape 1 : Relever les keyframes sous le plancher ──────────────────
    # On travaille sur une copie de la liste pour éviter les problèmes d'itération
    kp_list = list(fc.keyframe_points)

    for kp in kp_list:
        if kp.co.y < floor_v:
            # Modifier la valeur
            kp.co.y = floor_v
            # Ajuster aussi les handles pour éviter les dépassements vers le bas
            # Les handles sont dans l'espace (frame, value) — ABSOLUS
            if kp.handle_left[1] < floor_v:
                kp.handle_left_type = "FREE"
                kp.handle_left[1]   = floor_v
            if kp.handle_right[1] < floor_v:
                kp.handle_right_type = "FREE"
                kp.handle_right[1]   = floor_v
            n_modified += 1

    # ── Étape 2 : Combler les creux entre keyframes (courbe peut passer sous le plancher)
    # Insérer des points de plancher dans les gaps où la courbe creuserait
    kp_sorted = sorted(kp_list, key=lambda k: k.co.x)
    to_insert = []
    for i in range(len(kp_sorted) - 1):
        f0, v0 = kp_sorted[i].co.x,   kp_sorted[i].co.y
        f1, v1 = kp_sorted[i+1].co.x, kp_sorted[i+1].co.y
        gap = f1 - f0
        if gap < 3:
            continue
        # Évaluer au centre du gap
        mid_f = (f0 + f1) * 0.5
        try:
            mid_v = fc.evaluate(mid_f)
        except Exception:
            mid_v = (v0 + v1) * 0.5
        if mid_v < floor_v - 0.02:
            # Insérer un keyframe plancher légèrement décalé de chaque côté
            to_insert.append((f0 + max(1.5, gap * 0.15), floor_v))
            to_insert.append((f1 - max(1.5, gap * 0.15), floor_v))

    for frame, val in to_insert:
        try:
            kp = fc.keyframe_points.insert(frame, val, options={"FAST"})
            kp.interpolation    = "BEZIER"
            kp.handle_left_type  = "AUTO_CLAMPED"
            kp.handle_right_type = "AUTO_CLAMPED"
            n_modified += 1
        except Exception:
            pass

    fc.update()
    return f"[{emotion}] '{sk_name}' floor={floor_v:.2f} — {n_modified} pts modifiés"


# ── 2. Chuchotement — scaling global ─────────────────────────────────────────

def apply_whisper_reduction(obj, props, sk_map: dict) -> str:
    if getattr(props, "emotion_preset", "NEUTRAL") != "WHISPER":
        return ""
    intensity = getattr(props, "emotion_intensity", 0.5)
    if intensity <= 0.01:
        return ""

    action = _get_action(obj)
    if not action:
        return ""

    scale = 1.0 - intensity * 0.55
    n, fcs = 0, []
    for ph, sk_name in sk_map.items():
        if not sk_name or ph == "SIL":
            continue
        fc = _find_fcurve(action, sk_name)
        if not fc:
            continue
        for kp in list(fc.keyframe_points):
            if kp.co.y > 0.02:
                new_y = max(0.0, min(1.0, kp.co.y * scale))
                kp.co.y = new_y
                # Handles proportionnels
                kp.handle_left_type  = "FREE"
                kp.handle_right_type = "FREE"
                kp.handle_left[1]  = max(0.0, kp.handle_left[1]  * scale)
                kp.handle_right[1] = max(0.0, kp.handle_right[1] * scale)
                n += 1
        fcs.append(fc)

    _refresh(fcs)
    return f"[WHISPER] ×{scale:.2f} — {n} keyframes"


# ── 3. Coarticulation — handles Bézier dans l'espace Graph Editor ────────────

def apply_coarticulation_handles(obj, props, sk_map: dict) -> str:
    """
    Déplace les handles Bézier pour simuler l'articulation organique.

    Espace du Graph Editor :
      X = frame (absolu)  |  Y = valeur shape key (absolu, 0→1)

    Anticipation : le handle GAUCHE d'un pic est tiré vers le bas et en arrière
                   (frame plus petite, valeur plus basse) → la bouche s'ouvre
                   légèrement AVANT le phonème prévu.

    Burst       : le handle DROIT d'un pic est tiré vers l'avant et légèrement
                   bas → descente rapide après le pic (explosion consonantique).

    Décélération : le handle GAUCHE d'un zéro après un pic est étiré vers l'arrière
                   à hauteur intermédiaire → fermeture progressive et naturelle.

    Pour modifier les handles il FAUT d'abord passer en handle_type FREE.
    Les valeurs sont des positions absolues dans l'espace frame/value.
    """
    strength = getattr(props, "coarticulation_strength", 0.6)
    if strength <= 0.01:
        return ""

    action = _get_action(obj)
    if not action:
        return "[Coart] Pas d'action"

    total, fcs = 0, []

    for ph, sk_name in sk_map.items():
        if not sk_name:
            continue
        fc = _find_fcurve(action, sk_name)
        if not fc:
            continue

        resist = COART_RESISTANCE.get(ph, 0.5)
        eff    = strength * (1.0 - resist)
        if eff < 0.02:
            continue

        kps = sorted(fc.keyframe_points, key=lambda k: k.co.x)
        n   = len(kps)
        if n < 2:
            continue

        changed = False

        for i, kp in enumerate(kps):
            fr    = kp.co.x   # frame actuelle (absolu)
            val   = kp.co.y   # valeur actuelle (absolu)

            # ── PIC (valeur > 0.75) ────────────────────────────────────────
            if val > 0.75:
                # Anticipation : handle gauche tiré en arrière et vers le bas
                fr_prev = kps[i-1].co.x if i > 0 else fr - 12
                gap_l   = fr - fr_prev
                antici_frames = max(2.0, gap_l * 0.40 * eff)
                antici_val    = val * 0.30 * eff   # valeur basse d'anticipation

                kp.handle_left_type = "FREE"
                # Position absolue : fr - antici_frames frames avant, valeur basse
                kp.handle_left[0] = fr - antici_frames
                kp.handle_left[1] = antici_val

                # Burst : handle droit tiré en avant, légèrement bas
                fr_next = kps[i+1].co.x if i < n-1 else fr + 10
                gap_r   = fr_next - fr
                burst_frames = max(1.5, gap_r * 0.20 * eff)

                kp.handle_right_type = "FREE"
                kp.handle_right[0] = fr + burst_frames
                kp.handle_right[1] = val * (1.0 - 0.15 * eff)

                changed = True
                total  += 1

            # ── ZÉRO (valeur < 0.12) ──────────────────────────────────────
            elif val < 0.12:
                # Après un pic → décélération : handle gauche étiré vers l'arrière
                if i > 0 and kps[i-1].co.y > 0.60:
                    fr_prev  = kps[i-1].co.x
                    gap      = fr - fr_prev
                    decel_fr = max(1.5, gap * 0.35 * eff)
                    decel_v  = kps[i-1].co.y * 0.25 * eff  # intermédiaire

                    kp.handle_left_type = "FREE"
                    kp.handle_left[0] = fr - decel_fr
                    kp.handle_left[1] = decel_v
                    changed = True
                    total  += 1

                # Avant un pic → pré-ouverture : handle droit tiré vers le haut
                if i < n-1 and kps[i+1].co.y > 0.60:
                    fr_next = kps[i+1].co.x
                    gap     = fr_next - fr
                    preop_fr = max(1.5, gap * 0.30 * eff)
                    preop_v  = kps[i+1].co.y * 0.20 * eff

                    kp.handle_right_type = "FREE"
                    kp.handle_right[0] = fr + preop_fr
                    kp.handle_right[1] = preop_v
                    changed = True
                    total  += 1

        if changed:
            fcs.append(fc)

    # Tilt organique
    _apply_tilt(obj, props, sk_map, action, fcs)

    _refresh(fcs)
    return f"[Coarticulation] {total} handles modifiés (anticipation/burst/décel.) strength={strength:.2f}"


# ── 4. Tilt organique ─────────────────────────────────────────────────────────

def _apply_tilt(obj, props, sk_map, action, fcs_out):
    if not getattr(props, "organic_tilt", True):
        return
    amt = float(getattr(props, "tilt_amount", 0.04))
    if amt <= 0.001:
        return
    for ph, sk_name in sk_map.items():
        if not sk_name or ph == "SIL":
            continue
        fc = _find_fcurve(action, sk_name)
        if not fc:
            continue
        changed = False
        for i, kp in enumerate(fc.keyframe_points):
            # Appliquer un décalage sinusoïdal sur les handles Y (pas sur co.y)
            # pour ne pas déformer le timing mais créer une légère asymétrie
            if 0.05 < kp.co.y < 0.95:
                tilt = math.sin(kp.co.x * 0.073 + i * 0.41) * amt
                if kp.handle_left_type not in ("FREE",):
                    kp.handle_left_type = "FREE"
                if kp.handle_right_type not in ("FREE",):
                    kp.handle_right_type = "FREE"
                kp.handle_left[1]  = max(0.0, min(1.0, kp.handle_left[1]  + tilt))
                kp.handle_right[1] = max(0.0, min(1.0, kp.handle_right[1] - tilt))
                changed = True
        if changed and fc not in fcs_out:
            fcs_out.append(fc)


# ── POINT D'ENTRÉE ────────────────────────────────────────────────────────────

def run_post_processing(operator, obj, props, sk_map: dict, *, strategy: str = "") -> None:
    """
    Appelé APRÈS le bake SEQUENCE pour appliquer émotion & coarticulation.
    NON appelé en mode SPECTRAL (par conception).
    """
    if "SPECTRAL" in strategy.upper():
        return

    emotion   = getattr(props, "emotion_preset",        "NEUTRAL")
    intensity = getattr(props, "emotion_intensity",       0.5)
    coart     = getattr(props, "coarticulation_strength", 0.6)

    do_e = emotion != "NEUTRAL" and intensity > 0.01
    do_c = coart > 0.01
    if not (do_e or do_c):
        return

    tag = f"[{strategy}] " if strategy else ""
    msgs = []

    if do_e:
        if emotion == "WHISPER":
            msgs.append(apply_whisper_reduction(obj, props, sk_map))
        else:
            msgs.append(apply_emotion_floor(obj, props, sk_map))

    if do_c:
        msgs.append(apply_coarticulation_handles(obj, props, sk_map))

    for msg in msgs:
        if msg:
            print(f"[AudioShapePRO] {tag}{msg}")
            try:
                operator.report({"INFO"}, msg[:100])
            except Exception:
                pass

    _refresh([])
