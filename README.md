# Bangkok Watch — Vols & Hôtels

Surveillance auto-hébergée des prix **vols + hôtels** pour la Thaïlande pendant les vacances scolaires Zone A (Lyon).

Stack : Docker · Python 3.12 · FastAPI · SQLite · fli (Google Flights) · Duffel API · Playwright (Google Hotels) · ntfy.

## Fonctionnalités

### Vols

| | |
|---|---|
| **Dashboard web** | Cartes par période avec sparklines, prix/personne, courbes d'évolution par route |
| **Google Flights + compagnies directes** | fli (API reverse-engineered) + Duffel (300+ compagnies) |
| **Toutes les combinaisons dates** | Scan ±N jours aller × ±N jours retour (configurable via admin) |
| **Graphique multi-routes** | Une courbe par route (CDG→BKK, LYS→HKT...) à chaque run, sélecteur 30j/60j/90j/tout |
| **Score achat 0-100** | Composite percentile + tendance + jour semaine + délai départ |
| **Flash mode** | Checks toutes les 5 min pendant 48h quand le seuil est atteint |
| **Comparaison A/R vs 2 allers simples** | Détecte si 2 OW est moins cher |
| **Open-jaw** | Compare CDG→BKK + CNX→LYS vs A/R classique |

### Hôtels

| | |
|---|---|
| **Google Hotels via Playwright** | Scraping Chromium headless, recherche par nom d'hôtel |
| **Multi-providers** | Booking.com, Agoda, Expedia, Hotels.com, Trip.com, Traveloka, eDreams... |
| **Dates indépendantes** | Check-in / check-out configurables indépendamment des vols |
| **Alertes prix** | Notification quand le prix passe sous le seuil configuré |
| **Échecs visibles** | Un scrape raté est tracé (dernière erreur, échecs consécutifs), jamais confondu avec « pas de prix » |

### Général

| | |
|---|---|
| **Page Admin** | Aéroports, voyageurs (adultes + enfants), périodes on/off, hôtels, seuils, durée vol max |
| **Prix par personne** | Affiché sur chaque carte quand >1 voyageur |
| **Sparklines** | Mini courbes 30j directement sur les cartes de la vue d'ensemble |
| **Météo Bangkok + cours EUR/THB** | Directement dans le dashboard |
| **Notifications ntfy** | Vols : tendance, score, comparaisons. Hôtels : prix par provider |
| **Watchdog** | Détecte les runs bloqués (>15min), libère le scheduler, reset les locks fantômes |

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

Un seul conteneur. Pas de VPN, pas de base externe, pas de worker séparé.

## Installation

### 1. Prérequis

Ubuntu (paquets de la distribution) :

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker $USER  # logout/login après
```

> Le paquet `docker-compose-plugin` n'existe que dans le dépôt Docker Inc. Sur une
> Ubuntu vierge, c'est `docker-compose-v2` qui fournit `docker compose`. Si vous
> utilisez le dépôt officiel Docker, installez plutôt `docker-ce` +
> `docker-compose-plugin`.

### 2. Cloner et configurer

```bash
git clone https://github.com/globulejoul/bkk.git
cd bkk
cp .env.example .env
nano .env  # voir la note Duffel ci-dessous

cp config.example.yml config.yml
nano config.yml  # personnaliser, en particulier le topic ntfy
```

**Clé Duffel** : créer un compte sur https://app.duffel.com → Access Tokens →
token live Read+Write.

> **Duffel est facturable.** Au-delà d'un ratio de 1 500 recherches par
> réservation, Duffel facture 0,005 $ par recherche supplémentaire
> (« excess search »). Un compte qui ne réserve jamais dépasse ce ratio dès les
> premiers runs. Sur cette installation la clé est **volontairement laissée
> vide / commentée** : sans clé, la recherche Duffel est ignorée et fli reste la
> seule source de vols. N'activez la clé qu'en connaissance de cause et en
> surveillant la facturation.

### 3. Personnaliser `config.yml`

Le fichier `config.example.yml` sert de référence. `config.yml` est gitignored
(jamais écrasé par un deploy).

- `origins` / `destinations` : aéroports IATA
- `adults` / `children` : nombre de voyageurs et âges des enfants
- `max_fly_duration_hours` : durée de vol max acceptée
- `schedule_cron` : fréquence des checks (`"0 */6 * * *"` = toutes les 6h)
- `ntfy.topic` : **à changer obligatoirement** (voir Sécurité)
- `trips[]` : périodes de vacances avec fenêtres dates, seuil, toggle on/off
- `hotels[]` : hôtels à surveiller avec dates check-in/check-out et seuil

Tous ces paramètres sauf le bloc `ntfy` sont aussi modifiables depuis la
**page Admin** du dashboard.

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

**Vue d'ensemble** : carte par période avec prix actuel, prix/personne, sparkline 30j, plus bas/moyenne/plus haut, seuil cible, badges tendance et score achat. Météo Bangkok et cours EUR/THB.

**Détail période** : graphique multi-courbes (une ligne par route à chaque run, sélecteur 30j/60j/90j/tout), tableau des meilleurs prix par combinaison (origine × destination × source × dates), heatmap calendrier, statistiques (tendance, score, prix moyen par jour).

**Hôtels** : prix actuel par provider, historique, comparaison Booking vs Agoda vs Expedia, etc.

**Admin** : aéroports de départ/arrivée, voyageurs (adultes + enfants avec âges), durée vol max, périodes de vacances avec dates officielles Zone A, toggle on/off et flexibilité des dates, hôtels surveillés avec dates et seuils.

**Alertes** : historique avec contexte complet.

## Comportement des sources

À chaque run :

1. **fli / Google Flights** : 1 recherche par paire (origin × dest) sur la date médiane.
2. **Duffel** : toutes les combinaisons de dates, rate limiting adaptatif (60 req/min).
   Ignoré si `DUFFEL_API_KEY` est absente.
3. **Sur alerte vol** : comparaison 2 OW vs A/R + open-jaw.
4. **Google Hotels** : 1 scrape Playwright par hôtel configuré (timeout 60s par scrape).

Les périodes désactivées, échues, ou sans combinaison de dates valide sont
sautées, de même que les hôtels dont le check-in est passé.

### Note sur le paquet `fli`

Trois noms différents pour la même dépendance :

| | |
|---|---|
| Nom PyPI | `flights` — c'est ce qui figure dans `requirements.txt` |
| Nom d'import Python | `fli` |
| Dépôt | https://github.com/punitarani/fli |

`pip install fli` installe un paquet sans rapport. La version est épinglée dans
`requirements.txt`. En cas de casse (Google change son format de réponse),
regarder les issues du dépôt plutôt que de mettre à jour à l'aveugle.

## Coûts

| Service | Coût |
|---|---|
| Duffel | Gratuit tant que le ratio recherches/réservations est respecté — au-delà, 0,005 $ par recherche |
| fli / Google Flights | Gratuit |
| Playwright / Google Hotels | Gratuit |
| Frankfurter / Open-Meteo | Gratuit |
| ntfy.sh | Gratuit |

**Total : 0 €/mois** avec la clé Duffel désactivée.

## Maintenance

```bash
# Logs
docker compose logs -f watcher

# Est-ce qu'un run est en cours ? (à vérifier AVANT tout déploiement)
curl -s localhost:8080/healthz            # {"ok":true,"next_run":"...","running":false}
curl -s "localhost:8080/api/runs?limit=1"

# Déclencher un check immédiat (409 si un run tourne déjà)
curl -X POST localhost:8080/api/run-now

# Deploy (ne touche pas à config.yml)
git pull && docker compose build watcher && docker compose up -d watcher
```

Aucun `restart` n'est nécessaire après une modification via la page Admin : la
config est relue à chaque requête et le cron est re-planifié à chaud. Un
`docker compose restart` gratuit interrompt le run en cours (un run complet peut
durer longtemps).

## Sauvegarde / restauration

La base SQLite est en mode **WAL**. Un simple `cp` pendant que le conteneur
tourne n'est **pas** une sauvegarde fiable : au mieux vous copiez une base
valide mais amputée des dernières écritures restées dans le `-wal`, au pire vous
écrasez le fichier ouvert et obtenez un `SQLITE_CORRUPT`.

### Sauvegarder

```bash
# Copie cohérente depuis le conteneur, sans rien installer
docker compose exec watcher python -c "import sqlite3, datetime; d = datetime.date.today().isoformat(); sqlite3.connect('/app/data/prices.db').execute('VACUUM INTO ' + repr('/app/data/prices.db.backup-' + d))"

# Variante depuis l'hôte si le CLI sqlite3 y est installé (sudo apt install sqlite3)
sqlite3 data/prices.db ".backup data/prices.db.backup-$(date +%F)"
```

Le CLI `sqlite3` n'est pas présent dans l'image Docker : la première commande
est celle qui fonctionne partout. Le fichier produit est un fichier unique et
cohérent, sans `-wal` ni `-shm` à côté.

À automatiser côté hôte, par exemple une fois par semaine avec rotation de 4
copies, et à recopier ailleurs (`scp`/`rsync`) : `data/` est gitignored, il n'en
existe aucune copie hors du VPS.

### Restaurer

```bash
docker compose stop watcher                    # OBLIGATOIRE : ne jamais écrire sous le conteneur
rm -f data/prices.db-wal data/prices.db-shm    # un WAL résiduel serait rejoué sur la base restaurée
cp data/prices.db.backup-AAAA-MM-JJ data/prices.db
docker compose start watcher
docker compose logs -f watcher                 # vérifier les migrations au démarrage
```

Ne copiez jamais `prices.db-wal` / `prices.db-shm` séparément : ces fichiers
n'ont de sens qu'avec la base exacte qui les a produits.

## Sécurité

- `.env` contient la clé Duffel — **jamais commité** (`.gitignore`)
- `config.yml` est **gitignored** — les modifications admin ne sont jamais écrasées par un deploy
- **Le topic ntfy est un secret.** Sur ntfy.sh, un topic non réservé est ouvert
  en lecture **et** en écriture à quiconque connaît son nom : il peut lire vos
  alertes en temps réel et vous envoyer de fausses notifications. Utilisez un
  nom long et aléatoire (`openssl rand -hex 16`), ne le commitez jamais, et ne
  le partagez pas avec un autre usage. `NTFY_TOKEN` ne protège que l'émission,
  pas la lecture.
- Port 8080 bindé à `127.0.0.1` uniquement
- `/docs`, `/redoc` et `/openapi.json` sont désactivés sauf si `DEBUG=1`
- `robots.txt` + meta `noindex` — pas d'indexation par les moteurs de recherche
- Pas d'auth native — ajouter basic auth via Caddy si exposé sur internet.
  `PUT /api/admin/config` réécrit la configuration sans authentification.
