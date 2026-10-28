# <img src="src/pledger/web/static/favicon.svg" alt="" width="36" align="top"> pledger

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
| 📋 **Ledger** | Lists, adds, edits and deletes movements, with the balances of their accounts right after each one, filtered by month, account and base movement. Conversions, debt movements and card payments are added in their own tabs instead. |
| 🏦 **Accounts** | Balances, total assets, liabilities and equity, a form to add accounts, edit the asset and liability ones (✎) and correct their balance (⚖), the composition of assets and liabilities (by currency or by liquidity) and, at each month end, debt over equity and net liquidity (liquid assets minus liquid liabilities) over equity. |
| 💱 **Currencies** | Balances per currency, the balance of each account in other currencies in its own currency, the daily CPI of the base currency, its monthly inflation, the nominal and real value of each other currency, and the share of each currency in the assets at each month end. The conversions are added (picking the currency sold and the one bought), edited and deleted here, filtered by currency and account, each with the account's balance in its own currency after it. |
| 📊 **Results** | The nominal and real change of equity in the period and the months of expenses it covers, gains, losses and profit by result type (also cumulative), a waterfall of the result by type (with its result statement as a table, both also as a share of the gains of the first result type, e.g. *Operative*) and equity over time. Result types and base movements are listed and added here. |
| 💼 **Work** | The company, job position and salary of each month, from gross to net through its deductions (taxes, contributions...), with a chart of both salaries (0 for the months without a job). |
| 📈 **Investments** | The investments of the investment accounts with their price (amount over quantity), their composition by type or by account, the results of each one, the interest on money of regular accounts, and charts of the total invested and the total result. The broker fees of each investment are shown over its amount. The **Investment account**, **Investment** and **Base movement** filters narrow the movements down (an investment shows all its movements, whatever their accounts), each with the total of its account and the amount and quantity of its investment after it. The internal `Invest` movements are added, edited and deleted here. |
| 🤝 **Debt** | Who we owe and who owes us in the debt account `De` (people, services and companies), with alerts and the movements of the month, each with what we owe whoever it names after it. The debt movements are added and edited here. |
| 💳 **Credit Cards** | What we owe on each card, what was charged and paid in the month, a chart of the monthly charges, the net owed over equity, and the movements of the month, each with what we owe on its card after it. The card payments are added and edited here. |
| ❓ **Help** | The concepts, explained for everyday users. |

A few things are shared across tabs:

- 🗓️ The **month filter** is shared by Ledger, Accounts, Results, Debt and Credit Cards, and
  remembered in a cookie.
- 🔮 In the Currencies tab, the months that haven't ended yet are **projected**: dashed in the
  CPI chart and faded in the inflation one.
- 👁️ The **eye button** in the top bar replaces every amount with `******` until clicked
  again. The browser remembers the choice.
- 🔗 The **ID** of a movement links to its other face: from the Ledger tab to its row in the tab
  that handles it (like its colored circle), and from the Investments, Currencies, Debt and
  Credit Cards tabs back to the Ledger tab.
- ↕️ The bars of Accounts, Results, Investments and Debt have an **Expand all / Collapse all**
  toggle.
- ⏻ **Save & Quit**, in the top bar, saves what is left to save (the IDs of the rows added to the
  CSVs by hand) and stops the server, `make serve` included, closing the window if the browser
  allows it.
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
| `movements/investment.csv` | | `PLEDGER_INVESTMENT_MOVEMENTS` | The movements of the investments |
| `movements/interest.csv` | | `PLEDGER_INTEREST` | The interest on money of the regular accounts |
| `currencies.yaml` | ✅ | `PLEDGER_CURRENCIES` | Base and other currencies (with `base_currency.csv` and `currencies.csv` next to it) |
| `investment.yaml` | | `PLEDGER_INVESTMENTS` | The investments of each investment account |
| `creditors.yaml` | | `PLEDGER_CREDITORS` | Who each debt account is owed by or owes to |
| `credit_cards.yaml` | | `PLEDGER_CREDIT_CARDS` | The credit card accounts |
| `work.csv` | | `PLEDGER_WORK` | The work history, one row per month |

On startup, `pledger-web` checks that every one of these files (and `base_currency.csv` and
`currencies.csv`) exists. Any missing ones are created, the YAMLs empty and the CSVs with just
their header. pledger then lists them and exits, so you can fill in the required ones before
you start it again.

**The CSV stays the source of truth.** Adding a movement appends one line, deleting removes
one line, and the rest of the file is left byte-for-byte as it was. New accounts and base
movements are inserted at the end of their section in the YAML files (gains and losses under
their result type, transfers under `transfer`), leaving the rest of each file as it was.

**Every movement has an id that never changes**, shown in the Ledger tab and the API. The
rows of the four CSVs of movements keep theirs in a last `id` column: `L-1`, `L-2`… in
`ledger.csv`, `C-…` in `conversion.csv`, `I-…` in `investment.csv` and `M-…` in
`interest.csv`. **Pledger gives them itself**: on startup and whenever it adds a movement to a
CSV, every row without a valid id (one added by hand, or copied with another row's id) gets
the next number, appended to its line, and the column is added if missing. Leave the `id`
empty when adding rows by hand. The generated movements get theirs from what they come from:
`A-<account>` (initial amounts), `W-<month>-<week>` (salaries) and `R-<account>-<date>`
(results of the accounts in other currencies).

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

The top-level `internal` key has the same sections as the rest of the file, with exactly the
three **internal accounts**, which pledger needs and no other section has:

- `Fi` (asset): an intermediary between movements, like the conversions between two other
  currencies (see [Conversions](#conversions)). Usually with the `net_zero` check.
- `De` (liability): the debt account, with its creditors in `creditors.yaml` (see [Debt](#debt)).
- `Eq` (equity): the only equity account.

`Fi` and `De` are in the base currency. Every account but `Eq`, the investment ones and `De` must
set an `initial_amount`, in its currency (those must not). Accounts also take these optional keys:

- `currency`: the base currency by default.
- `is_liquid`: `false` by default.
- `initial_date`: the initial week by default (see `previous_month` below), else the first date
  of the ledger. With a `previous_month`, it must be after its initial week.
- `checks`: a list of checks on the account balance at the end of each day of the ledger, with
  all the movements of the day summed. None by default.
  - `net_positive`: the balance is never negative.
  - `net_zero`: the balance is always 0.00, for accounts like an intermediary between
    movements that should never hold money.

  The ledger doesn't load if an account breaks a check, and a movement that would break one
  can't be added or deleted. The New account form can set one, and the Accounts tab shows them
  in its Checks column.

```yaml
previous_month: 2025-12
asset:
  Ca:
    description: ARS in physical safety deposit box
    is_liquid: true
    initial_amount: 400000.00
    checks:
    - net_positive
liability: null
internal:
  asset:
    Fi:
      description: Ficticious balance as an intermediary between movements
      initial_amount: 0.00
      checks:
      - net_zero
  liability:
    De:
      description: Net owed money (debts - loans) in base currency
  equity:
    Eq:
      description: Equity account
```

The optional top-level `previous_month` (`YYYY-MM`, a past month) is the last month left out of
the movements: an initial picture of the accounts. Its last week (a week belongs to the month of
its Thursday) is the **initial week**: the initial movements of the accounts are on its Monday,
and they are the only movements in it, so the movements and the salaries of the work history
start on the week after it, the first one of the next month. Accounts starting later set their
own `initial_date`, after the initial week. The values of the currencies in the initial week
revalue nothing until the first date of `currencies.csv` after it. Without a `previous_month`,
the ledger starts on the date of its first movement.

The Accounts tab groups their balances by currency or by liquidity. Its ✎ edits an asset or
liability account in its YAML, the internal ones too (all but its name, type and currency), and its ⚖ corrects the
balance of one (but an investment account) to the one checked by hand at the end of a date, today
by default, in its currency. The difference is a movement of one of these internal base movements,
on that date, in the ledger CSV:

```yaml
internal:
  result_types:
    Corr:   # or any other internal result type
      id: 9
      long_name: Corrections
      description: Results because of untracked movements that prompted balance corrections
      base_movements:
        gain:
          Correction (G): Money correction in favour of oneself
        loss:
          Correction (L): Money correction against oneself
```

The Ledger tab shows them with a 🟣 linking to their account, and can delete them.

### Base movements and internal movements

A base movement is a dict (`description` and optional fixed `debit`/`credit` accounts) or just
its description. A side set to `INVESTMENT`, `CREDIT CARD` or `FOREIGN CURRENCY` (reserved
names) takes any investment, credit card or foreign currency account (in another currency, but
the investment ones) instead of a fixed one, and the forms only offer those there. The Accounts
tab marks the investment, credit card and debt accounts in its Special column.

The top-level `internal` key mirrors the rest of the file: its `result_types`, sharing their ids
with the other ones, next to a `transfer` section. Its base movements are reserved for pledger
itself. They can't be picked for a new movement in the Ledger tab, their movements can't be deleted
by hand (but those of the Investments, Debt and Credit Cards tabs, from there), and its result
types can't get new base movements.

> [!NOTE]
> **Exception: investments.** The base movements of the internal `Invest` result type (capital
> gains and losses, interest, broker fees...) and the internal transfers with an `INVESTMENT`
> side (buying and selling investments...) can't be added or deleted from the Ledger tab or
> `/api/movements`, but they can from the Investments tab and `/api/investments/movements`.
> Their movements are kept in `movements/investment.csv` and `movements/interest.csv` (see
> [Investments](#investments)), never in `ledger.csv`.

The internal gain `Foreign currency (G)` and loss `Foreign currency (L)` are required in the same way
(their side may also be `FOREIGN CURRENCY`) once an account in another currency has to be revalued (see
[Currencies and inflation](#currencies-and-inflation)). Their movements are generated, and
can't be in `ledger.csv`, just like the initial ones below, but they aren't hidden.

The internal gain `Initial (G)` and loss `Initial (L)` are required (with no fixed debit or
credit account, respectively). Every account with a non-zero initial amount gets one such
movement against equity, on its initial date. That of an account in another currency is at the
value of its currency on that date (the last one up to then, else its first one). These movements:

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

The Currencies tab also shows the share of equity in the accounts in other currencies, valued
in the base currency.

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
2026-01-12;Foreign currency - Bought;BMD;17,293.72;Fi;;11.7400
2026-01-12;Foreign currency - Sold;Fi;7,645.18;BMD;;5.1900
```

Each one is between an account in the base currency and one in another currency (`comment` is
optional, and other columns are kept). The ledger gets it as a movement of
`amount_base_currency`. They are added and deleted from the Currencies tab and
`/api/conversions`, and the file is created with the first one.

Conversions are of these internal transfers, for every other currency: `Foreign currency -
Bought` debiting the account in the other currency, or `Foreign currency - Sold` crediting it.
The Currencies tab picks it from the currency sold and the one bought, so they can't be added
from the Ledger or Investments tabs.

Selling one other currency for another (e.g. USC for USD) is a **cross conversion**, through the
internal `Fi`: a `Foreign currency - Sold` of the currency sold into it, right followed by a
`Foreign currency - Bought` of the one bought out of it, both of the amount sold at the value of
its currency on that date (in `currencies.csv`, the last one up to then), so `Fi` is left as it
was. The Currencies tab adds them from its form, and marks both rows with ⇄ and the ID of the
other one; they are edited and deleted together. Two such rows added by hand count as one too.

```yaml
internal:
  transfer:
    Foreign currency - Bought:
      description: Buying of foreign currency with base_currency
      debit: FOREIGN CURRENCY
    Foreign currency - Sold:
      description: Selling of foreign currency for base_currency
      credit: FOREIGN CURRENCY
```

#### Results of the other currencies

The accounts in other currencies hold their balance in the base currency in the ledger, but
pledger also follows it **in their currency**: the conversions with their
`amount_other_currency`, its initial amount, in its currency, and every other movement at the
value of the currency on its date, the last one up to then (or its first one, if none). At the
end of each date of `currencies.csv`, from its first movement on, each one is **revalued**: a
`Foreign currency (G)` or `Foreign currency (L)` movement against equity, to the cent, brings its
balance in the base currency to its balance in its currency at that value. Investment accounts
are left out: their capital gains and losses already do that.

### Investments

Investment accounts are the asset accounts listed as top-level keys in `investment.yaml`. Each
one maps its investments to their `type`, initial `amount` and initial `quantity`, or just to
their type (with both at 0). The account's initial amount is the sum of those amounts, and a
quantity can't go below 0.

The type is one of `public_bond`, `private_bond`, `mutual_fund`, `etf`, `forex`, `stock`,
`derivative` and `fixed_term`.

**How movements change the investments.** The movements of the investment base movements,
but the interest on money, are kept in `movements/investment.csv`, with the columns of
`ledger.csv` plus the `quantity` and the `investment`:

```csv
move_date;base_movement;acc_debit;amount;acc_credit;quantity;investment;comment
2026-01-12;Investment - Bought;BMI;1,494,988.65;BM;27.00;OC01Q;
2026-01-12;Broker fees;Eq;2,261.18;BM;;OC01Q;
```

The investment it names, in the investment account on either side, gets its amount and
quantity on a debit, and loses them on a credit. The file is created when adding the first one.

- The investment is required when a side is `INVESTMENT`.
- The quantity is required for `Investment - Bought`, `Investment - Sold`, `Stock split (More)`
  and `Stock split (Less)`. `Capital repayments` may have one, and no other base movement can.
- A movement of an investment account names one of its investments: one in `investment.yaml`
  or only in the ledger. Add a new one first, with its type (and optionally its initial amount
  and quantity), in the *New investment* form of the Investments tab (or `POST /api/investments`).
- An investment that is only in the ledger has no type until it is added to `investment.yaml`.
  The Investments tab warns about these, and its *New investment* form gives them their type.
- The tab lists the investments held, with their price (amount over quantity), their results,
  the `Broker fees` as a share of their total result (when positive) and a totals row, and lists
  the ones no longer held apart.

A free side (neither fixed nor `INVESTMENT`) of an investment base movement can't be an
investment account, in the form or in `investment.csv`. What pays for an investment bought, gets
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

**Interest on money.** The interest earned and paid on the money of a regular account (e.g.
`MP`), with no investment involved, is kept in `movements/interest.csv`: the account, never an
investment one, and the amount, **positive if earned, negative if paid**.

```csv
move_date;acc_debit;amount;comment
2026-01-05;MP;21,697.40;
2026-01-12;St;-1,500.00;Overdraft
```

Earned interest is an `Interest on Money (G)` movement debiting the account, and paid interest
an `Interest on Money (L)` one crediting it, against equity: both internal base movements are
required for the rows that use them. The Investments tab adds and deletes them like the other
investment movements, totals them in their own section, by account and month by month, and adds
their net (its *Net interest on money* card) to the total result. The interest of an
investment is one of its results instead (e.g. `Dividends & Interest`, in `investment.csv`).

It also charts the total invested and the total result at each month end, and warns about
the movements naming an investment of no investment account.

### Debt

The debt account is the internal `De`, the only top-level key of `creditors.yaml` (optional:
without it, `De` has no creditors but the companies). It has up to three sections, `people`,
`services` and `companies`, mapping who we have debts with to the initial amount: **positive if
we owe them, negative if they owe us**. A name can only be in one section.

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

Every company of `work.csv` is a company of `De`, with an initial amount of 0
unless its `companies` section sets one. The section can also list companies that aren't in the
work history. The account's initial amount is the sum of its initial amounts, and its balance
can go below 0 (we are owed more than we owe).

**Who a movement counts for.** The ledger movements with a debt account on either side add to
whoever is named in their `comment`: as a whole word, ignoring case, the earliest name there,
and the longest one if several start there. A credit is more that we owe them, and a debit is
less.

The **debt movements** are those of four internal transfers, which the Debt tab needs under
`transfer` in the `internal` section:

```yaml
internal:
  transfer:
    Debt - Incurred:   # we owe more
      description: Debt incurred by oneself
      credit: De
    Debt - Paid:       # we owe less
      description: Debt paid by oneself
      debit: De
    Loan - Given:      # we are owed more
      description: Loan given to another person
      debit: De
    Loan - Collected:  # we are owed less
      description: Loan repaid by another person
      credit: De
```

They are added, edited and deleted from the Debt tab only (or `/api/debt/movements`), picking one
of them in two steps (Debt, then Incurred or Paid; Loan, then Given or Collected). Its form
asks who the movement is about, and names them first in the comment: people and services, never
a company, whose debt comes from the salaries of the Work tab (it is still listed as usual). The
Ledger tab shows them with a 🟡 linking there.

Like Accounts, the Debt tab shows everyone as of the end of the month picked. It lists people,
services and companies apart, and the movements naming no one as unassigned. It alerts when
there is unassigned debt (listing it by comment) and when anyone is in the opposite role to the
expected one. It also lists the movements of `De` in the month picked, the initial
ones included, with whoever each one names, and the **Who** filter shows only the movements of
one of them (or of no one).

### Credit cards

Credit card accounts are the top-level keys of `credit_cards.yaml`. They are set like the
accounts of `accounts.yaml`, with a `description`, an `initial_amount` and the optional
`currency` and `initial_date`, but they aren't listed there. They are always liquid
liabilities, and can't be investment or internal accounts.

```yaml
Vi:
  description: Visa Credit Card from Santander
  initial_amount: 57236.80
```

The **card payments** and **surpluses** (negative payments: money the card gives back) are
those of two internal transfers, which the Credit Cards tab needs under `transfer` in the
`internal` section:

```yaml
internal:
  transfer:
    Credit card - Payment:
      description: Payment of a credit card balance
      debit: CREDIT CARD
    Credit card - Surplus:
      description: Surplus of a credit card balance (a negative payment)
      credit: CREDIT CARD
```

They are added, edited and deleted from the Credit Cards tab only (or
`/api/credit-cards/payments`), picking the base movement, the card and the other account. The Ledger tab
shows them with a 🔴 linking there. The **Credit card** filter shows only the movements of one card.

The Credit Cards tab warns about the cards with a negative balance (more paid than charged),
which is rare and may be an error. It also charts what was charged to them in each month (their
credits, without the payments) and the net owed on them over equity at each month end. It lists
the movements of the cards in the month picked, the initial ones included.

### Work

The Work tab reads the work history from `work.csv`, one row per month, in order:

```csv
Month;Company;Position;Work - Salary Main;Received payment date;Taxes - Income (L);Taxes - Pension
2024-09;Acme;Acme Sr;4291295.67;2024-10-03;100,000.00;79,334.45
2024-10;;;;;;
2024-11;Globex;GX Dev;3165000.42;;250,000.00;220,405.42
```

- **No month can be missing.** A month without a job is a row with no company, position or
  salary.
- `Work - Salary Main` is the gross salary, required for a worked month.
- **Every other column is a deduction** of the gross salary, named after its base movement:
  taxes, contributions, fees... The **net salary** is the gross one minus them all, so they
  can't add up to more than it. Empty cells are 0.
- The `Received payment date` column is optional. It is added when a date is first entered.
- The companies and their job positions come from the rows. The job of the last month, if it
  isn't a gap, is the current one.

The tab shows the gross, deducted and net salary of each month, and how this year's gross salary
gets to the net one through each deduction. Its form adds a month right after the last one (or
right before the first one) or, for an existing month, replaces it. It has a field for each
deduction column; add a new deduction as a column of `work.csv`. Its *Fill in from* button fills
the form in with the company, position and salaries of the last month (not its month or received
payment date), in the browser only. Each row's ✎ button loads it into the form. Only the first and last
months can be deleted. The file is created if missing.

**Salaries.** The gross salary of each worked month is in the ledger **week by week**, from the
first date of the ledger on. Weeks go from Monday to Sunday, and each one belongs to the month
with at least 4 of its days (the month of its Thursday), so a month has 4 or 5 weeks. The gross
salary is split equally between them, to the cent (the cents left over go to the first weeks),
as a `Work - Salary Main` movement on each Monday.

- `Work - Salary Main` must be a gain with a fixed debit account, e.g. the debt account `De`, so
  that the company owes the salary until it's paid.
- Its comment names the company first (e.g. `Galicia (GAL DS): week 2/5 of 2026-01`), so a debt
  account attributes it to the company (see [Debt](#debt)).
- These movements are generated when the ledger is loaded, like the initial ones: they can't be
  added by hand or be in `ledger.csv`. Editing a month of the Work tab updates them.

**Deductions.** Each deduction of a month is split between the same weeks the same way, as a
movement of its base movement on each Monday, next to the salary's. It credits the debit account
of `Work - Salary Main`, so that account ends up with the net salary.

- The base movement of a deduction must be a non-internal loss with a fixed debit account
  (e.g. `Eq`), and no fixed credit account other than the debit one of `Work - Salary Main`.
- Unlike the salary's, these base movements can still be used by hand in `ledger.csv`, e.g. for
  taxes paid outside of work. Don't record a deduction there too, or it counts twice.

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
