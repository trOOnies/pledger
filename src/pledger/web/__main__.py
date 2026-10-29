"""Run the pledger web app: `pledger-web` or `python -m pledger.web`."""

import argparse
import os

import uvicorn

from pledger.web.store import LOCAL_HOSTS, Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the pledger web app.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Reload on code changes")
    args = parser.parse_args()
    created = Settings.from_env().create_missing_files()
    if created:
        files = "\n".join(f"  - {path}" for path in created)
        raise SystemExit(
            f"pledger did not start: these data files were missing, so they were created empty:\n{files}\n"
            "Fill them in (accounts.yaml, result_types.yaml and currencies.yaml can't be left empty; "
            "see 'Your data files' in the README) and start pledger again."
        )
    if args.host.strip("[]") not in LOCAL_HOSTS:
        # Bound beyond loopback on purpose: answer to any host, unless told otherwise. The env
        # var also reaches the app under the reloader, which runs it in another process.
        os.environ.setdefault("PLEDGER_ALLOWED_HOSTS", "*")
    uvicorn.run("pledger.web.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
