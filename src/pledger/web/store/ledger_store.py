"""ledger_store.py: reading and writing the data files."""

from __future__ import annotations

from pledger.web.store.account_store import AccountStore
from pledger.web.store.conversion_store import ConversionStore
from pledger.web.store.estimate_store import EstimateStore
from pledger.web.store.investment_store import InvestmentStore
from pledger.web.store.linked_store import LinkedStore
from pledger.web.store.service_store import ServiceStore
from pledger.web.store.work_store import WorkStore


class LedgerStore(AccountStore, LinkedStore, InvestmentStore, ConversionStore, WorkStore, ServiceStore, EstimateStore):
    """Reads and writes the ledger files. The files are the source of truth. Each part of it is in its own class (see StoreBase)."""
