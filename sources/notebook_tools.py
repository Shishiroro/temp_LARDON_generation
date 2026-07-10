"""
notebook_tools.py — Outils appelables depuis notebook/features.ipynb
====================================================================

Regroupe les fonctions utilisees par notebook/features.ipynb (dataset, sanity checks,
generation yolo_box/lard_box, params_trace.xml, xplane_config.json, video MP4)
pour que le notebook se reduise a une suite d'appels.

Usage depuis le notebook :
    from notebook_tools import (
        build_dataset, regroup_images,
        build_yolo_box, show_sanity,
        build_lard_box, show_sanity_lard,
        build_xplane_config, build_params_trace,
        build_video,
    )
"""

import _paths  # noqa: F401  (bootstrap sys.path)

import csv
import json
import shutil
from collections import defaultdict
from pathlib import Path
from xml.dom import minidom
from xml.etree import ElementTree as ET

import cv2
import yaml

from lard_bridge import annotate_gt, load_gt_corners, generate_frame_times, _lard_cwd
from evaluation.yolo.predict import MODEL_PATH  # SUT YOLO (banc d'eval)
from runs import find_runs, list_images, pick_image_source
from runway import runway_from_run_name


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
DATASET_DIR = RUNS_DIR / "dataset"
REGROUP_DIR = RUNS_DIR / "dataset_regroup"

# Colonnes du metadata.csv — alignees sur le CSV LARD natif.
# Conventions (toutes valeurs LARD copiees telles quelles, sans normalisation) :
#   - yaw, pitch, roll      : degres (pitch = 90 + pitch_reel, LARD natif)
#   - slant_distance, along_track_distance : milles nautiques (NM)
#   - height_above_runway   : pieds (ft)
#   - type                  : constant "xplane"
#   - scenario              : {ICAO}_{RWY}_{NNN:03d} (numerote par piste sur le batch)
#   - time                  : "DD/MM/YYYY HH:MM" (date du run + heure simulee X-Plane)
#   - time_fps              : HH:MM:SS (heure systeme du run, utilisee pour le fps)
#   - weather               : nom du template XML utilise (ex: "rain_heavy")
META_COLS = [
    "height", "width", "type",
    "scenario", "airport", "runway",
    "time", "time_fps", "weather",
    "yaw", "pitch", "roll",
    "slant_distance", "along_track_distance", "height_above_runway",
    "lateral_path_angle", "vertical_path_angle",
    "x_TR", "y_TR", "x_TL", "y_TL", "x_BL", "y_BL", "x_BR", "y_BR",
    "image",
]

# Colonnes copiees telles quelles depuis le CSV LARD vers le metadata.
LARD_PASSTHROUGH = [
    "height", "width",
    "airport", "runway",
    "yaw", "pitch", "roll",
    "slant_distance", "along_track_distance", "height_above_runway",
    "lateral_path_angle", "vertical_path_angle",
    "x_TR", "y_TR", "x_TL", "y_TL", "x_BL", "y_BL", "x_BR", "y_BR",
]

# Defaults Cessna 172 (vus dans les anciens xplane_config.json)
PILOT_EYE_DEFAULT = {"x": -0.25, "y": 0.40, "z": 0.26}


# ---------------------------------------------------------------------------
# Helpers prives
# ---------------------------------------------------------------------------

def _load_lard_rows(run, target_rwy):
    """Charge le CSV LARD du run, filtre sur la piste cible, indexe par nom d'image."""
    csvs = list(run.glob("*_labels.csv"))
    if not csvs:
        return {}
    rows = {}
    with open(csvs[0], newline="") as f:
        for row in csv.DictReader(f, delimiter=";"):
            if row.get("runway") != target_rwy:
                continue
            rows[Path(row["image"]).name] = row
    return rows


def _read_template_name(run):
    """Lit trajectory.template_file_name depuis poses_cam_export.json."""
    pp = run / "poses_cam_export.json"
    if not pp.exists():
        return ""
    try:
        data = json.loads(pp.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    return str((data.get("trajectory") or {}).get("template_file_name", ""))


def _load_time_of_day(run):
    """Lit weather_profile.json et retourne time_of_day_h, '' si absent."""
    wp = run / "weather_profile.json"
    if not wp.exists():
        return ""
    try:
        data = json.loads(wp.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    return data.get("weather", {}).get("time_of_day_h", "")


def _weather_label(template_file_name):
    """'rain/rain_heavy.xml' -> 'rain_heavy', '' -> ''."""
    return Path(template_file_name).stem if template_file_name else ""


def _format_time(time_lard, time_of_day_h):
    """Combine date LARD + heure X-Plane -> 'DD/MM/YYYY HH:MM'.

    time_lard     : 'YYYY-MM-DD HH:MM:SS' (ecrit par lard_bridge).
    time_of_day_h : float (heures decimales, ex 12.5 -> 12:30).
    """
    if not time_lard:
        return ""
    try:
        y, m, d = time_lard.split(" ")[0].split("-")
        date_fmt = f"{d}/{m}/{y}"
    except (ValueError, IndexError):
        return ""
    if time_of_day_h in ("", None):
        return date_fmt
    try:
        h_float = float(time_of_day_h)
    except (TypeError, ValueError):
        return date_fmt
    hh = int(h_float) % 24
    mm = int(round((h_float - int(h_float)) * 60)) % 60
    return f"{date_fmt} {hh:02d}:{mm:02d}"


def _format_time_fps(time_lard):
    """'YYYY-MM-DD HH:MM:SS' -> 'HH:MM:SS'."""
    if not time_lard or " " not in time_lard:
        return ""
    return time_lard.split(" ", 1)[1]


def _runway_key(run_name):
    """Cle de regroupement : ICAO_RWY sans le suffixe _002/_003/..."""
    airport = run_name.split("_")[0]
    return f"{airport}_{runway_from_run_name(run_name)}"


def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=META_COLS)
        w.writeheader()
        w.writerows(rows)


def _xml_value(parent, tag, value):
    el = ET.SubElement(parent, tag)
    el.text = f"{value}"


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def build_dataset(out_dir=DATASET_DIR):
    """Construit dataset/ avec arborescence par piste / scenario.

    Sortie :
        dataset/
            metadata.csv                          # toutes pistes / scenarios
            <RWY>/
                metadata.csv                      # tous scenarios de la piste
                <RWY>_<NNN>/
                    images/NNNNNN.jpg ...
                    metadata.csv                  # ce scenario uniquement
    """
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    # Regroupe les runs par piste (KPDX_10L, KPDX_10L_002 -> meme groupe).
    by_runway = defaultdict(list)
    for run in find_runs(all_runs=True):
        by_runway[_runway_key(run.name)].append(run)

    summary = {}
    all_rows = []
    for rwy_key, runs in by_runway.items():
        runs.sort(key=lambda r: r.name)
        rwy_dir = out_dir / rwy_key
        rwy_dir.mkdir(parents=True, exist_ok=True)
        rwy_rows = []

        for idx, run in enumerate(runs, start=1):
            src = pick_image_source(run)
            kind = src.name
            images = list_images(src)
            if not images:
                summary[run.name] = (kind, 0, "absent")
                continue

            target_rwy = runway_from_run_name(run.name)
            lard_rows = _load_lard_rows(run, target_rwy)
            time_of_day = _load_time_of_day(run)
            weather_label = _weather_label(_read_template_name(run))
            scen_id = f"{rwy_key}_{idx:03d}"

            scen_dir = rwy_dir / scen_id
            img_dir = scen_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)

            scen_rows = []
            for i, img in enumerate(images):
                new_name = f"{i:06d}{img.suffix.lower()}"
                shutil.copy2(img, img_dir / new_name)

                base = {c: "" for c in META_COLS}
                lard = lard_rows.get(img.name, {})
                for k in LARD_PASSTHROUGH:
                    if k in lard:
                        base[k] = lard[k]
                base["type"] = "xplane"
                base["scenario"] = scen_id
                base["airport"] = base["airport"] or run.name.split("_")[0]
                base["runway"] = base["runway"] or target_rwy
                base["time"] = _format_time(lard.get("time", ""), time_of_day)
                base["time_fps"] = _format_time_fps(lard.get("time", ""))
                base["weather"] = weather_label

                # 3 vues du chemin image, profondeur croissante.
                scen_rows.append({**base, "image": f"images/{new_name}"})
                rwy_rows.append({**base, "image": f"{scen_id}/images/{new_name}"})
                all_rows.append({**base, "image": f"{rwy_key}/{scen_id}/images/{new_name}"})

            _write_csv(scen_dir / "metadata.csv", scen_rows)
            summary[run.name] = (kind, len(scen_rows), f"ok -> {rwy_key}/{scen_id}")

        _write_csv(rwy_dir / "metadata.csv", rwy_rows)

    _write_csv(out_dir / "metadata.csv", all_rows)

    total = sum(n for _, n, _ in summary.values())
    print(f"Dataset : {out_dir}")
    for name, (kind, n, status) in summary.items():
        print(f"  {name:<25} <- {kind:<8} ({n:>4} imgs) [{status}]")
    print(f"Total : {total} images, {len(by_runway)} piste(s)")
    print(f"Metadata racine : {out_dir / 'metadata.csv'} ({len(all_rows)} lignes)")
    return summary


def regroup_images(mode="piste", src_dir=DATASET_DIR, dest_dir=REGROUP_DIR):
    """Regroupe et renumerote les images de dataset/, avec metadata.csv associe.

    Deux modes :
      mode="piste" (defaut) : un dossier par piste, renumerotation par piste.
          dataset_regroup/<RWY>/img/000000.jpg ...
          dataset_regroup/<RWY>/metadata.csv      (scenarios de cette piste)
      mode="all"            : tout dans un seul dossier, renumerotation globale.
          dataset_regroup/datasetr/img/000000.jpg ...
          dataset_regroup/datasetr/metadata.csv   (toutes pistes confondues)

    Chaque metadata.csv recopie les lignes du dataset (colonnes META_COLS) en
    remplacant 'image' par le nouveau nom aplati. Les deux modes coexistent
    (chacun nettoie uniquement son propre sous-dossier de dataset_regroup/).
    """
    if mode not in ("piste", "all"):
        raise ValueError(f"mode invalide : {mode!r} (attendu 'piste' ou 'all')")
    if not src_dir.exists():
        print(f"Dataset introuvable : {src_dir}")
        print("Lance d'abord build_dataset() (cellule precedente).")
        return

    # Indexe le metadata racine du dataset : 'image' (chemin relatif) -> ligne.
    meta_by_relpath = {}
    root_meta = src_dir / "metadata.csv"
    if root_meta.exists():
        with open(root_meta, newline="") as f:
            for row in csv.DictReader(f):
                meta_by_relpath[row["image"]] = row

    exts = {".jpg", ".jpeg", ".png"}

    def _meta_row(img, new_image):
        """Recopie la ligne metadata du dataset, 'image' -> nouveau nom aplati."""
        relpath = img.relative_to(src_dir).as_posix()
        base = {c: "" for c in META_COLS}
        base.update({k: v for k, v in meta_by_relpath.get(relpath, {}).items()
                     if k in META_COLS})
        base["image"] = new_image
        return base

    if mode == "piste":
        # Regroupe par piste (img = .../<RWY>/<scenario>/images/<NNNNNN>.jpg).
        by_runway = defaultdict(list)
        for img in sorted(src_dir.rglob("images/*")):
            if img.is_file() and img.suffix.lower() in exts:
                by_runway[img.parent.parent.parent.name].append(img)
        if not by_runway:
            print(f"Aucune image trouvee dans {src_dir}")
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
            for idx, img in enumerate(imgs):
                new_name = f"{idx:06d}{img.suffix.lower()}"
                shutil.copy2(img, img_dir / new_name)
                rows.append(_meta_row(img, f"img/{new_name}"))
            _write_csv(rwy_dir / "metadata.csv", rows)
            print(f"  {rwy:<25} {len(imgs):>4} imgs -> {rwy}/img/")
            total += len(imgs)
        print(f"Total : {total} images, {len(by_runway)} piste(s)")
    else:
        # Tout dans dataset_regroup/datasetr/img/ (renumerotation globale).
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
            print(f"Aucune image trouvee dans {src_dir}")
            return

        _write_csv(flat_dir / "metadata.csv", rows)
        print(f"Regroupement global : {flat_dir}")
        for rwy, n in sorted(by_runway.items()):
            print(f"  {rwy:<25} {n:>4} imgs")
        print(f"Total : {idx} images, {len(by_runway)} piste(s)")


# ---------------------------------------------------------------------------
# YOLO box + sanity
# ---------------------------------------------------------------------------

def build_yolo_box(run_name: str | None = None,
                   line_width: int = 2, conf: float = 0.25, imgsz: int = 512):
    """Trace les bbox YOLO (trait fin) dans run_dir/yolo_box/. None = tous les runs.

    Meme appel que evaluation/yolo/predict.py (model.predict + save=True), seul `line_width`
    est ajoute (non expose par predict()).
    """
    from ultralytics import YOLO

    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    model = YOLO(str(MODEL_PATH))
    for run in targets:
        src = pick_image_source(run)
        if not list_images(src):
            print(f"  [skip] {run.name} : pas d'images dans {src.name}/")
            continue
        out = run / "yolo_box"
        if out.exists():
            shutil.rmtree(out)
        model.predict(source=str(src), conf=conf, imgsz=imgsz, save=True,
                      line_width=line_width, project=str(run), name="yolo_box",
                      exist_ok=True, verbose=False)
        print(f"  {run.name} -> yolo_box/")


def show_sanity(run_name: str | None = None,
                line_width: int = 2, conf: float = 0.25, imgsz: int = 512):
    """Affiche premiere / milieu / derniere image avec bbox YOLO via result.plot()."""
    import matplotlib.pyplot as plt
    from ultralytics import YOLO

    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    if not targets:
        print("[!] aucun run trouve")
        return
    run = targets[0]

    src = pick_image_source(run)
    images = list_images(src)
    if not images:
        print(f"[!] pas d'images dans {src}")
        return

    n = len(images)
    picks = [(images[0], 0), (images[n // 2], n // 2), (images[-1], n - 1)]

    model = YOLO(str(MODEL_PATH))

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for ax, (img_path, idx) in zip(axes, picks):
        result = model.predict(source=str(img_path), conf=conf, imgsz=imgsz,
                               verbose=False)[0]
        annotated = result.plot(line_width=line_width)  # numpy BGR avec bbox
        ax.imshow(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB))
        ax.set_title(f"{img_path.name} ({idx + 1}/{n})")
        ax.axis("off")
    fig.suptitle(f"{run.name} — sanity check YOLO (source: {src.name}/)")
    plt.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# LARD box + sanity
# ---------------------------------------------------------------------------

def build_lard_box(run_name: str | None = None):
    """Genere runs/<run>/lard_box/ (bbox GT LARD dessinees) via annotate_gt()."""
    for run in find_runs(run_name=run_name, all_runs=run_name is None):
        src = pick_image_source(run)
        if not list_images(src):
            print(f"  [skip] {run.name} : pas d'images dans {src.name}/")
            continue
        out = run / "lard_box"
        if out.exists():
            shutil.rmtree(out)
        annotate_gt(run, out_dir=out, prefix="")
        print(f"  {run.name} -> lard_box/")


def show_sanity_lard(run_name: str | None = None, line_width: int = 2):
    """Affiche premiere / milieu / derniere image avec bbox GT LARD (piste cible uniquement)."""
    import matplotlib.pyplot as plt
    from PIL import Image

    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    if not targets:
        print("[!] aucun run trouve")
        return
    run = targets[0]

    src = pick_image_source(run)
    images = list_images(src)
    if not images:
        print(f"[!] pas d'images dans {src}")
        return

    target_rwy = runway_from_run_name(run.name)
    csvs = list(run.glob("*_labels.csv"))
    if not csvs:
        print(f"[!] pas de *_labels.csv dans {run}")
        return
    gt = load_gt_corners(csvs[0], runway=target_rwy)

    n = len(images)
    picks = [(images[0], 0), (images[n // 2], n // 2), (images[-1], n - 1)]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    for ax, (img_path, idx) in zip(axes, picks):
        img = Image.open(img_path)
        ax.imshow(img)
        for corners in gt.get(img_path.name, []):
            xs = [c[0] for c in corners] + [corners[0][0]]
            ys = [c[1] for c in corners] + [corners[0][1]]
            ax.plot(xs, ys, color="cyan", linewidth=line_width)
        ax.set_title(f"{img_path.name} ({idx + 1}/{n})")
        ax.axis("off")
    fig.suptitle(f"{run.name} — sanity check LARD GT (piste {target_rwy}, source: {src.name}/)")
    plt.tight_layout()
    plt.show()


# ---------------------------------------------------------------------------
# xplane_config.json + params_trace.xml
# ---------------------------------------------------------------------------

def build_xplane_config(run_name: str | None = None):
    """Ecrit run_dir/xplane_config.json depuis yaml + weather_profile.json."""
    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    for run in targets:
        yamls = list(run.glob("*.yaml"))
        if not yamls:
            print(f"  [skip] {run.name} : pas de yaml")
            continue
        meta = yaml.safe_load(yamls[0].read_text())
        img = meta.get("image", {})

        weather_status = "absent"
        wp = run / "weather_profile.json"
        if wp.exists():
            weather_status = "ok"

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
        out = run / "xplane_config.json"
        with open(out, "w") as f:
            json.dump(cfg, f, indent=2)
        print(f"  {run.name} -> xplane_config.json ({cfg['width']}x{cfg['height']}, weather={weather_status})")


def build_params_trace(run_name: str | None = None):
    """Ecrit run_dir/params_trace.xml en agregeant les profils JSON."""
    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    for run in targets:
        poses = json.loads((run / "poses_cam_export.json").read_text()) \
            if (run / "poses_cam_export.json").exists() else {}
        weather = (json.loads((run / "weather_profile.json").read_text()).get("weather", {})
                   if (run / "weather_profile.json").exists() else {})
        faults = (json.loads((run / "fault_profile.json").read_text()).get("faults", [])
                  if (run / "fault_profile.json").exists() else [])

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
                if k == "fault_type":
                    continue
                _xml_value(sub, k, v)

        xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
        (run / "params_trace.xml").write_text(xml_str, encoding="utf-8")
        print(f"  {run.name} -> params_trace.xml")


# ---------------------------------------------------------------------------
# Export .esp (Google Earth Studio)
# ---------------------------------------------------------------------------

def build_esp(run_name: str | None = None, fov_vertical: float = 30.0):
    """Genere run_dir/<run>.esp (projet Google Earth Studio) depuis les poses.

    Pont renderer-agnostique -> GES : relit poses_cam_export.json et reconstruit
    le .esp via LARD GEODataset.create_scenario (charge data/template.json,
    normalise chaque canal dans [0,1] via min/maxValueRange, empile les keyframes).
    On ne reimplemente pas le format GES : on reutilise le code LARD.

    Convention pitch : poses_cam_export.json stocke deja le pitch en convention
    GES (90 deg = horizontal), donc transmis tel quel a create_scenario.

    :param run_name: chemin compose '<gen>/<run>' ; None = tous les runs.
    :param fov_vertical: FOV vertical en degres (GES historique LARD = 30).
    """
    from src.geo.geo_dataset import GEODataset  # LARD (sys.path via _paths)

    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    for run in targets:
        poses_file = run / "poses_cam_export.json"
        if not poses_file.exists():
            print(f"  [skip] {run.name} : pas de poses_cam_export.json")
            continue

        data = json.loads(poses_file.read_text())
        poses = data.get("poses", [])
        if not poses:
            print(f"  [skip] {run.name} : poses vides")
            continue

        # poses -> flight_data (lon, lat, alt, yaw, pitch, roll) attendu par LARD.
        flight_data = [
            (p["lon"], p["lat"], p["alt_m"], p["heading"], p["pitch"], p["roll"])
            for p in poses
        ]
        fps = int(data.get("fps", 25))
        n_frames = len(flight_data)
        times = generate_frame_times(n_frames, fps)

        dataset = GEODataset(str(run), run.name)
        with _lard_cwd():  # CWD = LARD_ROOT pour data/template.json
            scenario, _ = dataset.create_scenario(
                flight_data,
                fov_vertical=fov_vertical,
                width=1024, height=1024,
                nb_frames=n_frames, fps=fps,
                times=times,
            )

        out = run / f"{run.name}.esp"
        with open(out, "w") as f:
            json.dump(scenario, f, indent=2)
        print(f"  {run.name} -> {out.name}  ({n_frames} frames @ {fps}fps, fov={fov_vertical} deg)")


# ---------------------------------------------------------------------------
# Video MP4
# ---------------------------------------------------------------------------

def build_video(run_name: str | None = None, source: str | None = None):
    """Genere runs/<run>/<run>.mp4 (fps depuis poses_cam_export.json).

    source : 'degraded' ou 'footage' (defaut: degraded prio sinon footage).
    """
    targets = find_runs(run_name=run_name, all_runs=run_name is None)
    for run in targets:
        src = (run / source) if source else pick_image_source(run)
        images = list_images(src)
        if not images:
            print(f"  [skip] {run.name} : pas d'images dans {src.name}/")
            continue

        fps = 10
        poses_file = run / "poses_cam_export.json"
        if poses_file.exists():
            fps = int(json.loads(poses_file.read_text()).get("fps", 10))

        first = cv2.imread(str(images[0]))
        if first is None:
            print(f"  [skip] {run.name} : impossible de lire {images[0].name}")
            continue
        h, w = first.shape[:2]

        out = run / f"{run.name}.mp4"
        writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        for img_path in images:
            img = cv2.imread(str(img_path))
            if img is not None:
                writer.write(img)
        writer.release()

        print(f"  {run.name} -> {out.name}  ({len(images)} frames @ {fps}fps, {src.name}/)")
