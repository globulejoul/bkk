# Bangkok Watch — Vols & Hôtels

Surveillance auto-hébergée des prix **vols + hôtels** pour la Thaïlande pendant les vacances scolaires Zone A (Lyon).

Stack : Docker · Python · FastAPI · SQLite · fli (Google Flights) · Duffel API · Playwright (Google Hotels) · ntfy.

## Fonctionnalités

### Vols

| | |
|---|---|
| **Dashboard web** | UI temps réel avec courbes, heatmap calendrier, breakdown par aéroport |
| **Google Flights + compagnies directes** | fli (API reverse-engineered) + Duffel (300+ compagnies) |
| **Toutes les combinaisons dates** | Scan ±N jours aller × ±N jours retour (configurable) |
| **Score achat 0-100** | Composite percentile + tendance + jour semaine + délai départ |
| **Flash mode** | Checks toutes les 5 min pendant 48h quand le seuil est atteint |
| **Comparaison A/R vs 2 allers simples** | Détecte si 2 OW est moins cher |
| **Open-jaw** | Compare CDG→BKK + CNX→LYS vs A/R classique |

### Hôtels

| | |
|---|---|
| **Google Hotels via Playwright** | Scraping Chromium headless, compare les prix par provider |
| **Multi-providers** | Booking.com, Agoda, Expedia, Hotels.com, Trip.com, Traveloka, eDreams... |
| **Dates flexibles** | Check-in / check-out configurables indépendamment des vols |
| **Alertes prix** | Notification quand le prix passe sous le seuil configuré |

### Général

| | |
|---|---|
| **Page Admin** | Configuration aéroports, voyageurs (adultes + enfants), dates, hôtels, seuils |
| **Météo Bangkok + cours EUR/THB** | Directement dans le dashboard |
| **Notifications ntfy** | Avec tendance, score, comparaisons marchés |
| **Watchdog** | Détecte les runs bloqués et libère le scheduler automatiquement |

## Architecture

```
docker-compose
└── watcher (FastAPI :8080 + APScheduler + SQLite + Playwright/Chromium)
    ├─→ fli             ← Google Flights (API directe)
    ├─→ Duffel API      ← compagnies aériennes (AF, Emirates, QR, EY...)
    ├─→ Playwright      ← Google Hotels (Chromium headless)
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

cp config.example.yml config.yml
nano config.yml  # personnaliser
```

**Clé Duffel** : créer un compte sur https://app.duffel.com → Access Tokens → token live Read+Write.

### 3. Personnaliser `config.yml`

Le fichier `config.example.yml` sert de référence. `config.yml` est gitignored (jamais écrasé par un deploy).

- `origins` / `destinations` : aéroports IATA
- `adults` / `children` : nombre de voyageurs et âges des enfants
- `schedule_cron` : fréquence des checks (`"0 */6 * * *"` = toutes les 6h)
- `trips[].price_threshold` : seuil alerte vol
- `hotels[]` : hôtels à surveiller avec dates check-in/check-out et seuil

Tous ces paramètres sont aussi modifiables depuis la **page Admin** du dashboard.

### 4. Lancer

```bash
docker compose up -d
docker compose logs -f watcher
```

Le premier build est plus long (~2 min) car il installe Chromium pour Playwright.

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

**Vue d'ensemble** : carte par période avec prix actuel, plus bas/moyenne/plus haut, seuil cible, badges tendance et score achat. Météo Bangkok et cours EUR/THB.

**Détail période** : courbe d'évolution, tableau des meilleurs prix par combinaison (origine × destination × source × dates), heatmap calendrier, statistiques (tendance, score, prix moyen par jour).

**Hôtels** : prix actuel par provider, historique, comparaison Booking vs Agoda vs Expedia, etc.

**Admin** : aéroports de départ/arrivée, voyageurs (adultes + enfants avec âges), périodes de vacances avec toggle on/off et flexibilité des dates, hôtels surveillés avec dates et seuils, durée de vol max.

**Alertes** : historique avec contexte complet.

## Comportement des sources

À chaque run :

1. **fli / Google Flights** : 1 recherche par paire (origin × dest) sur la date médiane.
2. **Duffel** : toutes les combinaisons de dates. Rate limiting adaptatif (60 req/min).
3. **Sur alerte vol** : comparaison 2 OW vs A/R + open-jaw.
4. **Google Hotels** : 1 scrape Playwright par hôtel configuré (timeout 60s par scrape).

## Coûts

| Service | Coût |
|---|---|
| Duffel | Gratuit (facturation sur bookings uniquement) |
| fli / Google Flights | Gratuit |
| Playwright / Google Hotels | Gratuit |
| Frankfurter / Open-Meteo | Gratuit |
| ntfy.sh | Gratuit |

**Total : 0€/mois.**

## Maintenance

```bash
# Logs
docker compose logs -f watcher

# Restart après modif config via admin
docker compose restart watcher

# Deploy (ne touche pas à config.yml)
git pull && docker compose build watcher && docker compose up -d watcher

# Backup base
cp data/prices.db data/prices.db.backup-$(date +%F)
```

## Sécurité

- `.env` contient la clé Duffel — **jamais commité** (`.gitignore`)
- `config.yml` est **gitignored** — les modifications admin ne sont jamais écrasées par un deploy
- Port 8080 bindé à `127.0.0.1` uniquement
- `robots.txt` + meta `noindex` — pas d'indexation par les moteurs de recherche
- Pas d'auth native — ajouter basic auth via Caddy si exposé sur internet
