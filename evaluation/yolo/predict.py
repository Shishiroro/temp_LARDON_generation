"""
Detection YOLOv8 sur images d'approche.
Genere les predictions (CSV avec bbox) et les images annotees.
"""

import csv
import json
import shutil
import sys
from pathlib import Path
from ultralytics import YOLO

# --- Chemins ---
YOLO_DIR = Path(__file__).resolve().parent       # evaluation/yolo/
WEIGHTS_DIR = YOLO_DIR / "weights"               # poids .pt (plusieurs modeles OOD)
IMAGES_DIR = YOLO_DIR / "test_images" / "test"
OUTPUT_DIR = YOLO_DIR / "output"

# Bootstrap sys.path via sources/_paths.py (evaluation/yolo -> racine -> sources)
_PROJECT_DIR = YOLO_DIR.parent.parent / "sources"
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))
import _paths  # noqa: F401
from runs import list_images, pick_image_source

# Config locale du SUT YOLO (poids choisi). Decouple de sources/settings.xml :
# le banc d'eval possede sa propre config, l'usine ne connait pas le modele.
_CONFIG = json.loads((YOLO_DIR / "yolo.json").read_text())
MODEL_PATH = WEIGHTS_DIR / _CONFIG["model"]



def _next_exp_dir() -> Path:
    """Trouve le prochain dossier exp<N> disponible."""
    i = 1
    while (OUTPUT_DIR / f"exp{i}").exists():
        i += 1
    exp_dir = OUTPUT_DIR / f"exp{i}"
    exp_dir.mkdir(parents=True, exist_ok=True)
    return exp_dir


def _txt_to_csv(labels_dir: Path, csv_path: Path, txt_dst: Path | None = None) -> int:
    """Consolide les .txt YOLO en un seul predictions.csv.

    Si txt_dst est fourni, deplace les .txt dans ce dossier; sinon les supprime.

    Returns:
        Nombre de detections ecrites.
    """
    txt_files = sorted(labels_dir.glob("*.txt"))
    n_rows = 0

    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["image", "class", "cx", "cy", "w", "h", "confidence"])
        for txt in txt_files:
            image_name = txt.stem
            for line in txt.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.strip().split()
                writer.writerow([image_name, int(parts[0]),
                                 parts[1], parts[2], parts[3], parts[4], parts[5]])
                n_rows += 1

    if txt_dst is not None:
        txt_dst.mkdir(parents=True, exist_ok=True)
        for txt in txt_files:
            shutil.move(str(txt), str(txt_dst / txt.name))
    else:
        for txt in txt_files:
            txt.unlink()

    return n_rows


def predict(start: int = 0, n_images: int | None = None, conf: float = 0.25, imgsz: int = 512,
            images_dir: Path | None = None, output_dir: Path | None = None):
    """Lance la prediction YOLOv8 sur les images d'approche.

    Args:
        start: Index de la premiere image (defaut: 0).
        n_images: Nombre d'images a traiter (None = toutes a partir de start).
        conf: Seuil de confiance.
        imgsz: Taille d'image pour l'inference.
        images_dir: Dossier des images (defaut: test_images/test/).
        output_dir: Dossier de sortie (defaut: yolo/output/expN/).
            predictions.csv + predictions_txt/ vont dans output_dir/.

    Returns:
        Path du predictions.csv (ou None si pas d'images).
    """
    src = images_dir or IMAGES_DIR
    images = list_images(src)
    end = start + n_images if n_images is not None else None
    images = images[start:end]

    if not images:
        print(f"Aucune image trouvee dans {src}")
        return None

    out = Path(output_dir) if output_dir is not None else _next_exp_dir()
    out.mkdir(parents=True, exist_ok=True)

    print(f"Prediction sur {len(images)} images avec {MODEL_PATH.name}")

    model = YOLO(str(MODEL_PATH))

    # Passer le chemin du dossier conserve les noms d'origine des images ;
    # passer une liste de chemins force ultralytics a renommer en image0,
    # image1... ce qui casse la correspondance avec les CSV GT.
    all_in_dir = (start == 0 and n_images is None)
    source = str(src) if all_in_dir else [str(img) for img in images]

    # Workspace temporaire pour ultralytics : on ne garde que les .txt labels.
    # Les bbox annotees sont produites a la demande depuis notebook/features.ipynb
    # (build_yolo_box / show_sanity) — pas besoin d'images annotees ici.
    yolo_tmp = (out / "_yolo_tmp").resolve()
    model.predict(
        source=source,
        imgsz=imgsz,
        conf=conf,
        save_txt=True,
        save_conf=True,
        save=False,
        project=str(yolo_tmp.parent),
        name=yolo_tmp.name,
        exist_ok=True,
    )

    yolo_labels = yolo_tmp / "labels"
    predictions_csv = out / "predictions.csv"
    predictions_dir = out / "predictions_txt"

    if yolo_labels.exists():
        n_dets = _txt_to_csv(yolo_labels, predictions_csv, txt_dst=predictions_dir)
        print(f"Predictions : {n_dets} detections dans {predictions_csv.name} (+ {predictions_dir.name}/)")
    else:
        _txt_to_csv(yolo_tmp, predictions_csv)
        print(f"Predictions : 0 detections")

    if yolo_tmp.exists():
        shutil.rmtree(yolo_tmp)

    return predictions_csv


def predict_run(run_dir, conf: float = 0.25, imgsz: int = 512, output_dir=None):
    """Lance YOLO sur un run.

    Utilise run_dir/degraded/ si present (fautes capteur deja appliquees),
    sinon run_dir/footage/. Sortie : output_dir/predictions.csv +
    output_dir/predictions_txt/ (output_dir defaut = run_dir).

    :param output_dir: dossier de sortie des predictions (namespacing par SUT,
        ex run_dir/eval/yolo/). Defaut : run_dir.
    :return: Path du predictions.csv genere (ou None si pas d'images)
    """
    run_dir = Path(run_dir)
    images_dir = pick_image_source(run_dir)
    n_images = len(list_images(images_dir))
    print(f"\n  [YOLO] Prediction sur {n_images} images depuis {images_dir.name}/ ({run_dir.name})...")

    predictions_csv = predict(
        images_dir=images_dir,
        conf=conf,
        imgsz=imgsz,
        output_dir=output_dir if output_dir is not None else run_dir,
    )

    if predictions_csv and predictions_csv.exists():
        print(f"  [YOLO] Predictions dans {predictions_csv.name}")
    else:
        print(f"  [YOLO] ATTENTION : pas de predictions generees")

    return predictions_csv


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="YOLOv8 prediction sur images d'approche")
    parser.add_argument("-s", "--start", type=int, default=0, help="Index de depart (defaut: 0)")
    parser.add_argument("-n", "--n-images", type=int, default=None, help="Nombre d'images (defaut: toutes)")
    parser.add_argument("--conf", type=float, default=0.25, help="Seuil de confiance (defaut: 0.25)")
    parser.add_argument("--imgsz", type=int, default=512, help="Taille image (defaut: 512)")
    parser.add_argument("--images", type=str, default=None, help="Dossier images (defaut: test_images/test/)")
    parser.add_argument("--output", type=str, default=None, help="Dossier de sortie (defaut: yolo/output/expN/)")
    args = parser.parse_args()

    predict(
        start=args.start, n_images=args.n_images, conf=args.conf, imgsz=args.imgsz,
        images_dir=Path(args.images) if args.images else None,
        output_dir=Path(args.output) if args.output else None,
    )
