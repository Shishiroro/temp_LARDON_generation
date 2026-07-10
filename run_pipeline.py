"""
run_pipeline.py — CLI orchestrateur LARD-LAAS-TAF
==================================================
Entry point CLI minimal. Toute la logique vit dans :
  - Phase 1            : sources/Generate.generate_runs
  - Phase 2            : sources/export/Export.render_run
  - Phase 3 (eval)     : evaluation/ (banc d'eval + interface SUT)
  - Modes batch usine  : sources/runs.render_runs / full_pipeline
  - Modes batch eval   : evaluation.runner.evaluate_runs / full_evaluate_pipeline

Modes :
    python run_pipeline.py generate -n 5
    python run_pipeline.py generate -n 100 --name pluie --clean
    python run_pipeline.py export generation_01/LFPO_24 --xplane-dir "C:/X-Plane 12"
    python run_pipeline.py export --all --generation generation_01 --xplane-dir "..."
    python run_pipeline.py evaluate generation_01/LFPO_24
    python run_pipeline.py evaluate --all --generation pluie_01
    python run_pipeline.py full -n 100 --name pluie --xplane-dir "C:/X-Plane 12"
    python run_pipeline.py full_evaluate -n 100 --name pluie --xplane-dir "C:/X-Plane 12"
"""

import sys
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path

# Bootstrap sys.path via sources/_paths.py
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sources"))
import _paths  # noqa: F401

from Generate import generate_runs
from runs import render_runs, full_pipeline               # usine (Phase 1/2)
from evaluation.runner import evaluate_runs, full_evaluate_pipeline  # banc d'eval (Phase 3)


def _add_generate_args(parser):
    """Ajoute les args de generation TAF partages par 'generate' et 'full'."""
    parser.add_argument("-n", "--nb-scenarios", type=int, default=None,
                        help="Nombre de scenarios (surcharge settings.xml)")
    parser.add_argument("--name", type=str, default=None,
                        help="Nom de la generation (cree runs/<name>_NN/ "
                             "au lieu de runs/generation_NN/)")
    parser.add_argument("--clean", action="store_true",
                        help="Vider runs/ avant la generation")
    parser.add_argument("--runway", type=str, default=None,
                        help="Forcer toutes les generations sur une piste "
                             "(format ICAO_RWY, ex LFPO_24)")


def _add_target_args(parser):
    """Ajoute les args de ciblage runs partages par 'render' et 'evaluate'."""
    parser.add_argument("run", nargs="?", default=None,
                        help="Run a traiter, format '<generation>/<nom>' "
                             "(ex: generation_01/LFPO_24) ou nom seul "
                             "si --generation est fourni")
    parser.add_argument("--all", action="store_true", dest="all_runs",
                        help="Traiter tous les runs d'une generation "
                             "(requiert --generation)")
    parser.add_argument("--generation", type=str, default=None,
                        help="Cible une generation existante "
                             "(ex: generation_01, pluie_03)")


def _add_yolo_args(parser):
    """Ajoute les args YOLO/IoU partages par les modes 'evaluate' et 'full'."""
    parser.add_argument("--conf", type=float, default=0.25,
                        help="Seuil confiance YOLO")
    parser.add_argument("--imgsz", type=int, default=512,
                        help="Taille image YOLO")
    parser.add_argument("--iou-thresh", type=float, default=0.5,
                        help="Seuil IoU")
    parser.add_argument("--iou-method", type=str, default="CIOU",
                        choices=["IOU", "GIOU", "DIOU", "CIOU"])


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline bout-en-bout LARD-LAAS-TAF (X-Plane 12)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Structure runs/ :
  runs/
    generation_01/                  <- un dossier par batch (--name pour personnaliser)
      LFPO_24/
        LFPO_24.yaml                <- genere par 'generate' (Phase 1)
        poses_cam_export.json       <- poses camera + trajectory_config TAF
        fault_profile.json          <- profil fautes capteur (si actif)
        weather_profile.json        <- profil meteo X-Plane (si actif)
        footage/                    <- images rendu X-Plane (Phase 2)
        degraded/                   <- images avec fautes capteur (Phase 2)
        LFPO_24_labels.csv          <- GT LARD (Phase 2)
        predictions.csv             <- predictions YOLO (Phase 3)
        predictions_txt/            <- labels YOLO (.txt, Phase 3)
      pipeline_report.json          <- rapport agrege (metriques IoU par run)
    pluie_01/                       <- generations nommees (via --name pluie)
      ...

  Visualisations on-demand via notebook/features.ipynb : yolo_box/, lard_box/,
  xplane_config.json, params_trace.xml.
        """,
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    xplane_args = argparse.ArgumentParser(add_help=False)
    _settings = {p.attrib["name"]: p.attrib["value"]
                 for p in ET.parse(ROOT / "sources" / "settings.xml").getroot()}
    _default_xp = _settings["xplane_dir"]
    xplane_args.add_argument("--xplane-dir", type=str, default=_default_xp,
                             help=f"Repertoire X-Plane 12 (defaut: {_default_xp})")

    p_gen = sub.add_parser("generate",
                           help="Phase 1 : genere les scenarios TAF (.yaml) dans runs/")
    _add_generate_args(p_gen)

    p_export = sub.add_parser("export", parents=[xplane_args],
                              help="Phase 2 : rendu X-Plane + fautes capteur")
    _add_target_args(p_export)

    p_eval = sub.add_parser("evaluate",
                            help="Phase 3 : GT LARD + Detection YOLO + IoU")
    _add_target_args(p_eval)
    p_eval.add_argument("--runway", type=str, default=None,
                        help="Filtrer GT sur une piste")
    _add_yolo_args(p_eval)

    p_full = sub.add_parser("full", parents=[xplane_args],
                            help="Phase 1 + 2 enchainees (generation + rendu)")
    _add_generate_args(p_full)

    p_full_eval = sub.add_parser("full_evaluate", parents=[xplane_args],
                                 help="Phase 1 + 2 + 3 (generation + rendu + YOLO/IoU)")
    _add_generate_args(p_full_eval)
    _add_yolo_args(p_full_eval)

    args = parser.parse_args()

    if args.mode == "generate":
        created = generate_runs(nb_scenarios=args.nb_scenarios,
                                name=args.name, clean=args.clean,
                                runway=args.runway)
        if created:
            gen = created[0].parent.name
            print(f"  Prochaine etape : run_pipeline.py export --all --generation {gen}")

    elif args.mode == "export":
        if not args.run and not args.all_runs:
            print("Specifier un run ou --all. Ex: export generation_01/LFPO_24 "
                  "ou export --all --generation generation_01")
            return
        if args.all_runs and not args.generation:
            print("[ERREUR] --all requiert --generation <nom>. "
                  "Ex: export --all --generation generation_01")
            return
        render_runs(
            run_name=args.run, all_runs=args.all_runs,
            generation=args.generation, xplane_dir=args.xplane_dir,
        )

    elif args.mode == "evaluate":
        if not args.run and not args.all_runs:
            print("Specifier un run ou --all. Ex: evaluate generation_01/LFPO_24 "
                  "ou evaluate --all --generation generation_01")
            return
        if args.all_runs and not args.generation:
            print("[ERREUR] --all requiert --generation <nom>. "
                  "Ex: evaluate --all --generation generation_01")
            return
        evaluate_runs(
            run_name=args.run, all_runs=args.all_runs,
            generation=args.generation, runway=args.runway,
            conf=args.conf, imgsz=args.imgsz,
            iou_thresh=args.iou_thresh, iou_method=args.iou_method,
        )

    elif args.mode == "full":
        full_pipeline(
            nb_scenarios=args.nb_scenarios,
            name=args.name, clean=args.clean, runway=args.runway,
            xplane_dir=args.xplane_dir,
        )

    elif args.mode == "full_evaluate":
        full_evaluate_pipeline(
            nb_scenarios=args.nb_scenarios,
            name=args.name, clean=args.clean, runway=args.runway,
            conf=args.conf, imgsz=args.imgsz,
            iou_thresh=args.iou_thresh, iou_method=args.iou_method,
            xplane_dir=args.xplane_dir,
        )


if __name__ == "__main__":
    main()
