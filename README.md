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
balances and adds accounts; Investments lists the investments of the investment accounts
and their composition by type or by account; Debt lists who we owe and who owes us in the
debt accounts; Work lists the salary, company and job position of each month; Setup adds result types and base movements. The Charts page
shows the composition of assets and liabilities, gains and losses by result type, a result
statement, and equity over time. The Help page explains the concepts for everyday users.
The month filter is shared by Ledger, Accounts and Charts (remembered in a cookie).

```sh
make serve     # http://127.0.0.1:8000 (API docs at /docs)
```

It reads `info/accounts.yaml`, `info/result_types.yaml`, `info/ledger.csv`,
`info/currencies.yaml` (with each other currency's rates in `info/currencies/<NAME>.csv`)
and, if present, `info/investment.yaml`, `info/creditors.yaml` and `info/work.csv` from the
current directory. Override with `PLEDGER_ACCOUNTS`, `PLEDGER_BASE_MOVEMENTS`,
`PLEDGER_LEDGER`, `PLEDGER_CURRENCIES`, `PLEDGER_INVESTMENTS`, `PLEDGER_CREDITORS` and
`PLEDGER_WORK`. The CSV stays the source of truth: adding a movement appends one line,
deleting removes one line, and the rest of the file is left byte-for-byte as it was.
New accounts and base movements are inserted at the end of their section in the YAML files
(gains and losses under their result type, transfers under `transfer`),
leaving the rest of each file as it was.

Every account but the equity, investment and debt ones must set an `initial_amount` (those must not).
Accounts also take optional `currency` (the base currency by default), `is_liquid`
(`false`) and `initial_date` (the first date of the ledger) keys; the
Charts tab groups their balances by currency or by liquidity.

Investment accounts are the asset accounts listed as top-level keys in `investment.yaml`,
each mapping its investments to their `type`, initial `amount` and initial `quantity`, or
just to their type (with both at 0). The type is one of `public_bond`, `private_bond`,
`mutual_fund`, `etf`, `forex`, `stock`, `derivative` and `fixed_term`. The account's
initial amount is the sum of those amounts. A quantity can't go below 0.

Debt accounts are the liability accounts listed as top-level keys in `creditors.yaml`, each
mapping the people we have debts with to the initial amount: positive if we owe them,
negative if they owe us. The account's initial amount is their sum, and its balance can
go below 0 (we are owed more than we owe). The ledger movements with a debt account on
either side add to the person named in their `comment` (as a whole word, ignoring case;
the earliest name there, and the longest one if several start there): a credit is more
that we owe them, a debit less. The Debt tab shows the movements naming no one as
unassigned. The companies of the Work tab are added to every debt account (with 0) when
the app starts and when a month with a new company is saved.

A base movement is a dict (`description` and optional fixed `debit`/`credit` accounts) or
just its description. A side set to `INVESTMENT` or `DEBT` (reserved names) takes any
investment or debt account instead of a fixed one, and the New movement form only offers
those there; the Accounts tab marks these accounts in its Special column. The top-level `internal` key mirrors the rest of the file (result
types, sharing their ids with the other ones, next to a `transfer` section); its base
movements are reserved for pledger itself: they can't be picked for a new movement and
their movements can't be deleted by hand, and its result types can't get new base movements. Balances and results don't use these yet.
The internal gain `Initial (G)` and loss `Initial (L)` are required (with no fixed debit or
credit account, respectively): every account with a non-zero initial amount gets one such
movement against equity, on its initial date. These movements are generated when the
ledger is loaded and are never stored in `ledger.csv`, which can't contain them. They
count towards balances and equity, but the movements, gains, losses, profit and result
charts leave out the movements of their result type unless the *Initial movements* filter
(or the `initial` API parameter) is on.

The Work tab reads the work history from `work.csv`, one row per month, in order:

```csv
Month;Company;Position;Gross salary;Net salary;Received payment date
2024-09;Acme;Acme Sr;4291295.67;4111961.22;2024-10-03
2024-10;;;;;
2024-11;Globex;GX Dev;3165000.42;2694595.00;
```

No month can be missing: a month without a job is a row with no company, position or
salary. A worked month needs both salaries, the net one no larger than the gross one; the
`Received payment date` column is optional (added when a date is first entered). The
companies and their job positions come from the rows, and the job of the last month, if
it isn't a gap, is the current one. The tab's form adds a month right after the last one
(or right before the first one) or, for an existing month, replaces it; each row's edit
button loads it into the form. Only the first and last months can be deleted. The file is
created if missing and doesn't affect the ledger.

The eye button in the top bar replaces every amount with `******` until clicked again; the
choice is remembered by the browser.

Amounts are shown with a `$` currency symbol and two decimals; set `PLEDGER_CURRENCY`
(e.g. `ARS`) to use another one.

The app has no authentication, so keep it bound to `127.0.0.1`.
