"""
runner.py — Orchestration generique du banc d'evaluation
========================================================
Ne connait AUCUN SUT en particulier : il prend un objet `SUT` (resolu par nom
via evaluation.sut.get_sut) et, pour chaque run filtre, appelle infer() puis
evaluate(), met en forme les metriques et agrege un rapport.

Reutilise l'usine (sources/) :
  - runs.find_runs        : resolution des runs a evaluer
  - runs._render_loop     : rendu X-Plane (Phase 2) pour le mode full_evaluate
  - Generate.generate_runs: generation TAF (Phase 1) pour le mode full_evaluate
  - runway.runway_from_run_name : libelle piste pour le rapport

Sorties namespacees par SUT : <generation>/eval/<sut>/pipeline_report.json.

API publique (consommee par run_pipeline.py et evaluation/main.py) :
    evaluate_runs(run_name, all_runs, generation, sut, **cfg)
    full_evaluate_pipeline(nb_scenarios, name, clean, sut, xplane_dir, **cfg)
"""

import json
from pathlib import Path

from runs import find_runs, PROJECT_ROOT, RUNS_DIR
from runway import runway_from_run_name
from sut import get_sut


def _resolve_sut(sut):
    """Accepte un nom (str) ou un objet SUT deja construit."""
    return get_sut(sut) if isinstance(sut, str) else sut


def _has_ground_truth(run_dir):
    """Le run a-t-il une GT LARD (*_labels.csv produite en Phase 2) ?"""
    return bool(list(Path(run_dir).glob("*_labels.csv")))


def _shape_result(run_dir, metrics, runway=None):
    """Met en forme un dict de metriques pour le rapport (generique a tout SUT)."""
    run_dir = Path(run_dir)
    rwy = runway or runway_from_run_name(run_dir.name)
    return {
        "run": run_dir.name,
        "runway": rwy,
        **{k: round(v, 4) if isinstance(v, float) else v
           for k, v in metrics.items()
           if k in ("ap", "f1", "p", "r", "c", "tp", "fp", "fn")},
    }


# ---------------------------------------------------------------------------
# Evaluation d'un run / d'une liste de runs
# ---------------------------------------------------------------------------

def evaluate_one(sut, run_dir, **cfg):
    """infer + evaluate un run avec un SUT donne.

    `cfg` est transmis tel quel a infer/evaluate (chaque SUT prend ce qu'il
    comprend et ignore le reste). Pour YOLO : conf, imgsz, runway, iou_thresh,
    iou_method.

    :return: dict resultat mis en forme, ou None si echec / pas de GT.
    """
    run_dir = Path(run_dir)
    print(f"\n  [Eval:{sut.name}] {run_dir.name}")

    if not _has_ground_truth(run_dir):
        print(f"  [Eval] Pas de GT pour {run_dir.name}. Lancer 'export' d'abord.")
        return None

    preds = sut.infer(run_dir, **cfg)
    if preds is None or not Path(preds).exists():
        print(f"  [Eval] Pas de predictions pour {run_dir.name}")
        return None

    metrics = sut.evaluate(run_dir, **cfg)
    if not metrics:
        return None
    return _shape_result(run_dir, metrics, cfg.get("runway"))


def _evaluate_loop(sut, runs, **cfg):
    """Boucle d'evaluation sur une liste de runs deja resolus."""
    results = []
    for run_dir in runs:
        print(f"\n{'-' * 50}")
        print(f" Run : {run_dir.parent.name}/{run_dir.name}")
        print(f"{'-' * 50}")
        result = evaluate_one(sut, run_dir, **cfg)
        if result:
            result.setdefault("run_dir", str(run_dir))
            results.append(result)
    return results


# ---------------------------------------------------------------------------
# Rapport agrege (namespace par SUT)
# ---------------------------------------------------------------------------

def aggregate_report(results, sut, generation_dir=None):
    """Affiche le rapport et le sauve dans <generation>/eval/<sut>/pipeline_report.json."""
    if not results:
        return
    print(f"\n{'=' * 60}")
    print(f" RAPPORT FINAL — SUT={sut.name} ({len(results)} run(s))")
    print(f"{'=' * 60}")
    print(f"{'Run':<20} {'AP':>6} {'F1':>6} {'P':>6} {'R':>6} {'TP':>5} {'FP':>5} {'FN':>5}")
    print(f"{'-' * 60}")
    for r in results:
        print(f"{r['run']:<20} {r.get('ap', 0):>6.3f} {r.get('f1', 0):>6.3f} "
              f"{r.get('p', 0):>6.3f} {r.get('r', 0):>6.3f} "
              f"{r.get('tp', 0):>5} {r.get('fp', 0):>5} {r.get('fn', 0):>5}")

    if generation_dir is None:
        first_run = Path(results[0].get("run_dir", "")) if results[0].get("run_dir") else None
        if first_run and first_run.parent.parent == RUNS_DIR:
            generation_dir = first_run.parent
        else:
            generation_dir = RUNS_DIR
    out_dir = Path(generation_dir) / "eval" / sut.name
    out_dir.mkdir(parents=True, exist_ok=True)
    report_file = out_dir / "pipeline_report.json"

    with open(report_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nRapport sauvegarde : {report_file.relative_to(PROJECT_ROOT)}")


# ---------------------------------------------------------------------------
# Modes publics
# ---------------------------------------------------------------------------

def evaluate_runs(run_name=None, all_runs=False, generation=None, sut="yolo", **cfg):
    """Mode "evaluate" : banc d'eval sur les runs filtres avec un SUT.

    La GT et les images doivent deja exister (produites par Phase 2, 'export').
    :return: list[dict] des resultats par run.
    """
    sut = _resolve_sut(sut)
    print("=" * 60)
    print(f" EVALUATION (SUT={sut.name})")
    print("=" * 60)

    runs = find_runs(run_name, all_runs, generation=generation)
    if not runs:
        print("[Pipeline] Aucun run valide trouve.")
        print(f"  Verifier que runs/<generation>/<nom>/ contient un .yaml et "
              f"poses_cam_export.json ou footage/")
        return []

    print(f"\n[Pipeline] {len(runs)} run(s) a evaluer")
    results = _evaluate_loop(sut, runs, **cfg)
    aggregate_report(results, sut, generation_dir=runs[0].parent)
    return results


def full_evaluate_pipeline(nb_scenarios=None, name=None, clean=False, runway=None,
                           sut="yolo", xplane_dir=None, **cfg):
    """Mode "full_evaluate" : Phase 1 + 2 (usine) + evaluation enchainees.

    Genere les scenarios (TAF), rend les images (X-Plane), puis evalue avec le
    SUT. Reutilise l'usine pour les phases 1 et 2.
    """
    from Generate import generate_runs
    from runs import _render_loop

    created_runs = generate_runs(nb_scenarios=nb_scenarios, name=name,
                                 clean=clean, runway=runway)
    if not created_runs:
        print("[Pipeline] Aucun scenario genere, arret.")
        return []

    generation_dir = created_runs[0].parent

    print(f"\n{'=' * 60}")
    print(" PHASE 2 : Rendu X-Plane + fautes capteur")
    print(f"{'=' * 60}")
    rendered = _render_loop(created_runs, xplane_dir)

    if not rendered:
        print("[Pipeline] Aucun rendu reussi, arret avant evaluation.")
        return []

    sut = _resolve_sut(sut)
    print(f"\n{'=' * 60}")
    print(f" EVALUATION (SUT={sut.name})")
    print(f"{'=' * 60}")
    results = _evaluate_loop(sut, rendered, **cfg)
    aggregate_report(results, sut, generation_dir=generation_dir)
    return results
