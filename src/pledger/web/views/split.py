"""split.py: the Split tab: splitting the tab of a get-together, and who pays whom to settle it. Nothing is saved."""

from __future__ import annotations

from pledger.web.store.common import _parse_number

YOU = "You"  # the user, always the first one of a get-together
MAX_OTHERS = 7  # besides the user


def _cents(value: str | float, what: str) -> int:
    """A non-negative amount in whole cents, from a number or text like '1,234.50'."""
    amount = _parse_number(value, what)
    if amount < 0:
        raise ValueError(f"The {what} cannot be negative")
    return round(amount * 100)


def _allocate(cents: int, weights: list[float]) -> list[int]:
    """
    `cents` split in proportion to `weights` (not all 0), in whole cents that add up to it: the
    cents left by rounding down go to the largest remainders, the earliest first.
    """
    total = sum(weights)
    exact = [cents * w / total for w in weights]
    shares = [int(e) for e in exact]
    by_remainder = sorted(range(len(weights)), key=lambda i: (-(exact[i] - shares[i]), i))
    for i in by_remainder[: cents - sum(shares)]:
        shares[i] += 1
    return shares


def _people(names: list[str]) -> list[str]:
    """The user and the others of a get-together, validated: 1 to MAX_OTHERS others, each named once."""
    people = [YOU]
    for i, name in enumerate(names, start=1):
        name = name.strip()
        if not name:
            raise ValueError(f"Person {i} needs a name")
        if name.casefold() in {p.casefold() for p in people}:
            raise ValueError(f"{name!r} is twice" if name.casefold() != YOU.casefold() else f"{YOU!r} is you already")
        people.append(name)
    if len(people) < 2:
        raise ValueError("Add at least one other person")
    if len(people) > MAX_OTHERS + 1:
        raise ValueError(f"At most {MAX_OTHERS} other people")
    return people


def split(names: list[str], items: list[dict], paid: list[str | float], percentages: bool = False) -> dict:
    """
    The split of the tab of a get-together of the user and the other people `names`: each item
    (`name`, `price` and `shares`, one per person in order) is fronted by the people with a share,
    evenly (a share is whether they front it), or with `percentages` in those percentages, which
    must add up to 100. `paid` is what each one actually put into the tab, which must add up to
    the total of the items. Empty items (no name, price nor share) are left out.

    Returns the people with what each one fronts (`owes`), `paid` and `balance` (positive: they
    get it back), in cents, and the `transfers` (`from`, `to`, `cents`) that settle them: the
    one who owes the most pays the one owed the most, until everyone is even, so there are at
    most one fewer transfers than people.
    """
    people = _people(names)
    n = len(people)
    owes = [0] * n
    rows = []
    for i, item in enumerate(items, start=1):
        name, price, shares = str(item.get("name", "")).strip(), str(item.get("price", "")).strip(), item.get("shares", [])
        shares = [*shares, *[""] * (n - len(shares))][:n]
        if not name and not price and not any(str(s).strip() for s in shares):
            continue
        label = repr(name) if name else f"item {i}"
        if not name:
            raise ValueError(f"The name of {label} is required")
        if not price:
            raise ValueError(f"The price of {label} is required")
        cents = _cents(price, f"price of {label}")
        if percentages:
            weights = [_parse_number(s, f"percentage of {label}") if str(s).strip() else 0.0 for s in shares]
            if any(w < 0 for w in weights):
                raise ValueError(f"The percentages of {label} cannot be negative")
            if not any(weights):
                raise ValueError(f"Give a percentage of {label} to someone")
            if abs(sum(weights) - 100) > 0.005:
                raise ValueError(f"The percentages of {label} add up to {sum(weights):g}%, not 100%")
        else:
            weights = [1.0 if s else 0.0 for s in shares]
            if not any(weights):
                raise ValueError(f"Check who fronts {label}")
        allocated = _allocate(cents, weights) if cents else [0] * n
        owes = [o + a for o, a in zip(owes, allocated)]
        rows.append({"name": name, "cents": cents, "shares": allocated, "base": str(item.get("base", "")).strip()})
    if not rows:
        raise ValueError("Add at least one item")
    put = [_cents(p, f"amount {people[i]} put in") if str(p).strip() else 0 for i, p in enumerate([*paid, *[""] * n][:n])]
    total = sum(r["cents"] for r in rows)
    if sum(put) != total:
        raise ValueError(
            f"The tab is {total / 100:,.2f}, but everyone put in {sum(put) / 100:,.2f}: "
            f"{'add' if sum(put) < total else 'take off'} {abs(total - sum(put)) / 100:,.2f}"
        )
    balance = [p - o for p, o in zip(put, owes)]
    return {
        "people": [
            {"name": name, "owes": o, "paid": p, "balance": b} for name, o, p, b in zip(people, owes, put, balance)
        ],
        "items": rows,
        "total": total,
        "transfers": settle(people, balance),
    }


def settle(people: list[str], balance: list[int]) -> list[dict]:
    """
    The transfers that even out these balances in cents (adding up to 0; positive: they get it
    back): the one who owes the most pays the one owed the most as much as they can, and so on.
    """
    left = list(balance)
    transfers = []
    while any(left):
        # The earliest of the people owing (and owed) the most, the user first
        debtor = min(range(len(left)), key=lambda i: (left[i], i))
        creditor = min(range(len(left)), key=lambda i: (-left[i], i))
        cents = min(-left[debtor], left[creditor])
        transfers.append({"from": people[debtor], "to": people[creditor], "cents": cents})
        left[debtor] += cents
        left[creditor] -= cents
    return transfers


def settling_rows(result: dict) -> list[dict]:
    """
    The transfers of a split that involve the user, to settle in Step 2: `who` the other one,
    `owes_you` whether they pay the user (else the user pays them) and `cents`.
    """
    return [
        {"who": t["to"] if t["from"] == YOU else t["from"], "owes_you": t["to"] == YOU, "cents": t["cents"]}
        for t in result["transfers"] if YOU in (t["from"], t["to"])
    ]
