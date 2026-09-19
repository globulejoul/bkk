"""ntfy notifications for flight price alerts."""
from __future__ import annotations

import logging
import os
from datetime import datetime

import requests

from app.config import Config

log = logging.getLogger(__name__)


def send_ntfy(cfg: Config, alert: dict) -> bool:
    """Renvoie True si ntfy a accepté la notification."""
    if not cfg.ntfy.topic:
        return False
    server = cfg.ntfy.server.rstrip("/")
    url = f"{server}/{cfg.ntfy.topic}"
    token = os.environ.get("NTFY_TOKEN")

    if alert["kind"] == "new_low":
        # Ce payload couvre aussi seuil et percentile : la flèche baissière
        # sur un prix remonté annonçait une baisse qui n'existait pas.
        # Le titre doit dire la même chose que le corps (_body_new_low).
        prev_low = alert.get("previous_low")
        is_new_low = prev_low is None or alert["price"] < prev_low - 0.5
        if alert.get("hit_threshold"):
            icon = "🎯"
        else:
            icon = "📉" if is_new_low else "📊"
        title = f"{icon} {alert['price']:.0f}€ — {alert['trip']}"
        tags = "airplane,chart_with_downwards_trend"
        priority = "high" if alert.get("hit_threshold") else "default"
        body = _body_new_low(alert)
    elif alert["kind"] == "rise":
        title = f"📈 Hausse {alert['price']:.0f}€ — {alert['trip']}"
        tags = "airplane,chart_with_upwards_trend"
        priority = "default"
        body = _body_rise(alert)
    else:
        return False

    headers = {
        "Title": title.encode("utf-8"),
        "Tags": tags,
        "Priority": priority,
        "Markdown": "yes",
    }
    if alert.get("booking_url"):
        headers["Click"] = alert["booking_url"]
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.post(url, data=body.encode("utf-8"),
                          headers=headers, timeout=15)
        if not r.ok:
            log.info(f"  ntfy HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"  ntfy error: {e}")
        return False


def _body_new_low(a: dict) -> str:
    pct = a.get("percentile")
    prev = a.get("previous_low")

    # Le payload kind='new_low' est aussi émis sur seuil ou percentile
    # sans nouveau record : même règle que le watcher (marge 0,50 €) pour
    # savoir si le prix a réellement battu le plus bas connu.
    price = a["price"]
    is_new_low = prev is None or price < prev - 0.5

    # Header line
    if a.get("hit_threshold"):
        tag = "🎯 SEUIL ATTEINT"
    elif is_new_low:
        tag = "📉 NOUVEAU PRIX BAS"
    elif pct is not None and pct <= 10:
        tag = f"📊 PRIX RARE ({pct:.0f}e percentile)"
    else:
        tag = "📉 PRIX BAS"

    # Sans test de signe, une alerte seuil sur un prix remonté affichait
    # « ↓-40€ », soit l'inverse de ce qui s'était passé.
    if prev is None:
        delta = ""
    elif is_new_low:
        delta = f" (↓{prev - price:.0f}€)"
    else:
        delta = f" (+{price - prev:.0f}€ vs plus bas {prev:.0f}€)"

    lines = [
        f"**{tag}** — {a['price']:.0f}€{delta}",
        f"{a['airlines']} • {a['origin']} → {a['destination']}",
        f"Aller: {a['outbound_date']} ({a['out_h']:.1f}h, {a['out_stops']} esc.)",
        f"Retour: {a['return_date']} ({a['ret_h']:.1f}h, {a['ret_stops']} esc.)",
    ]

    # Percentile context
    if pct is not None:
        lines.append(f"Historique: {pct:.0f}e percentile "
                     f"({'très bon' if pct <= 10 else 'bon' if pct <= 25 else 'correct'})")

    # Trend info
    trend = a.get("trend")
    if trend:
        direction = trend.get("direction", "stable")
        change = trend.get("change_pct", 0)
        if direction == "falling":
            lines.append(f"Tendance 7j: ↘ en baisse ({change:+.1f}%)")
        elif direction == "rising":
            lines.append(f"Tendance 7j: ↗ en hausse ({change:+.1f}%)")
        else:
            lines.append("Tendance 7j: → stable")

    # Buy score
    buy_score = a.get("buy_score")
    if buy_score is not None:
        if buy_score >= 70:
            label = "Bon moment pour acheter"
        elif buy_score >= 50:
            label = "Moment correct"
        elif buy_score >= 30:
            label = "Attendre si possible"
        else:
            label = "Pas le bon moment"
        lines.append(f"Score achat: {buy_score}/100 — {label}")

    # One-way comparison
    ow = a.get("oneway_comparison")
    if ow:
        lines.append("")
        saving = ow["saving"]
        if saving > 20:
            lines.append(f"**✂️ 2 allers simples = {ow['ow_total']:.0f}€ "
                         f"(économie {saving:.0f}€)**")
            lines.append(f"  Aller: {ow['ow_out_price']:.0f}€ "
                         f"({ow['ow_out_airlines']})")
            lines.append(f"  Retour: {ow['ow_ret_price']:.0f}€ "
                         f"({ow['ow_ret_airlines']})")
        elif saving < -20:
            lines.append(f"A/R moins cher que 2 OW ({ow['ow_total']:.0f}€)")

    # Open-jaw comparison
    oj = a.get("openjaw_comparison")
    if oj:
        lines.append("")
        oj_saving = oj["saving"]
        if oj_saving > 20:
            lines.append(
                f"**🔀 Open-jaw = {oj['oj_total']:.0f}€ "
                f"(économie {oj_saving:.0f}€)**"
            )
            lines.append(
                f"  Aller: {oj['oj_out_origin']}→{oj['oj_out_dest']} "
                f"{oj['oj_out_price']:.0f}€ ({oj['oj_out_airlines']})"
            )
            lines.append(
                f"  Retour: {oj['oj_ret_origin']}→{oj['oj_ret_dest']} "
                f"{oj['oj_ret_price']:.0f}€ ({oj['oj_ret_airlines']})"
            )
        elif oj_saving < -20:
            lines.append(
                f"A/R classique moins cher que open-jaw ({oj['oj_total']:.0f}€)"
            )

    return "\n".join(lines)


def _body_rise(a: dict) -> str:
    pct = a.get("percentile")
    pct_line = ""
    if pct is not None:
        pct_line = f"Percentile actuel: {pct:.0f}e\n"

    # Trend info
    trend_line = ""
    trend = a.get("trend")
    if trend:
        direction = trend.get("direction", "stable")
        change = trend.get("change_pct", 0)
        if direction == "falling":
            trend_line = f"Tendance 7j: ↘ en baisse ({change:+.1f}%)\n"
        elif direction == "rising":
            trend_line = f"Tendance 7j: ↗ en hausse ({change:+.1f}%)\n"
        else:
            trend_line = "Tendance 7j: → stable\n"

    # Buy score
    score_line = ""
    buy_score = a.get("buy_score")
    if buy_score is not None:
        if buy_score >= 70:
            label = "Bon moment pour acheter"
        elif buy_score >= 50:
            label = "Moment correct"
        elif buy_score >= 30:
            label = "Attendre si possible"
        else:
            label = "Pas le bon moment"
        score_line = f"Score achat: {buy_score}/100 — {label}\n"

    return (
        f"**📈 Prix remonte** — {a['price']:.0f}€\n"
        f"Plus bas 7j: {a['recent_low']:.0f}€\n"
        f"Hausse: +{a['rise_pct']:.1f}% (+{a['delta_eur']:.0f}€)\n"
        f"{pct_line}"
        f"{trend_line}"
        f"{score_line}"
        f"{a['airlines']} • {a['origin']} → {a['destination']}\n\n"
        f"Si tu visais cette période, le bas pourrait être derrière toi."
    )


def send_ops_ntfy(cfg: Config, title: str, body: str) -> bool:
    """Notification technique (panne de collecte), distincte des prix."""
    if not cfg.ntfy.topic:
        return False
    server = cfg.ntfy.server.rstrip("/")
    url = f"{server}/{cfg.ntfy.topic}"
    headers = {
        "Title": title.encode("utf-8"),
        "Tags": "warning",
        "Priority": "default",
    }
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(url, data=body.encode("utf-8"),
                          headers=headers, timeout=15)
        if not r.ok:
            log.info(f"  ntfy ops HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"  ntfy ops error: {e}")
        return False


def send_test_ntfy(cfg: Config) -> bool:
    """Notification de vérification de la chaîne ntfy.

    Seule une vraie alerte émettait jusqu'ici : un topic mal saisi ou un
    token expiré ne se découvrait qu'à la première alerte manquée,
    parfois des semaines après. Renvoie True si ntfy a accepté l'envoi.
    """
    if not cfg.ntfy.topic:
        log.info("  ntfy test: aucun topic configuré")
        return False
    server = cfg.ntfy.server.rstrip("/")
    url = f"{server}/{cfg.ntfy.topic}"
    headers = {
        "Title": "🔔 Test Bangkok Watch".encode("utf-8"),
        "Tags": "bell",
        "Priority": "default",
        "Markdown": "yes",
    }
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # L'horodatage distingue ce test d'une notification restée affichée.
    body = (
        "**Test de notification**\n"
        "Si tu lis ceci, le serveur, le topic et le token sont bons : "
        "les alertes de prix arriveront ici.\n"
        f"Serveur : {server}\n"
        f"Envoyé le {datetime.now():%d/%m/%Y à %H:%M}"
    )
    try:
        r = requests.post(url, data=body.encode("utf-8"),
                          headers=headers, timeout=15)
        if not r.ok:
            log.info(f"  ntfy test HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"  ntfy test error: {e}")
        return False


def send_hotel_ntfy(cfg: Config, alert: dict) -> bool:
    """Notification ntfy pour les alertes hôtel. Renvoie True si délivrée."""
    if not cfg.ntfy.topic:
        return False
    server = cfg.ntfy.server.rstrip("/")
    url = f"{server}/{cfg.ntfy.topic}"
    token = os.environ.get("NTFY_TOKEN")

    tag = "🎯 SEUIL" if alert.get("hit_threshold") else "📉 PRIX BAS"
    title = f"🏨 {alert['price']:.0f}€ — {alert['hotel']}"
    body_lines = [
        f"**{tag}** — {alert['price']:.0f}€ pour {alert['nights']} nuits",
        "Prix total du séjour, taxes et frais compris",
        f"{alert['hotel']}",
        f"{alert['checkin']} → {alert['checkout']}",
    ]
    prev = alert.get("previous_low")
    if prev:
        body_lines.append(f"Précédent bas: {prev:.0f}€")

    seen = alert.get("providers_seen") or []
    if seen:
        body_lines.append("")
        body_lines.append("Offres vues chez : " + ", ".join(seen))

    headers = {
        "Title": title.encode("utf-8"),
        "Tags": "hotel,chart_with_downwards_trend",
        "Priority": "high" if alert.get("hit_threshold") else "default",
        "Markdown": "yes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.post(url, data="\n".join(body_lines).encode("utf-8"),
                          headers=headers, timeout=15)
        if not r.ok:
            log.info(f"  ntfy hotel HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"  ntfy hotel error: {e}")
        return False


def send_probe_ntfy(cfg: Config, alert: dict) -> bool:
    """Notification pour une sonde compagnie. Renvoie True si délivrée.

    Distincte des alertes de marché, et le corps le dit explicitement :
    une sonde ne voit QU'UNE compagnie. Annoncer « nouveau plus bas »
    sans cette précision laisserait croire que le marché a bougé, alors
    que seul Air France ou KLM a bougé sur une route et des dates
    précises.
    """
    if not cfg.ntfy.topic:
        return False
    server = cfg.ntfy.server.rstrip("/")
    url = f"{server}/{cfg.ntfy.topic}"
    token = os.environ.get("NTFY_TOKEN")

    carrier = {"AF": "Air France", "KL": "KLM"}.get(
        alert.get("carrier", ""), alert.get("carrier", "compagnie"))
    title = f"✈️ {alert['price']:.0f}€ — {carrier} {alert['origin']}→{alert['destination']}"
    body_lines = [
        f"**Nouveau plus bas {carrier}** — {alert['price']:.0f}€",
        f"Prix d'UNE compagnie, pas du marché : à comparer au suivi habituel.",
        "",
        f"{alert['origin']} → {alert['destination']}",
        f"{alert['outbound_date']} → {alert['return_date']}",
    ]
    prev = alert.get("previous_low")
    if prev:
        ecart = prev - alert["price"]
        body_lines.append(f"Précédent bas de cette sonde : {prev:.0f}€ "
                          f"(−{ecart:.0f}€)")
    stops = alert.get("out_stops")
    if stops is not None:
        body_lines.append(
            "Direct" if not stops else f"{stops} escale(s) à l'aller")
    if alert.get("fare_family"):
        body_lines.append(f"Tarif : {alert['fare_family']}")

    headers = {
        "Title": title.encode("utf-8"),
        "Tags": "airplane,chart_with_downwards_trend",
        "Priority": "default",
        "Markdown": "yes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        r = requests.post(url, data="\n".join(body_lines).encode("utf-8"),
                          headers=headers, timeout=15)
        if not r.ok:
            log.info(f"  ntfy sonde HTTP {r.status_code}: {r.text[:200]}")
        return r.ok
    except Exception as e:
        log.error(f"  ntfy sonde error: {e}")
        return False
