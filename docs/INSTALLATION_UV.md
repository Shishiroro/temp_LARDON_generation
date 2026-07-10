# Installation avec uv

[uv](https://docs.astral.sh/uv/) est un gestionnaire d'environnements et de
paquets Python ultra-rapide (écrit en Rust). Il remplace `python -m venv` + `pip`
et installe les dépendances en quelques secondes, sans avoir à activer
l'environnement (`uv run` s'en charge).

> Ce guide est une **alternative** à l'installation `pip` classique décrite dans
> le [README]
---

## 1. Installer uv

**Windows (PowerShell) :**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Linux / macOS :**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Vérifier : `uv --version`.

---

## 2. Installer Python 3.10

uv télécharge et gère la version de Python pour toi (pas besoin de l'installer
au niveau système) :

```bash
uv python install 3.10
```

---

## 3. Créer l'environnement virtuel

Depuis la racine du projet :

```bash
uv venv --python 3.10
```

Cela crée un dossier `.venv/` à la racine. **Inutile de l'activer** si tu utilises
`uv run` (voir §5). Pour l'activer quand même :

```powershell
.venv\Scripts\Activate.ps1     # Windows (PowerShell)
```
```bash
source .venv/bin/activate       # Linux / macOS
```

---

## 4. Installer les dépendances (par batch)


Les deux fichiers sont dans `docs/`. Les commandes ci-dessous se lancent **depuis
la racine du projet**.

| Batch | Fichier | Phases couvertes |
|-------|---------|------------------|
| **base** | `docs/requirements.txt` | `generate` + `export` (rendu X-Plane, fautes capteur, vérité terrain LARD) |
| **eval** | `docs/requirements-eval.txt` | base **+** détection YOLO / IoU (Phase 3, tire PyTorch) |

```bash
# Batch base : génération + rendu, sans PyTorch
uv pip install -r docs/requirements.txt

# Batch complet : ajoute l'évaluation YOLO / IoU (PyTorch inclus)
uv pip install -r docs/requirements-eval.txt
```

> `docs/requirements-eval.txt` inclut `docs/requirements.txt` (ligne
> `-r requirements.txt`, relative à `docs/`) : installer le batch eval suffit, il
> tire aussi la base.

---

## 5. Lancer l'outil

Avec `uv run`, pas besoin d'activer l'environnement — uv utilise automatiquement
le `.venv/` du projet :

```bash
uv run py run_pipeline.py generate -n 5
uv run py run_pipeline.py export --all --generation generation_01
uv run py run_pipeline.py full_evaluate -n 5
```

(Si l'environnement est activé via §3, le préfixe `uv run` n'est pas nécessaire :
`py run_pipeline.py ...` suffit.)

---

## Aide-mémoire

| Action | Commande |
|--------|----------|
| Installer uv (Windows) | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| Installer Python 3.10 | `uv python install 3.10` |
| Créer le venv | `uv venv --python 3.10` |
| Deps base (generate + export) | `uv pip install -r docs/requirements.txt` |
| Deps complètes (+ eval) | `uv pip install -r docs/requirements-eval.txt` |
| Lancer sans activer | `uv run py run_pipeline.py <commande>` |
