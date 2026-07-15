"""
scenario.py — Cycle de vie des scenarios (generate) et datasets (export)
========================================================================
Deux arbres de sortie distincts :

  scenarios/<batch>/<scenario_name>/            (produit par 'generate')
      <scenario_name>.yaml                      scenario LARD (TAF)
      <scenario_name>.json                      poses camera
      <scenario_name>.esp                       projet GES (best-effort)
      [fault_profile.json]                      si fautes actives
      [weather_profile.json]                    si meteo active

  dataset/<simulator>/<airport_runway>/<scenario_name>/   (produit par 'export')
      images/                                   rendu simulateur
      corrupted_images/                         images + fautes capteur
      metadata.csv                              verite terrain LARD (GT)

Conventions de nommage :
  <batch>          = <default|nom>__<timestamp>
  <scenario_name>  = <airport>-<runway>__<nb_smpl>-smpl__<timestamp>__<indx>
  <airport_runway> = <airport>_<runway>   (ex: KPDX_10L)
  <simulator>      in {xplane, GES}

Ce module ne fait QUE la structure/nommage/resolution (pas d'orchestration
ni d'import de taf_export/taf_generate) : l'orchestration vit dans pipeline.py.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

import yaml

# --- Chemins ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"      # sortie 'generate'
DATASET_DIR = PROJECT_ROOT / "dataset"          # sortie 'export'
TAF_OUTPUT_DIR = PROJECT_ROOT / "output"        # dossier temporaire TAF

IMAGE_EXTS = (".jpeg", ".jpg", ".png")
SUPPORTED_SIMULATORS = ("xplane", "GES")


# ===========================================================================
# Nommage batch / scenario
# ===========================================================================

def timestamp_now():
    """Horodatage compact pour batch/scenario (ex: 20260714-153012)."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def new_batch_dir(name=None, timestamp=None):
    """Dossier de batch scenarios/<name|default>__<timestamp>/.

    Le timestamp garantit l'unicite (pas d'ecrasement d'un batch precedent).
    """
    ts = timestamp or timestamp_now()
    label = name if name else "default"
    return SCENARIOS_DIR / f"{label}__{ts}"


def make_scenario_name(airport, runway, nb_smpl, timestamp, indx):
    """<airport>-<runway>__<nb_smpl>-smpl__<timestamp>__<indx>."""
    return f"{airport}-{runway}__{nb_smpl}-smpl__{timestamp}__{indx}"


def airport_runway_from_scenario(scenario_name):
    """'KPDX-10L__250-smpl__...__0' -> 'KPDX_10L' (dossier <airport_ICAO>)."""
    head = scenario_name.split("__", 1)[0]      # 'KPDX-10L'
    return head.replace("-", "_", 1)            # 'KPDX_10L'


def clean_scenarios_dir():
    """Supprime tout le contenu de scenarios/ (utilise par --clean)."""
    if not SCENARIOS_DIR.exists():
        return
    for child in SCENARIOS_DIR.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    print("[Pipeline] scenarios/ vide.")


# ===========================================================================
# Helpers images / dataset
# ===========================================================================

def list_images(directory):
    """Liste triee des images (jpeg/jpg/png) d'un dossier (vide si absent)."""
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file() and p.suffix.lower() in IMAGE_EXTS)


def dataset_scenario_dir(simulator, scenario_name):
    """dataset/<simulator>/<airport_runway>/<scenario_name>/."""
    icao = airport_runway_from_scenario(scenario_name)
    return DATASET_DIR / simulator / icao / scenario_name


def has_images(dataset_dir):
    """Le dataset d'un scenario a-t-il des images dans images/ ?"""
    return bool(list_images(Path(dataset_dir) / "images"))


def pick_image_source(dataset_dir):
    """corrupted_images/ si non vide, sinon images/."""
    d = Path(dataset_dir)
    if list_images(d / "corrupted_images"):
        return d / "corrupted_images"
    return d / "images"


# ===========================================================================
# Resolution des scenarios (sortie 'generate')
# ===========================================================================

def _scan_all_scenarios():
    """Tous les scenarios : scenarios/<batch>/<scenario>/ contenant un .yaml."""
    if not SCENARIOS_DIR.exists():
        return []
    found = []
    for batch in sorted(SCENARIOS_DIR.iterdir()):
        if not batch.is_dir():
            continue
        for sub in sorted(batch.iterdir()):
            if sub.is_dir() and list(sub.glob("*.yaml")):
                found.append(sub)
    return found


def resolve_batch_dir(batch):
    """Path d'un batch existant sous scenarios/, ou None."""
    if not batch:
        return None
    d = SCENARIOS_DIR / batch
    return d if d.is_dir() else None


def find_scenarios(name=None, all_scenarios=False, batch=None):
    """Trouve les scenarios a rendre (dossiers contenant un .yaml).

    Resolution de `name` :
      - "<batch>/<scenario>"     : chemin compose (split sur / ou \\)
      - name + batch             : scenarios/<batch>/<name>/
      - chemin absolu existant   : utilise tel quel
      - nom seul                 : recherche dans tous les batchs (erreur si
                                   ambigu -> demander --batch)

    Modes `all_scenarios` :
      - + batch                  : tout scenarios/<batch>/
      - sans batch               : tous les batchs
    """
    if name:
        if "/" in name or "\\" in name:
            candidates = [SCENARIOS_DIR / Path(name)]
        elif batch:
            candidates = [SCENARIOS_DIR / batch / name]
        else:
            p = Path(name)
            if p.is_absolute() and p.exists():
                candidates = [p]
            else:
                matches = [d for d in _scan_all_scenarios() if d.name == name]
                if len(matches) > 1:
                    print(f"[ERREUR] '{name}' trouve dans plusieurs batchs :")
                    for m in matches:
                        print(f"  - {m.relative_to(PROJECT_ROOT)}")
                    print("  Specifier --batch ou le chemin compose '<batch>/<scenario>'.")
                    return []
                candidates = matches
    elif all_scenarios:
        if batch:
            bdir = resolve_batch_dir(batch)
            if not bdir:
                print(f"[ERREUR] Batch introuvable : scenarios/{batch}/")
                return []
            candidates = sorted(d for d in bdir.iterdir() if d.is_dir())
        else:
            candidates = _scan_all_scenarios()
    else:
        candidates = []

    valid = []
    for sdir in candidates:
        if not sdir.exists():
            print(f"[ERREUR] Scenario introuvable : {sdir}")
            continue
        if not list(sdir.glob("*.yaml")):
            print(f"[SKIP] {sdir.name} : pas de .yaml")
            continue
        valid.append(sdir)
    return valid


# ===========================================================================
# Reorganisation sortie TAF -> scenarios/<batch>/<scenario_name>/
# ===========================================================================

def _yaml_sample_number(yaml_file):
    """sample_number depuis le .yaml TAF (0 si illisible)."""
    try:
        data = yaml.safe_load(Path(yaml_file).read_text())
        return int(((data or {}).get("trajectory") or {}).get("sample_number", 0))
    except Exception:
        return 0


def _count_samples(poses_src, yaml_file):
    """Nombre d'images du scenario : n_frames des poses, sinon yaml sample_number."""
    if Path(poses_src).exists():
        try:
            n = int(json.loads(Path(poses_src).read_text()).get("n_frames", 0))
            if n:
                return n
        except (json.JSONDecodeError, OSError):
            pass
    return _yaml_sample_number(yaml_file)


def create_scenarios_from_taf_output(batch_dir, timestamp):
    """Reorganise output/ (TAF) vers scenarios/<batch>/<scenario_name>/.

    Pour chaque .yaml TAF (stem = ICAO_RWY), cree un dossier au nom
    <airport>-<runway>__<nb_smpl>-smpl__<timestamp>__<indx> contenant :
      - <scenario_name>.yaml
      - <scenario_name>.json   (ex poses_cam_export.json, scenario_name patche)
      - fault_profile.json / weather_profile.json (si presents)

    Le .esp (GES) est genere ensuite par taf_generate.generate_runs (via LARD).

    :return: list[Path] des dossiers scenarios crees
    """
    batch_dir = Path(batch_dir)
    batch_dir.mkdir(parents=True, exist_ok=True)
    yaml_files = sorted(TAF_OUTPUT_DIR.rglob("*.yaml"))

    created = []
    for indx, yf in enumerate(yaml_files):
        icao_rwy = yf.stem                       # ex: KPDX_10L
        airport, _, runway = icao_rwy.partition("_")

        poses_src = yf.parent / "poses_cam_export.json"
        nb_smpl = _count_samples(poses_src, yf)

        scenario_name = make_scenario_name(airport, runway, nb_smpl, timestamp, indx)
        sdir = batch_dir / scenario_name
        sdir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(yf, sdir / f"{scenario_name}.yaml")

        if poses_src.exists():
            data = json.loads(poses_src.read_text())
            data["scenario_name"] = scenario_name
            (sdir / f"{scenario_name}.json").write_text(json.dumps(data, indent=2))

        extras = ""
        for extra in ("fault_profile.json", "weather_profile.json"):
            src = yf.parent / extra
            if src.exists():
                shutil.copy2(src, sdir / extra)
                extras += f" + {extra}"

        created.append(sdir)
        rel = sdir.relative_to(PROJECT_ROOT)
        print(f"  [SCEN] {rel}/ <- .yaml + .json{extras}")

    print(f"\n[Pipeline] {len(created)} scenario(s) dans "
          f"{batch_dir.relative_to(PROJECT_ROOT)}/")
    return created


# Orchestration (render_scenarios / full_pipeline) : voir pipeline.py
# (module separe pour eviter le cycle d'imports scenario <-> taf_export/taf_generate).
