# pledger
Personal ledger

## Development

```sh
make install   # editable install with web + dev extras
make test
make lint
```

## Web app

A FastAPI + HTMX UI to browse the ledger, see balances and add or delete movements.

```sh
make serve     # http://127.0.0.1:8000 (API docs at /docs)
```

It reads `info/accounts.yaml`, `info/base_movements.yaml` and `info/ledger.csv` from the
current directory. Override with `PLEDGER_ACCOUNTS`, `PLEDGER_BASE_MOVEMENTS` and
`PLEDGER_LEDGER`. The CSV stays the source of truth: adding a movement appends one line,
deleting removes one line, and the rest of the file is left byte-for-byte as it was.

The app has no authentication, so keep it bound to `127.0.0.1`.
