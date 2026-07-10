# DocumentationWeather.md — Paramètres météo XML

Documentation des paramètres météo des templates (`sources/templates/`), de leur
effet et de leurs dépendances (quels paramètres doivent être réunis pour
qu'un effet — pluie, neige, accumulation au sol — fonctionne réellement).

> Portée : uniquement la météo X-Plane 12 (scène 3D, injectée 1× avant le
> rendu via le plugin XPPython3). Les fautes capteur (post-traitement OpenCV)
> ne sont pas couvertes ici.

Code de référence : `sources/export/xplane_weather.py`
(`build_plugin_command`, `inject_weather`).

---

## Vue d'ensemble — les 3 nœuds

Un template météo se compose de trois nœuds dans le scénario :

| Nœud | Rôle | Quand il agit |
|------|------|---------------|
| `weather`  | Météo de la scène 3D (pluie, nuages, brouillard, neige) | 1× avant rendu |
| `settings` | Réglages de rendu/sim (heure, délais, rayon zone) | 1× avant rendu |
| `faults`   | Fautes capteur (hors périmètre de ce document) | par frame |

Convention TAF dans les XML : `min`/`max` identiques = valeur fixe ;
`min`/`max` différents = TAF échantillonne dans la plage (solveur z3).

---

## Tester une météo sans lancer de scénario (`injection_weather_test.py`)

Avant de lancer un batch complet (long, rend des images), prévisualisation de 
la météo du XML actif directement dans X-Plane :

```powershell
py run_pipeline.py generate -n 1 (Pour générer une position)
py injection_weather_test.py
py injection_weather_test.py --run KPDX_10L
py injection_weather_test.py --alt-offset 200 
```

> Prérequis : `py run_pipeline.py generate -n 1`. 
Arg : --alt-offset 200 (200m au dessus de la piste, 100 est la valeur par défaut).
> 

---

## Nœud `weather`

### `precip_rate` — `[0, 1]`
Taux de précipitation. Interrupteur principal pluie/neige.
- `0` : pas de précipitation.
- `> 0` : active la pluie si les conditions sont réunies. Le type de précipitation (pluie vs neige)
  dépend de `temperature_c`, pas de ce paramètre.

### `cloud_type` — enum `{-1, 0, 1, 2, 3}`
Type de nuage manuel (`XPLMWeather`).

| Valeur | Type | Note |
|--------|------|------|
| `-1` | aucun nuage manuel | combiné à `precip>0` → Cumulonimbus forcé auto |
| `0`  | Cirrus | haut, fin |
| `1`  | Stratus | couche basse |
| `2`  | Cumulus | bourgeonnant |
| `3`  | Cumulonimbus | nuage d'orage |


### `cloud_coverage` — `[0, 1]`
Densité de la couverture nuageuse.
- Plus la valeur est haute, plus la zone est couverte → plus de chances que
  la précipitation tombe à l'endroit où se trouve l'avion.
- Pour de la pluie locale fiable, viser `cloud_coverage` proche de `1`.

### `cloud_thickness_m` — épaisseur du nuage (m)
`alt_top = alt_base + cloud_thickness_m`.
- Doit être > 0 pour avoir de la pluie/neige. Un nuage d'épaisseur `0`
  (base = sommet) est dégénéré pour `XPLMWeather` et ne génère aucune
  particule.
- Sécurité dans le code : si `<= 0`, une épaisseur par défaut est substituée
  selon le type (`THICKNESS_DEFAULT_BY_TYPE = {Cirrus:1000, Stratus:500,
  Cumulus:2000, Cb:6000}`). Toute valeur positive (même 50 m) est respectée
  telle quelle. Pour un Cumulonimbus de pluie, viser plusieurs milliers de m.

### `cloud_margin_m` — marge au-dessus de l'avion (m)
Hauteur de la base du nuage au-dessus de l'altitude max de l'avion :
`cloud_base = aircraft_max_alt + cloud_margin_m`.

- Ne pas mettre trop haut : si la base est trop loin au-dessus de l'avion,
  les particules de pluie/neige ne sont pas visibles dans le champ de la caméra.
  Garder une marge modérée pour voir la pluie pendant l'approche.

### `fog_visibility` — visibilité (m), `[500, 50000]`
Distance de visibilité (brouillard).
- `50000` (ou plus) : pas de brouillard.
- `< 50000` : brouillard ; plus la valeur est petite, plus c'est dense.

### `temperature_c` — température (°C), `[-30, 45]`
Détermine pluie vs neige quand `precip_rate > 0` :
- Température positive → pluie.
- Température négative → neige.

### `rain_scale` — taille des gouttes, `[0.1, 5.0]`
Taille visuelle des particules de pluie. 

- Purement esthétique, ignoré si `precip_rate = 0`.
- Dataref privé XP12, susceptible de casser dans une future version.

### `weather_effect_duration` — accumulation au sol (s), `[0, 60]`
Délai d'accumulation des effets sol : flaques de pluie / couche de neige
sur la piste et l'environnement. La sim tourne en accéléré pendant ce
délai.
- À augmenter si on veut des flaques d'eau / de la neige accumulée au sol.
- `0` : la pluie tombe mais la piste reste sèche (pas d'accumulation).
- Ignoré si `precip_rate = 0`.

---

## Nœud `settings`

### `time_of_day_h` — heure locale, `[0, 24]`
Heure civile locale ; convertie en UTC via le fuseau politique réel de
l'aéroport.

### `load_texture_duration` — délai chargement (s), `[0, 60]`
 Permet d'attendre le chargement des textures lors d'un passage à une autre piste + stabilisation météo (nuages..). 
 Appliqué à tous les scénarios,
même sans météo. ~30 s recommandé.

### `screenshot_duration` — délai pose (s)
Délai de stabilisation de la pose après chaque téléportation de la caméra,
avant la capture. Recommendation : ~0.07 s ou +

### `weather_zone_radius_nm` — rayon zone météo (nm), `[1, 10000]`
Rayon de la zone météo injectée dans `XPLMWeather` (1 nm = 1.852 km).
- Petit rayon → météo concentrée localement (effets constants). `50` recommandé
  en usage général.
- Grand rayon (plusieurs centaines) → nuages visibles au loin, rendu plus esthéthique.

---

## (Combinaisons requises de paramètres)

###  pluie 
Pour de la pluie visible et fiable, réunir :
1. `precip_rate > 0` 
2. `cloud_type` défini — n'importe lequel, mais `3` (Cumulonimbus) donne la
   meilleure couverture (ou laisser `-1` : le code force alors Cumulonimbus auto)
3. `cloud_coverage` élevé (proche de `1`) → la pluie tombe là où est l'avion
4. `cloud_thickness_m > 0` (sinon aucune particule) — viser plusieurs milliers de m pour Cb
5. `temperature_c > 0` (positif = pluie)
6. `cloud_margin_m` (nuage pas trop haut, sinon pluie hors champ)

Optionnel : `rain_scale` pour la taille des gouttes ;
`weather_effect_duration > 0` pour des flaques au sol.

###  neige
Identique à la pluie, mais `temperature_c < 0` (négatif = neige).
`weather_effect_duration > 0` pour accumuler une couche de neige au sol.

### nuages seuls (sans précipitation)
- `precip_rate = 0`
- `cloud_type >= 0`, `cloud_coverage` selon densité voulue,
  `cloud_thickness_m > 0`, `cloud_margin_m`.

### brouillard
- `fog_visibility < 50000` Indépendant du reste.

###  accumulation au sol (flaques / neige)
-  pluie ou neige + `weather_effect_duration > 0` (plus la valeur est
  grande, plus l'accumulation est marquée).

---

## Tableau récapitulatif des dépendances

| Effet voulu | Paramètres requis |
|-------------|-------------------|
| Pluie | `precip_rate>0` + `cloud_type` (idéal 3) + `cloud_coverage`↑ + `cloud_thickness_m>0` + `temperature_c>0` + `cloud_margin_m`  |
| Neige | idem pluie mais `temperature_c<0` |
| Nuages secs | `cloud_type>=0` + `cloud_thickness_m>0` + `precip_rate=0` |
| Brouillard | `fog_visibility<50000` |
| Flaques / neige au sol | recette pluie/neige + `weather_effect_duration>0` |
| Grosses/fines gouttes | `rain_scale` (avec pluie active) |
