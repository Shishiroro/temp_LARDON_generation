"""
yolo_sut.py — SUT YOLOv8 (1er systeme sous test)
================================================
Implemente l'interface evaluation.sut.SUT pour le detecteur YOLOv8 :
  - infer    : YOLO predict sur les images du run -> eval/yolo/predictions.csv
  - evaluate : IoU predictions vs GT LARD (*_labels.csv) -> dict metriques

S'appuie sur les modules predict.py / evaluate.py voisins (deplaces depuis
l'ancien yolo/). Reprend la logique de l'ex sources/Detection_Evaluation.py.
"""

from pathlib import Path

from sut import SUT


class YoloSUT(SUT):
    """Detecteur de piste YOLOv8 embarque (inference hors ligne)."""

    name = "yolo"

    def infer(self, run_dir, conf=0.25, imgsz=512, **_):
        """Lance YOLO sur les images du run, ecrit dans eval/yolo/."""
        from predict import predict_run

        out_dir = self.eval_dir(run_dir)
        try:
            return predict_run(run_dir, conf=conf, imgsz=imgsz, output_dir=out_dir)
        except Exception as e:
            print(f"  [Eval] YOLO ERREUR : {e}")
            return None

    def evaluate(self, run_dir, runway=None, iou_thresh=0.5, iou_method="CIOU", **_):
        """Calcule l'IoU des predictions YOLO vs la GT LARD du run."""
        from evaluate import evaluate_run

        run_dir = Path(run_dir)
        predictions_csv = self.eval_dir(run_dir) / "predictions.csv"
        if not predictions_csv.exists():
            print(f"  [Eval] Pas de predictions pour {run_dir.name}")
            return None

        try:
            return evaluate_run(
                run_dir, runway=runway,
                iou_thresh=iou_thresh, iou_method=iou_method,
                predictions_csv=predictions_csv,
            )
        except Exception as e:
            print(f"  [Eval] IoU ERREUR : {e}")
            return None
