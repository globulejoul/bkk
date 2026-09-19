"""Make `app` importable; also act as CLI for one-shot runs."""
import logging
import sys

log = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        # One-shot CLI run, useful for testing or manual triggers.
        # `docker compose exec watcher python -m app run` démarrait un run
        # en parallèle de celui du scheduler : le verrou de ce dernier est
        # un threading.Lock, invisible depuis un autre processus. On prend
        # donc le verrou de fichier, partagé par les deux.
        from app import config, watcher
        from app.api import file_run_lock
        with file_run_lock() as acquired:
            if not acquired:
                log.error("Un run est déjà en cours dans un autre processus "
                      "(scheduler ou autre commande). Abandon.")
                sys.exit(1)
            cfg = config.load()
            result = watcher.run_once(cfg)
            log.info(f"Done: {result}")
    else:
        # Default: start the FastAPI app with embedded scheduler
        import uvicorn
        uvicorn.run("app.api:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
