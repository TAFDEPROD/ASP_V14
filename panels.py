# SPDX-License-Identifier: MIT
"""panels.py v11 — UI adaptée version Blender + gestion modèle Vosk intégrée."""
from __future__ import annotations
import bpy
from bpy.types import Panel
from . import compat, previews, vosk_helper
from .formant_library import LANGUAGE_METADATA, compute_bake_range, get_formant_preview_data
from .performance import PerformanceTracker
from .properties import PREVIEW_VISEME_ITEMS
from .security import get_security_summary
from .utils import fmt_hz, get_object_summary

# v13 — phonèmes restructurés (OUE groupé, RL ajouté)
ALL_PHONEME_LABELS = [("A","A"),("OUE","O/U/E"),("I","I"),("MBP","M/B/P"),("FV","F/V"),("RL","R/L")]

def _has_sk(obj):
    return obj and getattr(obj,"data",None) and getattr(obj.data,"shape_keys",None)

def _vosk_status_lines() -> list[tuple[str,str]]:
    """Retourne des lignes (texte, icon) pour le statut Vosk."""
    lines = []
    bl = f"Blender {compat.BL_VER[0]}.{compat.BL_VER[1]}"
    # CORRECTIF v12 : Vosk supporté dès 4.2 (plus de warning inutile sur 4.4)
    if not compat.BL_GTE_42:
        lines.append((f"⚠ {bl} : AudioShapePRO requiert Blender ≥ 4.2", "ERROR"))
    avail = vosk_helper.is_vosk_available()
    if avail:
        lines.append((f"✓ Vosk {vosk_helper.get_vosk_version()} disponible", "CHECKMARK"))
    else:
        lines.append(("✗ Vosk non installé — relancez Blender ou cliquez Installer", "ERROR"))

    if vosk_helper.is_downloading():
        pct = int(vosk_helper.get_download_progress()*100)
        lines.append((f"⬇ {vosk_helper.get_download_status()} {pct}%", "TIME"))
    elif vosk_helper.is_any_model_ready():
        lines.append(("✓ Modèle installé", "CHECKMARK"))
    else:
        lines.append(("○ Aucun modèle installé", "INFO"))
    return lines


# ═══════════════════════════════════════════════════════════════════════════════
class VIEW3D_PT_AudioShapePRO(Panel):
    bl_label      = "AudioShapePRO  v11"
    bl_space_type = "VIEW_3D"
    bl_region_type= "UI"
    bl_category   = "Lip Sync"

    @classmethod
    def poll(cls, context): return True

    def draw(self, context):
        layout   = self.layout
        props    = context.scene.audioshape_props
        playback = context.scene.audio_playback
        obj      = context.object

        # En-tête
        h = layout.row(align=True)
        h.label(text="", icon="OUTLINER_OB_SPEAKER")
        h.label(text=get_object_summary(obj))
        h.operator("audioshape.open_help", text="", icon="QUESTION")

        # Pas initialisé
        if not props.is_initialized:
            b = layout.box(); c = b.column(align=True); c.scale_y=0.85
            c.label(text="Sélectionnez un objet avec shape keys", icon="INFO")
            r = layout.row(); r.scale_y=1.6
            r.operator("audioshape.add_lipsync", text="✨  Ajouter Lip Sync", icon="ADD")
            return

        # 1. Audio
        b = layout.box(); b.label(text="Audio", icon="SOUND")
        b.prop(props,"audio_filepath",text="")
        r = b.row(align=True)
        r.label(text=props.security_status, icon="CHECKMARK" if props.security_valid else "ERROR")
        r.operator("audioshape.validate_audio", text="Valider", icon="VIEWZOOM")
        r = b.row(align=True)
        r.prop(playback,"volume",slider=True,text="Vol.")
        if playback.is_playing: r.operator("audioshape.stop_audio",icon="PAUSE",text="Stop")
        else: r.operator("audioshape.play_audio",icon="PLAY",text="Lire")

        # 2. Config
        b = layout.box(); b.label(text="Configuration", icon="OPTIONS")
        b.prop(props,"language",text="Langue phonétique",icon="WORLD")
        b.prop(props,"voice_profile",text="Voix",icon="USER")
        b.prop(props,"bake_strategy",text="Stratégie")

        # ── Moteur de reconnaissance ──
        r_eng = b.row(align=True)
        r_eng.scale_y = 1.2
        # Indicateur de statut moteur (pastille colorée)
        from . import vosk_helper as _vh
        from . import asms_engine as _asms
        vosk_ok  = _vh.is_vosk_available() and _vh.is_model_ready()
        asms_ok  = getattr(_asms, "_NP", False)
        eng = getattr(props, "recognition_engine", "VOX")
        if   eng == "VOX":   status_icon = "CHECKMARK" if vosk_ok  else "ERROR"
        else:                status_icon = "CHECKMARK" if asms_ok   else "ERROR"  # ASMS
        r_eng.label(text=f"Moteur : {eng}", icon=status_icon)
        r_eng.operator("audioshape.select_engine", text="Changer…", icon="SETTINGS")
        # Info version Blender
        ic = layout.column(align=True); ic.scale_y=0.72
        if not compat.BL_GTE_45:
            ic.label(text=f"⚠ Blender {compat.BL_VER[0]}.{compat.BL_VER[1]}: certaines fonctions limitées",icon="ERROR")

        # 3. AUTOMAP
        layout.separator(factor=0.3)
        r = layout.row(); r.scale_y=1.4
        r.operator("audioshape.auto_map_shape_keys",text="🔗  AUTOMAP Shape Keys",icon="LINKED")

        # 4. Formants (SPECTRAL uniquement)
        strat = props.bake_strategy
        if strat == "SPECTRAL":
            b = layout.box(); b.label(text="Modificateur formants ×",icon="MODIFIER")
            c = b.column(align=True)
            _active_phs_f = {
                "SIMPLE":   ["A","OUE","I"],
                "ADVANCED": ["A","OUE","I","MBP"],
                "EXPERT":   ["A","OUE","I","MBP","FV","RL"],
            }
            for ph in _active_phs_f.get(props.phoneme_preset, _active_phs_f["ADVANCED"]):
                c.prop(props,f"formant_{ph}",slider=True)

        # 5. Animation
        if strat == "SEQUENCE":
            b = layout.box()
            r = b.row(align=True); r.scale_y=1.2
            for pid,lbl in [("REALISTE","Réaliste"),("CARTOON","Cartoon"),("ANIME","Anime"),("MURMURE","Murmure")]:
                op=r.operator("audioshape.apply_style_preset",text=lbl,depress=(props.anim_style==pid))
                op.preset_id=pid
            ex = b.row(align=True)
            ex.prop(props,"animation_panel_open",
                text="Réglages timing",
                icon="TRIA_DOWN" if props.animation_panel_open else "TRIA_RIGHT",
                emboss=False)
            if props.animation_panel_open:
                c = b.column(align=True)
                c.prop(props,"seq_inbetween_ms")
                c.prop(props,"seq_silence_ms")
                c.prop(props,"seq_silence_frame_threshold_ms")
                c.prop(props,"seq_in_between_frame_threshold_ms") 
                c.prop(props,"seq_close_duration_ms")
                c.prop(props,"seq_silence_stretch",slider=True)
                c.separator(factor=0.4)
                c.prop(props,"noise_gate_db",slider=True)
                c.prop(props,"smooth_curves",icon="IPO_BEZIER",toggle=True)
                if props.recognition_engine == "VOX":
                    c.separator(factor=0.6)
                    c.label(text="Anticipation / Relâchement (VOX)", icon="SORTTIME")
                    row_ar = c.row(align=True)
                    row_ar.prop(props, "seq_anticipation", slider=True)
                    row_ar.prop(props, "seq_release", slider=True)
        else:
            b = layout.box(); b.enabled=False
            b.label(text="Animation / Émotion / Coarticulation — mode SEQUENCE uniquement",icon="INFO")

        # 6. Plage de frames
        b = layout.box(); b.label(text="Plage de frames",icon="PREVIEW_RANGE")
        r = b.row(align=True)
        r.prop(props,"use_custom_frame_range",toggle=True,text="Custom",icon="SETTINGS")
        if props.use_custom_frame_range:
            r2 = b.row(align=True)
            r2.prop(props,"bake_frame_start",text="Début")
            r2.prop(props,"bake_frame_end",text="Fin")
        else:
            r2 = b.row(); r2.scale_y=0.8
            sc = context.scene
            r2.label(text=f"Auto : {sc.frame_start} → {sc.frame_end}",icon="INFO")

        # 7. BAKE
        layout.separator(factor=0.5)
        r = layout.row(); r.scale_y=1.8
        r.operator("audioshape.bake_all",icon="RENDER_ANIMATION",text="⚡  BAKE")

        layout.separator(factor=0.5)
        layout.operator("audioshape.remove_lipsync",text="Retirer Lip Sync",icon="X")


# ═══════════════════════════════════════════════════════════════════════════════
class VIEW3D_PT_ASP_VoskModel(Panel):
    """Panneau de gestion du modèle Vosk — style iocgpoly."""
    bl_label      = "Modèle Vosk"
    bl_space_type = "VIEW_3D"
    bl_region_type= "UI"
    bl_category   = "Lip Sync"
    bl_parent_id  = "VIEW3D_PT_AudioShapePRO"
    bl_options    = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls,context): return context.scene.audioshape_props.is_initialized

    def draw(self, context):
        layout = self.layout
        props  = context.scene.audioshape_props

        # Statut Vosk + compat
        for txt,icon in _vosk_status_lines():
            r = layout.row(); r.scale_y=0.82
            r.label(text=txt, icon=icon)

        # Bouton ré-installation si Vosk absent
        if not vosk_helper.is_vosk_available():
            row = layout.row()
            row.scale_y = 1.4
            row.operator(
                "audioshape.reinstall_vosk",
                text="⚙ Installer Vosk maintenant",
                icon="IMPORT",
            )
            layout.label(
                text="Si le bouton échoue, redémarrez Blender.",
                icon="INFO",
            )

        layout.separator(factor=0.3)

        # Sélecteur de langue (EnumProperty dynamique depuis JSON)
        b = layout.box()
        b.label(text="Langue du modèle", icon="WORLD")

        list_ok = vosk_helper._lang_list_file().is_file()
        if not list_ok:
            c = b.column(align=True); c.scale_y=0.82
            c.label(text="Liste non chargée — cliquez Actualiser", icon="INFO")
            if not bpy.app.online_access:
                c.label(text="Activez Online Access (Préf. > Système > Réseau)", icon="ERROR")

        r = b.row(align=True)
        r.enabled = list_ok
        r.prop(props,"vosk_language",text="")

        r2 = b.row(align=True)
        r2.operator("audioshape.fetch_vosk_list",text="Actualiser liste",icon="FILE_REFRESH")
        r2.enabled = bpy.app.online_access

        # Barre de progression si téléchargement en cours
        if vosk_helper.is_downloading():
            prog = vosk_helper.get_download_progress()
            status = vosk_helper.get_download_status()
            pb = b.row(align=True)
            if hasattr(pb,"progress"):
                pb.progress(factor=prog, type="BAR", text=status)
            else:
                pb.prop_tabs_enum(None,None)  # fallback visuel
                pb.label(text=f"{status} ({int(prog*100)}%)")

        # Bouton installer
        lang = getattr(props,"vosk_language","none")
        if lang and lang != "none":
            info = vosk_helper.get_model_info(lang)
            if info:
                sz = info.get("size_text","")
                if sz:
                    r3 = b.row(); r3.scale_y=0.78
                    r3.label(text=f"Taille : {sz}", icon="DISK_DRIVE")
            model_ok = vosk_helper.is_model_ready(lang)
            r4 = b.row(align=True); r4.scale_y=1.3
            if model_ok:
                r4.label(text="✓ Modèle installé", icon="CHECKMARK")
            elif vosk_helper.is_downloading():
                r4.enabled=False
                r4.label(text="⬇ Téléchargement…", icon="TIME")
            else:
                op = r4.operator("audioshape.install_vosk_model",
                                 text=f"⬇  Installer modèle ({lang})", icon="IMPORT")
                r4.enabled = bpy.app.online_access


# ═══════════════════════════════════════════════════════════════════════════════
class VIEW3D_PT_ASP_Mapping(Panel):
    bl_label="Mapping & Phonèmes"; bl_space_type="VIEW_3D"; bl_region_type="UI"
    bl_category="Lip Sync"; bl_parent_id="VIEW3D_PT_AudioShapePRO"; bl_options={"DEFAULT_CLOSED"}
    @classmethod
    def poll(cls,c): return c.scene.audioshape_props.is_initialized
    def draw(self,context):
        layout=self.layout; props=context.scene.audioshape_props; obj=context.object
        if not _has_sk(obj): layout.label(text="Pas de shape keys",icon="ERROR"); return

        # ── Sélecteur de preset phonèmes (v13) ───────────────────────────────
        box = layout.box()
        box.label(text="Mode phonèmes :", icon="DRIVER_TRANSFORM")
        r = box.row(align=True)
        r.scale_y = 1.3
        for pid, plbl, _ in [
            ("SIMPLE",   "🔵 Simple (3)",   ""),
            ("ADVANCED", "🟡 Avancé (4)",   ""),
            ("EXPERT",   "🔴 Expert (6)",   ""),
        ]:
            r.operator("audioshape.apply_phoneme_preset", text=plbl,
                       depress=(props.phoneme_preset == pid)).preset_id = pid
        # Description du mode actuel
        _mode_desc = {
            "SIMPLE":   "A · O/U/E · I",
            "ADVANCED": "A · O/U/E · I · M/P/B",
            "EXPERT":   "A · O/U/E · I · M/P/B · F/V · R/L",
        }
        box.label(text=_mode_desc.get(props.phoneme_preset, ""), icon="INFO")
        layout.separator(factor=0.3)

        # ── Mapping shape keys (selon le preset actif) ────────────────────
        _active_phs = {
            "SIMPLE":   [("A","A"),("OUE","O/U/E"),("I","I")],
            "ADVANCED": [("A","A"),("OUE","O/U/E"),("I","I"),("MBP","M/B/P")],
            "EXPERT":   [("A","A"),("OUE","O/U/E"),("I","I"),("MBP","M/B/P"),("FV","F/V"),("RL","R/L")],
        }
        for ph, lbl in _active_phs.get(props.phoneme_preset, _active_phs["ADVANCED"]):
            r = layout.row(align=True); r.label(text=lbl, icon="SHAPEKEY_DATA")
            r.prop_search(props, f"shape_key_{ph}", obj.data.shape_keys, "key_blocks", text="")
        layout.separator(factor=0.3)
        r=layout.row(align=True); r.label(text="Silence",icon="SHAPEKEY_DATA")
        r.prop_search(props,"shape_key_SIL",obj.data.shape_keys,"key_blocks",text="")


class VIEW3D_PT_ASP_Filters(Panel):
    bl_label="Filtre Audio"; bl_space_type="VIEW_3D"; bl_region_type="UI"
    bl_category="Lip Sync"; bl_parent_id="VIEW3D_PT_AudioShapePRO"; bl_options={"DEFAULT_CLOSED"}
    @classmethod
    def poll(cls,c): return c.scene.audioshape_props.is_initialized
    def draw(self,context):
        layout=self.layout; props=context.scene.audioshape_props
        c=layout.column(align=True); c.prop(props,"use_highpass",toggle=True)
        if props.use_highpass: c.prop(props,"highpass_freq",text="HP (Hz)")
        c=layout.column(align=True); c.prop(props,"use_lowpass",toggle=True)
        if props.use_lowpass: c.prop(props,"lowpass_freq",text="LP (Hz)")
        layout.separator(factor=0.3)
        layout.operator("audioshape.process_audio",icon="FILTER",text="Appliquer")


class VIEW3D_PT_ASP_Detection(Panel):
    bl_label="Détection"; bl_space_type="VIEW_3D"; bl_region_type="UI"
    bl_category="Lip Sync"; bl_parent_id="VIEW3D_PT_AudioShapePRO"; bl_options={"DEFAULT_CLOSED"}
    @classmethod
    def poll(cls,c):
        p=c.scene.audioshape_props; return p.is_initialized and bool(p.last_bake_summary)
    def draw(self,context):
        layout=self.layout; props=context.scene.audioshape_props
        if not props.last_bake_summary: layout.label(text="Aucun bake",icon="INFO"); return
        c=layout.column(align=True); c.scale_y=0.85
        c.label(text=props.last_bake_summary,icon="INFO")
        for w in list(props.detected_words)[:8]:
            r=c.row(align=True); r.scale_y=0.8
            r.label(text=f"« {w.text} »"); r.label(text=f"f.{w.frame_start}–{w.frame_end}")


class VIEW3D_PT_ASP_EmotionCoart(Panel):
    bl_label="Émotion & Coarticulation"; bl_space_type="VIEW_3D"; bl_region_type="UI"
    bl_category="Lip Sync"; bl_parent_id="VIEW3D_PT_AudioShapePRO"; bl_options={"DEFAULT_CLOSED"}
    @classmethod
    def poll(cls,c):
        p=c.scene.audioshape_props; return p.is_initialized and p.bake_strategy=="SEQUENCE"
    def draw(self,context):
        layout=self.layout; props=context.scene.audioshape_props

        ib=layout.box(); ib.scale_y=0.75
        ib.label(text="Appliqué APRÈS le bake — modifie les handles kp du Graph Editor",icon="INFO")

        # Presets émotionnels — boutons larges avec emoji
        b=layout.box(); b.label(text="Preset émotionnel",icon="FUND")
        r1=b.row(align=True); r1.scale_y=1.6
        for pid,lbl in [("NEUTRAL","😐 Neutre"),("HAPPY","😄 Joyeux"),("SAD","😢 Triste"),("ANGRY","😠 Colère")]:
            op=r1.operator("audioshape.apply_emotion_preset",text=lbl,depress=(props.emotion_preset==pid))
            op.preset_id=pid
        r2=b.row(align=True); r2.scale_y=1.6
        for pid,lbl in [("SURPRISED","😲 Surpris"),("WHISPER","🤫 Chuchoté"),("SINGING","🎵 Chanté")]:
            op=r2.operator("audioshape.apply_emotion_preset",text=lbl,depress=(props.emotion_preset==pid))
            op.preset_id=pid
        ir=b.row(align=True); ir.scale_y=1.2; ir.enabled=(props.emotion_preset!="NEUTRAL")
        ir.prop(props,"emotion_intensity",slider=True,text="Intensité")

        DESCS={"NEUTRAL":"Aucun modificateur","HAPPY":"Floor [I] — sourire élargi · handles anticipés",
               "SAD":"Floor [MBP] — mâchoire basse · décélération lente","ANGRY":"Floor [FV] — grande aperture · burst fort",
               "SURPRISED":"Floor [O] — voyelles longues","WHISPER":"Scaling ×scale global toutes shapes",
               "SINGING":"Floor [U] — voyelles tenues · transitions ultra-lisses"}
        d=DESCS.get(props.emotion_preset,"")
        if d and props.emotion_preset!="NEUTRAL":
            di=b.column(align=True); di.scale_y=0.78; di.label(text=d,icon="INFO")

        layout.separator(factor=0.3)
        # Coarticulation
        b=layout.box(); b.label(text="Coarticulation phonétique",icon="FORCE_VORTEX")
        c=b.column(align=True)
        c.prop(props,"coarticulation_strength",slider=True)
        c.separator(factor=0.3)
        c.prop(props,"organic_tilt")
        if props.organic_tilt: c.prop(props,"tilt_amount",slider=True)
        hi=b.column(align=True); hi.scale_y=0.75
        hi.label(text="Handles Bézier : anticipation (gauche) / burst (droit) / décel.",icon="IPO_BEZIER")


class VIEW3D_PT_ASP_Advanced(Panel):
    bl_label="Réglages avancés"; bl_space_type="VIEW_3D"; bl_region_type="UI"
    bl_category="Lip Sync"; bl_parent_id="VIEW3D_PT_AudioShapePRO"; bl_options={"DEFAULT_CLOSED"}
    @classmethod
    def poll(cls,c): return c.scene.audioshape_props.is_initialized
    def draw(self,context):
        layout=self.layout; props=context.scene.audioshape_props
        c=layout.column(align=True)
        c.prop(props,"bake_force_context"); c.prop(props,"fuzzy_shape_matching")
        c.prop(props,"auto_clear_keyframes")
        layout.separator(factor=0.3)
        # Compat info
        b=layout.box(); b.label(text="Compatibilité",icon="BLENDER")
        from .compat import feature_summary
        col=b.column(align=True); col.scale_y=0.8
        for line in feature_summary(): col.label(text=line)


PANEL_CLASSES = (
    VIEW3D_PT_AudioShapePRO,
    VIEW3D_PT_ASP_VoskModel,
    VIEW3D_PT_ASP_Mapping,
    VIEW3D_PT_ASP_Filters,
    VIEW3D_PT_ASP_Detection,
    VIEW3D_PT_ASP_EmotionCoart,
    VIEW3D_PT_ASP_Advanced,
)
