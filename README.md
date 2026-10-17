# pledger
Personal ledger

## Development

```sh
make install   # editable install with web + dev extras
make test
make lint
```

## Web app

A FastAPI + HTMX UI. The Ledger page lists, adds and deletes movements; Accounts shows
balances and adds accounts; Setup adds result types and base movements. The Charts page
shows the composition of assets and liabilities, gains and losses by result type, a result
statement, and equity over time. The Help page explains the concepts for everyday users.
The month filter is shared by Ledger, Accounts and Charts (remembered in a cookie).

```sh
make serve     # http://127.0.0.1:8000 (API docs at /docs)
```

It reads `info/accounts.yaml`, `info/result_types.yaml`, `info/ledger.csv` and
`info/currencies.yaml` (with each other currency's rates in `info/currencies/<NAME>.csv`)
from the current directory. Override with `PLEDGER_ACCOUNTS`, `PLEDGER_BASE_MOVEMENTS`,
`PLEDGER_LEDGER` and `PLEDGER_CURRENCIES`. The CSV stays the source of truth: adding a movement appends one line,
deleting removes one line, and the rest of the file is left byte-for-byte as it was.
New accounts and base movements are inserted at the end of their section in the YAML files
(gains and losses under their result type, transfers under `transfer`),
leaving the rest of each file as it was.

Every account but the equity ones must set an `initial_amount` (equity accounts must not).
Accounts also take optional `currency` (the base currency by default), `is_liquid`
(`false`) and `initial_date` (the first date of the ledger) keys. Balances and results
don't use these yet.

Amounts are shown with a `$` currency symbol and two decimals; set `PLEDGER_CURRENCY`
(e.g. `ARS`) to use another one.

The app has no authentication, so keep it bound to `127.0.0.1`.
