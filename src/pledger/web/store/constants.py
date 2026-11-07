"""constants.py: the names of the internal base movements, and the columns and ids of the CSVs of movements."""

from __future__ import annotations

from pledger.enums import LedgerSide, MovementType
from pledger.models import DEBT_ACCOUNT, LOAN_ACCOUNT, PREVIOUS_MONTH

BOM = "﻿"


# Internal base movements recording the initial amount of the accounts. Their
# movements are generated from the accounts YAML, never stored in the CSV.
INITIAL_GAIN = "Initial (G)"


INITIAL_LOSS = "Initial (L)"


# Internal base movements of the result of holding accounts in other currencies:
# the change of their value in the base currency. Their movements are generated
# when loading (see _result_rows), never stored in the CSV.
RESULT_GAIN = "Foreign currency (G)"


RESULT_LOSS = "Foreign currency (L)"


# Base movement of the salary of the work history, and the column of its gross salary.
# Its movements are generated each week from the gross salary of its month (see
# _salary_rows), never stored in the CSV, like those of the deductions of the salary.
SALARY = "Work - Salary Main"


# Internal base movements of the change in the value of the investments: their capital
# gains and losses, generated from their prices (see _capital_rows), never stored.
CAPITAL_GAIN, CAPITAL_LOSS = "Capital gains", "Capital losses"


GENERATED = [INITIAL_GAIN, INITIAL_LOSS, RESULT_GAIN, RESULT_LOSS, SALARY, CAPITAL_GAIN, CAPITAL_LOSS]


# The id of each movement of the ledger: unique and unchanging. The rows of the CSVs of
# movements keep theirs in an ID_COLUMN, the prefix of their CSV and a number, given by
# pledger itself (see LedgerStore.freeze_ids); the generated movements get theirs from
# what they are generated from, so they never change either.
ID_COLUMN = "id"


LEDGER_ID, CONVERSION_ID, INVESTMENT_ID, INTEREST_ID = "L", "C", "I", "M"


INITIAL_ID, SALARY_ID, RESULT_ID, PRICE_ID, SERVICE_ID = "A", "W", "R", "P", "S"


# Columns of the ledger CSV.
LEDGER_CSV_COLUMNS = ["move_date", "base_movement", "acc_debit", "amount", "acc_credit", "comment"]


# Optional column of the ledger CSV, added with the first one: the number of monthly
# installments of a charge to a credit card (crediting it), 1 if empty. The card is charged
# the whole amount on its date all the same: the installments only spread what is paid
# of it, one a month from its date on (see views.paid_chart).
INSTALLMENTS_COLUMN = "installments"


MAX_INSTALLMENTS = 120


# Columns of the conversions CSV, of the movements between an account in the base
# currency and one in another currency: the amount in each currency. Its movements
# are added to the ledger with the amount in the base currency.
BASE_AMOUNT, OTHER_AMOUNT = "amount_base_currency", "amount_other_currency"


# The audits CSV: a row per account audited, of the audit of that id. The difference is the
# inputted balance minus the actual one, then, 0 if they match. A correction accepted adds
# its base movement, its amount in the base currency and the id of its movement.
AUDIT_COLUMNS = [
    "audit", "date", "account", "currency", "inputted", "actual", "difference", "correction", "amount", "correction_id",
]

# The plans of the Budget and Trip tabs. A budget holds from its month on, until a later one
# of its base movement. The amount of a goal is in the currency of its account, and that
# of a planned expense in its own one.
BUDGET_COLUMNS = ["month", "base_movement", "amount"]


GOAL_COLUMNS = ["name", "account", "target", "due"]


TRIP_COLUMNS = ["name", "start", "end", "base_movement"]


TRIP_ITEM_COLUMNS = ["id", "trip", "category", "description", "amount", "currency"]


CONVERSION_COLUMNS = ["move_date", "base_movement", "acc_debit", BASE_AMOUNT, "acc_credit", "comment", OTHER_AMOUNT]


# Optional column of the conversions CSV: the ids of the movements of other CSVs a conversion
# was added with, through the INTERMEDIARY account, which only balances with all of them.
# They are deleted together (see linked_ids).
LINKED_COLUMN = "linked"


# The base movement of the broker fees, and those that can add them from their form.
BROKER_FEES = "Broker fees"


FEE_BASES = {"Dividends & Interest", "Investment - Bought", "Investment - Sold"}


# The internal transfers of the conversions of every other currency: bringing it in
# (debiting an account in it) or taking it out (crediting one).
CONVERSION_BOUGHT, CONVERSION_SOLD = "Foreign currency - Bought", "Foreign currency - Sold"


CONVERSIONS = {CONVERSION_BOUGHT, CONVERSION_SOLD}


# A cross conversion, between two other currencies: a CONVERSION_SOLD of one into an
# account in the base currency, right followed by a CONVERSION_BOUGHT of the other out
# of it (see conversion_pairs).
CROSS_CONVERSION = f"{CONVERSION_SOLD} + {CONVERSION_BOUGHT}"


# The internal transfers of the debt accounts, added from the Debt tab, with that side
# fixed to one of them (DEBT_BASE_ACCOUNTS): we owe more or less (the debts, of the
# DEBT_ACCOUNT), or are owed more or less (the loans, of the LOAN_ACCOUNT).
DEBT_INCURRED, DEBT_PAID = "Debt - Incurred", "Debt - Paid"


LOAN_GIVEN, LOAN_COLLECTED = "Loan - Given", "Loan - Collected"


DEBT_BASES = {
    DEBT_INCURRED: LedgerSide.CREDIT, DEBT_PAID: LedgerSide.DEBIT,
    LOAN_GIVEN: LedgerSide.DEBIT, LOAN_COLLECTED: LedgerSide.CREDIT,
}


DEBT_BASE_ACCOUNTS = {
    DEBT_INCURRED: DEBT_ACCOUNT, DEBT_PAID: DEBT_ACCOUNT, LOAN_GIVEN: LOAN_ACCOUNT, LOAN_COLLECTED: LOAN_ACCOUNT,
}


# The internal transfers paying a credit card (debiting it) and getting back a surplus of
# it (a negative payment, crediting it), added from the Credit Cards tab, with that side
# taking any credit card account (CREDIT_CARD).
CARD_PAYMENT, CARD_SURPLUS = "Credit card - Payment", "Credit card - Surplus"


CARD_PAYMENT_BASES = {CARD_PAYMENT: LedgerSide.DEBIT, CARD_SURPLUS: LedgerSide.CREDIT}


# Internal result type of the investment results. Its base movements, and the
# internal transfers with an INVESTMENT side, are added and deleted from the
# Investments tab instead of the Ledger tab.
INVEST = "Invest"


# Investment base movements of the interest earned or paid on the money of a
# regular account, with no investment involved.
INTEREST_GAIN = "Interest on Money (G)"


INTEREST_LOSS = "Interest on Money (L)"


INTEREST = {INTEREST_GAIN, INTEREST_LOSS}


# The internal gain and loss of the corrections of the balance of an account to the one
# checked by hand, in favour of oneself or against, in the ledger CSV: added from the
# Audit tab and deleted from the Ledger one (see LedgerStore.correct_balance)
CORRECTION_GAIN = "Correction (G)"


CORRECTION_LOSS = "Correction (L)"


CORRECTIONS = {CORRECTION_GAIN: MovementType.GAIN, CORRECTION_LOSS: MovementType.LOSS}


# Internal base movements settling what remains with someone after a debt movement, in its
# debt account, against the equity account: added from the Debt tab with that movement (see
# LedgerStore.add_debt_movement), and deleted from there or the Ledger tab. A gain if what
# remained is what we owed them.
DIFFERENCE_GAIN, DIFFERENCE_LOSS = "Difference (G)", "Difference (L)"


DIFFERENCES = {DIFFERENCE_GAIN: MovementType.GAIN, DIFFERENCE_LOSS: MovementType.LOSS}


# Columns of the investments CSV, of the movements of the investment base movements
# but the interest on money: those of the ledger CSV, plus the investment (its name
# in the investment account) and its quantity, like 1,234.50.
INVESTMENT_COLUMN, QUANTITY_COLUMN = "investment", "quantity"


INVESTMENT_COLUMNS = [
    "move_date", "base_movement", "acc_debit", "amount", "acc_credit", QUANTITY_COLUMN, INVESTMENT_COLUMN, "comment",
]


# Columns of the interest CSV, of the interest on money of a regular account: earned
# (an INTEREST_GAIN movement debiting it) if its amount is positive, else paid (an
# INTEREST_LOSS one crediting it).
INTEREST_ACCOUNT = "acc_debit"


INTEREST_COLUMNS = ["move_date", INTEREST_ACCOUNT, "amount", "comment"]


# Investment base movements that change the quantity of the investment: it is
# required to buy, sell and split, and optional for capital repayments.
QUANTITY_REQUIRED = {"Investment - Bought", "Investment - Sold", "Stock split (More)", "Stock split (Less)"}


QUANTITY_OPTIONAL = {"Capital repayments"}


# Investment base movements whose amount over their quantity is a price of their
# investment: it is revalued at it right before them (see _capital_rows).
PRICED = {"Investment - Bought", "Investment - Sold"}


# Columns of the prices CSV, of the prices of the investments: the price of one unit of
# an investment of an investment account on a date, or its initial price with
# INITIAL_PRICE as its date (see Account.load_yaml).
PRICE_COLUMNS = ["date", "account", INVESTMENT_COLUMN, "price"]


INITIAL_PRICE = PREVIOUS_MONTH


# The order of the generated capital gains and losses among the movements of the
# investments CSV: the row they come right before, or empty at the end of their date.
BEFORE_COLUMN = "before"


# The services CSV has the monthly values of the services of the debt accounts: a row per
# month, and a column per service, named after it, with a base movement in the creditors
# YAML (see Creditor.base_movement). Each value is owed to the service as movements of that
# loss base movement crediting the service's debt account (see service_account), split between the weeks of the month
# (see _service_rows).
SERVICE_MONTH = "month"


# The service of each of their generated movements
SERVICE_COLUMN = "service"


# The hosts the app answers to by default: only the loopback ones, so that no other site can
# reach it through a name of its own (DNS rebinding). PLEDGER_ALLOWED_HOSTS overrides them.
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


# Columns of the work CSV; the received payment date one is optional. Any other column
# is a deduction of the gross salary (taxes, contributions...) on the way to the net
# one, named after its loss base movement.
MONTH, COMPANY, POSITION, GROSS, PAYMENT_DATE = "Month", "Company", "Position", SALARY, "Received payment date"


WORK_COLUMNS = [MONTH, COMPANY, POSITION, GROSS, PAYMENT_DATE]
