"""
notebook_tools.py — Outils appelables depuis notebooks/features.ipynb
=====================================================================
Helpers de visualisation / dataset au-dessus du NOUVEAU layout :

  scenarios/<batch>/<scenario>/            (generate)
      <scenario>.yaml / <scenario>.json    scenario LARD + poses camera
      [fault_profile.json / weather_profile.json]

  dataset/<simulator>/<airport_runway>/<scenario>/   (export)
      footage/ , corrupted_footage/        rendu + fautes capteur
      metadata.csv                         verite terrain LARD (1 ligne / image)
      labels_lard.csv                      GT LARD brute (toutes pistes visibles)

Reutilise au maximum le code de l'usine (pas de duplication) :
  - scenario.py  : resolution des scenarios/datasets, listing images
  - metadata.py  : schema (META_COLS) + helpers (temps, meteo, GT)
  - la GT pour la visualisation est lue depuis metadata.csv (deja filtre par
    piste + une ligne par image), pas depuis labels_lard.csv brut.

Usage depuis le notebook :
    from notebook_tools import (
        build_dataset, regroup_images,
        build_lard_box, show_sanity_lard,
        build_xplane_config, build_params_trace,
        build_video,
    )
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # sources/ sur sys.path (imports plats)

import csv
import json
import shutil
from collections import defaultdict
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

import cv2
import yaml

# --- Reutilisation du code usine (source unique de verite) ---
from scenario import (
    PROJECT_ROOT, DATASET_DIR, RENDER_DIRNAME,
    find_scenarios, list_images, pick_image_source,
    dataset_scenario_dir, airport_runway_from_scenario,
)
from metadata import META_COLS  # schema partage (pas de copie locale)

# Sorties on-demand du notebook (hors pipeline)
DATASET_BUILD_DIR = PROJECT_ROOT / "dataset_build"      # consolidation par piste
REGROUP_DIR = PROJECT_ROOT / "dataset_regroup"          # images aplaties (train)

PILOT_EYE_DEFAULT = {"x": -0.25, "y": 0.40, "z": 0.26}  # defaults Cessna 172


# ===========================================================================
# Resolution scenario <-> dataset
# ===========================================================================

def find_runs(run_name=None, all_runs=False, generation=None):
    """Adaptateur : mappe l'ancienne API (run_name/all_runs/generation) vers
    find_scenarios(). Retourne les dossiers scenarios/<batch>/<scenario>/."""
    return find_scenarios(name=run_name, all_scenarios=all_runs, batch=generation)


def _dataset_dir(scenario_dir, simulator="xplane"):
    """dataset/<simulator>/<icao>/<scenario>/ correspondant a un scenario_dir."""
    return dataset_scenario_dir(simulator, Path(scenario_dir).name)


def _iter_run_datasets(run_name=None, all_runs=None, simulator="xplane"):
    """Itere (scenario_dir, dataset_dir) pour les scenarios cibles rendus.

    all_runs par defaut = (run_name is None). Ne garde que les scenarios dont
    le dataset possede un dossier footage/ (donc reellement rendus).
    """
    if all_runs is None:
        all_runs = run_name is None
    for scen in find_runs(run_name=run_name, all_runs=all_runs):
        ds = _dataset_dir(scen, simulator)
        if (ds / RENDER_DIRNAME).exists():
            yield scen, ds


def _corners_from_metadata(dataset_dir):
    """{nom_image: [(x,y) x4]} depuis metadata.csv (piste cible, 1 ligne/image).

    Ignore les lignes sans coins (frames ou la piste n'est pas dans le cone).
    """
    meta = Path(dataset_dir) / "metadata.csv"
    out = {}
    if not meta.exists():
        return out
    with open(meta, newline="") as f:
        for r in csv.DictReader(f):
            try:
                out[Path(r["image"]).name] = [
                    (float(r["x_TR"]), float(r["y_TR"])),
                    (float(r["x_TL"]), float(r["y_TL"])),
                    (float(r["x_BL"]), float(r["y_BL"])),
                    (float(r["x_BR"]), float(r["y_BR"])),
                ]
            except (KeyError, ValueError):
                continue  # ligne sans GT
    return out


def _read_fps(scenario_dir, default=12):
    """fps depuis <scenario>.json (poses), defaut si absent."""
    for pj in Path(scenario_dir).glob("*.json"):
        if pj.name in ("fault_profile.json", "weather_profile.json"):
            continue
        try:
            return int(json.loads(pj.read_text()).get("fps", default))
        except (json.JSONDecodeError, OSError, ValueError):
            return default
    return default


def _poses_json(scenario_dir):
    """<scenario>.json (poses), hors fault_/weather_profile.json."""
    for p in Path(scenario_dir).glob("*.json"):
        if p.name not in ("fault_profile.json", "weather_profile.json"):
            return p
    return None


# ===========================================================================
# LARD box + sanity check (GT depuis metadata.csv)
# ===========================================================================

def _draw_corners(img, corners, color=(255, 255, 0), width=2):
    """Dessine le quad GT (TR-TL-BL-BR) sur une image BGR OpenCV (cyan par defaut).

    color est en BGR (OpenCV) : (255, 255, 0) = cyan, coherent avec show_sanity_lard.
    """
    import numpy as np
    pts = np.array([(int(x), int(y)) for x, y in corners], dtype=np.int32)
    cv2.polylines(img, [pts], isClosed=True, color=color, thickness=width)


def build_lard_box(run_name=None, source=None, line_width=2):
    """Genere dataset/.../<scenario>/lard_box/ (images + bbox GT LARD dessinees).

    :param source: 'footage' ou 'corrupted_footage' (defaut: corrupted si dispo).
    """
    for scen, ds in _iter_run_datasets(run_name=run_name):
        src = (ds / source) if source else pick_image_source(ds)
        images = list_images(src)
        if not images:
            print(f"  [skip] {scen.name} : pas d'images dans {src.name}/")
            continue
        corners = _corners_from_metadata(ds)
        out = ds / "lard_box"
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True)
        drawn = 0
        for img_path in images:
            img = cv2.imread(str(img_path))
            if img is None:
                continue
            box = corners.get(img_path.name)
            if box:
                _draw_corners(img, box, width=line_width)
                drawn += 1
            cv2.imwrite(str(out / img_path.name), img)
        print(f"  {scen.name} -> lard_box/ ({drawn}/{len(images)} avec GT, source: {src.name}/)")


def show_sanity_lard(run_name=None, line_width=2, source=None):
    """Affiche 1ere / milieu / derniere image d'un scenario avec bbox GT LARD."""
    import matplotlib.pyplot as plt

    runs = list(_iter_run_datasets(run_name=run_name))
    if not runs:
        print("[!] aucun scenario rendu trouve")
        return
    scen, ds = runs[0]

    src = (ds / source) if source else pick_image_source(ds)
    images = list_images(src)
    if not images:
        print(f"[!] pas d'images dans {src}")
        return

    corners = _corners_from_metadata(ds)
    n = len(images)
    picks = [(images[0], 0), (images[n // 2], n // 2), (images[-1], n - 1)]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for ax, (img_path, idx) in zip(axes, picks):
        img = cv2.cvtColor(cv2.imread(str(img_path)), cv2.COLOR_BGR2RGB)
        ax.imshow(img)
        box = corners.get(img_path.name)
        if box:
            xs = [c[0] for c in box] + [box[0][0]]
            ys = [c[1] for c in box] + [box[0][1]]
            ax.plot(xs, ys, color="cyan", linewidth=line_width)
        ax.set_title(f"{img_path.name} ({idx + 1}/{n})")
        ax.axis("off")
    fig.suptitle(f"{scen.name} — sanity GT LARD (source: {src.name}/)")
    plt.tight_layout()
    plt.show()


# ===========================================================================
# Video MP4
# ===========================================================================

def build_video(run_name=None, source=None):
    """Genere dataset/.../<scenario>/<scenario>.mp4 (fps depuis <scenario>.json).

    :param source: 'footage' | 'corrupted_footage' (defaut: corrupted prio).
    """
    for scen, ds in _iter_run_datasets(run_name=run_name):
        src = (ds / source) if source else pick_image_source(ds)
        images = list_images(src)
        if not images:
            print(f"  [skip] {scen.name} : pas d'images dans {src.name}/")
            continue

        fps = _read_fps(scen)
        first = cv2.imread(str(images[0]))
        if first is None:
            print(f"  [skip] {scen.name} : lecture impossible {images[0].name}")
            continue
        h, w = first.shape[:2]

        out = ds / f"{scen.name}.mp4"
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        for img_path in images:
            img = cv2.imread(str(img_path))
            if img is not None:
                writer.write(img)
        writer.release()
        print(f"  {scen.name} -> {out.name}  ({len(images)} frames @ {fps}fps, {src.name}/)")


# ===========================================================================
# xplane_config.json + params_trace.xml (sorties on-demand, dans scenarios/)
# ===========================================================================

def build_xplane_config(run_name=None):
    """Ecrit scenarios/.../<scenario>/xplane_config.json depuis yaml + meteo."""
    for scen in find_runs(run_name=run_name, all_runs=run_name is None):
        yamls = list(scen.glob("*.yaml"))
        if not yamls:
            print(f"  [skip] {scen.name} : pas de yaml")
            continue
        meta = yaml.safe_load(yamls[0].read_text())
        img = meta.get("image", {})
        weather_status = "ok" if (scen / "weather_profile.json").exists() else "absent"

        cfg = {
            "width": int(img.get("width", 1024)),
            "height": int(img.get("height", 1024)),
            "fov_h": float(img.get("fov_x", 60.0)),
            "fov_v": float(img.get("fov_y", 60.0)),
            "pilot_eye_x": PILOT_EYE_DEFAULT["x"],
            "pilot_eye_y": PILOT_EYE_DEFAULT["y"],
            "pilot_eye_z": PILOT_EYE_DEFAULT["z"],
            "weather_status": weather_status,
        }
        (scen / "xplane_config.json").write_text(json.dumps(cfg, indent=2))
        print(f"  {scen.name} -> xplane_config.json "
              f"({cfg['width']}x{cfg['height']}, weather={weather_status})")


def _xml_value(parent, tag, value):
    ET.SubElement(parent, tag).text = f"{value}"


def build_params_trace(run_name=None):
    """Ecrit scenarios/.../<scenario>/params_trace.xml en agregeant les profils."""
    for scen in find_runs(run_name=run_name, all_runs=run_name is None):
        pj = _poses_json(scen)
        poses = json.loads(pj.read_text()) if pj else {}
        weather = (json.loads((scen / "weather_profile.json").read_text()).get("weather", {})
                   if (scen / "weather_profile.json").exists() else {})
        faults = (json.loads((scen / "fault_profile.json").read_text()).get("faults", [])
                  if (scen / "fault_profile.json").exists() else [])

        root = ET.Element("test_case")
        scenario = ET.SubElement(root, "scenario", instance="0/0")

        traj = ET.SubElement(scenario, "trajectory", instance="0/0")
        if "fps" in poses:
            _xml_value(traj, "fps", poses["fps"])
        for k, v in poses.get("trajectory", {}).items():
            _xml_value(traj, k, v)

        wn = ET.SubElement(scenario, "weather", instance="0/0")
        for k, v in weather.items():
            _xml_value(wn, k, v)

        fn = ET.SubElement(scenario, "faults", instance="0/0")
        for f in faults:
            ftype = f.get("fault_type", "unknown")
            sub = ET.SubElement(fn, ftype, instance="0/0")
            for k, v in f.items():
                if k != "fault_type":
                    _xml_value(sub, k, v)

        xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
        (scen / "params_trace.xml").write_text(xml_str, encoding="utf-8")
        print(f"  {scen.name} -> params_trace.xml")


# ===========================================================================
# Dataset consolide (agregation des metadata.csv par piste)
# ===========================================================================

def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_COLS)
        w.writeheader()
        w.writerows(rows)


def build_dataset(simulator="xplane", out_dir=DATASET_BUILD_DIR, source=None):
    """Consolide les datasets par-scenario en une arbo par piste, renumerotee.

    Le pipeline produit deja dataset/<sim>/<icao>/<scenario>/{images,metadata.csv}.
    Cette fonction AGREGE ces sorties (reutilise metadata.csv, ne recalcule rien) :

        dataset_build/
            metadata.csv                      # toutes pistes / scenarios
            <ICAO_RWY>/
                metadata.csv                  # tous scenarios de la piste
                <ICAO_RWY>_<NNN>/
                    images/000000.jpg ...
                    metadata.csv              # ce scenario

    :param source: 'footage' | 'corrupted_footage' (defaut: corrupted prio).
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # Regroupe les datasets rendus par piste (ICAO_RWY).
    by_runway = defaultdict(list)
    for scen, ds in _iter_run_datasets(all_runs=True, simulator=simulator):
        by_runway[airport_runway_from_scenario(scen.name)].append((scen, ds))

    summary = {}
    all_rows = []
    for rwy_key, runs in sorted(by_runway.items()):
        runs.sort(key=lambda t: t[0].name)
        rwy_dir = out_dir / rwy_key
        rwy_dir.mkdir(parents=True, exist_ok=True)
        rwy_rows = []

        for idx, (scen, ds) in enumerate(runs, start=1):
            src = (ds / source) if source else pick_image_source(ds)
            images = list_images(src)
            meta_rows = {r["image"].split("/")[-1]: r
                         for r in csv.DictReader(open(ds / "metadata.csv", newline=""))} \
                if (ds / "metadata.csv").exists() else {}
            if not images:
                summary[scen.name] = (src.name, 0, "absent")
                continue

            scen_id = f"{rwy_key}_{idx:03d}"
            img_dir = out_dir / rwy_key / scen_id / "images"
            img_dir.mkdir(parents=True, exist_ok=True)

            scen_rows = []
            for i, img in enumerate(images):
                new_name = f"{i:06d}{img.suffix.lower()}"
                shutil.copy2(img, img_dir / new_name)
                base = {c: "" for c in META_COLS}
                base.update({k: v for k, v in meta_rows.get(img.name, {}).items()
                             if k in META_COLS})
                base["scenario"] = scen_id
                scen_rows.append({**base, "image": f"images/{new_name}"})
                rwy_rows.append({**base, "image": f"{scen_id}/images/{new_name}"})
                all_rows.append({**base, "image": f"{rwy_key}/{scen_id}/images/{new_name}"})

            _write_csv(out_dir / rwy_key / scen_id / "metadata.csv", scen_rows)
            summary[scen.name] = (src.name, len(scen_rows), f"ok -> {rwy_key}/{scen_id}")

        _write_csv(rwy_dir / "metadata.csv", rwy_rows)

    _write_csv(out_dir / "metadata.csv", all_rows)

    total = sum(n for _, n, _ in summary.values())
    print(f"Dataset consolide : {out_dir}")
    for name, (kind, n, status) in summary.items():
        print(f"  {name:<45} <- {kind:<16} ({n:>4} imgs) [{status}]")
    print(f"Total : {total} images, {len(by_runway)} piste(s)")
    return summary


def regroup_images(mode="piste", src_dir=DATASET_BUILD_DIR, dest_dir=REGROUP_DIR):
    """Aplati/renumerotte les images de dataset_build/ (+ metadata.csv associe).

    mode="piste" : un dossier par piste (dataset_regroup/<ICAO_RWY>/img/...).
    mode="all"   : tout dans dataset_regroup/datasetr/img/ (renumerotation globale).
    Lance d'abord build_dataset().
    """
    if mode not in ("piste", "all"):
        raise ValueError(f"mode invalide : {mode!r} (attendu 'piste' ou 'all')")
    if not src_dir.exists():
        print(f"dataset_build introuvable : {src_dir}\nLance d'abord build_dataset().")
        return

    meta_by_relpath = {}
    root_meta = src_dir / "metadata.csv"
    if root_meta.exists():
        with open(root_meta, newline="") as f:
            for row in csv.DictReader(f):
                meta_by_relpath[row["image"]] = row

    exts = {".jpg", ".jpeg", ".png"}

    def _meta_row(img, new_image):
        relpath = img.relative_to(src_dir).as_posix()
        base = {c: "" for c in META_COLS}
        base.update({k: v for k, v in meta_by_relpath.get(relpath, {}).items()
                     if k in META_COLS})
        base["image"] = new_image
        return base

    if mode == "piste":
        by_runway = defaultdict(list)
        for img in sorted(src_dir.rglob("images/*")):
            if img.is_file() and img.suffix.lower() in exts:
                by_runway[img.parent.parent.parent.name].append(img)
        if not by_runway:
            print(f"Aucune image dans {src_dir}")
            return
        print(f"Regroupement par piste : {dest_dir}")
        total = 0
        for rwy, imgs in sorted(by_runway.items()):
            rwy_dir = dest_dir / rwy
            img_dir = rwy_dir / "img"
            if rwy_dir.exists():
                shutil.rmtree(rwy_dir)
            img_dir.mkdir(parents=True)
            rows = []
            for i, img in enumerate(imgs):
                new_name = f"{i:06d}{img.suffix.lower()}"
                shutil.copy2(img, img_dir / new_name)
                rows.append(_meta_row(img, f"img/{new_name}"))
            _write_csv(rwy_dir / "metadata.csv", rows)
            print(f"  {rwy:<15} {len(imgs):>4} imgs -> {rwy}/img/")
            total += len(imgs)
        print(f"Total : {total} images, {len(by_runway)} piste(s)")
    else:
        flat_dir = dest_dir / "datasetr"
        img_dir = flat_dir / "img"
        if flat_dir.exists():
            shutil.rmtree(flat_dir)
        img_dir.mkdir(parents=True)
        by_runway = defaultdict(int)
        rows = []
        idx = 0
        for img in sorted(src_dir.rglob("images/*")):
            if not img.is_file() or img.suffix.lower() not in exts:
                continue
            new_name = f"{idx:06d}{img.suffix.lower()}"
            shutil.copy2(img, img_dir / new_name)
            rows.append(_meta_row(img, f"img/{new_name}"))
            by_runway[img.parent.parent.parent.name] += 1
            idx += 1
        if idx == 0:
            print(f"Aucune image dans {src_dir}")
            return
        _write_csv(flat_dir / "metadata.csv", rows)
        print(f"Regroupement global : {flat_dir}")
        for rwy, n in sorted(by_runway.items()):
            print(f"  {rwy:<15} {n:>4} imgs")
        print(f"Total : {idx} images, {len(by_runway)} piste(s)")
