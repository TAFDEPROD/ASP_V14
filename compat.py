# SPDX-License-Identifier: MIT
"""
compat.py v11 — CORRECTIF Blender 5.x : Action.fcurves → layers/strips/channelbag
Compatible Blender 4.2 → 5.x
"""
from __future__ import annotations
import bpy
import os
import pathlib

BL_VER = bpy.app.version
BL_GTE = lambda *v: BL_VER >= tuple(v)   # noqa: E731

BL_GTE_30 = BL_GTE(3, 0)
BL_GTE_32 = BL_GTE(3, 2)
BL_GTE_40 = BL_GTE(4, 0)
BL_GTE_42 = BL_GTE(4, 2)   # Extension API — minimum requis par AudioShapePRO
BL_GTE_43 = BL_GTE(4, 3)
BL_GTE_44 = BL_GTE(4, 4)
BL_GTE_45 = BL_GTE(4, 5)
BL_GTE_50 = BL_GTE(5, 0)   # Blender 5.x — nouvelle API Action (layers/strips)

BL_MAJOR = BL_VER[0]
BL_MINOR = BL_VER[1]

# CORRECTIF v10 : Vosk fonctionne dès 4.2 avec extraction manuelle du wheel
VOSK_WHEEL_SUPPORTED = BL_GTE_42

# ── Abstraction Action.fcurves — Blender 4.x vs 5.x ─────────────────────────
# Blender 5.0+ a remplacé action.fcurves par un système layers/strips/channelbag.
# Ces helpers permettent au code existant de fonctionner sans modification.

def _get_channelbag(action):
    """Retourne le channelbag actif (Blender 5.x) ou None."""
    try:
        if not action.layers:
            action.layers.new(name="Layer")
        layer = action.layers[0]
        if not layer.strips:
            layer.strips.new(type="KEYFRAME")
        strip = layer.strips[0]
        if not strip.channelbags:
            return None
        return strip.channelbags[0]
    except Exception:
        return None


def _get_or_create_channelbag(action):
    """Retourne ou crée le channelbag actif (Blender 5.x)."""
    try:
        if not action.layers:
            action.layers.new(name="Layer")
        layer = action.layers[0]
        if not layer.strips:
            layer.strips.new(type="KEYFRAME")
        strip = layer.strips[0]
        if not action.slots:
            action.slots.new(id_type="KEY")
        slot = action.slots[0]
        for cb in strip.channelbags:
            if cb.slot_handle == slot.handle:
                return cb
        return strip.channelbags.new(slot)
    except Exception as e:
        raise RuntimeError(f"[ASP] Impossible de créer channelbag: {e}") from e


class _FcurvesProxy51:
    """
    Proxy transparent qui émule l'API action.fcurves (Blender 4.x)
    en utilisant les channelbags de Blender 5.x.
    Utilisé uniquement quand BL_GTE_50 est True.
    """
    def __init__(self, action):
        self._action = action

    def _channelbag(self, create=False):
        if create:
            return _get_or_create_channelbag(self._action)
        return _get_channelbag(self._action)

    def __iter__(self):
        cb = self._channelbag(create=False)
        if cb is None:
            return iter([])
        return iter(list(cb.fcurves))

    def find(self, data_path, index=0):
        cb = self._channelbag(create=False)
        if cb is None:
            return None
        return cb.fcurves.find(data_path, index=index)

    def new(self, data_path, index=0, action_group=""):
        cb = self._channelbag(create=True)
        try:
            return cb.fcurves.new(data_path, index=index)
        except Exception as e:
            raise RuntimeError(
                f"[ASP] fcurves.new({data_path!r}) échoué: {e}"
            ) from e

    def remove(self, fc):
        cb = self._channelbag(create=False)
        if cb is not None:
            try:
                cb.fcurves.remove(fc)
            except Exception:
                pass


def action_fcurves(action):
    """
    Retourne un objet compatible action.fcurves pour toutes versions Blender.
      - Blender 4.x : retourne action.fcurves directement
      - Blender 5.x : retourne un proxy _FcurvesProxy51 transparent
    Usage : remplacer 'action.fcurves' par 'action_fcurves(action)' partout.
    """
    if BL_GTE_50:
        return _FcurvesProxy51(action)
    return action.fcurves


def user_cache_dir(subdir: str = "vosk_cache") -> pathlib.Path:
    pkg = __package__ or "AudioShapePRO"
    try:
        if BL_GTE_42:
            p = pathlib.Path(
                bpy.utils.extension_path_user(pkg, path=subdir, create=True)
            )
        else:
            base = bpy.utils.user_resource("DATAFILES")
            p = pathlib.Path(base) / pkg / subdir
            p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        p = pathlib.Path(os.path.expanduser("~")) / f".{pkg}" / subdir
        p.mkdir(parents=True, exist_ok=True)
        return p

def temp_override(context, **kwargs):
    if BL_GTE_32:
        return context.temp_override(**kwargs)
    import contextlib
    @contextlib.contextmanager
    def _legacy_override():
        old = {}
        for k, v in kwargs.items():
            old[k] = getattr(context, k, None)
            try: setattr(context, k, v)
            except Exception: pass
        try: yield context
        finally:
            for k, v in old.items():
                try: setattr(context, k, v)
                except Exception: pass
    return _legacy_override()

def layout_property_split(layout, enabled: bool = True) -> None:
    try:
        layout.use_property_split    = enabled
        layout.use_property_decorate = False
    except Exception:
        pass

def safe_enum_items(items):
    result = []
    for i, item in enumerate(items):
        if len(item) == 3:   result.append((item[0], item[1], item[2], "NONE", i))
        elif len(item) == 4: result.append((item[0], item[1], item[2], item[3], i))
        else:                result.append(item)
    return result

def version_string() -> str:
    return f"Blender {BL_VER[0]}.{BL_VER[1]}.{BL_VER[2]}"

def feature_summary() -> list:
    lines = [f"Version : {version_string()}"]
    lines.append(f"Extension API : {'✓' if BL_GTE_42 else '✗ (< 4.2)'}")
    lines.append(f"Vosk supporté : {'✓' if VOSK_WHEEL_SUPPORTED else '⚠ < 4.2'}")
    lines.append(f"API Action 5.x : {'✓ (layers/channelbag)' if BL_GTE_50 else '✗ (fcurves directs)'}")
    return lines
