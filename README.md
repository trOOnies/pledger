# 📒 pledger

**Your money, one movement at a time.**

pledger is a personal, double-entry ledger that lives in plain files you own. You write down
every time money moves, and pledger works out the rest: balances, gains and losses, equity over
time, investments, debts, credit cards, salaries and inflation. It all shows up in a small local
web app.

![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-HTMX-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/license-GPL--3.0-blue)

- 📄 **Plain-text source of truth.** Movements live in a CSV and settings in YAML, so you can
  diff them, back them up and edit them by hand. pledger only ever appends or removes a single
  line and leaves the rest of the file byte-for-byte untouched.
- ⚖️ **Real double-entry bookkeeping.** Every movement debits one account and credits another,
  so equity always adds up.
- 📊 **Charts out of the box.** See what your assets and liabilities are made of, a result
  statement, equity over time, debt and liquidity ratios, and inflation-adjusted currencies.
- 🔒 **Local and private.** No accounts, no cloud. The 👁️ button masks every amount when
  someone is looking over your shoulder.

---

## Contents

- [Quick start](#-quick-start)
- [A tour of the app](#-a-tour-of-the-app)
- [Your data files](#-your-data-files)
- [Concepts](#-concepts)
- [Reference](#-reference)
  - [Accounts](#accounts)
  - [Base movements and internal movements](#base-movements-and-internal-movements)
  - [Currencies and inflation](#currencies-and-inflation)
  - [Investments](#investments)
  - [Debt](#debt)
  - [Credit cards](#credit-cards)
  - [Work](#work)
- [Development](#-development)

---

## 🚀 Quick start

```sh
make install   # editable install with the web and dev extras
make serve     # http://127.0.0.1:8000 (API docs at /docs)
```

pledger reads its files from `info/` in the current directory (see
[Your data files](#-your-data-files)). Open the app, go to **Ledger** and add your first
movement.

> [!WARNING]
> The app has no authentication, so keep it bound to `127.0.0.1`.

---

## 🧭 A tour of the app

A FastAPI + HTMX UI, one tab per area:

| Tab | What it shows |
| --- | --- |
| 📋 **Ledger** | Lists, adds and deletes movements, with the gains, losses and profit of the period. |
| 🏦 **Accounts** | Balances, total assets, liabilities and equity, a form to add accounts, the composition of assets and liabilities (by currency or by liquidity) and, at each month end, debt over equity and net liquidity (liquid assets minus liquid liabilities) over equity. |
| 💱 **Currencies** | Balances per currency, the balance of each account in other currencies in its own currency, the daily CPI of the base currency, its monthly inflation, the nominal and real value of each other currency, and the share of each currency in the assets at each month end. The conversions are added and deleted here. |
| 📊 **Results** | Gains and losses by result type, a waterfall of the result by type (with its result statement as a table) and equity over time. Result types and base movements are listed and added here. |
| 💼 **Work** | The company, job position, gross and net salary of each month, with a chart of both salaries (0 for the months without a job). |
| 📈 **Investments** | The investments of the investment accounts with their price (amount over quantity), their composition by type or by account, the results of each one, the interest on money of regular accounts, and charts of the total invested and the total result. The internal `Invest` movements are added and deleted here. |
| 🤝 **Debt** | Who we owe and who owes us in the debt accounts (people, services and companies), with alerts and the movements of the month. |
| 💳 **Credit Cards** | What we owe on each card, what was charged and paid in the month, a chart of the monthly charges, the net owed over equity, and the movements of the month. |
| ❓ **Help** | The concepts, explained for everyday users. |

A few things are shared across tabs:

- 🗓️ The **month filter** is shared by Ledger, Accounts, Results, Debt and Credit Cards, and
  remembered in a cookie.
- 🔮 In the Currencies tab, the months that haven't ended yet are **projected**: dashed in the
  CPI chart and faded in the inflation one.
- 👁️ The **eye button** in the top bar replaces every amount with `******` until clicked
  again. The browser remembers the choice.
- 💲 Amounts are shown with a `$` symbol and two decimals. Set `PLEDGER_CURRENCY` (e.g. `ARS`)
  to use another one.

---

## 🗂️ Your data files

Everything lives in `info/`, relative to the current directory. Each path can be overridden
with an environment variable:

| File | Required | Override | Holds |
| --- | :---: | --- | --- |
| `accounts.yaml` | ✅ | `PLEDGER_ACCOUNTS` | The accounts |
| `result_types.yaml` | ✅ | `PLEDGER_BASE_MOVEMENTS` | Result types and base movements |
| `movements/ledger.csv` | ✅ | `PLEDGER_LEDGER` | The movements |
| `movements/conversion.csv` | | `PLEDGER_CONVERSIONS` | The conversions between the base currency and the other ones |
| `currencies.yaml` | ✅ | `PLEDGER_CURRENCIES` | Base and other currencies (with `base_currency.csv` and `currencies.csv` next to it) |
| `investment.yaml` | | `PLEDGER_INVESTMENTS` | The investments of each investment account |
| `creditors.yaml` | | `PLEDGER_CREDITORS` | Who each debt account is owed by or owes to |
| `credit_cards.yaml` | | `PLEDGER_CREDIT_CARDS` | The credit card accounts |
| `work.csv` | | `PLEDGER_WORK` | The work history, one row per month |

**The CSV stays the source of truth.** Adding a movement appends one line, deleting removes
one line, and the rest of the file is left byte-for-byte as it was. New accounts and base
movements are inserted at the end of their section in the YAML files (gains and losses under
their result type, transfers under `transfer`), leaving the rest of each file as it was.

---

## 💡 Concepts

| Term | Meaning |
| --- | --- |
| **Account** | A place where money is kept or owed: an *asset*, a *liability* or *equity* (assets minus liabilities). |
| **Movement** | One ledger line: a date, a base movement, a debit account, a credit account and an amount. |
| **Base movement** | The category of a movement (e.g. `Basic - Food`). It can fix the debit or credit account. |
| **Result type** | A family of gain and loss base movements (e.g. *Operative*, *Taxes*). |
| **Gain / Loss / Transfer** | A gain is credited to equity, a loss is debited from it, and a transfer moves money between your own accounts. |

---

## 📚 Reference

### Accounts

Every account but the equity, investment and debt ones must set an `initial_amount` (those must
not). Accounts also take these optional keys:

- `currency`: the base currency by default.
- `is_liquid`: `false` by default.
- `initial_date`: the first date of the ledger by default.
- `checks`: a list of checks on the account balance at the end of each day of the ledger, with
  all the movements of the day summed. None by default.
  - `net_positive`: the balance is never negative.
  - `net_zero`: the balance is always 0.00, for accounts like an intermediary between
    movements that should never hold money.

  The ledger doesn't load if an account breaks a check, and a movement that would break one
  can't be added or deleted. The New account form can set one, and the Accounts tab shows them
  in its Checks column.

```yaml
asset:
  Fi:
    description: Fictitious balance as an intermediary between movements
    initial_amount: 0.00
    checks:
    - net_zero
```

The Accounts tab groups their balances by currency or by liquidity.

### Base movements and internal movements

A base movement is a dict (`description` and optional fixed `debit`/`credit` accounts) or just
its description. A side set to `INVESTMENT` or `DEBT` (reserved names) takes any investment or
debt account instead of a fixed one, and the New movement form only offers those there. The
Accounts tab marks these accounts in its Special column.

The top-level `internal` key mirrors the rest of the file: result types, sharing their ids with
the other ones, next to a `transfer` section. Its base movements are reserved for pledger
itself. They can't be picked for a new movement, their movements can't be deleted by hand,
and its result types can't get new base movements.

> [!NOTE]
> **Exception: investments.** The base movements of the internal `Invest` result type (capital
> gains and losses, interest, broker fees...) and the internal transfers with an `INVESTMENT`
> side (buying and selling investments...) can't be added or deleted from the Ledger tab or
> `/api/movements`, but they can from the Investments tab and `/api/investments/movements`.

The internal gain `USD - Result (G)` and loss `USD - Result (L)` are required in the same way
once an account in another currency has to be revalued (see
[Currencies and inflation](#currencies-and-inflation)). Their movements are generated, and
can't be in `ledger.csv`, just like the initial ones below, but they aren't hidden.

The internal gain `Initial (G)` and loss `Initial (L)` are required (with no fixed debit or
credit account, respectively). Every account with a non-zero initial amount gets one such
movement against equity, on its initial date. These movements:

- are generated when the ledger is loaded, are never stored in `ledger.csv`, and can't be
  written there;
- count towards balances and equity;
- are left out of the movements, gains, losses, profit and result charts, unless the
  *Initial movements* filter (or the `initial` API parameter) is on.

### Currencies and inflation

`currencies.yaml` names the `base_currency` and the `other_currencies`, each with its
`description`.

`base_currency.csv` has the `monthly_inflation` of the base currency for each `month`, as a
percentage (the `%` is optional), with no month missing:

```csv
month;monthly_inflation
2026-01;2.90%
2026-02;2.90%
```

From it, pledger builds a **daily customer price index (CPI)**. The CPI is 100 at the end of the
day before the first month, and each month's inflation compounds evenly over its days. So on
the month's last day, the CPI has grown by the whole month's inflation. On day `d` of `n`, it
is the previous month's CPI times `(1 + inflation) ** (d / n)`.

`currencies.csv` has, for each `date`, the value of each other currency in the base one, one
column each (no other columns). Values must be positive and dates can't repeat. An empty cell
is a date without that value. Without other currencies, the file is optional.

```csv
date;USD;USC
2026-01-05;1504.7200;1504.7200
2026-01-12;1473.0600;1473.0600
```

The Currencies tab charts each other currency's **nominal** value in the base one next to its
**real** value: deflated by the daily CPI, in the base currency of its last date with a CPI.

#### Conversions

Buying or selling another currency is a **conversion**, kept in `movements/conversion.csv`
instead of `ledger.csv`, with the amount in each currency:

```csv
move_date;base_movement;acc_debit;amount_base_currency;acc_credit;comment;amount_other_currency
2026-01-12;USD - Bought;BMD;17,293.72;Fi;;11.7400
2026-01-12;USD - Sold;Fi;7,645.18;BMD;;5.1900
```

Each one is a transfer, of a non-internal base movement, between an account in the base
currency and one in another currency (`comment` is optional, and other columns are kept). The
ledger gets it as a movement of `amount_base_currency`. They are added and deleted from the
Currencies tab and `/api/conversions`, and the file is created with the first one.

#### Results of the other currencies

The accounts in other currencies hold their balance in the base currency in the ledger, but
pledger also follows it **in their currency**: the conversions with their
`amount_other_currency`, and every other movement (the initial amount too) at the value of the
currency on its date, the last one up to then (or its first one, if none). At the end of each
date of `currencies.csv`, from its first movement on, each one is **revalued**: a
`USD - Result (G)` or `USD - Result (L)` movement against equity, to the cent, brings its
balance in the base currency to its balance in its currency at that value. Investment accounts
are left out: their capital gains and losses already do that.

### Investments

Investment accounts are the asset accounts listed as top-level keys in `investment.yaml`. Each
one maps its investments to their `type`, initial `amount` and initial `quantity`, or just to
their type (with both at 0). The account's initial amount is the sum of those amounts, and a
quantity can't go below 0.

The type is one of `public_bond`, `private_bond`, `mutual_fund`, `etf`, `forex`, `stock`,
`derivative` and `fixed_term`.

**How movements change the investments.** The investment named in the `additional_2` column of
`ledger.csv`, in the investment account on either side, gets the amount and the quantity of the
`additional` column (`Q = 1,234.50`) on a debit, and loses them on a credit.

- The investment is required when a side is `INVESTMENT`.
- The quantity is required for `Investment - Bought`, `Investment - Sold`, `Stock split (More)`
  and `Stock split (Less)`. `Capital repayments` may have one, and no other base movement can.
- An investment that is only in the ledger has no type until it is added to `investment.yaml`.
  The Investments tab warns about these, and its form adds the type of a new investment there.
- The tab lists the investments held, with their price (amount over quantity), and lists the
  ones no longer held apart.

A free side (neither fixed nor `INVESTMENT`) of an investment base movement can't be an
investment account, in the form or in `ledger.csv`. What pays for an investment bought, gets
the money of one sold, or gets its dividends or pays its fees is another account (e.g. `Fi`).
So only the `INVESTMENT` sides change the investments.

**Results per investment.** The gains and losses naming an investment, except stock splits,
are totalled per investment and base movement, whatever their other account. They are counted
in the investment account on a side, if any, else in the one with an investment of that name.
For each investment, held or not, and overall, the Investments tab shows:

- its **net capital gains**: the gains and losses with an `INVESTMENT` side, e.g.
  `Capital gains` minus `Capital losses`, which add up over time whatever is bought or sold;
- the total of each other one (`Dividends & Interest`, `Broker fees`...);
- the **total result**: all of them, losses subtracted, plus the net interest on money.

**Interest on money.** The `Interest on Money (G)` and `Interest on Money (L)` movements that
name no investment are the interest earned and paid on the money of a regular account (e.g.
`MP`), with no investment involved. The Investments tab totals them in their own section, by
the account that isn't equity and month by month, and adds their net (its *Net interest on
money* card) to the total result. Naming an investment makes one a result of that investment
instead.

It also charts the total invested and the total result at each month end, and warns about
the movements naming an investment of no investment account.

### Debt

Debt accounts are the liability accounts listed as top-level keys in `creditors.yaml`. Each one
has up to three sections, `people`, `services` and `companies`, mapping who we have debts with
to the initial amount: **positive if we owe them, negative if they owe us**. A name can only be
in one section of an account.

Instead of the amount, a name can map to its `amount` and its `expected` role:

| `expected` | Meaning |
| --- | --- |
| `creditor` | We should owe them, or nothing. |
| `debtor` | They should owe us, or nothing. |
| `null` | No expectation. |

Without `expected`, people have no expected role, services are expected to be creditors and
companies debtors.

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

Every company of `work.csv` is a company of every debt account, with an initial amount of 0
unless its `companies` section sets one. The section can also list companies that aren't in the
work history. The account's initial amount is the sum of its initial amounts, and its balance
can go below 0 (we are owed more than we owe).

**Who a movement counts for.** The ledger movements with a debt account on either side add to
whoever is named in their `comment`: as a whole word, ignoring case, the earliest name there,
and the longest one if several start there. A credit is more that we owe them, and a debit is
less.

Like Accounts, the Debt tab shows everyone as of the end of the month picked. It lists people,
services and companies apart, and the movements naming no one as unassigned. It alerts when
there is unassigned debt (listing it by comment) and when anyone is in the opposite role to the
expected one. It also lists the movements of the debt accounts in the month picked, the initial
ones included, with whoever each one names.

### Credit cards

Credit card accounts are the top-level keys of `credit_cards.yaml`. They are set like the
accounts of `accounts.yaml`, with a `description`, an `initial_amount` and the optional
`currency` and `initial_date`, but they aren't listed there. They are always liquid
liabilities, and can't be investment or debt accounts.

```yaml
Vi:
  description: Visa Credit Card from Santander
  initial_amount: 57236.80
```

The Credit Cards tab warns about the cards with a negative balance (more paid than charged),
which is rare and may be an error. It also charts what was charged to them in each month (their
credits, without the payments) and the net owed on them over equity at each month end. It lists
the movements of the cards in the month picked, the initial ones included.

### Work

The Work tab reads the work history from `work.csv`, one row per month, in order:

```csv
Month;Company;Position;Gross salary;Net salary;Received payment date
2024-09;Acme;Acme Sr;4291295.67;4111961.22;2024-10-03
2024-10;;;;;
2024-11;Globex;GX Dev;3165000.42;2694595.00;
```

- **No month can be missing.** A month without a job is a row with no company, position or
  salary.
- A worked month needs both salaries, and the net one can't be larger than the gross one.
- The `Received payment date` column is optional. It is added when a date is first entered.
- The companies and their job positions come from the rows. The job of the last month, if it
  isn't a gap, is the current one.

The tab's form adds a month right after the last one (or right before the first one) or, for an
existing month, replaces it. Each row's ✎ button loads it into the form. Only the first and last
months can be deleted. The file is created if missing and doesn't affect the ledger.

---

## 🛠️ Development

```sh
make install   # editable install with the web and dev extras
make test      # pytest with coverage
make lint      # ruff check --fix
make serve     # run the app with auto-reload
make build     # build the package
```

## 📜 License

[GPL-3.0](LICENSE)
