# SPDX-License-Identifier: MIT
"""properties.py v11 — Ajout vosk_language pour le sélecteur de modèle."""
from __future__ import annotations
import bpy
from bpy.props import *
from bpy.types import PropertyGroup
from .formant_library import get_language_items, get_profile_items
from . import vosk_helper

STYLE_PRESETS = {
    "REALISTE": {"label":"Réaliste",  "seq_inbetween_ms":42.0, "seq_silence_ms":220.0,"seq_close_duration_ms":200.0,"seq_silence_stretch":1.0,"noise_gate_db":-40.0},
    "CARTOON":  {"label":"Cartoon",   "seq_inbetween_ms":80.0, "seq_silence_ms":120.0,"seq_close_duration_ms":80.0, "seq_silence_stretch":0.2,"noise_gate_db":-35.0},
    "ANIME":    {"label":"Anime",     "seq_inbetween_ms":120.0,"seq_silence_ms":300.0,"seq_close_duration_ms":300.0,"seq_silence_stretch":1.8,"noise_gate_db":-40.0},
    "MURMURE":  {"label":"Murmure",   "seq_inbetween_ms":60.0, "seq_silence_ms":400.0,"seq_close_duration_ms":400.0,"seq_silence_stretch":2.0,"noise_gate_db":-50.0},
}
PREVIEW_VISEME_ITEMS = [
    ("SIL","SIL",""),("A","A",""),("OUE","O/U/E",""),("I","I",""),
    ("MBP","M/B/P",""),("FV","F/V",""),("RL","R/L",""),
]

class AudioPlaybackProperties(PropertyGroup):
    is_playing: BoolProperty(default=False)  # type: ignore
    volume:     FloatProperty(default=0.8, min=0.0, max=1.0, subtype="FACTOR")  # type: ignore

class DetectedWordItem(PropertyGroup):
    text:        StringProperty(default="")  # type: ignore
    frame_start: IntProperty(default=0)      # type: ignore
    frame_end:   IntProperty(default=0)      # type: ignore
    score:       FloatProperty(default=0.0, min=0.0, max=1.0, subtype="FACTOR")  # type: ignore

class AudioShapeProperties(PropertyGroup):
    is_initialized: BoolProperty(default=False)  # type: ignore
    audio_filepath: StringProperty(subtype="FILE_PATH")  # type: ignore
    language:       EnumProperty(items=get_language_items(), default="FR", name="Langue phonétique")  # type: ignore
    voice_profile:  EnumProperty(items=get_profile_items(), default="MALE", name="Profil vocal")  # type: ignore
    bake_strategy:  EnumProperty(name="Stratégie", items=[
        ("SEQUENCE","SEQUENCE","Séquenceur + Vosk si dispo — émotion/coarticulation activés"),
        ("SPECTRAL","SPECTRAL","FFT+LPC pur — Vosk/émotion/coarticulation désactivés"),
    ], default="SEQUENCE")  # type: ignore

    # ── Vosk : langue du modèle (séparée de la langue phonétique)
    vosk_language: EnumProperty(
        name="Langue du modèle Vosk",
        description="Langue utilisée pour la reconnaissance vocale Vosk",
        items=vosk_helper.get_language_items_for_enum,
        default=0,
    )  # type: ignore

    anim_style:          EnumProperty(name="Style", items=[("REALISTE","Réaliste",""),("CARTOON","Cartoon",""),("ANIME","Anime",""),("MURMURE","Murmure",""),("CUSTOM","Custom","")], default="REALISTE")  # type: ignore
    recognition_engine: EnumProperty(
        name="Moteur de reconnaissance",
        description="VOX = Vosk (durée des mots, sans pics audio) | ASMS = LPC/Formants Praat (sans Vosk)",
        items=[
            ("VOX",  "VOX",  "Vosk — LipSyncBlender si phonemizer, sinon G2P embarqué", "SPEAKER", 0),
            ("ASMS", "ASMS", "Analyse LPC/Formants style Praat — sans Vosk",     "SOUND",   1),
        ],
        default="VOX",
    )  # type: ignore
    asms_lpc_order:     bpy.props.IntProperty(name="Ordre LPC", default=12, min=8, max=24)  # type: ignore
    asms_win_ms:        bpy.props.FloatProperty(name="Fenêtre (ms)", default=25.0, min=10.0, max=100.0)  # type: ignore
    animation_panel_open:BoolProperty(default=False, name="Réglages Animation")  # type: ignore
    seq_inbetween_ms:    FloatProperty(name="In Between (ms)", default=42.0, min=10.0, max=500.0)  # type: ignore
    seq_silence_ms:      FloatProperty(name="Silence (ms)", default=220.0, min=20.0, max=2000.0)  # type: ignore
    seq_silence_frame_threshold_ms:     FloatProperty(name="Seuil silence (ms)", default=220.0, min=20.0, max=2000.0)  # type: ignore
    seq_in_between_frame_threshold_ms:      FloatProperty(name="Seuil in-between (ms)",default=42.0, min=10.0,max=500.0,)  # type: ignore
    seq_close_duration_ms:FloatProperty(name="Lip Close (ms)", default=200.0, min=0.0, max=1000.0)  # type: ignore
    seq_silence_stretch: FloatProperty(name="Stretch ×", default=1.0, min=0.25, max=4.0, subtype="FACTOR")  # type: ignore
    noise_gate_db:       FloatProperty(name="Noise Gate (dB)", default=-40.0, min=-60.0, max=0.0)  # type: ignore
    smooth_curves:       BoolProperty(name="Smooth Bézier", default=False)  # type: ignore

    seq_anticipation:  FloatProperty(
        name="Anticipation ×",
        description="Fraction de la durée du mot pour avancer les visèmes (0 = désactivé).",
        default=0.18, min=0.0, max=0.5, subtype="FACTOR",
    )  # type: ignore
    seq_release:       FloatProperty(
        name="Relâchement ×",
        description="Fraction de la durée du mot pour prolonger la fin du mot (0 = désactivé).",
        default=0.22, min=0.0, max=0.5, subtype="FACTOR",
    )  # type: ignore

    use_highpass:  BoolProperty(name="Passe-haut", default=False)  # type: ignore
    highpass_freq: FloatProperty(name="HP (Hz)", default=80.0, min=20.0, max=1000.0)  # type: ignore
    use_lowpass:   BoolProperty(name="Passe-bas", default=False)  # type: ignore
    lowpass_freq:  FloatProperty(name="LP (Hz)", default=8000.0, min=1000.0, max=20000.0)  # type: ignore

    use_custom_frame_range: BoolProperty(name="Plage custom", default=False)  # type: ignore
    bake_frame_start:       IntProperty(name="Frame début", default=1, min=0)  # type: ignore
    bake_frame_end:         IntProperty(name="Frame fin", default=250, min=1)  # type: ignore

    bake_force_context:     BoolProperty(name="Forcer contexte", default=True)  # type: ignore
    fuzzy_shape_matching:   BoolProperty(name="Matching tolérant", default=True)  # type: ignore
    auto_clear_keyframes:   BoolProperty(name="Nettoyer avant bake", default=True)  # type: ignore

    # ── Preset de phonèmes (v13) ─────────────────────────────────────────────
    phoneme_preset: EnumProperty(
        name="Mode Phonèmes",
        description=(
            "SIMPLE = 3 phonèmes (A, O/U/E, I) | "
            "ADVANCED = 4 phonèmes (A, O/U/E, I, M/P/B) | "
            "EXPERT = 6 phonèmes (A, O/U/E, I, M/P/B, F/V, R/L)"
        ),
        items=[
            ("SIMPLE",   "Simple   — 3 ph.",   "A · O/U/E · I",                       "LAYER_USED",   0),
            ("ADVANCED", "Avancé   — 4 ph.",   "A · O/U/E · I · M/P/B",               "LAYER_ACTIVE",  1),
            ("EXPERT",   "Expert   — 6 ph.",   "A · O/U/E · I · M/P/B · F/V · R/L",  "SOLO_ON",       2),
        ],
        default="ADVANCED",
    )  # type: ignore

    formant_A:   FloatProperty(name="A ×",     default=1.0, min=0.0, max=3.0, subtype="FACTOR")  # type: ignore
    formant_OUE: FloatProperty(name="O/U/E ×", default=1.0, min=0.0, max=3.0, subtype="FACTOR")  # type: ignore
    formant_I:   FloatProperty(name="I ×",     default=1.0, min=0.0, max=3.0, subtype="FACTOR")  # type: ignore
    formant_MBP: FloatProperty(name="MBP ×",   default=1.0, min=0.0, max=3.0, subtype="FACTOR")  # type: ignore
    formant_FV:  FloatProperty(name="FV ×",    default=1.0, min=0.0, max=3.0, subtype="FACTOR")  # type: ignore
    formant_RL:  FloatProperty(name="R/L ×",   default=1.0, min=0.0, max=3.0, subtype="FACTOR")  # type: ignore

    preview_show:          BoolProperty(default=False)  # type: ignore
    preview_manual_viseme: EnumProperty(items=PREVIEW_VISEME_ITEMS, default="SIL")  # type: ignore

    emotion_preset: EnumProperty(name="Preset émotionnel", items=[
        ("NEUTRAL","😐 Neutre",""),("HAPPY","😄 Joyeux",""),("SAD","😢 Triste",""),
        ("ANGRY","😠 Colère",""),("SURPRISED","😲 Surpris",""),
        ("WHISPER","🤫 Chuchoté",""),("SINGING","🎵 Chanté",""),
    ], default="NEUTRAL")  # type: ignore
    emotion_intensity:        FloatProperty(name="Intensité", default=0.5, min=0.0, max=1.0, subtype="FACTOR")  # type: ignore
    coarticulation_strength:  FloatProperty(name="Coarticulation", default=0.6, min=0.0, max=1.0, subtype="FACTOR")  # type: ignore
    organic_tilt:             BoolProperty(name="Tilt organique", default=True)  # type: ignore
    tilt_amount:              FloatProperty(name="Tilt ×", default=0.04, min=0.0, max=0.3, subtype="FACTOR")  # type: ignore

    # Shape keys v13 — phonèmes restructurés
    shape_key_A:   StringProperty(default="")  # type: ignore
    shape_key_OUE: StringProperty(default="")  # type: ignore   # O/U/E groupés
    shape_key_I:   StringProperty(default="")  # type: ignore
    shape_key_MBP: StringProperty(default="")  # type: ignore
    shape_key_FV:  StringProperty(default="")  # type: ignore
    shape_key_RL:  StringProperty(default="")  # type: ignore   # R/L liquides
    shape_key_SIL: StringProperty(default="")  # type: ignore

    security_status:  StringProperty(default="Aucun fichier validé")  # type: ignore
    security_valid:   BoolProperty(default=False)  # type: ignore
    preview_phoneme:  EnumProperty(items=[("A","A",""),("OUE","O/U/E",""),("I","I",""),("MBP","M/B/P",""),("FV","F/V",""),("RL","R/L","")], default="A")  # type: ignore
    show_detection_report: BoolProperty(default=True)  # type: ignore
    detected_words:        CollectionProperty(type=DetectedWordItem)  # type: ignore
    last_bake_summary:     StringProperty(default="")  # type: ignore

PROPERTY_GROUPS = (AudioPlaybackProperties, DetectedWordItem, AudioShapeProperties)

def register_properties():
    for c in PROPERTY_GROUPS: bpy.utils.register_class(c)
    bpy.types.Scene.audioshape_props = bpy.props.PointerProperty(type=AudioShapeProperties)
    bpy.types.Scene.audio_playback   = bpy.props.PointerProperty(type=AudioPlaybackProperties)

def unregister_properties():
    for attr in ("audioshape_props","audio_playback"):
        try: delattr(bpy.types.Scene, attr)
        except: pass
    for c in reversed(PROPERTY_GROUPS):
        try: bpy.utils.unregister_class(c)
        except: pass
