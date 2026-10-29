"""yaml_edit.py: the fields of the accounts, and editing the YAMLs in place."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

import yaml

from pledger.enums import AccountCheck
from pledger.web.store.common import _check_description, _parse_amount

if TYPE_CHECKING:
    from pledger.web.store.snapshot import Snapshot


def _account_fields(
    snapshot: Snapshot,
    description: str,
    initial_amount: str | float,
    currency: str,
    is_liquid: bool,
    initial_date: str,
    checks: Sequence[str],
) -> dict:
    """
    The fields of an account in the accounts YAML, validated: without a currency, it is
    in the base currency; without an initial date, or with the Monday of the initial
    week (see Snapshot.initial_week), it has none (it starts on that one). Its `checks` are AccountCheck values.
    """
    description = _check_description(description, "account")
    currency = currency.strip() or snapshot.base_currency.name
    if currency not in snapshot.currencies:
        raise ValueError(f"Unknown currency: {currency!r}")
    if not str(initial_amount).strip():
        raise ValueError("The initial amount is required")
    amount = _parse_amount(initial_amount, "initial amount")
    fields = {
        "description": description,
        "currency": currency,
        "is_liquid": bool(is_liquid),
        "initial_amount": amount,
    }
    if initial_date.strip():
        try:
            date = dt.date.fromisoformat(initial_date.strip())
        except ValueError:
            raise ValueError(f"Invalid initial date: {initial_date!r}") from None
        week = snapshot.initial_week
        if week is not None and date != week.start and date <= week.end:
            raise ValueError(
                f"The initial date must be after the initial week ({week.start} to {week.end}), "
                "or left empty to start on it"
            )
        if week is None or date != week.start:
            fields["initial_date"] = date
    try:
        checks = [AccountCheck(c) for c in checks]
    except ValueError:
        raise ValueError(f"Invalid checks: {list(checks)}") from None
    if len(set(checks)) != len(checks):
        raise ValueError(f"Repeated checks: {[c.value for c in checks]}")
    if checks:
        fields["checks"] = [c.value for c in checks]
    return fields


def _replace_yaml_entry(text: str, path: tuple[str, ...], update: Callable[[dict], dict]) -> str:
    """
    Return `text` with the mapping at `path`, a path of keys from the top level (e.g.
    `("asset", "Bank")`), replaced by `update` of it, as text, so the rest of the file
    is left as is. The comments within the entry are lost.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)

    def is_content(line: str) -> bool:
        return bool(line.strip()) and not line.lstrip().startswith("#")

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    def key_of(line: str):
        try:
            parsed = yaml.safe_load(line.strip())
        except yaml.YAMLError:
            return None
        return next(iter(parsed)) if isinstance(parsed, dict) and len(parsed) == 1 else None

    head, indent, lo, hi = None, 0, 0, len(lines)
    for key in path:
        children = [i for i in range(lo, hi) if is_content(lines[i])]
        if not children:
            raise ValueError(f"{key!r} not found; edit the file by hand")
        indent = indent_of(lines[children[0]])
        head = next((i for i in children if indent_of(lines[i]) == indent and key_of(lines[i]) == key), None)
        if head is None:
            raise ValueError(f"{key!r} not found; edit the file by hand")
        lo = head + 1
        hi = next((i for i in range(lo, hi) if is_content(lines[i]) and indent_of(lines[i]) <= indent), hi)

    body = [i for i in range(head, hi) if is_content(lines[i])]
    end = body[-1] + 1
    entry = yaml.safe_load("\n".join(line[indent:] for line in lines[head:end]))[path[-1]]
    if not isinstance(entry, dict):
        raise ValueError(f"{path[-1]!r} is not a mapping; edit the file by hand")  # noqa: TRY004 (a bad config value)
    step = indent_of(lines[body[1]]) - indent if len(body) > 1 else 2
    lines[head:end] = _yaml_entry(path[-1], update(entry), indent, step)
    return newline.join(lines)


def _insert_yaml_entry(text: str, section: tuple[str, ...], name: str, fields: dict) -> str:
    """
    Return `text` with `name: fields` appended to the mapping at `section`, a path
    of keys from the top level (e.g. `("result_types", "Op", "base_movements", "loss")`).
    Missing keys along the path are created at the end of their parent.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)

    def is_content(line: str) -> bool:
        return bool(line.strip()) and not line.lstrip().startswith("#")

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip())

    # The block of the current key: its header line, indent and child lines [lo, hi).
    head, head_indent, lo, hi = None, -1, 0, len(lines)
    step = None  # indentation step, taken from the file
    for depth, key in enumerate(section):
        children = [i for i in range(lo, hi) if is_content(lines[i])]
        child_indent = indent_of(lines[children[0]]) if children else None
        if child_indent is not None and head is not None:
            step = step or child_indent - head_indent
        if child_indent is None:
            child_indent = head_indent + (step or 2) if head is not None else 0

        # An empty section (null, ~ or {}) is a header too, emptied of its value
        header = re.compile(rf"^( {{{child_indent}}}{re.escape(key)}:)\s*(?:null|~|\{{\}})?\s*(#.*)?$")
        found = next((i for i in children if header.match(lines[i])), None)
        if found is not None:
            match = header.match(lines[found])
            lines[found] = match[1] + (f" {match[2]}" if match[2] else "")
        if found is None:
            if any(lines[i].lstrip().startswith(f"{key}:") and indent_of(lines[i]) == child_indent
                   for i in children):
                raise ValueError(f"Cannot add to the {key!r} section; edit the file by hand")
            # Append the missing key (and the rest of the path) after the block.
            at = (children[-1] + 1) if children else lo
            if head is None:
                while lines and not lines[-1].strip():
                    lines.pop()
                at = len(lines)
            unit = step or 2
            new = [
                " " * (child_indent + unit * j) + f"{k}:"
                for j, k in enumerate(section[depth:])
            ]
            entry_indent = child_indent + unit * len(new)
            new += _yaml_entry(name, fields, entry_indent, unit)
            lines[at:at] = new + ([""] if head is None else [])
            return newline.join(lines)

        head, head_indent = found, child_indent
        lo = found + 1
        hi = next(
            (i for i in range(lo, hi) if is_content(lines[i]) and indent_of(lines[i]) <= head_indent),
            hi,
        )

    # Insert after the last content line of the section.
    children = [i for i in range(lo, hi) if is_content(lines[i])]
    if children:
        entry_indent = indent_of(lines[children[0]])
        step = step or entry_indent - head_indent
        at = children[-1] + 1
    else:
        step = step or 2
        entry_indent = head_indent + step
        at = head + 1
    lines[at:at] = _yaml_entry(name, fields, entry_indent, step)
    return newline.join(lines)


def _yaml_entry(name: str, fields: dict, indent: int, step: int) -> list[str]:
    dumped = yaml.safe_dump(
        {name: fields}, sort_keys=False, allow_unicode=True,
        default_flow_style=False, indent=step, width=10**9,
    )
    return [" " * indent + line for line in dumped.splitlines()]
