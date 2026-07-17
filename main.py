"""
main.py — CLI orchestrateur LARDON (usine de generation/rendu)
==============================================================
Entry point CLI minimal, sans logique propre. Les deux phases vivent dans :
  - Phase 1 (generate) : sources/taf_generate.generate_scenarios
  - Phase 2 (render)   : sources/render.render_scenarios

Le mode 'full' n'est que l'enchainement des deux : il ne merite pas de module
d'orchestration dedie.

L'evaluation (YOLO/IoU) a ete extraite dans un projet separe : ce depot ne
contient plus que l'usine a donnees (generation + rendu + verite terrain).

Modes :
    python main.py generate -n 5
    python main.py generate -n 100 --name pluie --clean
    python main.py render <batch>/<scenario> --xplane-dir "C:/X-Plane 12"
    python main.py render --all --batch pluie__20260714-153012 --simulator xplane
    python main.py full -n 100 --name pluie --xplane-dir "C:/X-Plane 12"
"""

import sys
import argparse
from pathlib import Path

# sources/ sur sys.path pour les imports plats du projet
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sources"))

from config import XPLANE_DIR
from taf_generate import generate_scenarios
from scenario import SUPPORTED_SIMULATORS
from render import render_scenarios


def _add_generate_args(parser):
    """Args de generation TAF partages par 'generate' et 'full'."""
    parser.add_argument("-n", "--nb-scenarios", type=int, default=None,
                        help="Nombre de scenarios (surcharge settings.xml)")
    parser.add_argument("--name", type=str, default=None,
                        help="Nom du batch (cree output/scenarios/<name>__<timestamp>/ "
                             "au lieu de output/scenarios/default__<timestamp>/)")
    parser.add_argument("--clean", action="store_true",
                        help="Vider output/scenarios/ avant la generation")
    parser.add_argument("--runway", type=str, default=None,
                        help="Forcer toutes les generations sur une piste "
                             "(format ICAO_RWY, ex LFPO_24)")


def _add_render_args(parser):
    """Args de ciblage des scenarios pour 'render'."""
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
        description="Pipeline generation/rendu LARDON (X-Plane 12 / GES)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Structure de sortie (racine configurable : cle 'output_dir' de paths.local.json) :
  output/scenarios/<batch>/<scenario_name>/ <- 'generate' (Phase 1)
    <scenario_name>.yaml                    <- scenario LARD (TAF)
    <scenario_name>.json                    <- poses camera
    <scenario_name>.esp                     <- projet GES (best-effort)
    [fault_profile.json / weather_profile.json]

  output/data/<simulator>/                                <- 'render' (Phase 2)
    metadata.csv                            <- GT consolidee (tous scenarios du simulateur)
    <airport_runway>/<scenario_name>/
      footage/                              <- rendu simulateur (nom impose par LARD)
      corrupted_footage/                    <- images + fautes capteur
      metadata.csv                          <- verite terrain LARD (ce scenario)

  output/taf/                               <- sas TAF, jetable (vide a chaque generate)

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
                           help="Phase 1 : genere les scenarios TAF dans output/scenarios/")
    _add_generate_args(p_gen)

    p_render = sub.add_parser("render", parents=[xplane_args],
                              help="Phase 2 : rendu + fautes capteur + GT dans output/data/")
    _add_render_args(p_render)
    _add_simulator_arg(p_render)

    p_full = sub.add_parser("full", parents=[xplane_args],
                            help="Phase 1 + 2 enchainees (generation + rendu)")
    _add_generate_args(p_full)
    _add_simulator_arg(p_full)

    args = parser.parse_args()

    if args.mode == "generate":
        created = generate_scenarios(nb_scenarios=args.nb_scenarios,
                                     name=args.name, clean=args.clean,
                                     runway=args.runway)
        if created:
            batch = created[0].parent.name
            print(f"  Prochaine etape : main.py render --all --batch {batch}")

    elif args.mode == "render":
        if not args.scenario and not args.all_scenarios:
            print("Specifier un scenario ou --all. Ex: render <batch>/<scenario> "
                  "ou render --all --batch <batch>")
            return
        if args.all_scenarios and not args.batch:
            print("[ERREUR] --all requiert --batch <nom>. "
                  "Ex: render --all --batch pluie__20260714-153012")
            return
        render_scenarios(
            name=args.scenario, all_scenarios=args.all_scenarios,
            batch=args.batch, simulator=args.simulator,
            xplane_dir=args.xplane_dir,
        )

    elif args.mode == "full":
        # Les deux phases enchainees, sans module d'orchestration : le batch est
        # horodate, donc cibler --batch ne rend que les scenarios qu'on vient de creer.
        created = generate_scenarios(nb_scenarios=args.nb_scenarios,
                                     name=args.name, clean=args.clean,
                                     runway=args.runway)
        if not created:
            print("[Main] Aucun scenario genere, arret.")
            return
        render_scenarios(
            all_scenarios=True, batch=created[0].parent.name,
            simulator=args.simulator, xplane_dir=args.xplane_dir,
        )


if __name__ == "__main__":
    main()
