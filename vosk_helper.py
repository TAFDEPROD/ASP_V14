# SPDX-License-Identifier: MIT
"""
vosk_helper.py v12 — CORRECTIFS :
  - Installation robuste Blender 4.2–5.x (extraction ZIP directe, sans pip)
  - Vosk activé dès Blender 4.2 (était 4.5+) — fonctionne sur 4.4
  - Ordre tentatives : pip → extraction ZIP → site-packages alternatifs
"""
from __future__ import annotations
import json, os, pathlib, platform, subprocess, sys, wave, zipfile, re
from typing import Optional
import bpy
from . import compat

MODEL_LIST_URL = "https://alphacephei.com/vosk/models/model-list.json"
_EXCLUDED_LANGS = {"kz", "ua"}

class _DL:
    proc = None; log_file = None
    lang = ""; progress = 0.0; status = ""
_dl = _DL()

def _addon_dir()  -> pathlib.Path: return pathlib.Path(__file__).parent
def _wheels_dir() -> pathlib.Path: return _addon_dir() / "wheels"
def _worker_path()-> pathlib.Path: return _addon_dir() / "workers" / "wrk_download_vosk.py"

def _cache_dir() -> pathlib.Path:
    try:
        from .compat import user_cache_dir
        return user_cache_dir("vosk_cache")
    except Exception:
        p = pathlib.Path(os.path.expanduser("~")) / ".audioshapepro_vosk"
        p.mkdir(parents=True, exist_ok=True)
        return p

def _lang_list_file() -> pathlib.Path:
    return _cache_dir() / "languages_list.json"

# ── Vosk disponible ──────────────────────────────────────────────────────────
def is_vosk_available() -> bool:
    try:
        import vosk  # noqa: F401
        return True
    except Exception:
        return False

def get_vosk_version() -> str:
    try:
        import vosk
        return getattr(vosk, "__version__", "?")
    except Exception:
        return ""

def _pick_wheel() -> Optional[pathlib.Path]:
    d = _wheels_dir()
    if not d.exists():
        return None
    s = platform.system().lower()
    m = {
        "windows": "win_amd64",
        "darwin":  "macosx_10_6_universal2",
    }.get(s, "manylinux_2_12_x86_64.manylinux2010_x86_64")
    for f in d.glob("vosk-*.whl"):
        if m in f.name:
            return f
    # Fallback : prend n'importe quelle wheel vosk
    for f in d.glob("vosk-*.whl"):
        return f
    return None

def _get_site_packages_candidates() -> list[pathlib.Path]:
    """
    Retourne tous les dossiers site-packages accessibles en écriture.
    Compatible Blender 4.2+ (extension system) et versions antérieures.
    """
    candidates: list[pathlib.Path] = []

    # 1. site.getsitepackages() — le plus direct
    try:
        import site
        if hasattr(site, "getsitepackages"):
            for p in site.getsitepackages():
                candidates.append(pathlib.Path(p))
    except Exception:
        pass

    # 2. site.getusersitepackages() — dossier utilisateur
    try:
        import site
        if hasattr(site, "getusersitepackages"):
            up = site.getusersitepackages()
            if up:
                candidates.append(pathlib.Path(up))
    except Exception:
        pass

    # 3. Déduit depuis sys.executable (Blender embarque Python)
    try:
        exe = pathlib.Path(sys.executable)
        for rel in ("../lib/python3.11/site-packages",
                    "../lib/python3.12/site-packages",
                    "lib/python3.11/site-packages",
                    "lib/python3.12/site-packages",
                    "../../lib/site-packages"):
            p = (exe / rel).resolve()
            if p.is_dir():
                candidates.append(p)
    except Exception:
        pass

    # 4. sys.path — filtre les dossiers site-packages existants
    for p_str in sys.path:
        if "site-packages" in p_str:
            p = pathlib.Path(p_str)
            if p.is_dir():
                candidates.append(p)

    # Déduplique en gardant l'ordre
    seen: set[str] = set()
    result: list[pathlib.Path] = []
    for p in candidates:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result

def _extract_wheel_manually(wheel: pathlib.Path) -> bool:
    """
    Extrait le wheel Vosk (= ZIP) directement dans le premier
    site-packages accessible en écriture.
    N'utilise PAS pip — compatible sandbox Blender 4.2+.
    """
    targets = _get_site_packages_candidates()
    if not targets:
        print("[ASP] Aucun site-packages trouvé pour l'extraction manuelle")
        return False

    for target in targets:
        try:
            target.mkdir(parents=True, exist_ok=True)
            # Teste si on peut écrire ici
            test_file = target / "_asp_write_test.tmp"
            test_file.write_text("test")
            test_file.unlink()

            with zipfile.ZipFile(wheel, "r") as zf:
                extracted = 0
                for member in zf.namelist():
                    # On extrait vosk/, _vosk*, vosk.*, libvosk*, libgfortran*
                    base = member.split("/")[0]
                    if base.startswith(("vosk", "_vosk", "libvosk", "libgfortran",
                                        "libopenblas", "libquadmath")):
                        zf.extract(member, target)
                        extracted += 1
                    # .dist-info pour que importlib trouve le package
                    elif member.endswith(".dist-info/METADATA") or \
                         member.endswith(".dist-info/RECORD"):
                        zf.extract(member, target)

            if extracted > 0:
                # Force le rechargement des modules
                if "vosk" in sys.modules:
                    del sys.modules["vosk"]
                # Ajoute au sys.path si absent
                target_str = str(target)
                if target_str not in sys.path:
                    sys.path.insert(0, target_str)
                if is_vosk_available():
                    print(f"[ASP] Vosk extrait dans {target} ({extracted} fichiers)")
                    return True
                else:
                    print(f"[ASP] Extraction OK dans {target} mais import toujours impossible")
        except PermissionError:
            continue
        except Exception as e:
            print(f"[ASP] Extraction échouée dans {target}: {e}")
            continue

    return False

def auto_install_vosk_if_needed() -> bool:
    """
    Installation automatique de Vosk.

    Stratégie adaptée selon la version Blender :

    Blender 5.x (Python 3.13) :
      1. Vosk déjà disponible → rien à faire
      2. pip install vosk --upgrade (depuis PyPI — confirmé fonctionnel sur 5.1)
      3. Fallback : wheel locale + extraction manuelle

    Blender 4.x (Python 3.11/3.12) :
      1. Vosk déjà disponible → rien à faire
      2. pip install depuis wheel locale (--no-index, sans connexion requise)
      3. Extraction ZIP manuelle dans site-packages
      4. pip --target explicite

    Compatible Blender 4.2+.
    """
    if is_vosk_available():
        return True

    if not compat.BL_GTE_42:
        print(f"[ASP] Blender {compat.BL_VER[0]}.{compat.BL_VER[1]} < 4.2 : "
              "installation Vosk non garantie")

    # ── Blender 5.x : pip install depuis PyPI (py3-none fonctionne sur 3.13) ──
    if compat.BL_GTE_50:
        print("[ASP] Blender 5.x détecté — installation Vosk via pip (PyPI)…")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "vosk", "--upgrade", "--quiet"],
                capture_output=True, text=True, timeout=180,
            )
            if r.returncode == 0 and is_vosk_available():
                print("[ASP] Vosk installé via pip (PyPI) — Blender 5.x OK.")
                return True
            if r.returncode == 0:
                print(f"[ASP] pip PyPI OK mais import échoue: {r.stderr[:200]}")
            else:
                print(f"[ASP] pip PyPI retcode={r.returncode}: {r.stderr[:300]}")
        except Exception as e:
            print(f"[ASP] pip PyPI exception: {e}")

        # Fallback 5.x : wheel locale (py3-none est compatible Python 3.13)
        wheel = _pick_wheel()
        if wheel:
            print(f"[ASP] Fallback wheel locale: {wheel.name}")
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--no-deps", "--quiet", str(wheel)],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0 and is_vosk_available():
                    print("[ASP] Vosk installé via wheel locale (Blender 5.x).")
                    return True
            except Exception as e:
                print(f"[ASP] wheel locale exception: {e}")
            # Extraction manuelle comme dernier recours
            if _extract_wheel_manually(wheel):
                return True

        print("[ASP] ⚠ Installation Vosk échouée sur Blender 5.x.")
        return False

    # ── Blender 4.x : stratégie wheels locales (sans connexion requise) ───────
    wheel = _pick_wheel()
    if not wheel:
        print("[ASP] Aucune wheel Vosk trouvée dans wheels/")
        return False

    print(f"[ASP] Installation Vosk depuis {wheel.name} (Blender 4.x)…")

    # ── Tentative 1 : pip depuis wheel locale ──────────────────────────────
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install",
             "--no-deps", "--no-index", "--quiet", str(wheel)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode == 0 and is_vosk_available():
            print("[ASP] Vosk installé via pip (wheel locale).")
            return True
        if r.returncode == 0:
            print(f"[ASP] pip OK mais import échoue — stderr: {r.stderr[:200]}")
        else:
            print(f"[ASP] pip retcode={r.returncode}: {r.stderr[:200]}")
    except FileNotFoundError:
        print("[ASP] pip non disponible — passage à l'extraction manuelle")
    except Exception as e:
        print(f"[ASP] pip exception: {e}")

    # ── Tentative 2 : extraction ZIP manuelle ─────────────────────────────
    if _extract_wheel_manually(wheel):
        return True

    # ── Tentative 3 : pip avec --target explicite ─────────────────────────
    try:
        targets = _get_site_packages_candidates()
        for target in targets:
            try:
                target.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install",
                     "--no-deps", "--no-index", "--quiet",
                     "--target", str(target), str(wheel)],
                    capture_output=True, text=True, timeout=120,
                )
                if r.returncode == 0:
                    target_str = str(target)
                    if target_str not in sys.path:
                        sys.path.insert(0, target_str)
                    if is_vosk_available():
                        print(f"[ASP] Vosk installé (pip --target {target})")
                        return True
            except Exception:
                continue
    except Exception as e:
        print(f"[ASP] pip --target : {e}")

    print("[ASP] ⚠ Toutes les tentatives d'installation Vosk ont échoué.")
    return False

# ── Liste des langues ────────────────────────────────────────────────────────
def fetch_language_list() -> bool:
    try:
        import urllib.request
        req = urllib.request.Request(
            MODEL_LIST_URL, headers={"User-Agent": "AudioShapePRO/12"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            all_m = json.loads(resp.read().decode())
        filt = [
            m for m in all_m
            if m.get("type") == "small"
            and m.get("obsolete") == "false"
            and m.get("lang") not in _EXCLUDED_LANGS
        ]
        with open(_lang_list_file(), "w", encoding="utf-8") as f:
            json.dump(filt, f, ensure_ascii=False)
        print(f"[ASP] Liste modèles : {len(filt)} langues disponibles")
        return True
    except Exception as e:
        print(f"[ASP] Erreur fetch liste: {e}")
        return False

def get_language_list_cached() -> list:
    p = _lang_list_file()
    if not p.is_file():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def get_language_items_for_enum(self=None, context=None) -> list:
    items = [("none", "-- Choisir une langue --", "Aucun modèle")]
    seen: set[str] = set()
    for m in sorted(
        get_language_list_cached(),
        key=lambda x: x.get("lang_text", x.get("lang", ""))
    ):
        lang = m.get("lang", "")
        text = m.get("lang_text", lang)
        if lang and lang not in seen:
            seen.add(lang)
            size = m.get("size_text", "")
            items.append((lang, f"{text} ({size})" if size else text, lang))
    return items

def get_model_info(lang: str) -> Optional[dict]:
    if not lang or lang == "none":
        return None
    return next(
        (m for m in get_language_list_cached()
         if m.get("lang") == lang and m.get("type") == "small"),
        None,
    )

# ── Détection modèle installé ────────────────────────────────────────────────
def get_model_dir(lang: str = "") -> Optional[pathlib.Path]:
    if not lang:
        lang = _get_prefs_lang()
    if not lang or lang == "none":
        return None
    cache = _cache_dir()
    info = get_model_info(lang)
    model_name = info["name"] if info else f"vosk-model-small-{lang}"
    for c in [
        cache / model_name,
        cache / f"vosk-model-{lang}",
        cache / f"vosk-model-small-{lang}",
        _addon_dir() / "model" / "fr",
        _addon_dir() / "vosk_model" / model_name,
    ]:
        if c.is_dir() and (c / "am").is_dir():
            return c
    return None

def is_model_ready(lang: str = "") -> bool:
    return get_model_dir(lang) is not None

def is_any_model_ready() -> bool:
    cache = _cache_dir()
    if cache.is_dir():
        for d in cache.iterdir():
            if d.is_dir() and (d / "am").is_dir():
                return True
    return (
        (_addon_dir() / "model" / "fr").is_dir()
        and (_addon_dir() / "model" / "fr" / "am").is_dir()
    )

def is_model_present_locally() -> bool:
    return is_any_model_ready()

def get_model_path() -> Optional[pathlib.Path]:
    return get_model_dir()

def get_model_cache_dir() -> pathlib.Path:
    return _cache_dir()

def _get_prefs_lang() -> str:
    try:
        pkg = __package__.split(".")[0]
        addon = bpy.context.preferences.addons.get(pkg)
        if addon and addon.preferences:
            return getattr(addon.preferences, "vosk_language", "fr")
    except Exception:
        pass
    return "fr"

# ── Téléchargement async ─────────────────────────────────────────────────────
def start_download(lang: str) -> bool:
    if _dl.proc is not None:
        print("[ASP] Téléchargement déjà en cours.")
        return False
    cache  = _cache_dir()
    worker = _worker_path()
    if not worker.is_file():
        print(f"[ASP] Worker introuvable: {worker}")
        return False
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(sys.path)
    log_path = cache / "download_vosk.log"
    try:
        log_f = open(log_path, "w", encoding="utf-8")
    except Exception:
        log_f = None
    _dl.proc = subprocess.Popen(
        [sys.executable, str(worker), lang, str(cache)],
        env=env, stdout=log_f, stderr=log_f, text=True,
    )
    _dl.log_file = log_f
    _dl.lang     = lang
    _dl.progress = 0.0
    _dl.status   = "Démarrage…"
    if not bpy.app.timers.is_registered(_check_worker):
        bpy.app.timers.register(_check_worker, first_interval=0.5)
    _tag_redraw()
    return True

def _check_worker() -> Optional[float]:
    if _dl.proc is None:
        return None
    log_path = _cache_dir() / "download_vosk.log"
    if log_path.is_file():
        try:
            txt   = log_path.read_text(encoding="utf-8", errors="ignore")
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            if lines:
                last      = lines[-1]
                _dl.status = last[:70]
                m = re.search(r"(\d+)%", last)
                if m:
                    _dl.progress = int(m.group(1)) / 100.0
        except Exception:
            pass
    if _dl.proc.poll() is None:
        _tag_redraw()
        return 0.5
    ret      = _dl.proc.returncode
    _dl.proc = None
    if _dl.log_file:
        try:
            _dl.log_file.close()
        except Exception:
            pass
        _dl.log_file = None
    _dl.status   = (
        "✓ Modèle installé avec succès" if ret == 0
        else f"✗ Erreur code {ret} — voir download_vosk.log"
    )
    _dl.progress = 1.0 if ret == 0 else 0.0
    _tag_redraw()
    return None

def is_downloading()        -> bool:  return _dl.proc is not None
def get_download_progress() -> float: return _dl.progress
def get_download_status()   -> str:   return _dl.status

def _tag_redraw() -> None:
    try:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type in ("VIEW_3D", "PREFERENCES"):
                    a.tag_redraw()
    except Exception:
        pass

# ── Classe VoskWord ──────────────────────────────────────────────────────────
class VoskWord:
    __slots__ = ("text", "start_s", "end_s", "conf")
    def __init__(self, text, start_s, end_s, conf=1.0):
        self.text    = text
        self.start_s = start_s
        self.end_s   = end_s
        self.conf    = conf
    def __repr__(self):
        return f"VoskWord({self.text!r},{self.start_s:.3f}-{self.end_s:.3f}s)"

# ── Reconnaissance ───────────────────────────────────────────────────────────
def recognize_words(wav_path: str, lang: str = "") -> Optional[list]:
    if not lang:
        lang = _get_prefs_lang()
    if not is_vosk_available():
        print("[ASP] Vosk non disponible.")
        return None
    model_dir = get_model_dir(lang)
    if not model_dir:
        print(f"[ASP] Modèle '{lang}' non installé.")
        return None
    try:
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        model = Model(str(model_dir))
    except Exception as e:
        print(f"[ASP] Chargement modèle: {e}")
        return None
    try:
        wf = wave.open(wav_path, "rb")
    except Exception as e:
        print(f"[ASP] WAV: {e}")
        return None
    all_words: list[VoskWord] = []
    try:
        if (wf.getnchannels() != 1
                or wf.getsampwidth() != 2
                or wf.getcomptype() != "NONE"):
            print("[ASP] WAV doit être mono PCM 16-bit.")
            return None
        sr  = wf.getframerate()
        rec = KaldiRecognizer(model, sr)
        rec.SetWords(True)
        chunk = max(4000, sr // 4)
        while True:
            data = wf.readframes(chunk)
            if not data:
                break
            if rec.AcceptWaveform(data):
                for w in json.loads(rec.Result()).get("result", []):
                    t = str(w.get("word", "")).strip()
                    if t:
                        all_words.append(VoskWord(
                            t,
                            float(w.get("start", 0)),
                            float(w.get("end",   0)),
                            float(w.get("conf",  1)),
                        ))
        for w in json.loads(rec.FinalResult()).get("result", []):
            t = str(w.get("word", "")).strip()
            if t:
                all_words.append(VoskWord(
                    t,
                    float(w.get("start", 0)),
                    float(w.get("end",   0)),
                    float(w.get("conf",  1)),
                ))
    except Exception as e:
        print(f"[ASP] Erreur reconnaissance: {e}")
        return None
    finally:
        wf.close()
    all_words.sort(key=lambda x: x.start_s)
    print(f"[ASP] Vosk: {len(all_words)} mots reconnus.")
    return all_words

def words_to_speech_segments(
    words, fps, frame_start, frame_end, silence_fr, merge_close=True
):
    if not words:
        return []
    raw = []
    for w in words:
        f0 = frame_start + int(round(w.start_s * fps))
        f1 = frame_start + int(round(w.end_s   * fps))
        f0 = max(frame_start, min(frame_end, f0))
        f1 = max(frame_start, min(frame_end, f1))
        if f1 > f0:
            raw.append((f0, f1))
    if not raw:
        return []
    if not merge_close:
        return raw
    merged = [raw[0]]
    for f0, f1 in raw[1:]:
        ps, pe = merged[-1]
        if f0 - pe < silence_fr:
            merged[-1] = (ps, max(pe, f1))
        else:
            merged.append((f0, f1))
    return merged
