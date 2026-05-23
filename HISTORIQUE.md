# Historique des décisions — Flight Watcher

Document chronologique des itérations et de leur rationale. Utile si Claude Code (ou un humain reprenant le projet) veut comprendre **pourquoi** une décision a été prise.

---

## Itération 1 — Définition du besoin

**Demande initiale** : trouver un outil pour vérifier régulièrement les prix de vols Paris/Lyon → Bangkok, à ±3 jours des vacances scolaires Zone A Lyon, durée de vol < 18h.

L'utilisateur avait déjà identifié **Algofly** mais le trouvait fastidieux : il aurait fallu créer ~8 alertes différentes (2 origines × 4 périodes) pour couvrir son besoin.

**Options évaluées** :

1. **Google Flights "dates flexibles" + Track Prices** : no-code, multi-origines natif, alertes par baisse significative. Limite : pas de filtrage fin sur fenêtre exacte ±3j ni de seuil de prix configurable. ~4 alertes à créer.
2. **Algofly multi-alertes** : ~8 alertes manuelles. Tedious.
3. **Hopper / Kayak / Skyscanner** : alertes basiques, pas de multi-origines fin.
4. **Script custom avec une API** : Kiwi Tequila (free tier, supporte fenêtres dates natives), Amadeus Self-Service, ou SerpAPI Google Flights.

**Choix** : script custom, solution la plus flexible. Le user était à l'aise avec un peu de code.

**Vérifications faites avant de coder** :
- Dates Zone A 2026-2027 : sources contradictoires entre Zone A et Zone C sur les vacances d'hiver. Vérifié auprès de la source officielle (arrêté du 22 octobre 2025) : Hiver Zone A 2027 = 13 fév → 1er mars 2027. À ne pas confondre avec Zone C (6 fév → 22 fév).
- Statut de l'API Kiwi Tequila : encore accessible librement en 2026 pour les solutions Meta Search.

---

## Itération 2 — Première implémentation (v1)

**Stack v1** :
- Python + Kiwi Tequila API (1 call par période grâce aux fenêtres de dates natives `dateFrom/dateTo` + `returnFrom/returnTo`)
- GitHub Actions cron quotidien
- Storage : commit `history.csv` + `lowest_prices.json` dans le repo
- Notifications : ntfy.sh (topic configuré dans config.yml)
- Détection : nouveau bas all-time + seuil par période

**Pourquoi Kiwi Tequila comme primary** : c'est la seule API gratuite avec support natif des fenêtres de dates flexibles. Avec ±3 jours sur l'aller ET le retour, ça fait 49 combinaisons (7×7) — un seul call Tequila au lieu de 49.

---

## Itération 3 — v2

Ajouts demandés par l'utilisateur :

- **Notifications ntfy** plus riches avec markdown
- **Alertes hausse** : si le prix remonte de +10% par rapport au plus bas observé sur les 7 derniers jours, prévenir → aide à décider "j'achète maintenant ou j'attends que ça redescende ?"
- **Cross-check Google Flights** (via SerpAPI à ce stade) pour la couverture metasearch
- **Dashboard HTML statique** auto-généré et publié sur GitHub Pages

Mise en place d'une fenêtre rolling de 14 jours dans `lowest_prices.json` pour faire de la détection de hausse.

---

## Itération 4 — Multi-marché (v3)

**Trois questions** de l'utilisateur :

### Q1 : "Tequila vérifie chez les agences (Expedia) et compagnies en direct ?"

**Réponse** : NON. Tequila est l'API B2B de Kiwi.com, qui est lui-même une OTA. On récupère **uniquement le catalogue Kiwi** (avec leurs marges et virtual interlining). Pas d'Expedia, pas de Booking, pas d'AF direct.

**Conséquence** : pour la vraie couverture metasearch, il faut **Google Flights** (via SerpAPI à l'époque, fast-flights depuis). Google Flights agrège ~300 compagnies + OTAs principales.

### Q2 : "On peut comparer EUR / THB ?"

**Réponse** : oui sur les deux côtés.
- Tequila EUR vs Tequila THB → révèle la marge FX de Kiwi
- Google Flights FR (gl=fr) vs Google Flights TH (gl=th) → révèle la vraie geo-discrimination

Ajout d'un module `fx.py` qui chope les taux EUR↔THB via **Frankfurter.app** (ECB rates, gratuit, sans clé). Fallback sur **open.er-api.com** si Frankfurter tombe.

### Q3 : "J'ai NordVPN, on peut ajouter une vérification depuis la Thaïlande ?"

**Réponse à ce moment-là** : non, inutile, parce que :
- Tequila est B2B → ne fait pas de geo discrimination sur ses clients API
- SerpAPI accepte `gl=th` qui simule un user thaï auprès de Google

→ **Pas de VPN à cette étape**. Décision revue à l'itération suivante.

### Bonus : démythification

Au passage, recherche sur les "mythes de pricing aérien" :

**Faux** (études DOT 2017, Consumer Reports 2016) :
- Les cookies / IP / recherches répétées font monter les prix → débunké
- Mode incognito fait baisser → débunké
- "Mardi 15h heure idéale" → statistique aggregée, pas individuel

**Vrai** :
- Geo (IP/pays apparent) → discrimination réelle documentée
- Devise de paiement → jusqu'à 7% via Dynamic Currency Conversion (étude Wise 2024)
- Site direct compagnie vs OTA → différences réelles fréquentes

---

## Itération 5 — Exploration de l'écosystème

L'utilisateur demande : VPS plus flexible que GitHub Actions ? Et de chercher sur GitHub/GitLab les projets utiles qu'on aurait ratés.

**Constat important** : l'utilisateur a déjà un **home server** qui tourne 24/7 (Frigate = NVR caméras self-hosted). Donc "VPS" = home server pour lui, coût marginal nul.

### Verdict VPS vs GHA pour ce cas

| | GHA | Home server |
|---|---|---|
| Setup | 5 min | 30 min |
| Coût | 0€ | 0€ déjà payé |
| Fréquence min | 15-30 min | 1 min si voulu |
| VPN rotation | Compliqué fragile | Trivial via Gluetun |
| Dashboard temps réel | Non (HTML statique) | Oui (FastAPI) |
| Persistance | Git commit dance | SQLite local |

→ Bascule recommandée si VPN rotation et dashboard live valent le coup.

### Projets découverts

| Projet | Verdict |
|---|---|
| **Fairtrail** (affromero/fairtrail) | Concurrent direct, fait exactement la même chose en plus polish, avec VPN rotation et extraction LLM. À considérer comme alternative complète. L'utilisateur a quand même choisi de continuer notre stack pour garder le contrôle. |
| **fast-flights** (AWeirdDev/flights) | Killer feature : scraping Google Flights via décodage du Protobuf URL `tfs`. **Gratuit, sans clé API, sans quota visible**. Remplace SerpAPI ($50/mois) parfaitement. |
| **Gluetun** (qdm12/gluetun) | Container sidecar VPN multi-provider, supporte NordVPN nativement (OpenVPN ou WireGuard), expose un HTTP proxy interne. Industry standard pour ce use case. |
| **travel-hacking-toolkit** (borski) | MCP servers pour Kiwi/Duffel/Skiplagged/Seats.aero/AwardWallet. Overkill mais intéressant si jamais le user veut explorer les vols award en miles. |
| **Skiplagged-*** | Hidden city ticketing. Niche : one-way only, pas de bagage soute, contre les CGV. À garder en tête, pas prioritaire. |
| **changedetection.io** | Watcher générique web. Pourrait surveiller une page promo AF en complément. |

---

## Itération 6 — Migration v4

**Choix utilisateur** (multi-select) :
- Direction : Docker sur home server Ubuntu
- Améliorations à intégrer : fast-flights (remplace SerpAPI), VPN sidecar Gluetun pour vraie rotation Thailand, UI web temps réel FastAPI, tracker séparé par aéroport de départ, multi-destinations Thaïlande

**Décisions techniques importantes** :

### Pourquoi SerpAPI → fast-flights
- Économie : free vs ~$50/mois
- Liberté : pas de quota mensuel à surveiller
- Trade-off : scraping = plus fragile. Mitigé en n'appelant que sur alerte.

### Pourquoi VPN par proxy HTTP plutôt que `network_mode: service:gluetun`
- Permet au watcher de faire des calls **locaux** ET des calls **VPN-routés** dans la même run, en swappant les vars d'env `http_proxy/https_proxy` sur les calls fast-flights.
- Plus simple que de gérer plusieurs network namespaces.
- Trade-off : nécessite que le user/lib respecte les vars d'env (Python `requests` le fait nativement).

### Pourquoi OpenVPN NordVPN (pas WireGuard)
- WireGuard avec NordVPN nécessite d'extraire manuellement la clé privée via un script externe (procédure compliquée non documentée par NordVPN).
- OpenVPN : credentials "service" copiables directement depuis le dashboard NordVPN.

### Pourquoi SQLite (pas PostgreSQL)
- Volume : ~100 rows/jour × 365 jours = ~36k rows/an. SQLite gère ça les doigts dans le nez.
- Backup = `cp prices.db prices.db.$(date)`.
- Pas de service supplémentaire à monitorer.

### Pourquoi APScheduler dans le même process que FastAPI
- Un seul container, une seule logique de restart, lifespan FastAPI gère le cycle de vie du scheduler.
- Alternative envisagée : container watcher séparé avec un loop sleep. Rejeté car plus de plumbing pour rien.

### Pourquoi vanilla JS + Chart.js (pas React)
- Volume code frontend ridicule (~400 lignes JS + 250 lignes CSS).
- Pas de build step, pas de node_modules dans le container Docker.
- Le user gère ça sans souci, pas besoin de framework.

### Pourquoi tracking par aéroport
- Demande user explicite. Tequila renvoie déjà `cityCodeFrom` et `cityCodeTo` sur chaque résultat.
- Implémentation : la table `checks` stocke chaque combinaison (origin, destination, date) séparément. Aggregations dans les queries SQL.
- L'UI montre un breakdown table avec le ★ sur la meilleure combinaison.

### Pourquoi multi-destinations Thaïlande (BKK + DMK + HKT + CNX + KBV)
- Demande user.
- Tequila supporte le multi-destination natif via `fly_to=BKK,DMK,HKT,CNX,KBV`.
- L'UI les affiche dans le breakdown, le user voit instantanément si Phuket bat Bangkok ce mois-ci.

---

## Améliorations qu'on a explicitement choisi de NE PAS faire (à ce stade)

L'utilisateur a vu ces options dans un multi-select et **ne les a pas cochées**. À ne refaire que sur redemande :

- ❌ Anomaly detection percentile-based
- ❌ Round-trip vs 2 one-ways comparison
- ❌ Filtre horaire de vol
- ❌ Seats.aero (vols en miles)

Ces items restent dans `CLAUDE.md` section "Améliorations identifiées non implémentées" pour mémoire.

---

## Choses qui ont été essayées et abandonnées

- **NordVPN sur GitHub Actions** (en v3) : envisagé, jugé inutilement complexe (openvpn + auth dans le workflow), abandonné au profit de SerpAPI `gl=th`. Puis ré-introduit en v4 quand on est passé sur Docker home server (Gluetun rend ça trivial).
- **Création d'un compte Telegram pour les alertes** : remplacé par ntfy dès la v2 (préférence utilisateur).
- **Email SMTP via Gmail** : envisagé, abandonné car ntfy déjà en place.

---

## Conventions de communication observées

L'utilisateur :
- Préfère le **français** dans la prose, accepte l'anglais dans le code et les logs
- Aime les **tableaux comparatifs** quand on évalue plusieurs options
- Apprécie les **trade-offs explicites** plutôt que les recommandations péremptoires
- Pose des **questions de fond** régulièrement (mythes pricing, sources d'info) plutôt que de juste consommer la solution
- Veut comprendre **comment** ça marche, pas seulement **que** ça marche
- Préfère **proposer 2-3 directions** et choisir, plutôt qu'imposer directement

→ Garder ce mode de fonctionnement dans les prochaines itérations.
