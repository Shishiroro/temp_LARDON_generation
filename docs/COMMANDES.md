# Référence des commandes

> ⚠️ **Mise à jour en cours.** Le CLI est désormais **`main.py`** (plus
> `run_pipeline.py`). L'**évaluation** (`evaluate` / `full_evaluate`, YOLO/IoU) a
> été **retirée** (projet séparé). Le layout de sortie a changé : `runs/` →
> **`scenarios/<batch>/`** (generate) + **`dataset/<simulator>/<airport_runway>/`**
> (export) ; l'option `--generation` devient **`--batch`** ; nouvelle option
> **`--simulator {xplane,GES}`**. Pour l'usage **à jour**, voir
> [README.md](../README.md) → « Lancer l'outil ». Les sections détaillées
> ci-dessous restent à migrer (noms/exemples anciens).

Référence complète du CLI (toutes les sous-commandes et leurs options) et de ses
équivalents notebook.

- Pour l'**installation**, les **prérequis** et la **configuration des scénarios
  (XML)** → voir [README.md](../README.md).
- Pour le **mode interactif** → voir `notebook/generation.ipynb` (les 3 phases)
  et `notebook/features.ipynb` (outils annexes : dataset, regroup, sanity,
  exports, vidéo). Chaque section est documentée en tête de cellule.

Toutes les commandes se lancent depuis la racine du projet.

---

## Vue d'ensemble

L'outil se découpe en **3 phases indépendantes**, plus **2 modes qui les
enchaînent**.

| Commande | Phases | Rôle | X-Plane requis |
|----------|--------|------|----------------|
| `generate`      | 1       | Échantillonne N scénarios via TAF/z3 (`.yaml` + poses + profils JSON) | Non |
| `export`        | 2       | Rend les images sous X-Plane 12, applique les fautes capteur **et génère la vérité terrain LARD** | **Oui** |
| `evaluate`      | 3       | Lance la détection YOLO + calcule l'IoU vs la vérité terrain (produite en Phase 2) | Non |
| `full`          | 1 + 2   | `generate` puis `export` (**sans** évaluation) | **Oui** |
| `full_evaluate` | 1 + 2 + 3 | Cycle complet de bout en bout | **Oui** |



> **Attention :** `full` n'enchaîne **que** la génération et le rendu. Pour le
> cycle complet (avec détection + IoU), utiliser **`full_evaluate`**.

Aide intégrée :

```bash
py run_pipeline.py --help            # liste des sous-commandes + structure runs/
py run_pipeline.py <commande> --help # options d'une sous-commande
```

---

## Les 3 phases

1. **`generate`** (Phase 1) — TAF résout les contraintes XML et écrit, pour
   chaque scénario, `runs/<generation>/<ICAO_RWY>/` avec `.yaml`,
   `poses_cam_export.json`, et si actifs `fault_profile.json` /
   `weather_profile.json`. Hors ligne, ne touche pas à X-Plane.
2. **`export`** (Phase 2) — X-Plane 12 rend les images dans `footage/`, les
   fautes capteur produisent `degraded/`, puis la **vérité terrain LARD** est
   générée (`*_labels.csv`, projection 3D→2D des coins de piste). Requiert
   X-Plane lancé.
3. **`evaluate`** (Phase 3) — lance YOLO (`predictions.csv`), calcule l'IoU
   contre la vérité terrain **produite en Phase 2** et agrège un
   `pipeline_report.json`. Hors ligne, ré-exécutable à volonté avec d'autres
   seuils sans re-rendre.

---

## Concepts communs

### Générations (batchs)

Chaque exécution de `generate` crée un **dossier de génération** sous `runs/` qui
regroupe tous les scénarios du batch :

```
runs/
└── generation_01/                  ← une génération = un batch
    ├── LFPO_24/                    ← un dossier par scénario (format ICAO_RWY)
    │   └── eval/yolo/predictions.csv   ← sorties d'éval namespacées par SUT
    ├── KLAX_25R/
    └── eval/yolo/pipeline_report.json  ← rapport agrégé (après evaluate)
```

- Sans `--name` : `generation_01`, `generation_02`, … (auto-incrément).
- Avec `--name pluie` : `pluie_01`, `pluie_02`, … (auto-incrément séparé).
- Si la **même piste** est générée 2× dans le même batch, suffixe automatique :
  `LFPO_24`, `LFPO_24_002`, `LFPO_24_003`, …

### Cibler des runs (`export` / `evaluate`)

Ces deux commandes prennent **soit** un run précis, **soit** `--all` :

| Forme | Exemple | Effet |
|-------|---------|-------|
| Chemin composé | `export generation_01/LFPO_24` | Cible exactement ce run |
| Nom + `--generation` | `export LFPO_24 --generation generation_01` | Idem, autre syntaxe |
| Nom seul | `export LFPO_24` | Cherche dans toutes les générations ; **erreur** si le nom existe dans plusieurs |
| `--all --generation` | `export --all --generation generation_01` | Tous les runs de cette génération |

> **`--all` exige `--generation`** en ligne de commande, pour éviter de mélanger
> plusieurs batchs par inadvertance.

---

## Référence par sous-commande

### `generate` — Phase 1

```bash
py run_pipeline.py generate [-n N] [--name NOM] [--clean] [--runway PISTE]
```

Échantillonne les scénarios via TAF et crée la génération sous `runs/`.

| Option | Défaut | Description |
|--------|--------|-------------|
| `-n`, `--nb-scenarios` | `nb_test_cases` de `settings.xml` (= 3) | Nombre de scénarios à générer |
| `--name NOM` | `generation` | Préfixe du dossier de génération (`<NOM>_NN/`) |
| `--clean` | désactivé | Vide **tout** `runs/` avant de générer |
| `--runway PISTE` | aucune (TAF échantillonne) | Force **tous** les scénarios sur une piste (format `ICAO_RWY`, ex. `LFPO_24`) |

```bash
py run_pipeline.py generate -n 5
py run_pipeline.py generate -n 100 --name pluie --clean
py run_pipeline.py generate -n 10 --runway LFPO_24
```

> `--clean` supprime l'intégralité de `runs/` (toutes les générations).
>
> `--runway` réécrit le paramètre `airport_runway` du template avec la seule
> piste demandée (le reste — trajectoire, météo, fautes — reste échantillonné).
> La piste doit figurer dans la liste du template, sinon la commande s'arrête
> avec une erreur explicite. Disponible aussi sur `full` et `full_evaluate`.

---

### `export` — Phase 2

```bash
py run_pipeline.py export (<run> | --all --generation NOM) [--xplane-dir CHEMIN]
```

Rend les images sous X-Plane 12, applique les fautes capteur, puis génère la
vérité terrain LARD (`*_labels.csv`). Suppose la Phase 1 déjà faite. **X-Plane 12
doit être lancé** (mode fenêtré, scaling 100 %).

| Option | Défaut | Description |
|--------|--------|-------------|
| `run` (positionnel) | — | Run à rendre : `<gen>/<run>` ou nom seul |
| `--all` | désactivé | Tous les runs de la génération (requiert `--generation`) |
| `--generation NOM` | — | Génération ciblée |
| `--xplane-dir CHEMIN` | `xplane_dir` de `settings.xml` (= `C:/X-Plane 12`) | **Optionnel.** Surcharge ponctuelle du répertoire X-Plane 12. Sert uniquement à localiser le plugin météo ; sans météo, sa valeur n'a aucun effet. À renseigner de préférence dans `settings.xml`. |

```bash
py run_pipeline.py export generation_01/LFPO_24
py run_pipeline.py export --all --generation generation_01
py run_pipeline.py export --all --generation pluie_01 --xplane-dir "D:/X-Plane 12"
```

---

### `evaluate` — Phase 3

```bash
py run_pipeline.py evaluate (<run> | --all --generation NOM) \
    [--runway PISTE] [--conf C] [--imgsz S] [--iou-thresh T] [--iou-method M]
```

Lance la détection YOLO et calcule l'IoU contre la vérité terrain. Suppose la
Phase 2 faite : images (`footage/` ou `degraded/`) **et** `*_labels.csv` déjà
présents. **Pas besoin de X-Plane.** Écrit `pipeline_report.json` dans la génération.

| Option | Défaut | Description |
|--------|--------|-------------|
| `run` (positionnel) | — | Run à évaluer : `<gen>/<run>` ou nom seul |
| `--all` | désactivé | Tous les runs de la génération (requiert `--generation`) |
| `--generation NOM` | — | Génération ciblée |
| `--runway PISTE` | aucun filtre | Restreint la vérité terrain à une piste |
| `--conf` | `0.25` | Seuil de confiance YOLO |
| `--imgsz` | `512` | Taille d'image pour l'inférence YOLO |
| `--iou-thresh` | `0.5` | Seuil d'IoU pour le matching prédiction/GT |
| `--iou-method` | `CIOU` | Variante d'IoU : `IOU`, `GIOU`, `DIOU`, `CIOU` |

```bash
py run_pipeline.py evaluate generation_01/LFPO_24
py run_pipeline.py evaluate --all --generation pluie_01
py run_pipeline.py evaluate --all --generation generation_01 --conf 0.4 --iou-method GIOU
```

> Le modèle YOLO utilisé est défini par le champ `model` dans
> `evaluation/yolo/yolo.json` (poids dans `evaluation/yolo/weights/`,
> défaut : `yolov8nTest.pt`).

---

### `full` — Phases 1 + 2

```bash
py run_pipeline.py full [-n N] [--name NOM] [--clean] [--runway PISTE] [--xplane-dir CHEMIN]
```

Enchaîne `generate` puis `export` sur les runs créés. **Sans évaluation.**
X-Plane 12 doit être lancé.  C'est la commande à utiliser pour un cycle complet sans l'évaluation.


```bash
py run_pipeline.py full -n 5
py run_pipeline.py full -n 20  --clean 
py run_pipeline.py full -n 100 --name pluie --clean 
```

---

### `full_evaluate` — Phases 1 + 2 + 3

```bash
py run_pipeline.py full_evaluate [-n N] [--name NOM] [--clean] [--runway PISTE] \
    [--xplane-dir CHEMIN] [--conf C] [--imgsz S] [--iou-thresh T] [--iou-method M]
```

**Cycle complet de bout en bout** : génération + rendu + détection + IoU,
zéro intervention. C'est la commande à utiliser pour un cycle complet avec évaluation.
X-Plane 12 doit être lancé.


```bash
py run_pipeline.py full_evaluate -n 5
py run_pipeline.py full_evaluate -n 100 --name nuage_et_pluie 
```

---

## Tableau récapitulatif des options

| Option | `generate` | `export` | `evaluate` | `full` | `full_evaluate` |
|--------|:--:|:--:|:--:|:--:|:--:|
| `-n / --nb-scenarios` | ✓ | | | ✓ | ✓ |
| `--name`              | ✓ | | | ✓ | ✓ |
| `--clean`             | ✓ | | | ✓ | ✓ |
| `run` (positionnel)   | | ✓ | ✓ | | |
| `--all`               | | ✓ | ✓ | | |
| `--generation`        | | ✓ | ✓ | | |
| `--xplane-dir` *(opt.)* | | ✓ | | ✓ | ✓ |
| `--runway` *(génère sur la piste)* | ✓ | | | ✓ | ✓ |
| `--runway` *(filtre la GT)* | | | ✓ | | |
| `--conf`              | | | ✓ | | ✓ |
| `--imgsz`             | | | ✓ | | ✓ |
| `--iou-thresh`        | | | ✓ | | ✓ |
| `--iou-method`        | | | ✓ | | ✓ |

---

## Workflows typiques

**Générer et rendre (Cas courant)** :

```bash
py run_pipeline.py full -n 20 --name brouillard 
```

**Cycle complet en une commande** :

```bash
py run_pipeline.py full_evaluate -n 5 
```

**Phase par phase** (contrôle fin, ré-exécution ciblée) :

```bash
py run_pipeline.py generate -n 5 --name test
py run_pipeline.py export   --all --generation test_01
py run_pipeline.py evaluate --all --generation test_01
```

**Ré-évaluer sans re-rendre** (tester d'autres seuils YOLO/IoU) :

```bash
py run_pipeline.py evaluate --all --generation test_01 --conf 0.4 --iou-method DIOU
```



---

## Équivalents dans le notebook

Deux notebooks : `notebook/generation.ipynb` reproduit les 3 phases (Setup,
Generate, Export, Evaluate — exécutables séparément) ; `notebook/features.ipynb`
ajoute les outils à la demande. Lancer les cellules dans l'ordre après la
section **Setup**.

| Notebook (fonction)       | CLI équivalent | Rôle |
|---------------------------|----------------|------|
| `generate_runs(...)`      | `generate`     | Phase 1 |
| `render_runs(...)`        | `export`       | Phase 2 |
| `evaluate_runs(...)`      | `evaluate`     | Phase 3 |
| `build_yolo_box(...)`     | —              | Images annotées avec les bbox YOLO (`yolo_box/`) |
| `build_lard_box(...)`     | —              | Images annotées avec la vérité terrain LARD (`lard_box/`) |
| `show_sanity(...)` / `show_sanity_lard(...)` | — | Aperçu rapide des box yolo ou lard (1re / milieu / dernière image) |
| `build_xplane_config(...)`| —              | Génère `xplane_config.json` |
| `build_params_trace(...)` | —              | Génère `params_trace.xml` |
| `build_video(...)`        | —              | Assemble les images d'un/des run en MP4 |

Les fonctions du notebook acceptent les mêmes formes de ciblage que le CLI (voir Notebook)

---

## Test d'injection météo (`scripts/injection_weather_test.py`)

Outil de **prévisualisation** : injecte la météo du XML actif dans X-Plane et
laisse la sim en pause pour observer la scène, **sans** générer ni rendre de
scénario.

```bash
py scripts/injection_weather_test.py
```

> **Ce script n'échantillonne pas via TAF** : pour chaque paramètre il prend
> le **milieu** de la plage `[min, max]` du XML (`(min + max) / 2`). Il injecte
> donc une seule météo déterministe, pas un tirage aléatoire.
>
> **Conséquence pour `cloud_type`** (enum 0=Cirrus, 1=Stratus, 2=Cumulus,
> 3=Cumulonimbus) : 
> Pour prévisualiser un type, fige-le dans le XML avec `min = max` :
>
> ```xml
> <parameter name="cloud_type" type="integer" min="3" max="3"/>  <!-- Cumulonimbus -->
> ```
>
> Les autres paramètres (épaisseur, visibilité, couverture…) peuvent rester en
> plage : leur milieu est une valeur d'aperçu raisonnable. La variété réelle des
> 4 types de nuages n'apparaît que via `generate` (tirage TAF/z3 par scénario).

---

## Où vont les résultats

Voir la section **Résultats** du [README.md](../README.md) pour le détail de
l'arborescence `runs/<generation>/<ICAO_RWY>/`. 

En résumé :

- `footage/` — images brutes X-Plane ; `degraded/` — images avec fautes capteur
- `*_labels.csv` — vérité terrain LARD (racine du run, produite par l'usine)
- `eval/<sut>/predictions.csv` — détections du SUT (ex `eval/yolo/`), namespacé par SUT
- `eval/<sut>/pipeline_report.json` — métriques agrégées (IoU, AP, F1, P, R) par batch
