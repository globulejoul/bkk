# Flight Watcher — Lyon → Thaïlande

Surveillance auto-hébergée des prix vols **CDG/LYS/BSL/GVA/MXP → BKK/HKT/CNX** pendant les vacances scolaires Zone A (Lyon).

Stack : Docker · Python · FastAPI · SQLite · fli (Google Flights) · Duffel API · ntfy.

## Fonctionnalités

| | |
|---|---|
| **Dashboard web** | UI temps réel avec courbes, heatmap calendrier, breakdown par aéroport |
| **Google Flights + compagnies directes** | fli (API reverse-engineered) + Duffel (300+ compagnies) |
| **Toutes les combinaisons dates** | Scan ±3j aller × ±3j retour (49 combos par période) |
| **Score achat 0-100** | Composite percentile + tendance + jour semaine + délai départ |
| **Flash mode** | Checks toutes les 5 min pendant 48h quand le seuil est atteint |
| **Comparaison A/R vs 2 allers simples** | Détecte si 2 OW est moins cher |
| **Open-jaw** | Compare CDG→BKK + CNX→LYS vs A/R classique |
| **Météo Bangkok + cours EUR/THB** | Directement dans le dashboard |
| **Notifications ntfy** | Avec tendance, score, comparaisons marchés |

## Architecture

```
docker-compose
└── watcher (FastAPI :8080 + APScheduler + SQLite)
    ├─→ fli             ← Google Flights (API directe, pas de scraping)
    ├─→ Duffel API      ← compagnies aériennes (AF, Emirates, QR, EY...)
    ├─→ frankfurter.app ← taux EUR↔THB
    ├─→ Open-Meteo      ← météo Bangkok
    └─→ ntfy.sh         ← notifs mobile
```

## Installation

### 1. Prérequis

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER  # logout/login après
```

### 2. Cloner et configurer

```bash
git clone https://github.com/globulejoul/bkk.git
cd bkk
cp .env.example .env
nano .env  # remplir DUFFEL_API_KEY
```

**Clé Duffel** : créer un compte sur https://app.duffel.com → Access Tokens → token live Read+Write.

### 3. Personnaliser `config.yml`

- `origins` / `destinations` : aéroports à surveiller
- `schedule_cron` : fréquence des checks (`"0 */6 * * *"` = toutes les 6h)
- `trips[].price_threshold` : seuil sous lequel tu reçois une alerte prioritaire

### 4. Lancer

```bash
docker compose up -d
docker compose logs -f watcher
```

### 5. Reverse proxy (recommandé)

Derrière Caddy :
```caddy
bkk.exemple.fr {
    reverse_proxy localhost:8080
}
```

Le port 8080 est bindé à localhost par défaut.

### 6. Notifications ntfy

App ntfy → ajouter le topic configuré dans `config.yml` (serveur `https://ntfy.sh`).

## Dashboard

**Vue d'ensemble** : carte par période avec prix actuel, plus bas/moyenne/plus haut, seuil cible, badges tendance et score achat. Météo Bangkok et cours EUR/THB en bas.

**Détail période** : courbe d'évolution, tableau des meilleurs prix par combinaison (origine × destination × source × dates), heatmap calendrier des prix (toutes les combos de dates en vert/orange), statistiques (tendance, score, prix moyen par jour de la semaine).

**Alertes** : historique ntfy avec contexte complet (tendance, score, comparaisons OW/open-jaw).

## Comportement des sources

À chaque run (~70 min pour 5 périodes) :

1. **fli / Google Flights** : 1 recherche par paire (origin × dest) sur la date médiane. Couvre les OTAs (Expedia, Booking) et les compagnies directes.
2. **Duffel** : toutes les combinaisons de dates (49 combos × 15 paires = 735 appels). Rate limiting adaptatif (60 req/min). Retourne les prix directs des compagnies aériennes.
3. **Sur alerte** : comparaison 2 allers simples vs A/R + open-jaw (origines/destinations croisées).

## Coûts

| Service | Coût |
|---|---|
| Duffel | Gratuit (facturation uniquement sur les bookings, on n'en fait pas) |
| fli / Google Flights | Gratuit (API reverse-engineered) |
| Frankfurter / Open-Meteo | Gratuit |
| ntfy.sh | Gratuit |

**Total : 0€/mois.**

## Maintenance

```bash
# Logs
docker compose logs -f watcher

# Restart après modif config.yml
docker compose restart watcher

# Rebuild après modif code Python
docker compose build watcher && docker compose up -d watcher

# Backup base
cp data/prices.db data/prices.db.backup-$(date +%F)
```

## Sécurité

- `.env` contient la clé Duffel — **jamais commité** (dans `.gitignore`)
- Port 8080 bindé à `127.0.0.1` uniquement (accès via reverse proxy)
- Pas d'auth native — ajouter basic auth via Caddy si exposé sur internet
