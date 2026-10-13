"""main.py"""

from models import Account, Ledger, MovementBase


def main() -> None:
    accounts = Account.load_yaml("info/accounts.yaml")
    base_movements = MovementBase.load_yaml("info/base_movements.yaml", accounts)

    ledger = Ledger.from_csv("info/ledger.csv", accounts, base_movements)
    ledger.print_data()


if __name__ == "__main__":
    main()
