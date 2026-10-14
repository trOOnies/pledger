"""main.py"""

from ledger import Ledger
from models import Account, MovementBase


def main() -> None:
    accounts = Account.load_yaml("info/accounts.yaml")
    base_movements = MovementBase.load_yaml("info/base_movements.yaml", accounts)

    ledger = Ledger.from_csv("info/ledger.csv", accounts, base_movements)
    ledger.print_data()
    ledger.print_results()


if __name__ == "__main__":
    main()
