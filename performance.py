# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
performance.py — Module de performance AudioShapePRO.

Responsabilités :
  - Minuterie de précision pour chaque opération de bake
  - Historique des bakes (phonème, durée, plage Hz, keyframes)
  - Collecte des statistiques Blender (FPS, objets, durée de session)
  - Export du rapport de performance
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import bpy
    _HAS_BPY = True
except ImportError:
    _HAS_BPY = False


# ---------------------------------------------------------------------------
# Structure d'un enregistrement de bake
# ---------------------------------------------------------------------------

@dataclass
class BakeRecord:
    """Résultat d'un bake de phonème."""
    phoneme: str
    strategy: str           # "DOMINANT", "WEIGHTED", etc.
    gender: str             # "MALE" / "FEMALE"
    freq_low_hz: int
    freq_high_hz: int
    modifier: float
    duration_s: float       # Durée du bake (secondes)
    keyframes_count: int
    timestamp: float = field(default_factory=time.time)

    @property
    def duration_ms(self) -> float:
        return self.duration_s * 1000.0

    @property
    def freq_range_str(self) -> str:
        lo = f"{self.freq_low_hz / 1000:.1f} kHz" if self.freq_low_hz >= 1000 else f"{self.freq_low_hz} Hz"
        hi = f"{self.freq_high_hz / 1000:.1f} kHz" if self.freq_high_hz >= 1000 else f"{self.freq_high_hz} Hz"
        return f"{lo} – {hi}"

    def to_dict(self) -> dict:
        return {
            "phoneme": self.phoneme,
            "strategy": self.strategy,
            "gender": self.gender,
            "freq_range": self.freq_range_str,
            "modifier": self.modifier,
            "duration_ms": round(self.duration_ms, 1),
            "keyframes": self.keyframes_count,
        }


# ---------------------------------------------------------------------------
# Tracker de performance (singleton)
# ---------------------------------------------------------------------------

class PerformanceTracker:
    """Suit les performances de tous les bakes de la session."""

    _instance: Optional["PerformanceTracker"] = None
    _MAX_HISTORY = 25

    def __init__(self) -> None:
        self._timers: dict[str, float] = {}
        self._history: list[BakeRecord] = []
        self._session_start: float = time.time()
        self._total_bakes: int = 0
        self._total_time_s: float = 0.0
        self._fastest_bake_ms: float = float("inf")
        self._slowest_bake_ms: float = 0.0

    # ------------------------------------------------------------------
    # Accès au singleton
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> "PerformanceTracker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Réinitialise le tracker (appelé à l'activation de l'addon)."""
        cls._instance = cls()

    # ------------------------------------------------------------------
    # Minuteries
    # ------------------------------------------------------------------

    def start(self, label: str = "default") -> None:
        """Démarre un chrono pour l'opération label."""
        self._timers[label] = time.perf_counter()

    def stop(self, label: str = "default") -> float:
        """Arrête le chrono et retourne le temps écoulé (secondes)."""
        if label not in self._timers:
            return 0.0
        elapsed = time.perf_counter() - self._timers.pop(label)
        return elapsed

    def elapsed_ms(self, label: str = "default") -> float:
        """Temps écoulé depuis start() sans arrêter le chrono (ms)."""
        if label not in self._timers:
            return 0.0
        return (time.perf_counter() - self._timers[label]) * 1000.0

    # ------------------------------------------------------------------
    # Enregistrement des bakes
    # ------------------------------------------------------------------

    def record_bake(
        self,
        phoneme: str,
        strategy: str,
        gender: str,
        freq_low: int,
        freq_high: int,
        modifier: float,
        duration_s: float,
        keyframes: int,
    ) -> BakeRecord:
        """Enregistre un bake dans l'historique."""
        rec = BakeRecord(
            phoneme=phoneme,
            strategy=strategy,
            gender=gender,
            freq_low_hz=freq_low,
            freq_high_hz=freq_high,
            modifier=modifier,
            duration_s=duration_s,
            keyframes_count=keyframes,
        )
        self._history.append(rec)
        if len(self._history) > self._MAX_HISTORY:
            self._history.pop(0)

        self._total_bakes += 1
        self._total_time_s += duration_s
        ms = duration_s * 1000.0
        self._fastest_bake_ms = min(self._fastest_bake_ms, ms)
        self._slowest_bake_ms = max(self._slowest_bake_ms, ms)
        return rec

    # ------------------------------------------------------------------
    # Métriques agrégées
    # ------------------------------------------------------------------

    @property
    def total_bakes(self) -> int:
        return self._total_bakes

    @property
    def avg_bake_ms(self) -> float:
        if self._total_bakes == 0:
            return 0.0
        return (self._total_time_s / self._total_bakes) * 1000.0

    @property
    def fastest_ms(self) -> float:
        return self._fastest_bake_ms if self._total_bakes > 0 else 0.0

    @property
    def slowest_ms(self) -> float:
        return self._slowest_bake_ms

    @property
    def session_minutes(self) -> float:
        return (time.time() - self._session_start) / 60.0

    @property
    def last_bake(self) -> Optional[BakeRecord]:
        return self._history[-1] if self._history else None

    def recent_history(self, n: int = 5) -> list[BakeRecord]:
        """Retourne les n derniers bakes (du plus récent au plus ancien)."""
        return list(reversed(self._history[-n:]))

    # ------------------------------------------------------------------
    # Statistiques Blender
    # ------------------------------------------------------------------

    def get_blender_stats(self) -> dict:
        """Collecte les statistiques Blender actives."""
        stats: dict = {
            "version": "N/A",
            "fps": 0,
            "frame_range": "N/A",
            "scene_objects": 0,
            "active_object": "Aucun",
            "shape_keys_count": 0,
        }

        if not _HAS_BPY:
            return stats

        try:
            stats["version"] = ".".join(str(v) for v in bpy.app.version)
        except Exception:  # noqa: BLE001
            pass

        try:
            scene = bpy.context.scene
            if scene:
                stats["fps"] = scene.render.fps
                stats["frame_range"] = f"{scene.frame_start} – {scene.frame_end}"
                stats["scene_objects"] = len(scene.objects)
        except Exception:  # noqa: BLE001
            pass

        try:
            obj = bpy.context.object
            if obj:
                stats["active_object"] = obj.name
                if (
                    obj.data
                    and hasattr(obj.data, "shape_keys")
                    and obj.data.shape_keys
                ):
                    stats["shape_keys_count"] = len(obj.data.shape_keys.key_blocks)
        except Exception:  # noqa: BLE001
            pass

        return stats

    def get_full_report(self) -> dict:
        """Rapport complet pour l'affichage ou l'export."""
        return {
            "session_minutes": round(self.session_minutes, 1),
            "total_bakes": self.total_bakes,
            "avg_bake_ms": round(self.avg_bake_ms, 1),
            "fastest_ms": round(self.fastest_ms, 1),
            "slowest_ms": round(self.slowest_ms, 1),
            "recent_bakes": [r.to_dict() for r in self.recent_history(5)],
            "blender": self.get_blender_stats(),
        }

    def format_report_lines(self) -> list[str]:
        """Lignes formatées pour l'affichage dans le panneau Blender."""
        lines = []
        blender = self.get_blender_stats()

        lines.append(f"Blender {blender['version']}  —  FPS : {blender['fps']}")
        lines.append(f"Timeline : {blender['frame_range']}")
        lines.append(f"Objets scène : {blender['scene_objects']}")
        if blender["active_object"] != "Aucun":
            lines.append(
                f"Objet actif : {blender['active_object']}  "
                f"({blender['shape_keys_count']} shape keys)"
            )
        lines.append("─" * 28)
        lines.append(f"Session : {self.session_minutes:.1f} min")
        lines.append(f"Bakes total : {self.total_bakes}")
        if self.total_bakes > 0:
            lines.append(f"Durée moy. : {self.avg_bake_ms:.0f} ms")
            lines.append(f"Plus rapide : {self.fastest_ms:.0f} ms")
            lines.append(f"Plus lent   : {self.slowest_ms:.0f} ms")

        if self.last_bake:
            r = self.last_bake
            lines.append("─" * 28)
            lines.append(f"Dernier bake : [{r.phoneme}]")
            lines.append(f"  Plage : {r.freq_range_str}")
            lines.append(f"  Durée : {r.duration_ms:.0f} ms")
            lines.append(f"  Clés  : {r.keyframes_count}")
        return lines
