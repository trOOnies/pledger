"""work.py: the Work tab: the work history, salaries and the month form, and what the companies owe in Wo."""

from __future__ import annotations

import datetime as dt

from pandas import Series

from pledger.enums import CreditorKind, DebtRole, LedgerSide
from pledger.models import DebtAccount, next_month, rounds_to_zero
from pledger.web.store import LOAN_COLLECTED, Snapshot, Work, work_fingerprint
from pledger.web.views.charts import line_chart, with_real
from pledger.web.views.common import month_end
from pledger.web.views.debt import kind_movements, net_creditors
from pledger.web.views.forms import by_group


def work_form(work: Work, month: str = "", values: dict | None = None) -> dict:
    """
    The state of the work month form: a month's values to edit it, or by default
    the month after the last one, at the same job.
    """
    by_month = {m.month: m for m in work.months}
    last = work.months[-1] if work.months else None
    if not month:
        month = next_month(last.month) if last else dt.date.today().strftime("%Y-%m")
    if values is None:
        m = by_month.get(month)
        if m is None:  # a new month, at the last job by default
            values = {"company": last.company if last else "", "position": last.position if last else ""}
        else:
            values = {
                "company": m.company or "",
                "position": m.position or "",
                "gross": "" if m.gross is None else f"{m.gross:,.2f}",
                "deductions": {n: f"{d:,.2f}" for n, d in m.deductions.items()},
                "payment_date": m.payment_date.isoformat() if m.payment_date else "",
            }
    return {
        "fields": {"month": month, **values},
        "deductions": work.deductions,
        "autofill": last_month_fields(work),
        "editing": month in by_month,
        "company_names": list(work.companies),
        "position_names": sorted({p for c in work.companies.values() for p in c.positions}),
    }


def last_month_fields(work: Work) -> dict | None:
    """
    The values of the fields of the work month form for the last month, to fill it
    in with them in the browser: all but its month and its received payment date.
    None without months.
    """
    if not work.months:
        return None
    m = work.months[-1]
    return {
        "month": m.month,
        "fields": {
            "company": m.company or "",
            "position": m.position or "",
            "gross": "" if m.gross is None else f"{m.gross:,.2f}",
            **{f"deduction:{n}": f"{m.deductions[n]:,.2f}" if n in m.deductions else "" for n in work.deductions},
        },
    }


def build_work(work: Work, form: dict | None = None, cpi: Series | None = None) -> dict:
    """
    The companies worked for and their job positions, every month of the work
    history (the newest first) and the totals of this year, from the gross salary
    to the net one through each deduction (the largest first), plus the form state.
    Worked months already over with no received payment date are flagged as unpaid.
    With the `cpi` of the base currency, the salary charts also get their real values.
    """
    today = dt.date.today()
    year, this_month = str(today.year), today.strftime("%Y-%m")
    this_year = [m for m in work.months if m.month[:4] == year and not m.is_gap]
    gross, net = sum(m.gross for m in this_year), sum(m.net for m in this_year)
    deducted = {n: sum(m.deductions.get(n, 0.0) for m in this_year) for n in work.deductions}
    last_index = len(work.months) - 1
    months = [
        {
            "month": m,
            "index": i,
            "fingerprint": work_fingerprint(row),
            "deletable": i in {0, last_index},
            "unpaid": not m.is_gap and m.month < this_month and m.payment_date is None,
        }
        for i, (m, (_, row)) in enumerate(zip(work.months, work.rows.iterrows()))
    ][::-1]
    return {
        "companies": list(work.companies.values()),
        "current": work.current,
        "months": months,
        "unpaid": [r["month"].month for r in months if r["unpaid"]],
        "year": year,
        "gross": gross,
        "deducted": gross - net,
        "net": net,
        "deductions": [
            {"name": n, "amount": a, "share": a / gross if gross else 0.0}
            for n, a in sorted(deducted.items(), key=lambda d: -d[1]) if a
        ],
        "form": form or work_form(work),
        **salary_charts(work, cpi),
    }


def salary_charts(work: Work, cpi: Series | None = None) -> dict:
    """
    The gross and net salary of every month, with the months without a job at 0,
    and their real values at the CPI of the end of each month (see with_real).
    """
    def chart(key: str) -> dict | None:
        series = [
            {"date": f"{m.month}-01", "month": m, "value": getattr(m, key) or 0.0}
            for m in work.months
        ]
        real_date = with_real(series, cpi, [month_end(m.month) for m in work.months])
        drawn = line_chart(series)
        if drawn:
            drawn["real_date"] = real_date
        return drawn

    return {
        "gross_chart": chart("gross"),
        "net_chart": chart("net"),
    }


def build_work_loan(snapshot: Snapshot) -> dict:
    """
    What the companies of the work history owe us in Wo, their debt account (a loan
    account): the salaries, net of their deductions, minus what they paid. Each company
    with what it owes now and what it paid in all, and the movements of Wo but the
    generated salaries and deductions (those are the months of the Work tab), newest
    first, each with what its company owes right after it; the debt movements and their
    differences can be deleted.
    """
    accounts, movements = kind_movements(snapshot, CreditorKind.COMPANY)
    companies = net_creditors(snapshot, accounts)[0]
    paid = dict.fromkeys(companies, 0.0)
    for m in movements:
        if m["base"] == LOAN_COLLECTED and m["who"] in paid:
            paid[m["who"]] += m["amount"]
    unassigned = sum(a.unassigned for a in accounts)
    return {
        "loan_account": accounts[0].name if accounts else "",
        "companies": [
            {"name": n, "owed": -c.amount, "paid": paid[n]} for n, c in companies.items()
        ],
        "owed": -sum(c.amount for c in companies.values()),
        "unassigned": -unassigned,
        "has_unassigned": not rounds_to_zero(unassigned),
        "movements": [m for m in movements if not m["salary"]],
    }


def collection_form(snapshot: Snapshot, company: str = "", values: dict | None = None) -> dict:
    """
    The state of the Salary received form of the Work tab: a LOAN_COLLECTED crediting Wo,
    from a company (the current job's by default) into an account in the base currency
    (not a debt account), its amount what that company owes now, if anything.
    """
    work = snapshot.work
    names = list(work.companies)
    if company not in names:
        company = work.current.company if work.current and work.current.company in names else next(iter(names), "")
    base = snapshot.debt_base_movements().get(LOAN_COLLECTED)
    loan = snapshot.base_debt_account(DebtRole.DEBTOR, CreditorKind.COMPANY)
    options = [
        a.name for a in (snapshot.side_accounts(base, LedgerSide.DEBIT) if base is not None else [])
        if a.currency.is_base_currency and not isinstance(a, DebtAccount)
    ]
    values = dict(values or {})
    values.setdefault("move_date", dt.date.today().isoformat())
    if "amount" not in values:
        owed = 0.0
        if loan is not None:
            owed = -next((a.creditors[company].amount for a in snapshot.debt_accounts(kind=CreditorKind.COMPANY)
                          if company in a.creditors), 0.0)
        values["amount"] = f"{owed:,.2f}" if owed > 0.005 else ""
    return {
        "companies": names,
        "company": company,
        "base_movement": LOAN_COLLECTED if base is not None else "",
        "loan_account": loan.name if loan is not None else "",
        "groups": by_group(snapshot, options),
        "values": values,
    }
