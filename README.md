# Sentinel-2 L1C Time-Series Pipeline

Imagine que tu veux un **album photo d'un même endroit vu du ciel**, une photo par jour où le satellite
Sentinel-2 est passé. Cette pipeline fabrique cet album automatiquement à partir des données du
[Copernicus Data Space Ecosystem (CDSE)](https://dataspace.copernicus.eu/).

---

## Ce qu'il se passe dans la pipeline

Elle travaille en 4 étapes.

### 1. Chercher (discovery)

Tu donnes un point (latitude, longitude) et deux dates. La pipeline demande au catalogue de Copernicus :
« Quelles photos Sentinel-2 existent ici entre ces deux dates ? »

Elle note **toutes** les réponses dans un cahier (`metadata.csv`), même celles qu'elle jettera ensuite.

### 2. Trier (selection)

Elle enlève ce qui ne sert à rien :

- les **doublons** : même photo retraitée plusieurs fois par l'ESA, elle garde la plus récente ;
- les photos **trop nuageuses** ;
- les photos qui **ne couvrent pas bien** ta zone ;
- s'il y a plusieurs photos le même jour, elle garde **la meilleure**.

Pour chaque photo jetée, elle écrit **pourquoi** dans le cahier.

### 3. Vérifier (contrôle qualité)

Pour les photos gardées, elle télécharge un carré de **10 km × 10 km** (1024 × 1024 pixels, 10 m par
pixel), dans les **13 couleurs** (bandes) du satellite. Ensuite elle regarde chaque pixel :

- « Est-ce qu'il y a vraiment une donnée ici ? » (sinon c'est un trou, du *nodata*) ;
- « Est-ce que c'est un nuage ? Un pixel abîmé ? Trop brillant (saturé) ? »

Si la photo a trop de trous, de nuages ou de défauts, elle est **rejetée**.

### 4. Ranger (output)

Les photos qui passent le contrôle sont enregistrées en fichiers `.npz`, un par date. Le cahier
`metadata.csv` dit pour chaque photo si elle a été **sauvée**, **rejetée**, en **échec** ou **déjà là**.

Toutes les photos sont posées sur **exactement la même grille** : le pixel (500, 500) est le même
endroit au sol à chaque date.

---

## Tuto : faire ton premier appel

### Étape 0 : installer (une seule fois)

Dans un terminal, dans le dossier du projet :

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

(Sous Linux / macOS, remplace `.venv\Scripts\python.exe` par `.venv/bin/python` partout dans ce tuto.)

### Étape 1 : donner tes clés Copernicus (une seule fois)

Chercher des photos est gratuit et anonyme, mais **télécharger** demande un compte CDSE gratuit.

1. Crée un compte sur <https://dataspace.copernicus.eu/>.
2. Génère des clés S3 sur <https://eodata-s3keysmanager.dataspace.copernicus.eu>.
3. Crée un fichier `.env` à la racine du projet avec :

```text
CDSE_S3_ACCESS_KEY=ta_cle_d_acces
CDSE_S3_SECRET_KEY=ta_cle_secrete
```

Le fichier `.env` est ignoré par git : tes clés ne partent pas sur GitHub.

### Étape 2 : regarder avant de télécharger

Un appel = une commande avec **où**, **quand** et **dans quel dossier ranger**. Ajoute `--dry_run` pour
juste voir la liste des dates, sans rien télécharger :

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --output_dir data/paris_2023 --dry_run
```

Tu obtiens quelque chose comme :

```text
date        tile   cloud%  cover  base        crs  item_id
2023-02-07  31UDQ    0.00  1.000  5.10 EPSG:32631  S2A_MSIL1C_20230207T110221_N0510_R094_T31UDQ_...
2023-02-14  31UDQ    0.00  1.000  5.10 EPSG:32631  S2A_MSIL1C_20230214T105141_N0510_R051_T31UDQ_...
...
```

### Étape 3 : télécharger pour de vrai

Même commande, **sans** `--dry_run` :

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 20 --output_dir data/paris_2023
```

Compte environ 30 secondes par date. Si ça s'arrête en route (coupure, PC fermé), **relance exactement
la même commande** : la pipeline reprend où elle en était.

### Étape 4 : ouvrir tes images

```python
import numpy as np

z = np.load("data/paris_2023/2023-02-07_31UDQ.npz")
image = z["data"]        # les 13 bandes : forme (13, 1024, 1024)
bons = z["valid_mask"]   # True = pixel fiable, False = trou ou pixel douteux
bandes = z["bands"].tolist()

rouge = image[bandes.index("B04")]
vert = image[bandes.index("B03")]
bleu = image[bandes.index("B02")]
```

Pour savoir pourquoi une date manque, ouvre `data/paris_2023/metadata.csv` (par exemple dans Excel) et
regarde les colonnes `status` et `rejection_reason`.

### Autres exemples d'appels

Les 20 images les plus claires entre 2020 et 2024 à Toulouse :

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 43.6047 --lon 1.4442 --start_date 2020-01-01 --end_date 2024-12-31 --max_cloud 20 --max_images 20 --sort cloud --output_dir data/toulouse_top20
```

Des images vraiment sans nuages **sur ta zone** (tri grossier puis tri fin) :

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --lat 48.8566 --lon 2.3522 --start_date 2023-01-01 --end_date 2023-12-31 --max_cloud 30 --max_cloud_fraction 0.05 --output_dir data/paris_2023_clair
```

---

## Les paramètres qui t'intéressent

### Les indispensables

| Paramètre | C'est quoi | Exemple |
|---|---|---|
| `--lat`, `--lon` | Le centre de ta zone | `--lat 48.8566 --lon 2.3522` (Paris) |
| `--start_date`, `--end_date` | Tes 2 dates (incluses), format `AAAA-MM-JJ` | `--start_date 2023-01-01 --end_date 2023-12-31` |
| `--output_dir` | Le dossier où ranger les fichiers | `--output_dir data/paris_2023` |

### Pour choisir combien de photos et lesquelles

| Paramètre | C'est quoi | Conseil |
|---|---|---|
| `--max_cloud` | % de nuages maximum sur toute la tuile satellite (100 × 100 km) | `20` pour des images assez claires |
| `--max_images` | Nombre maximum de dates | `--max_images 20` |
| `--sort` | Avec `--max_images` : `date` prend les premières dates, `cloud` les plus claires | `--sort cloud` |
| `--dry_run` | Juste regarder, sans télécharger | Toujours à faire en premier |

### Pour la qualité (déjà réglés par défaut, à changer seulement si besoin)

| Paramètre | Défaut | C'est quoi |
|---|---|---|
| `--min_valid_fraction` | `0.99` | Au moins 99 % des pixels doivent être bons dans les 13 bandes |
| `--max_cloud_fraction` | `1.0` (désactivé) | % max de nuages **dans ton carré de 10 km**, plus précis que `--max_cloud`. Exemple : `0.05` = 5 % |
| `--max_artefact_fraction` | `0.05` | % max de pixels abîmés par le capteur |
| `--max_saturated_fraction` | `0.01` | % max de pixels trop brillants |
| `--harmonize` | désactivé | Retire le décalage de +1000 que l'ESA ajoute aux valeurs depuis 2022. Utile pour comparer avec d'anciennes données |

**Astuce :** si tu veux des images vraiment sans nuages sur ta zone, combine `--max_cloud 30` (tri
grossier, rapide) et `--max_cloud_fraction 0.05` (tri fin sur ton carré).

Pour voir **tous** les paramètres :

```bash
.venv\Scripts\python.exe s2_l1c_pipeline.py --help
```

---

## Source des données

Contains modified Copernicus Sentinel data, provided by the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/).
