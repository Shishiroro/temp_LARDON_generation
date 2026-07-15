"""
pipeline.py — Orchestration usine (Phase 1 generate + Phase 2 export)
=====================================================================
Separe de scenario.py pour eviter le cycle d'imports
scenario <-> taf_export / taf_generate : ici on importe les trois en tete.

  - render_scenarios : Phase 2 (rendu + fautes + GT) sur les scenarios filtres
  - full_pipeline    : Phase 1 + 2 enchainees
"""

from scenario import find_scenarios
from taf_export import render_scenario_run
from taf_generate import generate_runs
from xplane_weather import reset_if_active


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
    if simulator == "xplane":
        reset_if_active(xplane_dir)
    return rendered


def render_scenarios(name=None, all_scenarios=False, batch=None,
                     simulator="xplane", xplane_dir=None):
    """Mode "export" : Phase 2 (rendu + fautes + GT) sur les scenarios filtres.

    :return: list[Path] des dossiers dataset produits
    """
    print("=" * 60)
    print(f" PHASE 2 : Rendu ({simulator}) + fautes capteur + GT")
    print("=" * 60)

    scenarios = find_scenarios(name, all_scenarios, batch=batch)
    if not scenarios:
        print("[Pipeline] Aucun scenario valide trouve.")
        return []

    print(f"\n[Pipeline] {len(scenarios)} scenario(s) a rendre")
    return _render_loop(scenarios, simulator, xplane_dir)


def full_pipeline(nb_scenarios=None, name=None, clean=False, runway=None,
                  simulator="xplane", xplane_dir=None):
    """Mode "full" : Phase 1 + Phase 2 enchainees (sans evaluation)."""
    created = generate_runs(nb_scenarios=nb_scenarios,
                            name=name, clean=clean, runway=runway)
    if not created:
        print("[Pipeline] Aucun scenario genere, arret.")
        return []

    print(f"\n{'=' * 60}")
    print(f" PHASE 2 : Rendu ({simulator}) + fautes capteur + GT")
    print(f"{'=' * 60}")
    return _render_loop(created, simulator, xplane_dir)
