"""main.py"""

from pledger.ledger import Ledger
from pledger.models import Account, MovementBase


def main() -> None:
    accounts = Account.load_yaml("info/accounts.yaml")
    base_movements, _ = MovementBase.load_yaml("info/result_types.yaml", accounts)

    ledger = Ledger.from_csv("info/ledger.csv", accounts, base_movements)
    ledger.print_data()
    ledger.print_results()


if __name__ == "__main__":
    main()
