"""Make `app` importable; also act as CLI for one-shot runs."""
import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        # One-shot CLI run, useful for testing or manual triggers
        from app import config, watcher
        cfg = config.load()
        result = watcher.run_once(cfg)
        print(f"Done: {result}")
    else:
        # Default: start the FastAPI app with embedded scheduler
        import uvicorn
        uvicorn.run("app.api:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
