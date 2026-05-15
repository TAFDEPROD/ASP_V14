# SPDX-License-Identifier: MIT
# Copyright (c) 2025 TAF DE PROD
"""
security.py — Module de sécurité AudioShapePRO.

Responsabilités :
  - Validation des fichiers audio avant import (taille, format, magic bytes)
  - Gestion du budget mémoire de l'addon
  - Nettoyage des fichiers temporaires
  - Protection contre les surcharges mémoire
"""
from __future__ import annotations

import os
import sys
import bpy
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constantes de sécurité
# ---------------------------------------------------------------------------

MAX_FILE_SIZE_MB: float = 200.0
MAX_FILE_SIZE_BYTES: int = int(MAX_FILE_SIZE_MB * 1024 * 1024)
MIN_FILE_SIZE_BYTES: int = 128             # < 128 octets = corrompu
MAX_ADDON_MEMORY_MB: float = 400.0         # Budget total de l'addon
MAX_ADDON_MEMORY_BYTES: int = int(MAX_ADDON_MEMORY_MB * 1024 * 1024)
MAX_TEMP_FILES: int = 15                   # Nombre max de fichiers temp

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({
    ".wav", ".wave",
    ".mp3",
    ".ogg", ".oga",
    ".flac",
    ".aiff", ".aif",
    ".m4a", ".aac",
})

# Signatures binaires (magic bytes) des formats audio
_MAGIC_SIGNATURES: list[tuple[bytes, int, str]] = [
    # (signature, offset, nom_format)
    (b"RIFF",     0, "WAV"),
    (b"OggS",     0, "OGG/Vorbis"),
    (b"fLaC",     0, "FLAC"),
    (b"FORM",     0, "AIFF"),
    (b"ID3",      0, "MP3 (ID3)"),
    (b"\xff\xfb", 0, "MP3"),
    (b"\xff\xf3", 0, "MP3"),
    (b"\xff\xf2", 0, "MP3"),
    (b"\xff\xe3", 0, "MP3"),
    (b"\x00\x00\x00", 4, "M4A/AAC"),  # ftyp box
]


# ---------------------------------------------------------------------------
# Rapport de sécurité
# ---------------------------------------------------------------------------

@dataclass
class SecurityReport:
    """Résultat d'une validation de fichier audio."""
    valid: bool = False
    file_size_mb: float = 0.0
    detected_format: str = "Inconnu"
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return self.valid and not self.errors

    @property
    def status_icon(self) -> str:
        """Icône Blender représentant le statut."""
        if not self.valid:
            return "ERROR"
        if self.warnings:
            return "INFO"
        return "CHECKMARK"

    def summary(self) -> str:
        if self.errors:
            return f"❌ {self.errors[0]}"
        if self.warnings:
            return f"⚠️  {self.warnings[0]}"
        return f"✓  {self.file_size_mb:.1f} Mo — {self.detected_format}"


# ---------------------------------------------------------------------------
# Budget mémoire
# ---------------------------------------------------------------------------

@dataclass
class MemoryBudget:
    """Suivi du budget mémoire alloué par l'addon."""
    _allocated: int = 0
    _peak: int = 0
    _temp_files: list[str] = field(default_factory=list)
    _loaded_sounds: int = 0

    @property
    def allocated_mb(self) -> float:
        return self._allocated / (1024 * 1024)

    @property
    def peak_mb(self) -> float:
        return self._peak / (1024 * 1024)

    @property
    def remaining_mb(self) -> float:
        return (MAX_ADDON_MEMORY_BYTES - self._allocated) / (1024 * 1024)

    @property
    def usage_pct(self) -> float:
        return (self._allocated / MAX_ADDON_MEMORY_BYTES) * 100.0

    @property
    def is_critical(self) -> bool:
        """True si > 85 % du budget utilisé."""
        return self._allocated >= int(MAX_ADDON_MEMORY_BYTES * 0.85)

    @property
    def is_over_budget(self) -> bool:
        return self._allocated >= MAX_ADDON_MEMORY_BYTES

    @property
    def temp_file_count(self) -> int:
        return len(self._temp_files)

    def try_allocate(self, size_bytes: int) -> bool:
        """Tente d'allouer size_bytes. Retourne False si impossible."""
        if self._allocated + size_bytes > MAX_ADDON_MEMORY_BYTES:
            return False
        self._allocated += size_bytes
        self._peak = max(self._peak, self._allocated)
        return True

    def free(self, size_bytes: int) -> None:
        self._allocated = max(0, self._allocated - size_bytes)

    def register_temp(self, path: str, size_bytes: int = 0) -> None:
        if path not in self._temp_files:
            self._temp_files.append(path)
            if size_bytes > 0:
                self.try_allocate(size_bytes)

    def cleanup_all_temps(self) -> tuple[int, list[str]]:
        """
        Supprime tous les fichiers temporaires enregistrés.
        Retourne (nombre supprimés, [erreurs]).
        """
        removed = 0
        errors: list[str] = []
        remaining: list[str] = []

        for path in self._temp_files:
            try:
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    os.remove(path)
                    self.free(size)
                    removed += 1
                else:
                    removed += 1  # Déjà absent → OK
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                remaining.append(path)

        self._temp_files = remaining
        return removed, errors

    def cleanup_oldest_temps(self, keep: int = 5) -> int:
        """Supprime les fichiers temp excédentaires (garde les plus récents)."""
        if len(self._temp_files) <= keep:
            return 0
        to_delete = self._temp_files[: len(self._temp_files) - keep]
        removed = 0
        for path in to_delete:
            try:
                if os.path.exists(path):
                    size = os.path.getsize(path)
                    os.remove(path)
                    self.free(size)
                self._temp_files.remove(path)
                removed += 1
            except (OSError, ValueError):
                pass
        return removed

    def status_color(self) -> str:
        """Couleur indicative pour l'UI (info textuelle)."""
        if self.is_over_budget:
            return "ROUGE"
        if self.is_critical:
            return "ORANGE"
        return "VERT"


# Singleton global
_budget = MemoryBudget()


def get_budget() -> MemoryBudget:
    """Retourne le singleton du budget mémoire."""
    return _budget


def reset_budget() -> None:
    """Réinitialise le budget (appelé à l'enregistrement de l'addon)."""
    global _budget
    _budget = MemoryBudget()


# ---------------------------------------------------------------------------
# Validation des fichiers audio
# ---------------------------------------------------------------------------

def _detect_format(filepath: str) -> str:
    """Détecte le format audio par lecture des magic bytes."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
        for magic, offset, name in _MAGIC_SIGNATURES:
            end = offset + len(magic)
            if len(header) >= end and header[offset:end] == magic:
                return name
    except OSError:
        pass
    return "Inconnu"


def validate_audio_file(filepath: str) -> SecurityReport:
    """
    Valide un fichier audio pour AudioShapePRO.

    Vérifie :
      1. Existence et accessibilité
      2. Taille (min/max)
      3. Extension autorisée
      4. Signature binaire (magic bytes)
      5. Budget mémoire disponible

    Returns:
        SecurityReport avec valid=True si toutes les vérifications passent.
    """
    report = SecurityReport()

    # 1. Chemin vide
    if not filepath or not filepath.strip():
        report.errors.append("Aucun fichier sélectionné")
        return report

    # 2. Existence
    try:
        filepath = bpy.path.abspath(filepath)
    except Exception:
        filepath = os.path.abspath(filepath)

    # 3. Lisibilité
    if not os.access(filepath, os.R_OK):
        report.errors.append("Permission refusée — fichier non lisible")
        return report

    # 4. Taille
    try:
        size = os.path.getsize(filepath)
    except OSError as exc:
        report.errors.append(f"Impossible de lire la taille : {exc}")
        return report

    report.file_size_mb = size / (1024 * 1024)

    if size < MIN_FILE_SIZE_BYTES:
        report.errors.append(
            f"Fichier trop petit ({size} octets) — probablement corrompu"
        )
        return report

    if size > MAX_FILE_SIZE_BYTES:
        report.errors.append(
            f"Fichier trop volumineux : {report.file_size_mb:.1f} Mo "
            f"(limite : {MAX_FILE_SIZE_MB:.0f} Mo)"
        )
        return report

    if report.file_size_mb > 80.0:
        report.warnings.append(
            f"Fichier volumineux ({report.file_size_mb:.0f} Mo) — "
            "le traitement sera lent, envisagez un découpage"
        )
    elif report.file_size_mb > 30.0:
        report.warnings.append(
            f"Fichier assez grand ({report.file_size_mb:.0f} Mo) — "
            "temps de traitement accru"
        )

    # 5. Extension
    ext = os.path.splitext(filepath)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        report.errors.append(
            f"Extension « {ext} » non supportée. "
            f"Formats acceptés : {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )
        return report

    # 6. Magic bytes
    report.detected_format = _detect_format(filepath)
    if report.detected_format == "Inconnu":
        report.warnings.append(
            "Signature binaire non reconnue — Blender tentera quand même la lecture"
        )

    # 7. Budget mémoire
    if _budget.is_over_budget:
        report.errors.append(
            f"Budget mémoire addon dépassé ({_budget.allocated_mb:.0f} Mo) — "
            "libérez de la mémoire via « Nettoyer la mémoire »"
        )
        return report

    if _budget.is_critical:
        report.warnings.append(
            f"Budget mémoire presque plein ({_budget.usage_pct:.0f} %) — "
            "pensez à nettoyer après le bake"
        )

    # 8. Limit de fichiers temp
    if _budget.temp_file_count >= MAX_TEMP_FILES:
        report.warnings.append(
            f"{_budget.temp_file_count} fichiers temporaires en attente — "
            "nettoyage recommandé"
        )

    report.valid = True
    return report


# ---------------------------------------------------------------------------
# Informations système
# ---------------------------------------------------------------------------

def get_system_memory_mb() -> float:
    """Estimation de la RAM utilisée par le processus Python (Mo)."""
    try:
        import resource  # Unix seulement
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # Linux : ru_maxrss en Ko / macOS : en octets
        if sys.platform == "darwin":
            return usage.ru_maxrss / (1024 * 1024)
        return usage.ru_maxrss / 1024
    except (ImportError, AttributeError):
        return 0.0


def get_security_summary() -> dict:
    """Résumé complet pour l'affichage dans le panneau Security."""
    return {
        "budget_allocated_mb": _budget.allocated_mb,
        "budget_peak_mb": _budget.peak_mb,
        "budget_remaining_mb": _budget.remaining_mb,
        "budget_pct": _budget.usage_pct,
        "budget_max_mb": MAX_ADDON_MEMORY_MB,
        "is_critical": _budget.is_critical,
        "is_over_budget": _budget.is_over_budget,
        "temp_files": _budget.temp_file_count,
        "max_temp_files": MAX_TEMP_FILES,
        "system_memory_mb": get_system_memory_mb(),
        "max_file_mb": MAX_FILE_SIZE_MB,
    }
