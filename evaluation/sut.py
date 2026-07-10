"""
sut.py — Interface "Systeme Sous Test" (SUT) du banc d'evaluation
=================================================================
Un SUT est un systeme qu'on veut evaluer sur un dataset produit par l'usine
(sources/) : detecteur YOLOv8 aujourd'hui, moniteur ML demain.

Chaque SUT expose deux operations sur un run (dossier <generation>/<ICAO_RWY>/) :
  - infer(run_dir)    : fait tourner le systeme sur les images, ecrit ses
                        predictions sous run_dir/eval/<name>/ (namespacing).
  - evaluate(run_dir) : compare ces predictions a la GT LARD (*_labels.csv,
                        a la racine du run) et renvoie un dict de metriques.

Le runner (evaluation/runner.py) est generique : il ne connait que cette
interface. Ajouter un moniteur = creer un nouveau module SUT et l'enregistrer
dans le registry ci-dessous, sans toucher au runner.
"""

from abc import ABC, abstractmethod
from pathlib import Path


class SUT(ABC):
    """Contrat commun a tous les systemes sous test."""

    #: identifiant court, sert a namespacer les sorties (run_dir/eval/<name>/)
    name: str = "sut"

    def eval_dir(self, run_dir):
        """Dossier de sortie propre a ce SUT : run_dir/eval/<name>/."""
        d = Path(run_dir) / "eval" / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @abstractmethod
    def infer(self, run_dir, **cfg):
        """Fait tourner le systeme sur les images du run.

        Doit ecrire ses predictions sous self.eval_dir(run_dir).
        :return: Path des predictions ecrites, ou None si echec / pas d'images.
        """

    @abstractmethod
    def evaluate(self, run_dir, **cfg):
        """Compare les predictions (deja produites par infer) a la GT du run.

        :return: dict de metriques, ou None si echec / pas de donnees.
        """


def get_sut(name="yolo"):
    """Resout un SUT par nom (import paresseux pour eviter les cycles)."""
    key = (name or "yolo").lower()
    if key == "yolo":
        from yolo_sut import YoloSUT
        return YoloSUT()
    raise ValueError(f"SUT inconnu : {name!r} (disponibles : 'yolo')")
