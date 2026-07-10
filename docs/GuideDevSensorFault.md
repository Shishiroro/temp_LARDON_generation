
Guide pour les futurs contributeurs souhaitant modifier le code afin d'ajouter :

1. [Une erreur capteur](#1-ajouter-une-erreur-capteur)
2. [Un dataref X-Plane](#2-ajouter-un-dataref-x-plane)


## 1. Ajouter une erreur capteur

Une erreur capteur est une dégradation OpenCV appliquée aux images après le
rendu X-Plane (dossier `degraded/`). Le pipeline : TAF échantillonne les
paramètres depuis le XML → `Export.py` écrit `fault_profile.json` → la Phase 2
applique les erreurs frame par frame.

Il y a 4 fichiers à toucher (3 obligatoires + 1 optionnel).

### Étape 1 — Implémenter la fonction OpenCV
Fichier : [sources/export/camera_sensor_errors/camera_sensor_errors.py](../sources/export/camera_sensor_errors/camera_sensor_errors.py)

La fonction prend une image BGR `uint8` + `severity ∈ [0, 1]` et retourne une
image BGR `uint8`. `severity` module l'intensité.

```python
def my_fault(img: np.ndarray, severity: float = 0.5) -> np.ndarray:
    strength = 0.1 + severity * 0.8        # mapping severity -> paramètre interne
    out = img.astype(np.float32)
    # ... transformation OpenCV/numpy ...
    return np.clip(out, 0, 255).astype(np.uint8)
```

### Etape 2 - Enregistrer dans `ERROR_REGISTRY`

```python
ERROR_REGISTRY: dict[str, Callable] = {
    ...
    "my_fault": my_fault,
}
```

### Étape 3 — Déclarer le type connu
Fichier : [sources/export/sensor_faults.py](../sources/export/sensor_faults.py)

Ajouter le nom dans `KNOWN_FAULT_TYPES` (set utilisé pour la validation et
le parcours de lecture du XML par `Export.py`) :

```python
KNOWN_FAULT_TYPES = {
    ...
    "my_fault",
}
```

> Le nom doit être identique dans `ERROR_REGISTRY` et `KNOWN_FAULT_TYPES`.

### Étape 4 — Ajouter le nœud XML
Fichier : [sources/templates/base.xml](../sources/templates/base.xml),
sous `<node name="faults">`.

```xml
<node name="my_fault">
    <parameter name="severity"     type="real" min="0"   max="0"/>
    <parameter name="start_pct"    type="real" min="0"   max="0"/>
    <parameter name="duration_pct" type="real" min="100" max="100"/>
    <constraint name="bounded" expressions=".\start_pct + .\duration_pct INFEQ 100"/>
</node>
```

- `severity` `min=max=0` désactive la faute ; `> 0` l'active.
- `start_pct` / `duration_pct` : fenêtre d'application en % de la trajectoire.

