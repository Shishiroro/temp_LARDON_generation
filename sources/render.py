"""
render.py — Phase 2 : rendu simulateur + fautes capteur + verite terrain
=======================================================================
Produit le dataset a partir des scenarios generes par la Phase 1 :

    output/scenarios/<batch>/<scenario_name>/   (entree, cf taf_generate)
        -> output/data/<simulator>/<airport_runway>/<scenario_name>/
               footage/            rendu simulateur (nom impose par LARD, cf
                                   scenario.RENDER_DIRNAME — ne pas renommer)
               corrupted_footage/  images + fautes capteur
               labels_lard.csv     GT LARD brute (toutes pistes visibles)
               metadata.csv        GT enrichie (1 ligne / image, piste cible)

Deux points d'entree :
  - render_scenario_run : un scenario (rendu -> fautes -> GT)
  - render_scenarios    : mode 'render' du CLI, sur les scenarios filtres

TAF n'appelle JAMAIS ce module : son point d'extension est taf_export.export(),
qui ne fait que la Phase 1. Les deux ne partagent aucun symbole.
"""

from pathlib import Path

from lard_bridge import generate_gt_csv
from metadata import build_metadata_csv, build_simulator_metadata
from scenario import (
    CORRUPTED_DIRNAME,
    RENDER_DIRNAME,
    dataset_scenario_dir,
    display_path,
    find_scenarios,
    list_images,
)
from sensor_faults import apply_faults
from xplane_bridge import render_xplane_scenario
from xplane_weather import reset_if_active


# ===========================================================================
# Etapes, par scenario
# ===========================================================================

def step_render_xplane(scenario_dir, images_dir, xplane_dir):
    """Rendu X-Plane 12 du scenario -> images_dir (meteo injectee si profil).

    :return: True si images presentes apres rendu, False sinon
    """
    scenario_dir = Path(scenario_dir)
    name = scenario_dir.name
    poses_json = scenario_dir / f"{name}.json"
    weather_json = scenario_dir / "weather_profile.json"

    ok = render_xplane_scenario(
        poses_json, images_dir, xplane_dir or "",
        weather_json=weather_json if weather_json.exists() else None,
    )
    if not ok:
        print(f"  [Image] Echec rendu X-Plane pour {name}")
        return False
    if not list_images(images_dir):
        print(f"  [Image] Pas d'images dans {RENDER_DIRNAME}/ pour {name}")
        return False
    return True


def step_faults(images_dir, corrupted_dir, scenario_dir):
    """Applique les fautes capteur si fault_profile.json present (no-op sinon).

    images_dir -> corrupted_dir. Les exceptions sont logees mais ne bloquent
    pas le pipeline (les fautes sont optionnelles).
    """
    fault_json = Path(scenario_dir) / "fault_profile.json"
    try:
        apply_faults(images_dir, corrupted_dir, fault_json)
    except Exception as e:
        print(f"  [Image] FAULTS ERREUR : {e}")


def step_ground_truth(scenario_dir, dataset_dir, images_dir, simulator):
    """Genere labels_lard.csv (GT LARD brute) puis metadata.csv (enrichi).

    Pure geometrie (offline), mais necessite images_dir (LARD parcourt les
    images). Skip si metadata.csv existe deja (idempotent).

    :return: True si metadata.csv present apres l'etape, False sinon
    """
    dataset_dir = Path(dataset_dir)
    if (dataset_dir / "metadata.csv").exists():
        print(f"  [Image] metadata.csv deja present, skip")
        return True
    try:
        gt = generate_gt_csv(scenario_dir, out_dir=dataset_dir, images_dir=images_dir)
        build_metadata_csv(scenario_dir, images_dir, gt, simulator,
                           out_csv=dataset_dir / "metadata.csv")
    except Exception as e:
        print(f"  [Image] GT ERREUR : {e}")
        return (dataset_dir / "metadata.csv").exists()
    return True


def render_scenario_run(scenario_dir, simulator="xplane", xplane_dir=None):
    """Phase 2 pour un scenario -> output/data/<simulator>/<airport_runway>/<name>/.

    Enchaine :
      1. rendu simulateur -> footage/          (xplane ; GES = externe)
      2. fautes capteur   -> corrupted_footage/ (si fault_profile.json)
      3. verite terrain   -> metadata.csv       (GT LARD)

    :param scenario_dir: dossier output/scenarios/<batch>/<scenario_name>/
    :param simulator: 'xplane' (rendu integre) ou 'GES' (rendu externe)
    :param xplane_dir: chemin vers X-Plane 12 (mode xplane)
    :return: Path du dataset dir si succes, None sinon
    """
    scenario_dir = Path(scenario_dir)
    name = scenario_dir.name
    dataset_dir = dataset_scenario_dir(simulator, name)
    images_dir = dataset_dir / RENDER_DIRNAME
    corrupted_dir = dataset_dir / CORRUPTED_DIRNAME

    print(f"\n  [Image] {name} -> {display_path(dataset_dir)}/  [sim={simulator}]")

    if simulator == "xplane":
        if not step_render_xplane(scenario_dir, images_dir, xplane_dir):
            return None
    elif simulator == "GES":
        # Rendu GES externe : on ne produit que le .esp (a l'etape generate).
        # On attend que les images rendues par GES soient deposees dans footage/.
        images_dir.mkdir(parents=True, exist_ok=True)
        if not list_images(images_dir):
            print(f"  [Image] GES : rendu externe. Deposez les images dans "
                  f"{images_dir} puis relancez le rendu pour fautes + GT.")
            return None

    else:
        print(f"  [Image] Simulateur inconnu : {simulator} (attendu: xplane, GES)")
        return None

    step_faults(images_dir, corrupted_dir, scenario_dir)
    step_ground_truth(scenario_dir, dataset_dir, images_dir, simulator)
    return dataset_dir


# ===========================================================================
# Boucle Phase 2
# ===========================================================================

def _render_loop(scenarios, simulator, xplane_dir):
    """Boucle Phase 2 : rendu + fautes + GT sur une liste de scenarios resolus.

    :return: sous-liste des dossiers dataset produits
    """
    rendered = []
    for sdir in scenarios:
        print(f"\n{'-' * 50}")
        print(f" Scenario : {sdir.parent.name}/{sdir.name}  [sim={simulator}]")
        print(f"{'-' * 50}")
        out = render_scenario_run(sdir, simulator=simulator, xplane_dir=xplane_dir)
        if out:
            rendered.append(out)
    if rendered:
        # metadata.csv consolide au niveau du simulateur (agrege tous les scenarios)
        build_simulator_metadata(simulator)
    if simulator == "xplane":
        reset_if_active(xplane_dir)
    return rendered


def render_scenarios(name=None, all_scenarios=False, batch=None,
                     simulator="xplane", xplane_dir=None):
    """Mode "render" : Phase 2 (rendu + fautes + GT) sur les scenarios filtres.

    :return: list[Path] des dossiers dataset produits
    """
    print("=" * 60)
    print(f" PHASE 2 : Rendu ({simulator}) + fautes capteur + GT")
    print("=" * 60)

    scenarios = find_scenarios(name, all_scenarios, batch=batch)
    if not scenarios:
        print("[Render] Aucun scenario valide trouve.")
        return []

    print(f"\n[Render] {len(scenarios)} scenario(s) a rendre")
    return _render_loop(scenarios, simulator, xplane_dir)
