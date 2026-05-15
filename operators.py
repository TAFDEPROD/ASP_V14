# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
operators.py — Opérateurs AudioShapePRO v7.0.

Changements v7.0 :
  - Suppression des opérateurs pip (PipInstallVosk, PipUpdateVosk)
  - Suppression de TestVosk (Vosk est intégré, pas de config manuelle)
  - Ajout de AUDIOSHAPE_OT_OpenHtmlGuide (ouvre guide.html dans le navigateur)
  - BakeAll : séquenceur Vosk + G2P, post-traitement émotion/coarticulation
  - GPU : pas de changement dans les opérateurs
  - OPERATOR_CLASSES complet et propre
"""

from __future__ import annotations

import os
import bpy
import aud
from bpy.props import StringProperty, BoolProperty
from bpy.types import Operator

from . import audio_core, previews, security, sequencer
from . import compat as _compat
from . import emotion_coarticulation
from .formant_library import compute_bake_range
from .performance import PerformanceTracker
from .properties import STYLE_PRESETS
from .utils import (
    auto_map_shape_keys,
    clear_shape_key_keyframes,
    count_keyframes_for_shape_key,
    ensure_fcurve_active,
    resolve_shape_key,
    restore_graph_editor_context,
    restore_user_context,
    save_user_context,
    setup_graph_editor_context,
)


# ═════════════════════════════════════════════════════════════════════════════
# 1. ONE-CLICK SETUP
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_AddLipSync(Operator):
    """One-click setup : initialise AudioShapePRO sur l'objet actif."""
    bl_idname  = "audioshape.add_lipsync"
    bl_label   = "Ajouter Lip Sync à la sélection"
    bl_description = (
        "Initialise AudioShapePRO sur l'objet actif :\n"
        "• Vérifie qu'il y a des shape keys\n"
        "• Auto-mappe les phonèmes (A, E, I, O, U, FV, MBP, SIL)\n"
        "• Applique les réglages par défaut (Réaliste, FR, Homme)\n"
        "• Active la fenêtre d'aperçu animé"
    )
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and getattr(obj, "data", None) is not None

    def execute(self, context):
        props = context.scene.audioshape_props
        obj   = context.active_object

        if obj is None:
            self.report({"ERROR"}, "Aucun objet actif")
            return {"CANCELLED"}

        has_sk = (
            getattr(obj, "data", None) is not None
            and hasattr(obj.data, "shape_keys")
            and obj.data.shape_keys is not None
        )
        if not has_sk:
            self.report({"WARNING"},
                        "L'objet n'a pas de shape keys — créez-en avant le bake.")

        if has_sk:
            mapping = auto_map_shape_keys(obj, props)
            if mapping:
                shapes = ", ".join(f"{k}→{v}" for k, v in mapping.items())
                self.report({"INFO"}, f"Mapping auto : {shapes}")
            else:
                self.report({"WARNING"},
                            "Aucune shape key reconnue. "
                            "Ouvrez le panneau « Mapping » pour les assigner.")

        if props.anim_style == "CUSTOM":
            props.anim_style = "REALISTE"
            preset = STYLE_PRESETS["REALISTE"]
            props.seq_inbetween_ms      = preset["seq_inbetween_ms"]
            props.seq_silence_ms        = preset["seq_silence_ms"]
            props.seq_close_duration_ms = preset["seq_close_duration_ms"]
            props.seq_silence_stretch   = preset["seq_silence_stretch"]
            props.noise_gate_db         = preset["noise_gate_db"]

        props.is_initialized = True

        try:
            from .properties import _apply_addon_prefs_defaults
            _apply_addon_prefs_defaults(props)
        except Exception:
            pass

        # Extraction proactive du modèle Vosk si une source locale existe
        # (zip embarqué legacy ou déjà téléchargé).
        # On ne déclenche PAS de téléchargement réseau ici — il aurait lieu
        # silencieusement au premier bake si nécessaire, ou via le bouton
        # « Télécharger le modèle maintenant » du panneau Vosk.
        try:
            from . import vosk_helper
            if (not vosk_helper.is_model_ready()
                    and vosk_helper.is_model_present_locally()):
                vosk_helper.get_model_path()  # extrait depuis source locale
        except Exception:
            pass

        self.report({"INFO"}, f"✓ Lip Sync activé sur « {obj.name} »")
        return {"FINISHED"}


class AUDIOSHAPE_OT_RemoveLipSync(Operator):
    """Réinitialise AudioShapePRO et supprime les keyframes des shape keys."""
    bl_idname  = "audioshape.remove_lipsync"
    bl_label   = "Retirer Lip Sync"
    bl_description = "Désactive AudioShapePRO et supprime les keyframes des phonèmes"
    bl_options = {"REGISTER", "UNDO"}

    keep_keyframes: BoolProperty(name="Conserver les keyframes", default=False)  # type: ignore

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        props = context.scene.audioshape_props
        obj   = context.active_object

        if obj is not None and not self.keep_keyframes:
            removed_total = 0
            for ph in ("A", "OUE", "I", "MBP", "FV", "RL", "SIL"):
                sk = getattr(props, f"shape_key_{ph}", "")
                if sk:
                    removed_total += clear_shape_key_keyframes(obj, sk)
            self.report({"INFO"}, f"{removed_total} keyframes supprimés")

        props.is_initialized = False
        return {"FINISHED"}


class AUDIOSHAPE_OT_AutoMapShapeKeys(Operator):
    """Tente un auto-mapping fuzzy des shape keys vers les phonèmes."""
    bl_idname  = "audioshape.auto_map_shape_keys"
    bl_label   = "Auto-mapper les shape keys"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.audioshape_props
        obj   = context.active_object
        if obj is None:
            self.report({"ERROR"}, "Aucun objet actif")
            return {"CANCELLED"}
        mapping = auto_map_shape_keys(obj, props)
        if not mapping:
            self.report({"WARNING"}, "Aucune shape key reconnue")
            return {"CANCELLED"}
        shapes = ", ".join(f"{k}→{v}" for k, v in mapping.items())
        self.report({"INFO"}, f"✓ {shapes}")
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# 2. APERÇU ANIMÉ
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_StartPreview(Operator):
    """Lance la lecture audio + l'aperçu animé de la bouche (les deux sont liés)."""
    bl_idname  = "audioshape.start_preview"
    bl_label   = "Lire (audio + aperçu)"
    bl_description = (
        "Lance simultanément la lecture audio et l'animation d'aperçu "
        "de la bouche.\nL'aperçu est intrinsèquement lié à l'audio : "
        "il s'arrête automatiquement à la fin du son."
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        props = getattr(context.scene, "audioshape_props", None)
        return bool(props and props.audio_filepath)

    def execute(self, context):
        # Délégué à play_audio : la preview démarre via on_audio_play().
        # Garantit que toute interaction Play/Stop passe par le même chemin.
        return bpy.ops.audioshape.play_audio()


class AUDIOSHAPE_OT_StopPreview(Operator):
    """Arrête l'audio et l'aperçu (les deux sont liés)."""
    bl_idname  = "audioshape.stop_preview"
    bl_label   = "Arrêter (audio + aperçu)"
    bl_options = {"REGISTER"}

    def execute(self, context):
        # Délégué à stop_audio : la preview s'arrête via on_audio_stop().
        return bpy.ops.audioshape.stop_audio()


class AUDIOSHAPE_OT_SetPreviewViseme(Operator):
    """Définit le visème statique affiché dans l'aperçu."""
    bl_idname  = "audioshape.set_preview_viseme"
    bl_label   = "Définir le visème"
    bl_options = {"REGISTER", "UNDO"}

    viseme: StringProperty(default="SIL")  # type: ignore

    def execute(self, context):
        props = context.scene.audioshape_props
        previews.stop_preview_animation(context.scene)
        try:
            props.preview_manual_viseme = self.viseme
        except TypeError:
            self.report({"ERROR"}, f"Visème invalide : {self.viseme}")
            return {"CANCELLED"}
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# 3. LECTURE / TRAITEMENT AUDIO
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_PlayAudio(Operator):
    """Lance ou arrête la lecture du fichier audio source."""
    bl_idname  = "audioshape.play_audio"
    bl_label   = "Lire audio"
    bl_options = {"REGISTER"}

    def execute(self, context):
        audio_core.initialize_audio()
        props    = context.scene.audioshape_props
        playback = context.scene.audio_playback
        if not props.audio_filepath:
            self.report({"ERROR"}, "Aucun fichier audio sélectionné")
            return {"CANCELLED"}
        if audio_core.is_audio_playing():
            audio_core.stop_audio()
            playback.is_playing = False
            # Synchronisation preview : l'audio s'arrête → la preview s'arrête
            previews.on_audio_stop(context.scene)
        else:
            handle = audio_core.play_audio(props.audio_filepath, playback.volume)
            if handle is None:
                self.report({"ERROR"}, "Impossible de lire le fichier audio")
                return {"CANCELLED"}
            playback.is_playing = True
            # Synchronisation preview : l'audio démarre → la preview démarre
            previews.on_audio_play(context.scene)
        return {"FINISHED"}


class AUDIOSHAPE_OT_StopAudio(Operator):
    """Arrête la lecture audio en cours."""
    bl_idname  = "audioshape.stop_audio"
    bl_label   = "Arrêter audio"
    bl_options = {"REGISTER"}

    def execute(self, context):
        audio_core.stop_audio()
        context.scene.audio_playback.is_playing = False
        # Synchronisation preview : l'audio s'arrête → la preview s'arrête
        previews.on_audio_stop(context.scene)
        return {"FINISHED"}


class AUDIOSHAPE_OT_PlayProcessedAudio(Operator):
    """Lit le son après application du pipeline DSP (preview)."""
    bl_idname  = "audioshape.play_processed_audio"
    bl_label   = "Jouer son traité"
    bl_options = {"REGISTER"}

    def execute(self, context):
        playback = context.scene.audio_playback
        if audio_core.get_processed_sound() is None:
            self.report({"ERROR"}, "Aucun son traité — appliquez d'abord les filtres")
            return {"CANCELLED"}
        if audio_core.is_audio_playing():
            audio_core.stop_audio()
            playback.is_playing = False
        else:
            handle = audio_core.play_processed_audio(playback.volume)
            if handle is None:
                self.report({"ERROR"}, "Erreur lecture son traité")
                return {"CANCELLED"}
            playback.is_playing = True
        return {"FINISHED"}


class AUDIOSHAPE_OT_ProcessAudio(Operator):
    """Applique les filtres + noise gate (preview)."""
    bl_idname  = "audioshape.process_audio"
    bl_label   = "Appliquer filtres"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.audioshape_props
        if not props.audio_filepath:
            self.report({"ERROR"}, "Aucun fichier audio sélectionné")
            return {"CANCELLED"}
        sound = audio_core.process_audio(props.audio_filepath, props)
        if sound is None:
            self.report({"ERROR"}, "Erreur de traitement audio")
            return {"CANCELLED"}
        self.report({"INFO"}, "Filtres appliqués (preview)")
        return {"FINISHED"}


class AUDIOSHAPE_OT_ValidateAudio(Operator):
    """Valide le fichier audio (taille, format, budget mémoire)."""
    bl_idname  = "audioshape.validate_audio"
    bl_label   = "Valider"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.audioshape_props
        if not props.audio_filepath:
            self.report({"ERROR"}, "Aucun fichier audio sélectionné")
            return {"CANCELLED"}
        report = security.validate_audio_file(props.audio_filepath)
        props.security_valid  = report.is_safe
        props.security_status = report.summary()
        if not report.is_safe:
            for err in report.errors:
                self.report({"ERROR"}, err)
            return {"CANCELLED"}
        for warn in report.warnings:
            self.report({"WARNING"}, warn)
        self.report({"INFO"}, f"✓ {report.file_size_mb:.1f} Mo — {report.detected_format}")
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# 4. INSERTION MANUELLE
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_InsertKeyframe(Operator):
    """Insère une clé d'animation sur la shape key du phonème."""
    bl_idname  = "audioshape.insert_keyframe"
    bl_label   = "Insérer clé"
    bl_options = {"REGISTER", "UNDO"}

    target_phoneme: StringProperty(default="A")  # type: ignore

    def execute(self, context):
        props = context.scene.audioshape_props
        obj   = context.object
        if obj is None or not hasattr(getattr(obj, "data", None), "shape_keys"):
            self.report({"ERROR"}, "Sélectionnez un objet avec des shape keys")
            return {"CANCELLED"}
        if obj.data.shape_keys is None:
            self.report({"ERROR"}, "L'objet n'a pas de shape keys")
            return {"CANCELLED"}

        requested = getattr(props, f"shape_key_{self.target_phoneme}", "")
        sk_name = resolve_shape_key(
            obj, requested, self.target_phoneme,
            fuzzy=getattr(props, "fuzzy_shape_matching", True),
        )
        if not sk_name:
            self.report({"ERROR"},
                        f"Shape key introuvable pour [{self.target_phoneme}]")
            return {"CANCELLED"}
        try:
            obj.data.shape_keys.key_blocks[sk_name].keyframe_insert(data_path="value")
            self.report({"INFO"}, f"Clé insérée : '{sk_name}'")
            return {"FINISHED"}
        except Exception as exc:
            self.report({"ERROR"}, f"Erreur : {exc}")
            return {"CANCELLED"}


# ═════════════════════════════════════════════════════════════════════════════
# 5. BAKE FORMANT (sound_to_samples)
# ═════════════════════════════════════════════════════════════════════════════

def _bake_formant(operator, context, phoneme: str) -> bool:
    """Bake d'un phonème via graph.sound_to_samples."""
    props   = context.scene.audioshape_props
    obj     = context.object
    tracker = PerformanceTracker.get()

    if obj is None or not hasattr(getattr(obj, "data", None), "shape_keys"):
        operator.report({"ERROR"}, "Sélectionnez un objet avec des shape keys")
        return False
    if obj.data.shape_keys is None:
        operator.report({"ERROR"}, "L'objet n'a pas de shape keys")
        return False

    proc_path = audio_core.get_processed_filepath()
    filepath  = proc_path if proc_path else props.audio_filepath
    if not filepath or not os.path.isfile(filepath):
        operator.report({"ERROR"}, "Fichier audio introuvable")
        return False

    requested = getattr(props, f"shape_key_{phoneme}", "")
    sk_name = resolve_shape_key(
        obj, requested, phoneme,
        fuzzy=getattr(props, "fuzzy_shape_matching", True),
    )
    if sk_name is None:
        operator.report({"ERROR"},
                        f"[{phoneme}] shape key introuvable (« {requested} »)")
        return False

    strategy = "DOMINANT" if props.bake_strategy == "SEQUENCE" else props.bake_strategy
    modifier = getattr(props, f"formant_{phoneme}", 1.0)
    low_freq, high_freq = compute_bake_range(
        phoneme, props.voice_profile, modifier, strategy
    )

    user_state = save_user_context(context) \
        if getattr(props, "bake_force_context", True) else {}

    if getattr(props, "auto_clear_keyframes", True):
        clear_shape_key_keyframes(obj, sk_name)

    fc = ensure_fcurve_active(obj, sk_name)
    if fc is None:
        operator.report({"ERROR"}, f"Impossible d'activer la F-curve pour '{sk_name}'")
        return False

    graph_area, original_type = setup_graph_editor_context(context)
    if graph_area is None:
        operator.report({"ERROR"}, "Graph Editor introuvable")
        return False

    # ── Résolution de la plage de frames (custom ou auto depuis la scène) ──
    scene = context.scene
    use_custom = getattr(props, "use_custom_frame_range", False)
    if use_custom:
        bake_start = int(getattr(props, "bake_frame_start", scene.frame_start))
        bake_end   = int(getattr(props, "bake_frame_end",   scene.frame_end))
    else:
        bake_start = scene.frame_start
        bake_end   = scene.frame_end

    # Sauvegarder et rétablir la frame courante + plage scène
    saved_frame_current = scene.frame_current
    saved_frame_start   = scene.frame_start
    saved_frame_end     = scene.frame_end

    tracker.start("bake")
    try:
        # CRITIQUE : Initialiser le bake à frame_start (pas à la frame courante)
        # Résout le bug "bake démarre à la position courante de la timeline"
        scene.frame_start   = bake_start
        scene.frame_end     = bake_end
        scene.frame_current = bake_start

        with context.temp_override(
            area=graph_area,
            region=graph_area.regions[-1],
            window=context.window,
            screen=context.screen,
        ):
            bpy.ops.graph.sound_to_samples(
                filepath=filepath, low=low_freq, high=high_freq,
            )

        duration  = tracker.stop("bake")
        keyframes = count_keyframes_for_shape_key(obj, sk_name)
        tracker.record_bake(
            phoneme=phoneme, strategy=props.bake_strategy,
            gender=props.voice_profile, freq_low=low_freq, freq_high=high_freq,
            modifier=modifier, duration_s=duration, keyframes=keyframes,
        )
        operator.report(
            {"INFO"},
            f"[{phoneme}→{sk_name}] {low_freq}–{high_freq} Hz "
            f"({duration*1000:.0f} ms, {keyframes} clés)",
        )
        return True

    except Exception as exc:
        tracker.stop("bake")
        operator.report({"ERROR"}, f"Erreur bake [{phoneme}] : {exc}")
        return False
    finally:
        # Restaurer la frame et la plage courante
        try:
            scene.frame_current = saved_frame_current
            scene.frame_start   = saved_frame_start
            scene.frame_end     = saved_frame_end
        except Exception:
            pass
        # Restaurer frame courante + plage
        try:
            scene.frame_current = _saved_fc
            scene.frame_start   = _saved_fs
            scene.frame_end     = _saved_fe
        except Exception:
            pass
        restore_graph_editor_context(original_type, context)
        if user_state:
            restore_user_context(context, user_state)


class AUDIOSHAPE_OT_BakePhoneme(Operator):
    """Bake un seul phonème via graph.sound_to_samples."""
    bl_idname  = "audioshape.bake_phoneme"
    bl_label   = "Bake"
    bl_options = {"REGISTER", "UNDO"}

    target_phoneme: StringProperty(default="A")  # type: ignore

    def execute(self, context):
        ok = _bake_formant(self, context, self.target_phoneme)
        return {"FINISHED"} if ok else {"CANCELLED"}


# ═════════════════════════════════════════════════════════════════════════════
# 6. BAKE TOUT (séquenceur ou formant)
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_BakeAll(Operator):
    """Bake tous les phonèmes — séquenceur (Vosk+FFT) ou formant selon stratégie."""
    bl_idname  = "audioshape.bake_all"
    bl_label   = "Bake tout"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props   = context.scene.audioshape_props
        obj     = context.object
        scene   = context.scene
        tracker = PerformanceTracker.get()

        if obj is None or not hasattr(getattr(obj, "data", None), "shape_keys"):
            self.report({"ERROR"}, "Sélectionnez un objet avec des shape keys")
            return {"CANCELLED"}
        if obj.data.shape_keys is None:
            self.report({"ERROR"}, "L'objet n'a pas de shape keys")
            return {"CANCELLED"}
        if not props.audio_filepath:
            self.report({"ERROR"}, "Aucun fichier audio sélectionné")
            return {"CANCELLED"}

        # Validation auto si pas encore validé
        if not props.security_valid:
            report = security.validate_audio_file(props.audio_filepath)
            props.security_valid  = report.is_safe
            props.security_status = report.summary()
            if not report.is_safe:
                for err in report.errors:
                    self.report({"ERROR"}, err)
                return {"CANCELLED"}

        # Branchement : SEQUENCE ou SPECTRAL
        if props.bake_strategy == "SEQUENCE":
            return self._bake_sequencer(context, props, obj, scene, tracker)
        else:
            # SPECTRAL : bake formant + émotion/coarticulation
            return self._bake_spectral(context, props, obj)

    def _bake_sequencer(self, context, props, obj, scene, tracker):
        # Auto-détection Vosk (pas de sous-mode manuel)
        # Résolution mapping phonèmes → shape keys
        sk_map: dict[str, str] = {}
        # v13 — phonèmes actifs selon le preset
        from .formant_library import MODE_PHONEMES as _MP
        _preset = getattr(props, "phoneme_preset", "ADVANCED")
        _active_phs = _MP.get(_preset, _MP["ADVANCED"])
        for ph in _active_phs:
            requested = getattr(props, f"shape_key_{ph}", "")
            sk = resolve_shape_key(
                obj, requested, ph,
                fuzzy=getattr(props, "fuzzy_shape_matching", True),
            )
            if sk:
                sk_map[ph] = sk

        if not sk_map:
            self.report({"ERROR"}, "Aucune shape key valide assignée")
            return {"CANCELLED"}

        sil_sk = resolve_shape_key(
            obj, getattr(props, "shape_key_SIL", "SIL"), "SIL",
            fuzzy=getattr(props, "fuzzy_shape_matching", True),
        ) or ""

        fps = scene.render.fps / max(scene.render.fps_base, 0.001)
        # Résolution plage frames (custom ou auto)
        use_custom = getattr(props, "use_custom_frame_range", False)
        if use_custom:
            _bake_start = int(getattr(props, "bake_frame_start", scene.frame_start))
            _bake_end   = int(getattr(props, "bake_frame_end",   scene.frame_end))
            # Appliquer temporairement sur la scène pour le séquenceur
            _saved_fs, _saved_fe = scene.frame_start, scene.frame_end
            scene.frame_start = _bake_start
            scene.frame_end   = _bake_end
        params = sequencer.SequencerParams(
            inbetween_fr=max(0, int(props.seq_inbetween_ms * fps / 1000.0)),
            silence_fr=max(1, round(props.seq_silence_ms / 1000.0 * fps)),
            close_fr=max(1, round(props.seq_close_duration_ms / 1000.0 * fps)),
            silence_stretch=float(props.seq_silence_stretch),
            gate_db=float(props.noise_gate_db),
            profile=props.voice_profile,
            language=props.language,
            mode=getattr(props, "phoneme_preset", "ADVANCED"),
            use_vosk=True,  # FORCE VOSK
            smooth_curves=bool(props.smooth_curves),
            anticipation=float(getattr(props, "seq_anticipation", 0.18)),
            release=float(getattr(props, "seq_release", 0.22)),
            silence_frame_threshold=max(1, round(props.seq_silence_frame_threshold_ms / 1000.0 * fps)),
            in_between_frame_threshold=max(1, round(props.seq_in_between_frame_threshold_ms / 1000.0 * fps)),
        )
        user_state = save_user_context(context) \
            if getattr(props, "bake_force_context", True) else {}

        try:
            result = sequencer.run_sequencer(
                self, context, obj, sk_map,
                sil_sk_name=sil_sk,
                params=params,
                src_filepath=props.audio_filepath,
                use_highpass=bool(props.use_highpass),
                highpass_freq=float(props.highpass_freq),
                use_lowpass=bool(props.use_lowpass),
                lowpass_freq=float(props.lowpass_freq),
            )
        finally:
            if user_state:
                restore_user_context(context, user_state)
            # Restaurer la plage de frames si modifiée
            if use_custom:
                try:
                    scene.frame_start = _saved_fs
                    scene.frame_end   = _saved_fe
                except Exception:
                    pass

        if result is None:
            return {"CANCELLED"}

        tracker.record_bake(
            phoneme="SEQ/ALL", strategy="SEQUENCE",
            gender=props.voice_profile, freq_low=20, freq_high=20000,
            modifier=float(params.inbetween_fr),
            duration_s=result.duration_s, keyframes=result.total_keyframes,
        )

        props.detected_words.clear()
        for w in (result.detected_words or [])[:50]:
            item = props.detected_words.add()
            item.text        = w.as_text() if hasattr(w, "as_text") else str(w)
            item.frame_start = getattr(w, "frame_start", 0)
            item.frame_end   = getattr(w, "frame_end", 0)
            item.score       = float(getattr(w, "avg_score", 0.0))

        # ── POST-PROCESSING : Émotion & Coarticulation ──────────────────
        emotion = getattr(props, "emotion_preset", "NEUTRAL")
        coart   = getattr(props, "coarticulation_strength", 0.6)
        has_post = (emotion != "NEUTRAL") or (coart > 0.01)

        if has_post:
            sub = getattr(result, "analysis_mode", "VOSK_G2P")
            emotion_coarticulation.run_post_processing(
                self, obj, props, sk_map, strategy=f"SEQUENCE/{sub}"
            )

        sil_info  = f" + SIL '{sil_sk}'" if sil_sk else ""
        mode_info = f"[{getattr(result, 'analysis_mode', 'FFT')}] "
        smooth_info = " | Smooth Bézier" if props.smooth_curves else ""
        emotion_info = f" | {emotion}" if emotion != "NEUTRAL" else ""
        coart_info   = f" | Coart×{coart:.1f}" if coart > 0.01 else ""
        props.last_bake_summary = (
            f"{mode_info}gate {params.gate_db:.0f} dB • "
            f"{result.n_speech_segments} mots • "
            f"{result.n_silences} silences{sil_info}{smooth_info}"
            f"{emotion_info}{coart_info} • "
            f"{result.total_keyframes} clés ({result.duration_s*1000:.0f} ms)"
        )
        self.report({"INFO"}, f"✓ {props.last_bake_summary}")
        return {"FINISHED"}

    def _bake_spectral(self, context, props, obj):
        """SPECTRAL : bake formant (sound_to_samples) + pondération post-bake."""
        result = self._bake_formant_all(context, props)

        # ── Pondération spectrale — lissage des résultats bruts ──────────
        # En mode spectral, sound_to_samples produit des keyframes très denses
        # et brutes. On applique une pondération pour adoucir le résultat.
        self._apply_spectral_weighting(context, props, obj)

        return result

    def _apply_spectral_weighting(self, context, props, obj):
        """
        Pondération post-bake pour le mode SPECTRAL.

        Problème : sound_to_samples génère des keyframes toutes les frames →
        résultat très bruité et peu lisible.

        Solution : pour chaque F-curve de shape key, appliquer :
          1. Un FModifier SMOOTH (noise reduction) via le Graph Editor
          2. Un sous-échantillonnage décimé (supprimer les kf trop proches)
          3. Une interpolation Bézier AUTO_CLAMPED pour des transitions fluides
        """
        try:
            sk_data = getattr(getattr(obj, "data", None), "shape_keys", None)
            if sk_data is None:
                return
            anim = getattr(sk_data, "animation_data", None)
            if anim is None or anim.action is None:
                return
            action = anim.action

            # Récupérer les F-curves des shape keys mappées
            target_fcs = []
            for ph in ("A", "OUE", "I", "MBP", "FV", "RL"):
                sk_name = getattr(props, f"shape_key_{ph}", "")
                if not sk_name:
                    continue
                for path_tmpl in (f'key_blocks["{sk_name}"].value',
                                  f"key_blocks['{sk_name}'].value"):
                    fc = _compat.action_fcurves(action).find(path_tmpl, index=0)
                    if fc:
                        target_fcs.append(fc)
                        break

            for fc in target_fcs:
                kps = list(fc.keyframe_points)
                n = len(kps)
                if n < 4:
                    continue

                # ── Étape 1 : décimation — garder 1 kf sur N ─────────────
                # Adaptatif selon la densité
                decimation = max(2, min(8, n // 120))
                to_remove = []
                for i, kp in enumerate(kps):
                    if i % decimation != 0:
                        # Garder les pics importants (valeur > 0.6)
                        if kp.co.y < 0.60:
                            to_remove.append(kp)
                for kp in to_remove:
                    try:
                        fc.keyframe_points.remove(kp)
                    except Exception:
                        pass

                # ── Étape 2 : lissage Bézier AUTO_CLAMPED ────────────────
                for kp in fc.keyframe_points:
                    kp.interpolation    = "BEZIER"
                    kp.handle_left_type  = "AUTO_CLAMPED"
                    kp.handle_right_type = "AUTO_CLAMPED"

                # ── Étape 3 : FModifier SMOOTH (Blender 4.x) ─────────────
                # Supprime les anciens modifiers ASP
                for mod in list(fc.modifiers):
                    if getattr(mod, "name", "").startswith("ASP_SMOOTH"):
                        try:
                            fc.modifiers.remove(mod)
                        except Exception:
                            pass
                try:
                    smooth_mod = fc.modifiers.new(type="SMOOTH")
                    smooth_mod.name = "ASP_SMOOTH"
                    smooth_mod.factor = 1.0
                    smooth_mod.iterations = 4
                except (AttributeError, TypeError):
                    pass  # FModifier SMOOTH pas disponible sur cette version

                fc.update()

        except Exception as exc:
            print(f"[AudioShapePRO] Pondération spectrale : {exc}")

    def _bake_formant_all(self, context, props):
        from .formant_library import MODE_PHONEMES as _MP
        _preset = getattr(props, "phoneme_preset", "ADVANCED")
        phonemes = list(_MP.get(_preset, _MP["ADVANCED"]))
        success: list[str] = []
        failed: list[str]  = []
        for ph in phonemes:
            if _bake_formant(self, context, ph):
                success.append(ph)
            else:
                failed.append(ph)
        if failed:
            self.report({"WARNING"},
                f"{len(success)}/{len(phonemes)} OK. Échecs : {', '.join(failed)}")
        else:
            self.report({"INFO"}, f"✓ {len(success)} bakes terminés")
        return {"FINISHED"} if success else {"CANCELLED"}


# ═════════════════════════════════════════════════════════════════════════════
# 7. PRESETS DE STYLE
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_ApplyPhonemePreset(Operator):
    """Sélectionne le preset de phonèmes (Simple / Avancé / Expert)."""
    bl_idname  = "audioshape.apply_phoneme_preset"
    bl_label   = "Appliquer preset phonèmes"
    bl_options = {"REGISTER", "UNDO"}
    preset_id: StringProperty(default="ADVANCED")  # type: ignore
    def execute(self, context):
        props = context.scene.audioshape_props
        valid = {"SIMPLE", "ADVANCED", "EXPERT"}
        if self.preset_id not in valid:
            self.report({"ERROR"}, f"Preset inconnu : {self.preset_id}")
            return {"CANCELLED"}
        props.phoneme_preset = self.preset_id
        _labels = {"SIMPLE": "Simple (3 ph.)", "ADVANCED": "Avancé (4 ph.)", "EXPERT": "Expert (6 ph.)"}
        self.report({"INFO"}, f"Mode phonèmes : {_labels[self.preset_id]}")
        return {"FINISHED"}


class AUDIOSHAPE_OT_ApplyStylePreset(Operator):
    """Applique un preset de style d'animation."""
    bl_idname  = "audioshape.apply_style_preset"
    bl_label   = "Appliquer le style"
    bl_options = {"REGISTER", "UNDO"}

    preset_id: StringProperty(default="REALISTE")  # type: ignore

    def execute(self, context):
        props  = context.scene.audioshape_props
        preset = STYLE_PRESETS.get(self.preset_id)
        if preset is None:
            self.report({"ERROR"}, f"Preset inconnu : {self.preset_id}")
            return {"CANCELLED"}
        props.seq_inbetween_ms      = preset["seq_inbetween_ms"]
        props.seq_silence_ms        = preset["seq_silence_ms"]
        props.seq_close_duration_ms = preset["seq_close_duration_ms"]
        props.seq_silence_stretch   = preset["seq_silence_stretch"]
        props.noise_gate_db         = preset["noise_gate_db"]
        props.anim_style            = self.preset_id
        self.report({"INFO"}, f"Style « {preset['label']} » appliqué")
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# 8. MAINTENANCE
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_ClearMemory(Operator):
    """Libère le budget mémoire et supprime les fichiers temporaires."""
    bl_idname  = "audioshape.clear_memory"
    bl_label   = "Nettoyer la mémoire"
    bl_options = {"REGISTER"}

    def execute(self, context):
        budget = security.get_budget()
        removed, errors = budget.cleanup_all_temps()
        audio_core.cleanup_audio()
        msg = f"Mémoire libérée — {removed} fichier(s) temp."
        if errors:
            self.report({"WARNING"}, msg + f" ({len(errors)} erreur(s))")
        else:
            self.report({"INFO"}, msg)
        return {"FINISHED"}


class AUDIOSHAPE_OT_ResetPerf(Operator):
    """Remet à zéro les statistiques de performance."""
    bl_idname  = "audioshape.reset_perf"
    bl_label   = "Réinitialiser les stats"
    bl_options = {"REGISTER"}

    def execute(self, context):
        PerformanceTracker.reset()
        self.report({"INFO"}, "Statistiques remises à zéro")
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# 9. AIDE & GUIDE
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_OpenHelp(Operator):
    """Ouvre une fenêtre d'aide rapide AudioShapePRO."""
    bl_idname  = "audioshape.open_help"
    bl_label   = "Aide rapide"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        return {"FINISHED"}

    def invoke(self, context, event):
        return context.window_manager.invoke_popup(self, width=480)

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.scale_y = 0.9
        col.label(text="AudioShapePRO v7.0 — Aide rapide", icon="QUESTION")
        col.separator()

        b = col.box().column(align=True)
        b.scale_y = 0.85
        b.label(text="Démarrage", icon="PLAY")
        b.label(text="1. Sélectionnez l'objet avec Shape Keys.")
        b.label(text="2. Cliquez « Ajouter Lip Sync à la sélection ».")
        b.label(text="3. Chargez votre fichier audio.")
        b.label(text="4. Cliquez « Bake tout » — c'est terminé !")

        b = col.box().column(align=True)
        b.scale_y = 0.85
        b.label(text="Smooth Bézier (v7.0)", icon="IPO_BEZIER")
        b.label(text="Panneau « Vosk + Smooth » → cochez Smooth.")
        b.label(text="Slider 1.0 = standard, 2-3 = très lisse.")

        b = col.box().column(align=True)
        b.scale_y = 0.85
        b.label(text="Vosk intégré", icon="SPEAKER")
        b.label(text="Aucune installation pip — modèle FR embarqué.")
        b.label(text="Extrait automatiquement au premier bake.")

        col.separator(factor=0.4)
        col.operator("audioshape.open_html_guide",
                     text="📖 Guide complet (HTML)", icon="URL")


class AUDIOSHAPE_OT_OpenHtmlGuide(Operator):
    """Ouvre le guide utilisateur HTML dans le navigateur par défaut."""
    bl_idname  = "audioshape.open_html_guide"
    bl_label   = "Guide utilisateur HTML"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        import webbrowser
        import pathlib
        guide_path = pathlib.Path(__file__).parent / "guide.html"
        if guide_path.exists():
            webbrowser.open(guide_path.as_uri())
            self.report({"INFO"}, f"Guide ouvert : {guide_path}")
        else:
            self.report({"WARNING"}, "guide.html introuvable dans le dossier de l'addon")
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# 10. TÉLÉCHARGEMENT MANUEL DU MODÈLE VOSK
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_DownloadVoskModel(Operator):
    """Télécharge et extrait le modèle Vosk FR depuis l'URL officielle.

    Le modèle (~41 Mo) est téléchargé dans le cache utilisateur Blender
    et n'a besoin d'être obtenu qu'une seule fois — il est ensuite réutilisé
    indéfiniment, hors-ligne. Au premier bake, le téléchargement se déclenche
    automatiquement si l'utilisateur ne l'a pas déjà fait via ce bouton.
    """
    bl_idname  = "audioshape.download_vosk_model"
    bl_label   = "Télécharger le modèle Vosk FR"
    bl_description = (
        "Télécharge et extrait le modèle Vosk FR depuis alphacephei.com.\n"
        "Opération unique (~41 Mo). Mis en cache hors-ligne pour toujours."
    )
    bl_options = {"REGISTER", "INTERNAL"}

    @classmethod
    def poll(cls, context):
        from . import vosk_helper
        return not vosk_helper.is_model_ready()

    def execute(self, context):
        from . import vosk_helper
        if vosk_helper.is_model_ready():
            self.report({"INFO"}, "Modèle déjà prêt.")
            return {"FINISHED"}

        self.report({"INFO"}, "Téléchargement du modèle Vosk FR en cours…")
        # Bloquant — l'utilisateur a explicitement cliqué et accepte l'attente.
        path = vosk_helper.get_model_path()
        if path is None:
            self.report({"ERROR"},
                "Échec du téléchargement. Vérifiez la connexion réseau "
                "(voir la console pour les détails).")
            return {"CANCELLED"}
        self.report({"INFO"}, f"✓ Modèle téléchargé et prêt : {path.name}")
        # Forcer un redraw du panneau pour mettre à jour les statuts
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
        return {"FINISHED"}



class AUDIOSHAPE_OT_ApplyEmotionPreset(Operator):
    """Sélectionne un preset émotionnel depuis les boutons du panneau."""
    bl_idname  = "audioshape.apply_emotion_preset"
    bl_label   = "Preset émotionnel"
    bl_options = {"REGISTER", "UNDO"}

    preset_id: StringProperty(default="NEUTRAL")  # type: ignore

    def execute(self, context):
        props = context.scene.audioshape_props
        try:
            props.emotion_preset = self.preset_id
        except TypeError:
            self.report({"ERROR"}, f"Preset invalide : {self.preset_id}")
            return {"CANCELLED"}
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
        return {"FINISHED"}


# ═════════════════════════════════════════════════════════════════════════════
# REGISTRE — liste exhaustive de toutes les classes
# ═════════════════════════════════════════════════════════════════════════════


class AUDIOSHAPE_OT_FetchVoskList(bpy.types.Operator):
    """Télécharge la liste des modèles Vosk disponibles."""
    bl_idname = "audioshape.fetch_vosk_list"
    bl_label  = "Actualiser liste Vosk"
    def execute(self, context):
        from . import vosk_helper
        if vosk_helper.fetch_language_list():
            self.report({"INFO"}, "Liste modèles Vosk actualisée.")
        else:
            self.report({"ERROR"}, "Échec fetch liste — vérifiez Online Access.")
        for a in context.screen.areas:
            if a.type == "VIEW_3D": a.tag_redraw()
        return {"FINISHED"}


class AUDIOSHAPE_OT_ReinstallVosk(bpy.types.Operator):
    """Force la ré-installation de Vosk depuis les wheels embarquées."""
    bl_idname = "audioshape.reinstall_vosk"
    bl_label  = "Installer Vosk (wheels embarquées)"
    bl_description = (
        "Installe Vosk depuis les fichiers wheels/ inclus dans l'addon.\n"
        "Utilisez ce bouton si Vosk n'est pas détecté après l'installation."
    )

    def execute(self, context):
        from . import vosk_helper
        # Force la réinstallation même si Vosk semble disponible
        import sys
        for mod in list(sys.modules.keys()):
            if "vosk" in mod.lower():
                del sys.modules[mod]

        ok = vosk_helper.auto_install_vosk_if_needed()
        if ok:
            ver = vosk_helper.get_vosk_version()
            self.report({"INFO"}, f"Vosk {ver} installé avec succès ✓ Relancez Blender si les shapes ne bougent pas.")
        else:
            self.report({"ERROR"},
                        "Installation Vosk échouée. Vérifiez la console (Window > Toggle System Console).")
        return {"FINISHED"}


class AUDIOSHAPE_OT_InstallVoskModel(bpy.types.Operator):
    """Lance le téléchargement + installation du modèle Vosk sélectionné."""
    bl_idname = "audioshape.install_vosk_model"
    bl_label  = "Installer modèle Vosk"
    def execute(self, context):
        from . import vosk_helper
        lang = getattr(context.scene.audioshape_props, "vosk_language", "none")
        if not lang or lang == "none":
            self.report({"ERROR"}, "Aucune langue sélectionnée."); return {"CANCELLED"}
        if vosk_helper.is_downloading():
            self.report({"WARNING"}, "Téléchargement déjà en cours."); return {"CANCELLED"}
        ok = vosk_helper.start_download(lang)
        if ok: self.report({"INFO"}, f"Téléchargement '{lang}' démarré…")
        else:  self.report({"ERROR"}, "Impossible de démarrer le worker.")
        return {"FINISHED"} if ok else {"CANCELLED"}



# ═════════════════════════════════════════════════════════════════════════════
# MOTEUR DE RECONNAISSANCE — Boîte de dialogue
# ═════════════════════════════════════════════════════════════════════════════

class AUDIOSHAPE_OT_SelectEngine(bpy.types.Operator):
    """Choix du moteur de reconnaissance vocale et statut."""
    bl_idname  = "audioshape.select_engine"
    bl_label   = "Moteur de reconnaissance vocale"
    bl_options = {"REGISTER"}

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        props  = context.scene.audioshape_props

        # Sélecteur moteur
        b = layout.box()
        b.label(text="Sélectionnez le moteur :", icon="SOUND")
        c = b.column(align=True)
        for eid, elabel, edesc in [
            ("VOX",  "🎙  VOX",  "Vosk — LipSyncBlender si phonemizer, sinon G2P"),
            ("ASMS", "🔬  ASMS", "LPC/Formants Praat — analyse spectrale, sans Vosk"),
        ]:
            row = c.row(align=True)
            row.scale_y = 1.4
            row.prop_enum(props, "recognition_engine", eid)

        layout.separator(factor=0.4)

        # Statut en temps réel
        b2 = layout.box()
        b2.label(text="Statut des moteurs", icon="INFO")
        col = b2.column(align=True)
        col.scale_y = 0.85

        # VOX / Vosk
        from . import vosk_helper as _vh
        vosk_ok = _vh.is_vosk_available()
        model_ok = _vh.is_model_ready()
        if vosk_ok and model_ok:
            col.label(text="✓  VOX  — Vosk installé + modèle prêt", icon="CHECKMARK")
        elif vosk_ok:
            col.label(text="⚠  VOX  — Vosk OK mais modèle manquant", icon="ERROR")
        else:
            col.label(text="✗  VOX  — Vosk non installé", icon="X")

        from . import lsb_sequencer as _lsb
        if _lsb.is_available():
            col.label(text="✓  Séquence — phonemizer (style LipSyncBlender)", icon="CHECKMARK")
        else:
            col.label(text="○  Séquence — G2P embarqué (installer phonemizer pour LSB)", icon="INFO")

        # ASMS
        try:
            from . import asms_engine
            has_np = asms_engine._NP
        except Exception:
            has_np = False
        if has_np:
            col.label(text="✓  ASMS — numpy disponible", icon="CHECKMARK")
        else:
            col.label(text="⚠  ASMS — numpy manquant (mode fallback)", icon="ERROR")

        layout.separator(factor=0.4)
        # ASMS params avancés
        if props.recognition_engine == "ASMS":
            b3 = layout.box()
            b3.label(text="Paramètres ASMS", icon="SETTINGS")
            b3.prop(props, "asms_lpc_order")
            b3.prop(props, "asms_win_ms")

    def execute(self, context):
        eng = context.scene.audioshape_props.recognition_engine
        self.report({"INFO"}, f"Moteur sélectionné : {eng}")
        return {"FINISHED"}


OPERATOR_CLASSES: tuple[type, ...] = (
    # Setup
    AUDIOSHAPE_OT_AddLipSync,
    AUDIOSHAPE_OT_RemoveLipSync,
    AUDIOSHAPE_OT_AutoMapShapeKeys,
    # Aperçu
    AUDIOSHAPE_OT_StartPreview,
    AUDIOSHAPE_OT_StopPreview,
    AUDIOSHAPE_OT_SetPreviewViseme,
    # Audio
    AUDIOSHAPE_OT_PlayAudio,
    AUDIOSHAPE_OT_StopAudio,
    AUDIOSHAPE_OT_PlayProcessedAudio,
    AUDIOSHAPE_OT_ProcessAudio,
    AUDIOSHAPE_OT_ValidateAudio,
    # Bake
    AUDIOSHAPE_OT_InsertKeyframe,
    AUDIOSHAPE_OT_BakePhoneme,
    AUDIOSHAPE_OT_BakeAll,
    # Phonème preset (v13)
    AUDIOSHAPE_OT_ApplyPhonemePreset,
    # Style
    AUDIOSHAPE_OT_ApplyStylePreset,
    AUDIOSHAPE_OT_ApplyEmotionPreset,
    AUDIOSHAPE_OT_FetchVoskList,
    AUDIOSHAPE_OT_ReinstallVosk,
    AUDIOSHAPE_OT_InstallVoskModel,
    # Moteur
    AUDIOSHAPE_OT_SelectEngine,
    # Maintenance
    AUDIOSHAPE_OT_ClearMemory,
    AUDIOSHAPE_OT_ResetPerf,
    # Aide
    AUDIOSHAPE_OT_OpenHelp,
    AUDIOSHAPE_OT_OpenHtmlGuide,
    # Vosk
    AUDIOSHAPE_OT_DownloadVoskModel,
)
