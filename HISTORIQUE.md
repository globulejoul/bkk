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
- Backup simple à faire. (Réserve découverte plus tard : la base est en mode WAL, un `cp` à chaud n'est pas fiable. La procédure correcte — `VACUUM INTO` / `.backup`, et suppression des `-wal`/`-shm` à la restauration — est dans le README.)
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

## Itération 7 — v5 : fli + Duffel, abandon de Tequila et du VPN (mai 2026)

La v4 n'a pas survécu à sa première mise en service. Trois constats, dans l'ordre :

### Tequila abandonné

L'API Kiwi ne renvoie que le catalogue Kiwi (cf. Q1 de l'itération 4), avec ses
marges et son virtual interlining. Les prix ne correspondaient pas à ce que
l'utilisateur voyait ailleurs. Remplacé par deux sources complémentaires :

- **fli** (`b47df92`) — API Google Flights reverse-engineered, gratuite, sans clé.
- **Duffel** (`88c94cc`) — tarifs compagnies en direct, 300+ transporteurs.

**Piège de nommage, à retenir** : le paquet s'installe sous le nom PyPI
`flights`, s'importe sous le nom `fli`, et vit sur
`github.com/punitarani/fli` (`d2698e5` corrige précisément ce point). Les trois
noms sont différents ; `pip install fli` installe autre chose.

### fast-flights abandonné

Avant fli, on est passé par fast-flights pendant quelques heures : branche v3
au parseur cassé, puis branche v2, puis `fetch_mode=local`, puis suppression du
paramètre (`516ee4d`, `7732c0f`, `8edc350`). Trop instable pour être la source
principale. fli fait la même chose sans le détour par Playwright.

### VPN abandonné

Gluetun/NordVPN étaient là pour comparer le marché thaï au marché français.
Deux raisons de les retirer : ni fli ni Duffel ne se comportent comme une page
Google grand public soumise à la geo-discrimination, et le déploiement a
finalement quitté le home server pour un **VPS Hostinger mutualisé** derrière
Caddy — un sidecar VPN sur une machine partagée n'a plus la même innocuité.
`319cf14` acte la disparition de Tequila et de Gluetun dans la doc.

### Grille de dates exhaustive

Tequila couvrait les 49 combinaisons aller × retour en un appel. Duffel, non :
il faut une requête par combinaison. On l'a assumé (`14f5fc5`) parce que c'est
ce qui permet la **heatmap** — voir quelles dates précises sont les moins
chères, pas seulement le prix plancher de la période. Conséquence : un run
devient long et bruyant côté quota, d'où le rate limiting adaptatif lisant les
en-têtes de quota (`899ccd0`).

### Fonctionnalités de la même vague (`f9bbfa5`)

Heatmap, tendance, **score achat 0-100**, **flash mode** (checks toutes les
5 min pendant 48 h quand le seuil est atteint), **open-jaw**, et
**l'alerte percentile** et la **comparaison A/R vs 2 allers simples** — les deux
items listés plus bas comme « non retenus » ont donc finalement été
implémentés.

---

## Itération 8 — Page Admin et exploitabilité (mai 2026)

`0e1e5a8` et la série qui suit : page Admin pour éditer la configuration depuis
le dashboard (aéroports, voyageurs, périodes, seuils, durée de vol max, toggle
on/off par période), watchdog des runs bloqués, notifications de hausse.

**Conséquence importante** : `config.yml` est devenu un fichier *écrit par
l'application*. Il a donc été **retiré du suivi git** (`9830652`) au profit de
`config.example.yml`. Un `git pull` ne doit jamais écraser la configuration du
serveur. Le montage `:ro` du volume a été retiré en même temps (`9ee038b`).

---

## Itération 9 — Suivi hôtel via Playwright (mai 2026)

`3a64d3a` puis `9302827`. Google Hotels n'a pas d'API : Chromium headless via
Playwright, un scrape par hôtel. Première version par `entity_id`, réécrite
presque aussitôt en **recherche par nom** — l'identifiant d'entité était fragile
et opaque. Le champ `entity_id` subsiste dans la configuration mais n'est plus
utilisé.

---

## Itération 10 — Audit, puis coupure de Duffel (septembre 2026)

Un audit complet du projet (septembre 2026) a produit une centaine de constats,
dont le fait que `CLAUDE.md`, `.env.example` et `config.example.yml` décrivaient
encore la stack v4 disparue.

**Duffel coupé le 12 septembre 2026.** Motif : la tarification « excess search »
facture 0,005 $ par recherche au-delà d'un ratio de 1 500 recherches par
réservation — et ce compte ne réserve jamais rien. Avec une grille de dates
exhaustive à chaque run, le dépassement est structurel. La clé a été commentée
dans `/opt/bkk/.env` ; le code court-circuite (`search_duffel()` renvoie une
liste vide sans clé) et fli reste seule source de vols.

**À ne pas refaire sans décision explicite** : réactiver la clé « pour voir ».
Le coût n'est pas nul et personne ne surveille la facture.

---

## Itération 11 — Réparation du suivi hôtel (19 septembre 2026)

Le scraper hôtel renvoyait des prix qui ne correspondaient ni aux bonnes dates
ni au bon périmètre. Trois causes, trois correctifs (`22b1ac5`, `da75561`,
`5e40d7f`) :

1. **Google ignore `checkin`/`checkout` en clair dans l'URL** et répond avec ses
   dates par défaut. Les dates doivent voyager dans le paramètre `ts`, un
   protobuf encodé en base64 — d'où `hotels.build_ts()`.
2. **Le montant affiché est ambigu** (par nuit ou pour le séjour, taxes ou non).
   On ne retient plus que le prix lu dans le libellé d'accessibilité
   « X € pour les dates …, <nom de l'hôtel> », multiplié par le nombre de nuits
   quand Google affiche un prix par nuit. En son absence, le scrape **échoue**
   au lieu d'enregistrer une valeur douteuse — un chiffre faux dans un
   historique de prix est pire qu'un trou.
3. **La recherche par nom seul pouvait tomber sur un autre établissement.**
   Navigation en deux temps : recherche sans dates pour récupérer le handle
   d'entité `qs` de la fiche, puis rechargement de cette fiche avec les dates.

Les échecs sont désormais tracés (`hotel_state.last_error`,
`consecutive_failures`) : la surveillance ne peut plus mourir en silence.

Résultat vérifié en production : **240 € pour 2 nuits** au Chatrium Riverside,
13 → 15 février 2027.

---

## Améliorations envisagées et leur statut

Ces options avaient été présentées à l'utilisateur en multi-select à
l'itération 6 et non cochées à ce moment-là. Deux ont finalement été
implémentées depuis :

| Option | Statut |
|---|---|
| Anomaly detection percentile-based | **Implémenté** (`f9bbfa5`) — alerte quand le prix est dans le 10e percentile historique |
| Round-trip vs 2 one-ways | **Implémenté** (`f9bbfa5`) — `_compare_oneway()`, déclenché sur alerte basse uniquement |
| Filtre horaire de vol | Non retenu |
| Seats.aero (vols en miles) | Non retenu |

---

## Choses qui ont été essayées et abandonnées

- **Kiwi Tequila** (v1 → v3, source primaire) : abandonné en v5, ne renvoie que le catalogue Kiwi.
- **SerpAPI** (v2) : remplacé par du gratuit, jamais revenu.
- **fast-flights** (v4) : parseur instable, remplacé par fli après quelques heures.
- **NordVPN sur GitHub Actions** (v3) : jugé inutilement complexe, abandonné au profit de SerpAPI `gl=th`. Ré-introduit en v4 via Gluetun sur le home server, puis **définitivement retiré en v5** (sources non géo-discriminantes + VPS mutualisé).
- **GitHub Actions comme runtime** (v1 → v3) : remplacé par Docker, d'abord home server puis VPS.
- **`entity_id` Google Hotels** : première approche du scraper hôtel, remplacée par la recherche par nom puis par la navigation en deux temps via `qs`.
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
