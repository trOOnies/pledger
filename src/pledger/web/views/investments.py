"""investments.py: the Investments tab: holdings, results, interest on money and their forms."""

from __future__ import annotations

from pledger.enums import AccountType, InvestmentType, MovementType
from pledger.models import INVESTMENT, InvestmentAccount, next_month, rounds_to_zero
from pledger.web.store import (
    BEFORE_COLUMN,
    GENERATED,
    INTEREST,
    INVESTMENT_COLUMN,
    QUANTITY_COLUMN,
    QUANTITY_OPTIONAL,
    QUANTITY_REQUIRED,
    Snapshot,
    interest_fingerprint,
    is_capital,
    row_fingerprint,
)
from pledger.web.views.charts import column_chart, line_chart, with_real
from pledger.web.views.common import grouped_composition, month_end, movement_rows
from pledger.web.views.forms import account_fields, bases_by_type, default_move_type

INVESTMENT_TYPE_LABELS = {
    InvestmentType.PUBLIC_BOND: "Public bond",
    InvestmentType.PRIVATE_BOND: "Private bond",
    InvestmentType.MUTUAL_FUND: "Mutual fund",
    InvestmentType.ETF: "ETF",
    InvestmentType.FOREX: "Forex",
    InvestmentType.STOCK: "Stock",
    InvestmentType.DERIVATIVE: "Derivative",
    InvestmentType.FIXED_TERM: "Fixed term",
}


NO_TYPE = "No type"  # label of the investments only in the ledger, not in the investments YAML


BROKER_FEES = "Broker fees"  # the investment base movement of the fees, shown as a share of the amount


def is_closed(holding: dict) -> bool:
    """An investment no longer held (or never held): no quantity and no amount."""
    return holding["total_quantity"] == 0 and rounds_to_zero(holding["total_amount"])


def build_investments(snapshot: Snapshot) -> dict:
    """
    Investments held in each investment account, after the ledger movements, grouped
    by type and by account, with their results: the net capital gains (capital
    gains minus losses, whatever was bought or sold since), the totals of the other
    results (dividends, broker fees...) by base movement and their net total. The
    closed ones (see is_closed) are listed apart, and the ones with no type and the
    results naming an unknown investment are flagged.
    """
    unattributed: list[dict] = []
    accounts = snapshot.investment_accounts(unattributed)
    bases = snapshot.invest_base_movements()
    sign = result_signs(snapshot)
    capital = {n for n, b in bases.items() if is_capital(b)}
    # The other base movements with results, in their defined order, and whether they are losses.
    result_bases = {
        n: b.move_type == MovementType.LOSS
        for n, b in bases.items()
        if n not in capital and any(n in r for a in accounts for r in a.results.values())
    }

    def results_of(a: InvestmentAccount, name: str) -> dict:
        results = a.results.get(name, {})
        return {
            "capital": sum(sign[n] * v for n, v in results.items() if n in capital),
            "results": {n: v for n, v in results.items() if n not in capital},
            "total": sum(sign[n] * v for n, v in results.items()),
        }

    closed = sorted(
        (
            {
                "name": name,
                "account": a.name,
                "type": INVESTMENT_TYPE_LABELS.get(h["investment"].type, NO_TYPE),
                **results_of(a, name),
            }
            for a in accounts for name, h in a.investments.items() if is_closed(h)
        ),
        key=lambda c: (c["name"], c["account"]),
    )
    for a in accounts:
        a.investments = {n: h for n, h in a.investments.items() if not is_closed(h)}
    holdings = [
        {
            "account": a.name,
            "name": name,
            "type": INVESTMENT_TYPE_LABELS.get(h["investment"].type, NO_TYPE),
            "amount": h["total_amount"],
            "quantity": h["total_quantity"],
            # Amount per unit; none without a quantity.
            "price": h["total_amount"] / h["total_quantity"] if h["total_quantity"] else None,
            **results_of(a, name),
        }
        for a in accounts
        for name, h in a.investments.items()
    ]
    total = sum(h["amount"] for h in holdings)
    for h in holdings:
        h["share"] = round(100 * h["amount"] / total, 1) + 0.0 if total else 0.0
    held = {
        "amount": total,
        "capital": sum(h["capital"] for h in holdings),
        "results": {n: sum(h["results"].get(n, 0.0) for h in holdings) for n in result_bases},
        "total": sum(h["total"] for h in holdings),
    }
    for i in [*holdings, held]:
        i["fees_share"] = fees_share(i)

    # Types and accounts keep their defined order, listing only the ones with
    # investments. Investment names are only unique within an account.
    by_type = {
        t: {f"{h['name']} ({h['account']})": h["amount"] for h in holdings if h["type"] == t}
        for t in [*INVESTMENT_TYPE_LABELS.values(), NO_TYPE]
        if any(h["type"] == t for h in holdings)
    }
    by_account = {
        a.name: {name: h["total_amount"] for name, h in a.investments.items()}
        for a in accounts
        if a.investments
    }
    interest = build_interest(snapshot)
    return {
        "accounts": [
            {
                "name": a.name,
                "description": a.description,
                "currency": a.currency.name,
                "holdings": len(a.investments),
                "amount": sum(h["total_amount"] for h in a.investments.values()),
            }
            for a in accounts
        ],
        "holdings": holdings,
        "held": held,  # the totals of the holdings
        "has_fees": BROKER_FEES in result_bases,
        "closed": closed,
        "result_bases": result_bases,
        "has_capital": bool(capital),
        "has_results": bool(capital or result_bases),
        # Of the held and closed investments, and the interest on money
        "result_totals": {
            "capital": sum(i["capital"] for i in [*holdings, *closed]),
            **{n: sum(i["results"].get(n, 0.0) for i in [*holdings, *closed]) for n in result_bases},
            "interest": interest["interest_totals"]["net"],
            "total": sum(i["total"] for i in [*holdings, *closed]) + interest["interest_totals"]["net"],
        },
        **interest,
        "unattributed": unattributed,
        "untyped": [h for h in holdings if h["type"] == NO_TYPE],
        "total": total,
        "groupings": {
            "type": grouped_composition(by_type),
            "account": grouped_composition(by_account),
        },
        **investment_charts(snapshot),
    }


def fees_share(results: dict) -> float | None:
    """
    The broker fees of an investment (or of several) as a share of its amount, or
    None without fees or a positive amount.
    """
    fees = results["results"].get(BROKER_FEES, 0.0)
    return fees / results["amount"] if fees and results["amount"] > 0 else None


def interest_movements(snapshot: Snapshot) -> list:
    """
    The movements of interest on money, of the interest CSV: the interest earned or
    paid on the money of a regular account.
    """
    return snapshot.ledger(snapshot.interest_rows).movements


def build_interest(snapshot: Snapshot) -> dict:
    """
    The interest on money (see interest_movements) earned and paid in each account,
    in their defined order, and its net in each month, from the first month of the
    ledger to the last one. `has_interest` if its base movements are defined.
    """
    moves = interest_movements(snapshot)
    by_account: dict[str, dict] = {}
    for a in snapshot.accounts.values():
        # Each movement counts for its account on the side that isn't equity.
        mine = [m for m in moves if a in (m.acc_debit, m.acc_credit)]
        if mine and a.acc_type != AccountType.EQUITY:
            gains = sum(m.amount for m in mine if m.move_type == MovementType.GAIN)
            losses = sum(m.amount for m in mine if m.move_type == MovementType.LOSS)
            by_account[a.name] = {
                "name": a.name, "description": a.description, "currency": a.currency.name,
                "gains": gains, "losses": losses, "net": gains - losses, "count": len(mine),
            }
    totals: dict[str, float] = {}  # month to its net interest
    for m in moves:
        sign = 1 if m.move_type == MovementType.GAIN else -1
        month = str(m.move_date)[:7]
        totals[month] = totals.get(month, 0.0) + sign * m.amount
    series = []
    dates = snapshot.all_rows.move_date
    if moves and len(dates):
        cur, last = min(dates)[:7], max(dates)[:7]
        while cur <= last:
            series.append({"month": cur, "value": totals.get(cur, 0.0)})
            cur = next_month(cur)
    accounts = list(by_account.values())
    return {
        "has_interest": bool(INTEREST & snapshot.base_movements.keys()),
        "interest_accounts": accounts,
        "interest_totals": {k: sum(a[k] for a in accounts) for k in ("gains", "losses", "net")},
        "interest_chart": column_chart(series) if series else None,
        "interest_series": series,
    }


def result_signs(snapshot: Snapshot) -> dict[str, int]:
    """The sign each investment base movement adds to a result with: -1 for losses."""
    return {
        n: -1 if b.move_type == MovementType.LOSS else 1
        for n, b in snapshot.invest_base_movements().items()
    }


def investment_charts(snapshot: Snapshot) -> dict:
    """
    The total invested (the amounts of all investments) and the total result of
    the investments, held or not, and of the interest on money, at the end of each
    month of the ledger. The total invested also gets its real value (see with_real).
    """
    dates = snapshot.all_rows.move_date
    if not len(dates):
        return {"invested_chart": None, "invested_series": [], "result_chart": None, "result_series": []}
    sign = result_signs(snapshot)
    interest = [
        (str(m.move_date)[:7], m.amount if m.move_type == MovementType.GAIN else -m.amount)
        for m in interest_movements(snapshot)
    ]
    invested, result = [], []
    month, last = min(dates)[:7], max(dates)[:7]
    while month <= last:
        accounts = snapshot.investment_accounts(month=month)
        for series, value in (
            (invested, sum(h["total_amount"] for a in accounts for h in a.investments.values())),
            (result, sum(sign[n] * v for a in accounts for r in a.results.values() for n, v in r.items())
                + sum(v for m, v in interest if m <= month)),
        ):
            change = value - series[-1]["value"] if series else value
            series.append({"date": f"{month}-01", "month": month, "value": value, "change": change})
        month = next_month(month)
    invested_real = with_real(
        invested, snapshot.base_currency.customer_price_index, [month_end(p["month"]) for p in invested]
    )
    invested_chart = line_chart(invested)
    if invested_chart:
        invested_chart["real_date"] = invested_real
    return {
        "invested_chart": invested_chart,
        "invested_series": invested,
        "result_chart": line_chart(result),
        "result_series": result,
    }


def build_invest_movements(snapshot: Snapshot) -> list[dict]:
    """
    The movements of the investment base movements, newest first: those of the
    investments CSV, with their investment and quantity, their capital gains and losses
    (see store._capital_rows), right before the movement they come from or at the end of
    the date of their price, and those of the interest CSV. The `index` of the ones of a
    CSV is their row in the CSV of their `source`. Their `account` is the
    investment account on a side, else, like for their results (e.g. broker fees),
    the first one with an investment of their name, if any. Each one has the total amount
    of its investment account, and the amount and quantity of its investment, right after
    it, all the movements counted (see _apply_investments), or None without them.
    """
    holders = [a for a in snapshot.investment_accounts() if a.investments]

    def account_of(m: dict, investment: str) -> str | None:
        return next(
            (n for n in (m["debit"], m["credit"]) if isinstance(snapshot.accounts[n], InvestmentAccount)),
            next((a.name for a in holders if investment and investment in a.investments), None),
        )

    # The capital gains and losses by the row of the investments CSV they come right before
    capital, before = snapshot.capital_rows, {}
    generated = capital.drop(columns=[INVESTMENT_COLUMN, BEFORE_COLUMN])
    for m, (_, row) in zip(movement_rows(snapshot, generated, snapshot.ledger(generated))[::-1], capital.iterrows()):
        before.setdefault(row[BEFORE_COLUMN], []).append({
            **m, "fingerprint": "", "editable": False,
            "account": account_of(m, row[INVESTMENT_COLUMN]), "investment": row[INVESTMENT_COLUMN], "quantity": "",
        })
    movements = []
    for source, rows, generated, fingerprint in (
        ("investments", snapshot.investments, snapshot.investment_rows, row_fingerprint),
        ("interest", snapshot.interest, snapshot.interest_rows, interest_fingerprint),
    ):
        # movement_rows lists them newest first: back to the order of the CSV
        moves = movement_rows(snapshot, generated, snapshot.ledger(generated))[::-1]
        for i, (m, (_, row)) in enumerate(zip(moves, rows.iterrows())):
            investment = row.get(INVESTMENT_COLUMN, "")
            if source == "investments":
                movements.extend(before.pop(str(i), []))
            movements.append({
                **m, "index": i, "fingerprint": fingerprint(row), "source": source, "editable": True,
                "account": account_of(m, investment),  # the investment account, if any
                "investment": investment, "quantity": row.get(QUANTITY_COLUMN, ""),
            })
        if source == "investments":
            movements.extend(before.pop("", []))  # of the prices, at the end of their date
    movements.sort(key=lambda m: m["date"])  # stable: in their order on each date
    # From the initial amounts and quantities of the investments file
    totals = {a.name: sum(h["total_amount"] for h in a.investments.values())
              for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)}
    held = {(a.name, n): [h["total_amount"], h["total_quantity"]]
            for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount) for n, h in a.investments.items()}
    for m in movements:
        units = float(str(m["quantity"]).replace(",", "") or 0)
        for name, sign in ((m["debit"], 1), (m["credit"], -1)):
            if name in totals and m["investment"]:
                totals[name] += sign * m["amount"]
                holding = held.setdefault((name, m["investment"]), [0.0, 0.0])
                holding[0] += sign * m["amount"]
                holding[1] += sign * units
        holding = held.get((m["account"], m["investment"])) if m["investment"] else None
        m["account_total"] = totals.get(m["account"])
        m["investment_amount"], m["investment_quantity"] = holding if holding else (None, None)
    movements.reverse()
    return movements


def invest_filter(movements: list[dict], account: str = "", investment: str = "", base: str = "") -> dict:
    """
    The investment movements of an investment `account`, of an `investment` and of a
    `base` movement (all of them without any), whatever accounts are their debit and
    credit ones, with the accounts, the investments (of the account picked, if any) and
    the base movements to pick from. An investment not of the account picked is dropped.
    """
    by_account: dict[str, set[str]] = {}
    for m in movements:
        if m["account"]:
            by_account.setdefault(m["account"], set())
            if m["investment"]:
                by_account[m["account"]].add(m["investment"])
    account = account if account in by_account else ""
    names = by_account[account] if account else {m["investment"] for m in movements if m["investment"]}
    investment = investment if investment in names else ""
    bases = sorted({m["base"] for m in movements})
    base = base if base in bases else ""
    return {
        "movements": [
            m for m in movements
            if (not account or m["account"] == account) and (not investment or m["investment"] == investment)
            and (not base or m["base"] == base)
        ],
        "invest_bases": bases,
        "invest_base": base,
        "invest_accounts": sorted(by_account),
        "invest_names": sorted(names),
        "invest_account": account,
        "investment": investment,
    }


def invest_fields(snapshot: Snapshot, base_movement: str = "", move_type: str = "") -> dict:
    """
    The investment base movements to pick from for a movement type (without one,
    that of the base movement, else a loss: see default_move_type), but the
    conversions, and the fields of the selected one (the first one by default): its
    accounts, whether it names an investment ("required" with an INVESTMENT side,
    else "optional") and has a quantity ("required", "optional" or None), plus the
    known investments to pick from.
    """
    bases = {
        n: b for n, b in snapshot.invest_base_movements().items()
        if n not in snapshot.conversion_base_names and n not in GENERATED
    }
    by_type = {t: [n for n in bases if bases[n].move_type.value == t] for t in bases_by_type(bases)}
    if move_type not in by_type:
        move_type = bases[base_movement].move_type.value if base_movement in bases else default_move_type(by_type)
    if base_movement not in by_type[move_type]:
        base_movement = next(iter(by_type[move_type]), "")
    base = bases.get(base_movement)
    side = base is not None and INVESTMENT in {base.debit_special, base.credit_special}
    return {
        "move_types": list(by_type),
        "move_type": move_type,
        "bases": by_type[move_type],
        "base_movement": base_movement,
        "fields": account_fields(snapshot, base_movement),
        "investment": None if base_movement in INTEREST else "required" if side else "optional",
        "quantity": (
            "required" if base_movement in QUANTITY_REQUIRED
            else "optional" if base_movement in QUANTITY_OPTIONAL
            else None
        ),
        # Those of the investments YAML and those only in the ledger, by account
        "investments_by_account": {
            a.name: sorted(a.investments) for a in snapshot.investment_accounts() if a.investments
        },
    }


def price_fields(snapshot: Snapshot) -> dict:
    """
    The investments to pick from for a new price, by investment account: those of the
    investments YAML and those only in the ledger.
    """
    return {
        "investments_by_account": {
            a.name: sorted(a.investments) for a in snapshot.investment_accounts() if a.investments
        },
    }


def new_investment_fields(snapshot: Snapshot) -> dict:
    """The investment accounts and investment types to pick from for a new investment."""
    return {
        "accounts": [a.name for a in snapshot.accounts.values() if isinstance(a, InvestmentAccount)],
        "types": INVESTMENT_TYPE_LABELS,
    }
