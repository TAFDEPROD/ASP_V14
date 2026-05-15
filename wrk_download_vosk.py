"""
wrk_download_vosk.py — Worker subprocess pour télécharger un modèle Vosk.

Appelé par vosk_helper via subprocess.Popen :
  python wrk_download_vosk.py <lang_code> <cache_dir>

Utilise vosk.Model(lang=lang) qui gère le téléchargement automatiquement,
ou bien télécharge manuellement le zip si vosk n'est pas dispo.
"""
import json
import os
import pathlib
import sys
import zipfile

def main():
    if len(sys.argv) < 3:
        print("[ASP Worker] Usage: wrk_download_vosk.py <lang> <cache_dir>")
        sys.exit(1)

    lang      = sys.argv[1]
    cache_dir = pathlib.Path(sys.argv[2])
    cache_dir.mkdir(parents=True, exist_ok=True)

    print(f"[ASP Worker] Downloading model '{lang}' into {cache_dir}")
    sys.stdout.flush()

    # --- Méthode 1 : vosk.Model(lang=...) — téléchargement intégré à vosk
    try:
        from vosk import MODEL_DIRS, Model
        MODEL_DIRS[3] = str(cache_dir)
        print(f"[ASP Worker] Using vosk auto-download for lang='{lang}'")
        sys.stdout.flush()
        Model(lang=lang)
        print(f"[ASP Worker] Model '{lang}' installed successfully via vosk.")
        sys.exit(0)
    except ImportError:
        print("[ASP Worker] vosk not importable — trying manual download.")
    except Exception as exc:
        print(f"[ASP Worker] vosk auto-download failed: {exc} — trying manual.")

    sys.stdout.flush()

    # --- Méthode 2 : téléchargement manuel via liste JSON alphacephei
    try:
        import urllib.request
        import urllib.error

        MODEL_LIST_URL = "https://alphacephei.com/vosk/models/model-list.json"
        list_path = cache_dir / "languages_list.json"

        # Charger ou télécharger la liste
        if list_path.is_file():
            with open(list_path, "r", encoding="utf-8") as f:
                model_list = json.load(f)
        else:
            print("[ASP Worker] Fetching model list...")
            sys.stdout.flush()
            req = urllib.request.Request(
                MODEL_LIST_URL,
                headers={"User-Agent": "AudioShapePRO/11 (Blender addon)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                model_list = json.loads(resp.read().decode("utf-8"))
            with open(list_path, "w", encoding="utf-8") as f:
                json.dump([m for m in model_list if m.get("type") == "small"
                           and m.get("obsolete") == "false"], f)

        # Trouver le modèle correspondant à la langue
        model_info = next(
            (m for m in model_list
             if m.get("lang") == lang
             and m.get("type") == "small"
             and m.get("obsolete") == "false"),
            None
        )
        if model_info is None:
            print(f"[ASP Worker] No small model found for lang='{lang}'")
            sys.exit(2)

        model_name = model_info["name"]
        model_url  = model_info.get("url", f"https://alphacephei.com/vosk/models/{model_name}.zip")
        zip_path   = cache_dir / f"{model_name}.zip"
        model_dir  = cache_dir / model_name

        if model_dir.is_dir() and (model_dir / "am").is_dir():
            print(f"[ASP Worker] Model '{model_name}' already installed.")
            sys.exit(0)

        # Téléchargement
        print(f"[ASP Worker] Downloading {model_name} from {model_url}")
        sys.stdout.flush()

        req = urllib.request.Request(
            model_url,
            headers={"User-Agent": "AudioShapePRO/11 (Blender addon)"},
        )
        tmp_path = zip_path.with_suffix(".zip.part")
        total_written = 0

        with urllib.request.urlopen(req, timeout=120) as resp:
            try:
                total = int(resp.headers.get("Content-Length", 0))
            except Exception:
                total = 0

            chunk = 1024 * 256
            last_pct = -1
            with open(tmp_path, "wb") as f:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    f.write(data)
                    total_written += len(data)
                    if total > 0:
                        pct = int(total_written * 100 / total)
                        if pct >= last_pct + 5:
                            print(f"[ASP Worker] {pct}% ({total_written//1048576}/{total//1048576} MB)")
                            sys.stdout.flush()
                            last_pct = pct

        tmp_path.replace(zip_path)
        print(f"[ASP Worker] Download complete ({total_written//1048576} MB)")
        sys.stdout.flush()

        # Extraction
        print(f"[ASP Worker] Extracting {zip_path.name}...")
        sys.stdout.flush()
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(cache_dir)
        zip_path.unlink(missing_ok=True)

        if model_dir.is_dir():
            print(f"[ASP Worker] Model '{model_name}' extracted successfully.")
            sys.exit(0)
        else:
            print(f"[ASP Worker] Extraction failed — directory not found.")
            sys.exit(3)

    except Exception as exc:
        print(f"[ASP Worker] Manual download failed: {exc}")
        sys.exit(4)


if __name__ == "__main__":
    main()
