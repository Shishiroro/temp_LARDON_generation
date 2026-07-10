"""
evaluation/ — Banc d'evaluation (sous-projet)
=============================================
Prend un dataset produit par l'usine (sources/) et le fait passer dans un
"systeme sous test" (SUT) : YOLOv8 aujourd'hui, un moniteur ML demain.

Chaque SUT implemente l'interface `evaluation.sut.SUT` (infer + evaluate).
Le `runner` est generique : il ne connait aucun SUT en particulier.

Dependance UNIDIRECTIONNELLE : evaluation/ importe sources/ (helpers dataset,
runway, GT), jamais l'inverse.

Importer ce package execute le bootstrap sys.path ci-dessous, de sorte que les
imports plats internes (`from sut import ...`, `from runs import ...`) marchent
que le code soit lance via `python -m evaluation` ou importe depuis run_pipeline.
"""

import sys
from pathlib import Path

_EVAL_DIR = Path(__file__).resolve().parent      # evaluation/
_ROOT = _EVAL_DIR.parent
_SOURCES_DIR = _ROOT / "sources"
_YOLO_SUT_DIR = _EVAL_DIR / "yolo"

# Bootstrap usine : ajoute sources/, export/, LARD/, racine via sources/_paths.
if str(_SOURCES_DIR) not in sys.path:
    sys.path.insert(0, str(_SOURCES_DIR))
import _paths  # noqa: F401,E402  (sources/_paths)

# Dossiers propres au banc d'evaluation (SUT YOLO + son module eval/).
for _p in (_EVAL_DIR, _YOLO_SUT_DIR, _YOLO_SUT_DIR / "eval"):
    _sp = str(_p)
    if _sp not in sys.path:
        sys.path.insert(0, _sp)
