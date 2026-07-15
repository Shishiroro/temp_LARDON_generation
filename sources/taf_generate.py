"""
taf_generate.py — Lance TAF pour generer des scenarios d'approche
=================================================================
Utilisation :
    python sources/taf_generate.py      (depuis la racine)
    python taf_generate.py              (depuis sources/)

Ce script :
    1. Se place dans sources/ (CWD requis par TAF pour settings.xml)
    2. Insere sources/ en tete de sys.path (imports plats du projet)
    3. Insere taf/src/ dans sys.path (modules TAF)
    4. Copie taf_export.py -> taf/src/Export.py (point d'extension TAF)
    5. Lance TAF : parse template → z3 solve → export()
"""

import sys
import os
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from config import TAF_SRC
from ges_export import build_esp_for_scenario
from scenario import (
    TAF_OUTPUT_DIR,
    clean_scenarios_dir,
    create_scenarios_from_taf_output,
    new_batch_dir,
    timestamp_now,
)


def _precreate_output_dirs(project_dir):
    """
    Pre-cree l'arborescence de sortie TAF.

    TAF utilise `mkdir -p` (Unix) qui n'existe pas sur Windows.
    Sur Linux, TAF gere tout seul, mais on pre-cree quand meme
    pour uniformiser le comportement cross-platform.
    """
    tree = ET.parse(project_dir / "settings.xml")
    params = {p.attrib["name"]: p.attrib["value"] for p in tree.getroot()}

    nb_cases = int(params.get("nb_test_cases", 3))
    nb_artifacts = int(params.get("nb_test_artifacts", 1))
    experiment = Path(params["experiment_path"]) / params["experiment_folder_name"]

    for i in range(nb_cases):
        for j in range(nb_artifacts):
            d = experiment / f"{params['test_case_folder_name']}_{i}" / f"{params['test_artifact_folder_name']}_{j}"
            d.mkdir(parents=True, exist_ok=True)


def _force_single_runway(project_dir, runway):
    """Genere une copie du template ou airport_runway est force a une seule piste.

    TAF echantillonne aleatoirement parmi les pistes listees dans l'attribut
    `values` du parametre `airport_runway`. Pour generer sur UNE piste precise,
    on reecrit ce `values` avec la seule piste demandee, dans un template
    temporaire (le template de base reste intact).

    :param runway: piste cible au format ICAO_RWY (ex: "LFPO_24")
    :return: nom du fichier template temporaire (a placer dans template_path)
    :raises ValueError: si la piste n'est pas dans la liste du template
    """
    settings = {p.attrib["name"]: p.attrib["value"]
                for p in ET.parse(project_dir / "settings.xml").getroot()}
    template_dir = project_dir / settings["template_path"]
    template_file = template_dir / settings["template_file_name"]

    tree = ET.parse(template_file)
    param = tree.getroot().find(".//parameter[@name='airport_runway']")
    if param is None:
        raise ValueError(
            f"Parametre 'airport_runway' introuvable dans {template_file}")

    allowed = [r.strip() for r in param.attrib.get("values", "").split(";") if r.strip()]
    if runway not in allowed:
        raise ValueError(
            f"Piste '{runway}' absente du template {settings['template_file_name']} "
            f"({len(allowed)} pistes disponibles, format attendu ICAO_RWY ex LFPO_24)")

    param.set("values", runway)
    tmp_name = "_single_runway_tmp.xml"
    tree.write(template_dir / tmp_name)
    return tmp_name


def _sync_taf_settings():
    """Resynchronise Taf.SETTINGS depuis settings.xml.

    Taf.SETTINGS est un singleton cree au module-level (`SETTINGS = Settings()`),
    donc lu UNE SEULE FOIS au premier `import Taf`. Dans un kernel Jupyter
    l'import est ensuite mis en cache : au 2e appel de run(), SETTINGS garde
    les valeurs du 1er import meme si settings.xml a change entre-temps.
    _precreate_output_dirs lit pourtant settings.xml a jour -> les dossiers
    pre-crees et ceux generes par TAF se desynchronisent (FileNotFoundError
    sur scenario_N/scenario_N.xml).

    On reapplique donc le contenu de settings.xml dans le dict vivant de
    SETTINGS (CWD = sources/, deja en place via os.chdir).
    """
    import Taf
    params = Taf.SETTINGS.get_setting_parameters()
    for p in ET.parse("settings.xml").getroot():
        value = p.attrib["value"]
        params[p.attrib["name"]] = int(value) if p.attrib["type"] == "integer" else value


def run(nb_test_cases=None, runway=None, verbose=True):
    """
    Point d'entree pour lancer la generation TAF.

    :param nb_test_cases: surcharge le nb de scenarios (sinon, utilise settings.xml)
    :param runway: force toutes les generations sur une piste (format ICAO_RWY,
                   ex "LFPO_24") ; sinon TAF echantillonne parmi le template
    :param verbose: afficher les details TAF
    """
    project_dir = Path(__file__).resolve().parent

    # CWD = sources/ (TAF lit ./settings.xml depuis le CWD)
    os.chdir(project_dir)

    # sys.path : sources/ (imports plats) + taf/src/ (modules TAF)
    if str(project_dir) not in sys.path:
        sys.path.insert(0, str(project_dir))
    taf_src = str(TAF_SRC)
    if taf_src not in sys.path:
        sys.path.insert(1, taf_src)

    # Copier notre Export.py dans taf/src/ (point d'extension prevu par TAF).
    # taf/src est resolu via config (TAF_HOME / paths.local.json).
    src_export = project_dir / "taf_export.py"
    taf_export = TAF_SRC / "Export.py"
    shutil.copy2(src_export, taf_export)

    # TAF lie notre export() AU CHARGEMENT (taf/src/Tree.py: `from Export import
    # export`), pendant `import Taf`. sys.path a sources/ en tete : on place donc
    # notre VRAI code (taf_export.py) sous le nom Export.py AVANT `import Taf`,
    # pour que Tree lie notre export() et non le stub vide que
    # create_minimal_export creerait sinon.
    shutil.copy2(src_export, project_dir / "Export.py")

    # Surcharger nb_test_cases dans le XML avant import TAF (SETTINGS lit au module-level)
    if nb_test_cases is not None:
        settings_file = project_dir / "settings.xml"
        tree = ET.parse(settings_file)
        for p in tree.getroot():
            if p.attrib["name"] == "nb_test_cases":
                p.set("value", str(nb_test_cases))
        tree.write(settings_file)

    # TAF utilise `mkdir -p` (Unix) qui casse sur Windows, et sur Linux ouvre
    # un prompt interactif au premier passage. On pre-cree l'arborescence
    # avec Path.mkdir() (cross-platform) pour court-circuiter les deux.
    _precreate_output_dirs(project_dir)

    # NB : au 1er `import Taf`, TAF (Export_Generator.create_minimal_export)
    # teste ./Export.py et affiche sinon un message verbeux
    # ("Miscellaneous::file_check() -> \"Export.py\" does not exist"). C'est un
    # FAUX POSITIF : TAF cree aussitot le stub lui-meme. Sans consequence.
    import Taf

    # Resync SETTINGS : `import Taf` est cache par le kernel Jupyter, donc
    # le singleton SETTINGS garde le nb_test_cases du 1er appel sans ca.
    _sync_taf_settings()

    # Forcer une piste unique : reecrit airport_runway dans un template
    # temporaire et pointe SETTINGS dessus (base.xml reste intact).
    tmp_template = None
    if runway is not None:
        tmp_template = _force_single_runway(project_dir, runway)
        Taf.SETTINGS.get_setting_parameters()["template_file_name"] = tmp_template
        print(f"[Generate] Piste forcee : {runway}")

    taf = Taf.CLI()
    taf.verbose = verbose
    taf.auto = True
    taf.do_overwrite(None)

    print(f"[Generate] Template : {Taf.SETTINGS.get('template_path')}"
          f"{Taf.SETTINGS.get('template_file_name')}")
    print(f"[Generate] Sortie   : {Taf.SETTINGS.get('experiment_path')}"
          f"{Taf.SETTINGS.get('experiment_folder_name')}/")
    print(f"[Generate] Scenarios: {Taf.SETTINGS.get('nb_test_cases')}")
    print()

    taf.do_parse_template()
    taf.do_generate()

    # Nettoyage : retirer la copie transitoire ./Export.py (CWD)
    cwd_export = project_dir / "Export.py"
    if cwd_export.exists():
        cwd_export.unlink()

    # Restaurer le placeholder dans taf/src/ (garder taf/ propre)
    taf_export.write_text("def export(root_node, path):\n\tpass\n")

    # Nettoyer le template temporaire de piste unique
    if tmp_template is not None:
        settings = {p.attrib["name"]: p.attrib["value"]
                    for p in ET.parse(project_dir / "settings.xml").getroot()}
        tmp_path = project_dir / settings["template_path"] / tmp_template
        if tmp_path.exists():
            tmp_path.unlink()

    print("\n[Generate] Termine.")


def generate_runs(nb_scenarios=None, name=None, clean=False, runway=None):
    """Phase 1 complete : cleanup output/ + run() TAF + organise en scenarios/<batch>/.

    Wrapper haut-niveau de run() : nettoie le dossier temporaire output/,
    lance la generation TAF, puis reorganise les .yaml + poses vers
    scenarios/<batch>/<scenario_name>/ (+ .esp best-effort).

    :param name: nom optionnel du batch (genere `<name>__<timestamp>/`
                 au lieu de `default__<timestamp>/`)
    :param clean: si True, vide scenarios/ avant la generation
    :param runway: force toutes les generations sur une piste (format ICAO_RWY)
    :return: list[Path] des dossiers scenarios/<batch>/<scenario_name>/ crees
    """
    print("=" * 60)
    print(" PHASE 1 : Generation TAF")
    print("=" * 60)

    if clean:
        clean_scenarios_dir()

    ts = timestamp_now()
    batch_dir = new_batch_dir(name, timestamp=ts)
    print(f"[Pipeline] Batch : {batch_dir.name}/")

    if TAF_OUTPUT_DIR.exists():
        shutil.rmtree(TAF_OUTPUT_DIR)

    run(nb_test_cases=nb_scenarios, runway=runway)

    created = create_scenarios_from_taf_output(batch_dir, ts)

    # .esp (GES) pour chaque scenario, via LARD/GEODataset.
    for sdir in created:
        out = build_esp_for_scenario(sdir)
        print(f"  [ESP] {sdir.name} -> {Path(out).name}")

    return created


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generation de scenarios TAF")
    parser.add_argument("-n", "--nb-scenarios", type=int, default=None,
                        help="Nombre de scenarios (surcharge settings.xml)")
    parser.add_argument("--name", type=str, default=None,
                        help="Nom de la generation (sinon 'generation')")
    parser.add_argument("--clean", action="store_true",
                        help="Vider scenarios/ avant la generation")
    parser.add_argument("--runway", type=str, default=None,
                        help="Forcer une piste (format ICAO_RWY, ex LFPO_24)")
    args = parser.parse_args()

    generate_runs(nb_scenarios=args.nb_scenarios,
                  name=args.name, clean=args.clean, runway=args.runway)
