"""
main.py — CLI orchestrateur LARD-LAAS-TAF (usine de generation/export)
======================================================================
Entry point CLI minimal. Toute la logique vit dans :
  - Phase 1 (generate) : sources/taf_generate.generate_runs
  - Phase 2 (export)   : sources/taf_export.render_scenario_run
  - Modes batch usine  : sources/scenario.render_scenarios / full_pipeline

L'evaluation (YOLO/IoU) a ete extraite dans un projet separe : ce depot ne
contient plus que l'usine a donnees (generation + rendu + verite terrain).

Modes :
    python main.py generate -n 5
    python main.py generate -n 100 --name pluie --clean
    python main.py export <batch>/<scenario> --xplane-dir "C:/X-Plane 12"
    python main.py export --all --batch pluie__20260714-153012 --simulator xplane
    python main.py full -n 100 --name pluie --xplane-dir "C:/X-Plane 12"
"""

import sys
import argparse
from pathlib import Path

# sources/ sur sys.path pour les imports plats du projet
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sources"))

from config import XPLANE_DIR
from taf_generate import generate_runs
from scenario import SUPPORTED_SIMULATORS
from pipeline import render_scenarios, full_pipeline


def _add_generate_args(parser):
    """Args de generation TAF partages par 'generate' et 'full'."""
    parser.add_argument("-n", "--nb-scenarios", type=int, default=None,
                        help="Nombre de scenarios (surcharge settings.xml)")
    parser.add_argument("--name", type=str, default=None,
                        help="Nom du batch (cree scenarios/<name>__<timestamp>/ "
                             "au lieu de scenarios/default__<timestamp>/)")
    parser.add_argument("--clean", action="store_true",
                        help="Vider scenarios/ avant la generation")
    parser.add_argument("--runway", type=str, default=None,
                        help="Forcer toutes les generations sur une piste "
                             "(format ICAO_RWY, ex LFPO_24)")


def _add_export_args(parser):
    """Args de ciblage des scenarios pour 'export'."""
    parser.add_argument("scenario", nargs="?", default=None,
                        help="Scenario a rendre, format '<batch>/<scenario_name>' "
                             "ou nom seul si --batch est fourni")
    parser.add_argument("--all", action="store_true", dest="all_scenarios",
                        help="Traiter tous les scenarios d'un batch (requiert --batch)")
    parser.add_argument("--batch", type=str, default=None,
                        help="Cible un batch existant (ex: pluie__20260714-153012)")


def _add_simulator_arg(parser):
    parser.add_argument("--simulator", type=str, default="xplane",
                        choices=list(SUPPORTED_SIMULATORS),
                        help="Simulateur de rendu (defaut: xplane ; GES = rendu externe)")


def main():
    parser = argparse.ArgumentParser(
        description="Pipeline generation/export LARD-LAAS-TAF (X-Plane 12 / GES)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Structure de sortie :
  scenarios/<batch>/<scenario_name>/        <- 'generate' (Phase 1)
    <scenario_name>.yaml                    <- scenario LARD (TAF)
    <scenario_name>.json                    <- poses camera
    <scenario_name>.esp                     <- projet GES (best-effort)
    [fault_profile.json / weather_profile.json]

  dataset/<simulator>/<airport_runway>/<scenario_name>/   <- 'export' (Phase 2)
    images/                                 <- rendu simulateur
    corrupted_images/                       <- images + fautes capteur
    metadata.csv                            <- verite terrain LARD (GT)

  <batch>          = <default|nom>__<timestamp>
  <scenario_name>  = <airport>-<runway>__<nb_smpl>-smpl__<timestamp>__<indx>
  <airport_runway> = <airport>_<runway>   (ex: KPDX_10L)
        """,
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    xplane_args = argparse.ArgumentParser(add_help=False)
    xplane_args.add_argument("--xplane-dir", type=str, default=XPLANE_DIR,
                             help=f"Repertoire X-Plane 12 (defaut: {XPLANE_DIR or '(non defini)'})")

    p_gen = sub.add_parser("generate",
                           help="Phase 1 : genere les scenarios TAF dans scenarios/")
    _add_generate_args(p_gen)

    p_export = sub.add_parser("export", parents=[xplane_args],
                              help="Phase 2 : rendu + fautes capteur + GT")
    _add_export_args(p_export)
    _add_simulator_arg(p_export)

    p_full = sub.add_parser("full", parents=[xplane_args],
                            help="Phase 1 + 2 enchainees (generation + rendu)")
    _add_generate_args(p_full)
    _add_simulator_arg(p_full)

    args = parser.parse_args()

    if args.mode == "generate":
        created = generate_runs(nb_scenarios=args.nb_scenarios,
                                name=args.name, clean=args.clean,
                                runway=args.runway)
        if created:
            batch = created[0].parent.name
            print(f"  Prochaine etape : main.py export --all --batch {batch}")

    elif args.mode == "export":
        if not args.scenario and not args.all_scenarios:
            print("Specifier un scenario ou --all. Ex: export <batch>/<scenario> "
                  "ou export --all --batch <batch>")
            return
        if args.all_scenarios and not args.batch:
            print("[ERREUR] --all requiert --batch <nom>. "
                  "Ex: export --all --batch pluie__20260714-153012")
            return
        render_scenarios(
            name=args.scenario, all_scenarios=args.all_scenarios,
            batch=args.batch, simulator=args.simulator,
            xplane_dir=args.xplane_dir,
        )

    elif args.mode == "full":
        full_pipeline(
            nb_scenarios=args.nb_scenarios,
            name=args.name, clean=args.clean, runway=args.runway,
            simulator=args.simulator, xplane_dir=args.xplane_dir,
        )


if __name__ == "__main__":
    main()
