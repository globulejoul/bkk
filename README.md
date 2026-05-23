# ✈️ Flight Watcher v4 — Lyon/Paris → Thaïlande

Surveillance auto-hébergée des prix vols **CDG/LYS/BSL/GVA → BKK/HKT/CNX** pendant les vacances scolaires Zone A (Lyon).

Stack : Docker Compose · Python · FastAPI · SQLite · fli (Google Flights) · Duffel API · ntfy.

## Nouveautés v4

| | |
|---|---|
| 🏠 **Self-hosted Docker** | Tourne sur ton Ubuntu/home server, plus de dépendance GitHub Actions |
| 🆓 **fast-flights** remplace SerpAPI | Google Flights scraping gratuit, sans clé API, sans quota |
| 🌐 **VPN Thaïlande via Gluetun** | Container sidecar NordVPN → vrais prix marché thaï |
| 📊 **Dashboard web** sur :8080 | UI temps réel, courbes par période, breakdown par aéroport |
| ✈️ **Multi-destinations Thaïlande** | BKK, DMK, HKT (Phuket), CNX (Chiang Mai), KBV (Krabi) |
| 🛫 **Tracking par aéroport de départ** | Sais en un coup d'œil si LYS ou CDG est moins cher |
| 💾 **SQLite persistant** | Historique complet exportable, plus de dance Git |

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  docker-compose                                          │
│                                                          │
│  ┌──────────────────┐         ┌──────────────────────┐  │
│  │ gluetun (NordVPN)│ ◄───┐   │ watcher              │  │
│  │ Thailand exit    │     │   │ ├─ APScheduler cron  │  │
│  │ HTTP proxy :8888 │     │   │ │  → 2x/jour défaut  │  │
│  └──────────────────┘     │   │ ├─ FastAPI :8080     │  │
│                           │   │ ├─ SQLite /data      │  │
│                           └───┤ └─ ntfy push         │  │
│                               └──────────────────────┘  │
└──────────────────────────────────────────────────────────┘
       │
       ├─→ Tequila API (Kiwi)      ← prix Kiwi
       ├─→ fast-flights direct      ← Google Flights FR
       ├─→ fast-flights via gluetun ← Google Flights TH
       ├─→ frankfurter.app          ← taux EUR↔THB
       └─→ ntfy.sh/frigate-…        ← notifs mobile
```

## Installation (Ubuntu + Docker)

### 1. Prérequis

```bash
# Docker + Compose
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker

# Ton user dans le groupe docker (logout/login après)
sudo usermod -aG docker $USER
```

### 2. Cloner et configurer

```bash
git clone <ton-fork> flight-watcher
cd flight-watcher
cp .env.example .env
nano .env  # remplir TEQUILA_API_KEY, NORDVPN_USER, NORDVPN_PASS
```

**Récupérer les credentials NordVPN "service"** (différent du login compte) :
https://my.nordaccount.com/dashboard/nordvpn/manual-configuration/

**Récupérer une clé Tequila** (gratuit) :
https://tequila.kiwi.com/portal/login/register → My Solutions → Create Solution → Meta Search → "One way and Return"

### 3. Personnaliser `config.yml`

Toutes les options sont commentées dans le fichier. Notamment :
- `destinations` : enlève HKT/CNX/KBV si tu veux Bangkok seulement
- `schedule_cron` : `"0 7,19 * * *"` = 2x/jour, monte à `"0 */3 * * *"` pour toutes les 3h
- `trips[].price_threshold` : seuil sous lequel tu reçois une alerte prioritaire ntfy

### 4. Lancer

```bash
docker compose up -d
docker compose logs -f watcher  # vérifier que ça démarre
```

Le dashboard est sur **http://<ton-serveur>:8080**.

Pour vérifier que le VPN fonctionne :
```bash
docker compose exec gluetun wget -qO- https://ipinfo.io/country
# Doit afficher: TH
```

### 5. Lancer un check manuel (test)

Soit via l'UI (bouton **↻ Check**), soit en CLI :
```bash
docker compose exec watcher python -m app run
```

### 6. Abonnement ntfy

Sur ton mobile : app ntfy → ajouter le topic configuré dans `config.yml` (serveur `https://ntfy.sh`).

## Utilisation au quotidien

- **Dashboard** : http://serveur:8080 — vue d'ensemble, détail par période, alertes, logs runs
- **Notifications** : ntfy sur ton mobile, sur nouveau prix bas / seuil atteint / hausse significative
- **Données** : `./data/prices.db` (SQLite, ouvrable avec DBeaver/SQLiteBrowser)

## Comment lire le dashboard

**Vue d'ensemble** : une carte par période, prix actuel en gros, plus bas/moyenne/plus haut, seuil cible.

**Détail période** : courbe d'évolution (jour par jour, prix min toutes combinaisons), tableau du meilleur prix par combinaison `origine × destination`. Le ★ marque la combinaison gagnante du moment — utile pour savoir si CDG-HKT bat LYS-BKK ce mois-ci.

**Alertes** : historique de tout ce qui a été envoyé sur ntfy, avec contexte complet.

**Logs** : exécutions du watcher, durée, erreurs éventuelles.

## Comportement des sources

À chaque run, pour chaque période :

1. **Tequila EUR** : 1 appel API qui scanne toute la fenêtre ±3j × toutes les origines × toutes les destinations en une fois. C'est la **source primaire** qui détermine le best price.
2. **Tequila THB** : ne se déclenche que sur alerte. Révèle la marge FX de Kiwi.
3. **fast-flights direct** : ne se déclenche que sur alerte. Vérifie le top result sur Google Flights (couverture Expedia, Booking, compagnies en direct).
4. **fast-flights via VPN** : ne se déclenche que sur alerte. Même requête mais routée via le sidecar gluetun → Google retourne les prix marché thaïlandais en THB.

Comparaison nette dans la notif :
```
🎯 SEUIL ATTEINT — 712€ (↓138€)
TG+QR • CDG → BKK
Aller: 2026-10-17 (14.2h, 1 esc.)
Retour: 2026-11-02 (15.5h, 1 esc.)

Comparaison marchés:
• Kiwi THB ≈ 751€ (+5.4% FX margin)
• Google FR: 718€ (+0.8%) Thai Airways
• Google TH (VPN): 27400฿ ≈ 731€ (+2.6%) Thai Airways
```

## Coûts API

| Service | Volume mensuel (2 runs/j) | Limite |
|---|---|---|
| Tequila EUR | 300 calls | Free tier OK |
| Tequila THB | 5-30 calls (sur alerte) | Free tier OK |
| fast-flights | 10-60 calls (sur alerte) | Gratuit, scraping |
| Frankfurter | 60 calls | Gratuit illimité |
| NordVPN | usage standard | Forfait existant |
| ntfy.sh | quelques alertes | Gratuit |

**Total : 0€/mois** (hors NordVPN que tu as déjà).

## Migration depuis v3 (GitHub Actions)

Si tu as déjà des données dans le repo GH Actions (`history.csv`, `lowest_prices.json`), tu peux les importer dans la nouvelle base SQLite avec un script simple — dis-moi si tu veux que je te le fasse.

## Maintenance

```bash
# Logs
docker compose logs -f watcher
docker compose logs -f gluetun

# Restart après modif de config.yml (pas besoin de rebuild)
docker compose restart watcher

# Rebuild après modif de code Python
docker compose build watcher && docker compose up -d watcher

# Mise à jour des images (gluetun + python base)
docker compose pull && docker compose up -d

# Backup
cp data/prices.db data/prices.db.backup-$(date +%F)
```

## Reverse proxy (optionnel)

Si tu veux exposer le dashboard derrière Caddy/Traefik/nginx avec HTTPS, le service `watcher` écoute sur `0.0.0.0:8080`. Exemple Caddy :

```caddy
flights.exemple.fr {
    reverse_proxy localhost:8080
    basic_auth {
        toi <hash>
    }
}
```

## Sécurité

- `data/` contient l'historique des prix (pas d'info sensible mais à protéger)
- `.env` contient tes credentials NordVPN et Tequila — **jamais commité** (déjà dans `.gitignore`)
- L'UI n'a pas d'auth native. Si tu l'exposes en dehors du LAN : ajoute basic auth via reverse proxy.

## Évolutions possibles

- Anomaly detection percentile-based (le prix actuel est dans le 10e percentile historique → alerte rang)
- Round-trip vs 2 one-ways (parfois -100-300€ sur Asie)
- Filtre temps de vol par tranche horaire (éviter départ 4h matin)
- Seats.aero pour vols en miles Flying Blue / Avios
- Export iCal des "good buy zones"

Dis-moi laquelle prioriser pour une v5.
