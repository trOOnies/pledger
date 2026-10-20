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
(with their price, the amount over the quantity) and their composition by type or by account,
and adds and deletes the movements of the internal `Invest` base movements; Debt lists who we
owe and who owes us in the debt accounts; Credit Cards lists what we owe on each credit card
and what was charged and paid in the month; Work lists the salary, company and job position of each month; Setup adds result types and base movements. The Charts page
shows the composition of assets and liabilities, gains and losses by result type, a result
statement, equity over time and, at each month end, debt (the liabilities) over equity, net
liquidity (liquid assets minus liquid liabilities) over equity, and the share of each
currency in the assets. The Work tab charts the gross and net salary of each month (0 for
the months without a job), each in its own color. The Help page explains the concepts for everyday users.
The month filter is shared by Ledger, Accounts, Debt, Credit Cards and Charts (remembered in a cookie).

```sh
make serve     # http://127.0.0.1:8000 (API docs at /docs)
```

It reads `info/accounts.yaml`, `info/result_types.yaml`, `info/ledger.csv`,
`info/currencies.yaml` (with each other currency's rates in `info/currencies/<NAME>.csv`)
and, if present, `info/investment.yaml`, `info/creditors.yaml`, `info/credit_cards.yaml` and
`info/work.csv` from the current directory. Override with `PLEDGER_ACCOUNTS`,
`PLEDGER_BASE_MOVEMENTS`, `PLEDGER_LEDGER`, `PLEDGER_CURRENCIES`, `PLEDGER_INVESTMENTS`,
`PLEDGER_CREDITORS`, `PLEDGER_CREDIT_CARDS` and `PLEDGER_WORK`. The CSV stays the source of truth: adding a movement appends one line,
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

The investment movements (see the internal base movements below) then change the
investments: the one named in the `additional_2` column of `ledger.csv`, in the investment
account on either side, gets the amount and the quantity of the `additional` column
(`Q = 1,234.50`) on a debit, and loses them on a credit. The investment is required when a
side is `INVESTMENT`, and the quantity for `Investment - Bought`, `Investment - Sold`,
`Stock split (More)` and `Stock split (Less)`; `Capital repayments` may have one, and no
other base movement can. An investment only in the ledger has no type until it is added
to `investment.yaml` (the Investments tab warns about them, and its form adds the type
of a new investment there). The tab lists the investments held, with their price (amount
over quantity), and the ones no longer held apart.

A free side (neither fixed nor `INVESTMENT`) of an investment base movement can't be an
investment account, in the form or in `ledger.csv`: what pays for an investment bought,
gets the money of one sold, or gets its dividends or pays its fees is another account
(e.g. `Fi`). So only the `INVESTMENT` sides change the investments.

The gains and losses naming an investment, but stock splits, are totalled per investment
and base movement, whatever their other account: in the investment account on a side, if
any, else in the one with an investment of that name. The Investments tab shows for each
investment, held or not, and overall: its net capital gains (the gains and losses with an
`INVESTMENT` side, e.g. `Capital gains` minus `Capital losses`, which add up over time
whatever is bought or sold), the total of each other one (`Dividends & Interest`,
`Broker fees`...) and the total result (all of them, losses subtracted). It warns about
the movements naming an investment of no investment account.

Credit card accounts are the top-level keys of `credit_cards.yaml`, set like the accounts of
`accounts.yaml` (a `description`, an `initial_amount` and the optional `currency` and
`initial_date`), but not listed there: they are always liquid liabilities, and can't be
investment or debt accounts. The Credit Cards tab warns about the cards with a negative
balance (more paid than charged): rare, so it may be an error. It also charts the net
owed on them over equity at each month end.

```yaml
Vi:
  description: Visa Credit Card from Santander
  initial_amount: 57236.80
```

Debt accounts are the liability accounts listed as top-level keys in `creditors.yaml`, each
with up to three sections, `people`, `services` and `companies`, mapping who we have debts
with to the initial amount: positive if we owe them, negative if they owe us. A name can
only be in one section of an account. Instead of the amount, a name can map to its
`amount` and its `expected` role: `creditor` (we should owe them, or nothing), `debtor`
(they should owe us, or nothing) or `null` (no expectation). Without `expected`, people
have no expected role, services are expected to be creditors and companies debtors.

```yaml
De:
  people:
    Dad: 0.00
  services:
    Rent:
      amount: 0.00
      expected: debtor
  companies: null
```

Every company of `work.csv` is a company of every debt account, with an initial amount of
0 unless its `companies` section sets one (it can also list companies not in the work
history). The account's initial amount is the sum of its initial amounts, and its balance
can go below 0 (we are owed more than we owe). The ledger movements with a debt account on
either side add to whoever is named in their `comment` (as a whole word, ignoring case;
the earliest name there, and the longest one if several start there): a credit is more
that we owe them, a debit less. Like Accounts, the Debt tab shows everyone as of the end of
the month picked. It lists people, services and companies apart,
and the movements naming no one as unassigned. It alerts when there is unassigned debt
(listing it by comment) and when anyone is in the opposite role to the expected one.

A base movement is a dict (`description` and optional fixed `debit`/`credit` accounts) or
just its description. A side set to `INVESTMENT` or `DEBT` (reserved names) takes any
investment or debt account instead of a fixed one, and the New movement form only offers
those there; the Accounts tab marks these accounts in its Special column. The top-level `internal` key mirrors the rest of the file (result
types, sharing their ids with the other ones, next to a `transfer` section); its base
movements are reserved for pledger itself: they can't be picked for a new movement and
their movements can't be deleted by hand, and its result types can't get new base movements.
The exception are the base movements of the internal `Invest` result type (capital gains and
losses, interest, broker fees...) and the internal transfers with an `INVESTMENT` side (buying
and selling investments...): their movements can't be added or deleted from the Ledger tab or
`/api/movements`, but they can from the Investments tab and `/api/investments/movements`.
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
