# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
AudioShapePRO v13.0 — Preset phonèmes restructuré (OUE groupé, RL liquides) — SIMPLE/ADVANCED/EXPERT.

Nouveautés v7.2 :
  ✨ Modèle Vosk FR auto-téléchargé au premier bake (au lieu d'être
     embarqué) — addon ~24 Mo au lieu de ~63 Mo.
  ✨ Bouton « Télécharger le modèle maintenant » pour pré-fetch manuel.
  ✨ Cache hors-ligne : téléchargement unique, réutilisé pour toujours.

Hérité de v7.1 :
  ✨ Aperçu bouche intrinsèquement lié à la lecture audio
     (démarrage et arrêt synchronisés).
  ✨ UI réorganisée — boutons gros AUTOMAP + BAKE, Smooth Bézier
     juste avant le bake, langue/voix non dupliquées.

Hérité de v7.0 :
  ✨ Vosk entièrement intégré (roues embarquées Win/Linux/macOS).
  ✨ Smooth Bézier avec slider d'amplitude des handles.
  ✨ GPU via API Blender (CUDA, Optix, HIP, Metal, oneAPI).
  ✨ Guide HTML intégré.

Compatibilité Blender 4.2+.
"""

import bpy
from bpy.props import BoolProperty, EnumProperty
from bpy.types import AddonPreferences

from . import compat  # noqa: F401  — DOIT être en premier
from . import audio_core, operators, panels, previews, properties
from . import formant_library, performance, security, sequencer, utils  # noqa: F401
from . import vosk_helper  # noqa: F401
from . import emotion_coarticulation  # noqa: F401
from . import asms_engine  # noqa: F401 — moteur ASMS v13


# ─────────────────────────────────────────────────────────────────────────────
# Détection GPU via l'API Blender native
# ─────────────────────────────────────────────────────────────────────────────

def _detect_gpu_backend() -> str:
    """
    Retourne le backend GPU actif selon les Préférences Blender.

    bpy.context.preferences.system.compute_device_type :
      'NONE' | 'CUDA' | 'OPTIX' | 'HIP' | 'ONEAPI' | 'METAL'
    """
    try:
        sys_prefs = bpy.context.preferences.system
        device_type: str = sys_prefs.compute_device_type  # type: ignore
        if device_type == "NONE":
            return ""
        try:
            active = [d for d in sys_prefs.devices if d.use]  # type: ignore
            if active:
                names = ", ".join(d.name for d in active[:2])
                return f"{device_type} — {names}"
        except Exception:
            pass
        return device_type
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Préférences de l'addon
# ─────────────────────────────────────────────────────────────────────────────

class AudioShapePROPreferences(AddonPreferences):
    """Préférences globales AudioShapePRO — persistantes entre les sessions."""
    bl_idname = __package__

    default_language: EnumProperty(
        name="Langue par défaut",
        description="Langue utilisée à la création d'un nouveau projet",
        items=formant_library.get_language_items(),
        default="FR",
    )  # type: ignore

    default_auto_clear_keyframes: BoolProperty(
        name="Nettoyer les bakes précédents",
        description="Supprime les keyframes existants avant chaque bake",
        default=True,
    )  # type: ignore

    default_fuzzy_shape_matching: BoolProperty(
        name="Matching tolérant des shape keys",
        description="Accepte un mapping approximatif des noms de shape keys",
        default=True,
    )  # type: ignore

    default_bake_strategy: EnumProperty(
        name="Stratégie bake prioritaire",
        items=[
            ("SEQUENCE", "SEQUENCE",  "Vosk + analyse spectrale — recommandé"),
            ("SPECTRAL", "SPECTRAL",  "FFT+LPC+RMS — sans Vosk"),
        ],
        default="SEQUENCE",
    )  # type: ignore

    compute_device: EnumProperty(
        name="Dispositif de calcul (FFT)",
        description="Processeur pour l'analyse audio. GPU = selon Préférences Blender.",
        items=[
            ("CPU", "CPU", "Processeur principal", "CPU",   0),
            ("GPU", "GPU", "Carte graphique",      "GPUBL", 1),
        ],
        default="CPU",
    )  # type: ignore

    def draw(self, context) -> None:
        layout = self.layout
        layout.use_property_split    = True
        layout.use_property_decorate = False

        # ══ Version Vosk embarqué ════════════════════════════════════════
        box = layout.box()
        available = vosk_helper.is_vosk_available()
        version   = vosk_helper.get_vosk_version() if available else "non chargé"
        col_v = box.column(align=True)
        col_v.scale_y = 0.82
        col_v.label(text=f"Vosk embarqué : v{version}", icon="SPEAKER")
        # Infos de compatibilité
        for line in compat.feature_summary():
            col_v.label(text=line)

        # ══ Paramètres principaux ════════════════════════════════════════
        col = layout.column(align=True)
        col.prop(self, "default_fuzzy_shape_matching")
        col.prop(self, "default_auto_clear_keyframes")

        # ══ Guide utilisateur ════════════════════════════════════════════
        layout.separator(factor=0.3)
        guide_row = layout.row()
        guide_row.scale_y = 1.3
        guide_row.operator(
            "audioshape.open_html_guide",
            text="📖  Guide utilisateur (HTML)",
            icon="QUESTION",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_prefs() -> "AudioShapePROPreferences | None":
    addon = bpy.context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def get_compute_device() -> str:
    prefs = get_prefs()
    return prefs.compute_device if prefs else "CPU"


# ─────────────────────────────────────────────────────────────────────────────
# Register / Unregister — ordre strict (classe → operator → panel)
# ─────────────────────────────────────────────────────────────────────────────

def register() -> None:
    # 1. Installation silencieuse de Vosk depuis les roues embarquées
    # Fonctionne dès Blender 4.2 avec extraction ZIP directe (v12)
    try:
        ok = vosk_helper.auto_install_vosk_if_needed()
        if ok:
            ver = vosk_helper.get_vosk_version()
            print(f"[AudioShapePRO] Vosk {ver} prêt ✓")
        else:
            print("[AudioShapePRO] ⚠ Vosk non installé — bake en mode FFT fallback")
    except Exception as exc:
        print(f"[AudioShapePRO] Erreur installation Vosk : {exc}")

    # 2. Préférences
    bpy.utils.register_class(AudioShapePROPreferences)

    # 3. Audio
    try:
        audio_core.initialize_audio()
    except Exception as exc:
        print(f"[AudioShapePRO] Erreur init audio : {exc}")

    # 4. Previews
    try:
        previews.register()
    except Exception as exc:
        print(f"[AudioShapePRO] Erreur register previews : {exc}")

    # 5. PropertyGroups (ordre : sous-groupes avant groupe parent)
    properties.register_properties()

    # 6. Opérateurs
    for cls in operators.OPERATOR_CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as exc:
            print(f"[AudioShapePRO] Erreur register opérateur {cls.__name__} : {exc}")

    # 7. Panneaux (parent avant enfants — garanti par PANEL_CLASSES)
    for cls in panels.PANEL_CLASSES:
        try:
            bpy.utils.register_class(cls)
        except Exception as exc:
            print(f"[AudioShapePRO] Erreur register panneau {cls.__name__} : {exc}")


def unregister() -> None:
    # 1. Panneaux (inverse)
    for cls in reversed(panels.PANEL_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    # 2. Opérateurs (inverse)
    for cls in reversed(operators.OPERATOR_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

    # 3. PropertyGroups
    try:
        properties.unregister_properties()
    except Exception:
        pass

    # 4. Previews
    try:
        previews.unregister()
    except Exception:
        pass

    # 5. Audio
    try:
        audio_core.cleanup_audio()
    except Exception:
        pass

    # 6. Préférences
    try:
        bpy.utils.unregister_class(AudioShapePROPreferences)
    except Exception:
        pass
