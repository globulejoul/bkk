# Tag explicite : « python:3.12-slim » suit la Debian stable du moment et
# est passé de bookworm à trixie sans prévenir, ce qui a cassé la liste de
# paquets ci-dessous (libasound2, libatk1.0-0 et libcups2 y sont renommés
# avec le suffixe t64). Le build ne tenait plus que par le cache.
FROM python:3.12-slim-trixie

WORKDIR /app

# Install Python deps separately for layer caching
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + ses dépendances système. --with-deps laisse Playwright
# choisir les paquets adaptés à la distribution, au lieu d'une liste
# écrite à la main qui devient fausse à chaque changement de base.
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Copy app code
COPY app /app/app
COPY static /app/static

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# Le healthcheck est défini dans docker-compose.yml, seule voie de
# déploiement : une seule source de vérité.

CMD ["python", "-m", "app"]
