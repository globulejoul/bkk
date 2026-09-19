"""Paquet applicatif. Configure le journal pour tous les points d'entrée.

Les deux entrées (`python -m app` pour le serveur, `python -m app run`
pour la ligne de commande) importent ce paquet en premier : c'est le seul
endroit qui garantit un journal horodaté quel que soit le chemin. Avant,
une centaine de `print()` sortaient sans date, sans niveau et sans module,
ce qui rendait impossible de dater une panne dans `docker logs`.
"""
from __future__ import annotations

import logging
import os

_LEVEL = os.environ.get("BKK_LOG_LEVEL", "INFO").upper()

# force=True : uvicorn installe sa propre configuration à l'import, sans
# quoi la nôtre serait ignorée selon l'ordre des imports.
logging.basicConfig(
    level=getattr(logging, _LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

# Les journaux d'accès d'uvicorn sont bavards (le dashboard interroge
# l'API en boucle) et n'apprennent rien : on les laisse en WARNING.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
