"""ntfy notifications for flight price alerts."""
from __future__ import annotations

import os

import requests

from app.config import Config


def send_ntfy(cfg: Config, alert: dict) -> None:
    if not cfg.ntfy.topic:
        return
    server = cfg.ntfy.server.rstrip("/")
    url = f"{server}/{cfg.ntfy.topic}"
    token = os.environ.get("NTFY_TOKEN")

    if alert["kind"] == "new_low":
        title = f"📉 {alert['price']:.0f}€ — {alert['trip']}"
        tags = "airplane,chart_with_downwards_trend"
        priority = "high" if alert.get("hit_threshold") else "default"
        body = _body_new_low(alert)
    elif alert["kind"] == "rise":
        title = f"📈 Hausse {alert['price']:.0f}€ — {alert['trip']}"
        tags = "airplane,chart_with_upwards_trend"
        priority = "default"
        body = _body_rise(alert)
    else:
        return

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
        requests.post(url, data=body.encode("utf-8"),
                      headers=headers, timeout=15)
    except Exception as e:
        print(f"  ntfy error: {e}")


def _body_new_low(a: dict) -> str:
    pct = a.get("percentile")
    prev = a.get("previous_low")

    # Header line
    if a.get("hit_threshold"):
        tag = "🎯 SEUIL ATTEINT"
    elif pct is not None and pct <= 10:
        tag = f"📊 PRIX RARE ({pct:.0f}e percentile)"
    else:
        tag = "📉 NOUVEAU PRIX BAS"

    delta = f" (↓{prev - a['price']:.0f}€)" if prev else ""
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

    # Cross-checks
    cross = a.get("cross_checks") or []
    if cross:
        lines.append("")
        lines.append("**Comparaison marchés:**")
        baseline = a["price"]
        for cc in cross:
            eur = cc.get("eur_equiv")
            if eur is None:
                lines.append(f"• {cc['label']}: {cc['price']:.0f} {cc['currency']} "
                             "(FX indispo)")
            else:
                diff = (eur / baseline - 1) * 100
                sign = "+" if diff >= 0 else ""
                if cc["currency"] == "EUR":
                    p = f"{cc['price']:.0f}€"
                else:
                    p = f"{cc['price']:.0f} {cc['currency']} ≈ {eur:.0f}€"
                lines.append(f"• {cc['label']}: {p} ({sign}{diff:.1f}%) "
                             f"{cc.get('airlines', '')}")

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

    return "\n".join(lines)


def _body_rise(a: dict) -> str:
    pct = a.get("percentile")
    pct_line = ""
    if pct is not None:
        pct_line = f"Percentile actuel: {pct:.0f}e\n"

    return (
        f"**📈 Prix remonte** — {a['price']:.0f}€\n"
        f"Plus bas 7j: {a['recent_low']:.0f}€\n"
        f"Hausse: +{a['rise_pct']:.1f}% (+{a['delta_eur']:.0f}€)\n"
        f"{pct_line}"
        f"{a['airlines']} • {a['origin']} → {a['destination']}\n\n"
        f"Si tu visais cette période, le bas pourrait être derrière toi."
    )
