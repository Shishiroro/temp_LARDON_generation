"""
Evaluation IoU : compare les predictions YOLO (predictions.csv) aux ground truths LARD (.csv).
Calcule AP, F1, Precision, Recall via le module evaluation/yolo/eval/.
"""

import sys
from pathlib import Path
import numpy as np
import pandas as pd
import torch

# Bootstrap sys.path via sources/_paths.py (evaluation/yolo -> racine -> sources)
_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent / "sources"
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))
import _paths  # noqa: F401

from eval.box import box_extract
from eval.metrics_utils import bbox_convert
from eval.metrics import compute_metrics
from runway import reciprocal_runway, runway_from_run_name


# --- Colonnes GT LARD (NEW_CORNERS_NAMES = TR, TL, BL, BR) ---
CORNER_X_COLS = ["x_TR", "x_TL", "x_BL", "x_BR"]
CORNER_Y_COLS = ["y_TR", "y_TL", "y_BL", "y_BR"]


def load_predictions(csv_path: Path) -> tuple[torch.Tensor, list[str]]:
    """Charge predictions.csv et retourne (tenseur (N, 7), image_names).

    Tenseur : [img_id, cls, x1, y1, x2, y2, conf] en xyxy normalise.
    image_names : liste ordonnee des noms d'images (stems uniques).
    """
    df = pd.read_csv(csv_path, sep=";")

    if df.empty:
        return torch.zeros((0, 7)), []

    # Mapping nom image -> img_id (ordre d'apparition trie)
    image_names = sorted(df["image"].unique())
    name_to_id = {name: i for i, name in enumerate(image_names)}

    rows = []
    for _, row in df.iterrows():
        img_id = name_to_id[row["image"]]
        rows.append([img_id, int(row["class"]),
                     float(row["cx"]), float(row["cy"]),
                     float(row["w"]), float(row["h"]),
                     float(row["confidence"])])

    preds = torch.tensor(rows, dtype=torch.float32)
    # Convertir bbox cxcywh -> xyxy (colonnes 2:6)
    preds[:, 2:6] = bbox_convert(preds[:, 2:6], "cxcywh", "xyxy")
    return preds, image_names


def load_ground_truths(csv_path: Path, image_names: list[str], runway: str | None = None) -> torch.Tensor:
    """Charge le .csv LARD et retourne un tenseur (M, 6) [img_id, cls, x1, y1, x2, y2].

    Les 4 coins piste (pixels) sont convertis en bbox englobante xyxy normalise.
    Le mapping image_names[i] -> img_id=i assure la correspondance avec les predictions.
    Si runway est specifie, ne garde que les GT de cette piste.
    """
    df = pd.read_csv(csv_path, sep=";")
    if runway is not None:
        # Une piste physique a deux noms (sens opposes). LARD libelle ses GT
        # avec le nom du seuil oppose a l'approche : si on atterrit en 10L,
        # le CSV ecrit "28L". On garde les deux pour matcher quel que soit
        # le sens choisi en argument.
        recip = reciprocal_runway(str(runway))
        df = df[df["runway"].astype(str).isin([str(runway), recip])]
    rows = []

    # Creer un mapping nom_image -> img_id
    name_to_id = {name: i for i, name in enumerate(image_names)}

    for _, row in df.iterrows():
        # Extraire le nom du fichier image depuis la colonne 'image'
        img_name = Path(str(row["image"])).stem
        if img_name not in name_to_id:
            continue

        img_id = name_to_id[img_name]
        img_w = float(row["width"])
        img_h = float(row["height"])

        # 4 coins piste en pixels -> bbox englobante xyxy
        xs = np.array([row[c] for c in CORNER_X_COLS], dtype=np.float64)
        ys = np.array([row[c] for c in CORNER_Y_COLS], dtype=np.float64)
        bbox_px = box_extract(xs, ys)  # [x_min, y_min, x_max, y_max] pixels

        bbox_norm = np.clip(np.array([
            bbox_px[0] / img_w,
            bbox_px[1] / img_h,
            bbox_px[2] / img_w,
            bbox_px[3] / img_h,
        ]), 0.0, 1.0)

        rows.append([img_id, 0, *bbox_norm])  # cls_id=0 (piste)

    if not rows:
        return torch.zeros((0, 6))

    return torch.tensor(rows, dtype=torch.float32)


def evaluate(predictions_csv: Path, csv_path: Path, iou_thresh: float = 0.5, iou_method: str = "CIOU",
             runway: str | None = None):
    """Lance l'evaluation IoU et affiche les metriques.

    Args:
        predictions_csv: Fichier predictions.csv (genere par predict.py).
        csv_path: Fichier .csv ground truth LARD.
        iou_thresh: Seuil IoU.
        iou_method: Methode IoU (IOU/GIOU/DIOU/CIOU).
        runway: Filtrer GT sur une piste.
    """

    if not predictions_csv.exists():
        print(f"Fichier predictions introuvable : {predictions_csv}")
        return

    # Charger donnees
    preds, image_names = load_predictions(predictions_csv)

    if not image_names:
        print(f"Aucune prediction dans {predictions_csv.name}")
        return

    gts = load_ground_truths(csv_path, image_names, runway=runway)

    rwy_info = f" (runway {runway})" if runway else " (toutes pistes)"
    print(f"Predictions : {preds.shape[0]} detections sur {len(image_names)} images")
    print(f"Ground truths : {gts.shape[0]} bbox depuis {csv_path.name}{rwy_info}")

    if gts.shape[0] == 0:
        print("Aucun ground truth trouve (verifier correspondance noms images)")
        return

    # Evaluer
    metrics = compute_metrics(
        y_pred=preds,
        y_true=gts,
        iou_thresh=iou_thresh,
        iou_method=iou_method,
        box_format="xyxy",
    )

    # Afficher
    print(f"\n--- Resultats (IoU seuil={iou_thresh}, methode={iou_method}) ---")
    print(f"  AP       : {metrics['ap']:.4f}")
    print(f"  F1       : {metrics['f1']:.4f}")
    print(f"  Precision: {metrics['p']:.4f}")
    print(f"  Recall   : {metrics['r']:.4f}")
    print(f"  Confiance: {metrics['c']:.4f}")
    print(f"  TP={metrics['tp']}, FP={metrics['fp']}, FN={metrics['fn']}")

    return metrics


def evaluate_run(run_dir, runway: str | None = None,
                 iou_thresh: float = 0.5, iou_method: str = "CIOU",
                 predictions_csv: Path | None = None):
    """Evalue IoU sur un run.

    Cherche les predictions (predictions_csv, defaut run_dir/predictions.csv)
    et la GT run_dir/<stem>_labels.csv (toujours a la racine du run).
    Le runway est extrait du nom du run (ex: LFPG_09L_002 -> 09L) si non fourni.

    :param predictions_csv: chemin des predictions (namespacing par SUT,
        ex run_dir/eval/yolo/predictions.csv). Defaut : run_dir/predictions.csv.
    :return: dict de metriques (ou None si erreur/pas de donnees)
    """
    run_dir = Path(run_dir)
    if predictions_csv is None:
        predictions_csv = run_dir / "predictions.csv"

    csv_candidates = list(run_dir.glob("*_labels.csv"))
    if not csv_candidates:
        raise FileNotFoundError(f"Pas de *_labels.csv dans {run_dir}")
    csv_path = csv_candidates[0]

    rwy = runway if runway is not None else runway_from_run_name(run_dir.name)

    print(f"\n  [EVAL] IoU evaluation...")
    return evaluate(
        predictions_csv=predictions_csv,
        csv_path=csv_path,
        iou_thresh=iou_thresh,
        iou_method=iou_method,
        runway=rwy,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Evaluation IoU predictions YOLO vs GT LARD")
    parser.add_argument("--predictions", type=str, required=True, help="Fichier predictions.csv")
    parser.add_argument("--csv", type=str, required=True, help="Fichier .csv ground truth LARD")
    parser.add_argument("--iou-thresh", type=float, default=0.5, help="Seuil IoU (defaut: 0.5)")
    parser.add_argument("--iou-method", type=str, default="CIOU", choices=["IOU", "GIOU", "DIOU", "CIOU"], help="Methode IoU (defaut: CIOU)")
    parser.add_argument("--runway", type=str, default=None, help="Filtrer GT sur une piste (ex: 24). Sans filtre = toutes les pistes.")
    args = parser.parse_args()

    evaluate(
        predictions_csv=Path(args.predictions),
        csv_path=Path(args.csv),
        iou_thresh=args.iou_thresh,
        iou_method=args.iou_method,
        runway=args.runway,
    )
