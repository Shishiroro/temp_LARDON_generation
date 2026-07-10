"""
main.py — CLI standalone du banc d'evaluation
=============================================
Point d'entree autonome du sous-projet, independant de run_pipeline.py.

    python -m evaluation evaluate --all --generation generation_01
    python -m evaluation evaluate generation_01/LFPO_24
    python -m evaluation full_evaluate -n 5 --xplane-dir "C:/X-Plane 12"

(run_pipeline.py reste l'orchestrateur top-level et reroute ses modes
'evaluate' / 'full_evaluate' vers evaluation.runner.)
"""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

from runner import evaluate_runs, full_evaluate_pipeline

ROOT = Path(__file__).resolve().parent.parent
_SETTINGS = ROOT / "sources" / "settings.xml"


def _default_xplane_dir():
    try:
        s = {p.attrib["name"]: p.attrib["value"]
             for p in ET.parse(_SETTINGS).getroot()}
        return s.get("xplane_dir", "C:/X-Plane 12")
    except Exception:
        return "C:/X-Plane 12"


def _add_yolo_args(parser):
    parser.add_argument("--conf", type=float, default=0.25, help="Seuil confiance YOLO")
    parser.add_argument("--imgsz", type=int, default=512, help="Taille image YOLO")
    parser.add_argument("--iou-thresh", type=float, default=0.5, help="Seuil IoU")
    parser.add_argument("--iou-method", type=str, default="CIOU",
                        choices=["IOU", "GIOU", "DIOU", "CIOU"])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Banc d'evaluation LARD-LAAS-TAF (SUT vs GT LARD)")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_eval = sub.add_parser("evaluate", help="Evalue un SUT sur des runs existants")
    p_eval.add_argument("run", nargs="?", default=None,
                        help="Run '<generation>/<nom>' ou nom seul si --generation")
    p_eval.add_argument("--all", action="store_true", dest="all_runs",
                        help="Tous les runs d'une generation (requiert --generation)")
    p_eval.add_argument("--generation", type=str, default=None)
    p_eval.add_argument("--runway", type=str, default=None, help="Filtrer GT sur une piste")
    _add_yolo_args(p_eval)

    p_full = sub.add_parser("full_evaluate",
                            help="Generation + rendu + evaluation enchaines")
    p_full.add_argument("-n", "--nb-scenarios", type=int, default=None)
    p_full.add_argument("--name", type=str, default=None)
    p_full.add_argument("--clean", action="store_true")
    p_full.add_argument("--xplane-dir", type=str, default=_default_xplane_dir())
    _add_yolo_args(p_full)

    args = parser.parse_args(argv)

    if args.mode == "evaluate":
        if not args.run and not args.all_runs:
            print("Specifier un run ou --all. Ex: evaluate generation_01/LFPO_24")
            return
        if args.all_runs and not args.generation:
            print("[ERREUR] --all requiert --generation <nom>.")
            return
        evaluate_runs(
            run_name=args.run, all_runs=args.all_runs, generation=args.generation,
            runway=args.runway, conf=args.conf, imgsz=args.imgsz,
            iou_thresh=args.iou_thresh, iou_method=args.iou_method,
        )

    elif args.mode == "full_evaluate":
        full_evaluate_pipeline(
            nb_scenarios=args.nb_scenarios, name=args.name, clean=args.clean,
            xplane_dir=args.xplane_dir, conf=args.conf, imgsz=args.imgsz,
            iou_thresh=args.iou_thresh, iou_method=args.iou_method,
        )


if __name__ == "__main__":
    main()
