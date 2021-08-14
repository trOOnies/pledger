from numpy import isnan
from pandas._config.config import options
from utils import acct_format

acc_type_dict = {
    'A': 'Asset',
    'L': 'Liability',
    'E': 'Equity',
}
result_type_dict = {
    'G': 'Gain',
    'L': 'Loss',
    'T': 'Transactional',
}


class Account:
    def __init__(
        self,
        id,
        name,
        full_name,
        acc_type,
        description,
    ) -> None:
        self.id = id
        self.name = name
        self.full_name = full_name
        assert acc_type in acc_type_dict.keys()
        self.acc_type = acc_type
        description = str(description)
        if description == 'nan':
            self.description = None
        else:
            self.description = description
    
    def __str__(self):
        s = f"{self.name} [ID={self.id}]"
        s += f"\nFull name: {self.full_name}"
        s += f"\nAccount type: {acc_type_dict[self.acc_type]}"
        if self.description is not None:
            s += '\n' + self.description
        return s
    
    def __repr__(self):
        return f"Account('{self.name}')"


class MovementType:
    def __init__(
        self,
        id,
        name,
        result_type: str,
        eq_acc: int,
        deb=None,
        cred=None,
    ) -> None:
        assert result_type in result_type_dict.keys()
        assert (deb is None or eq_acc.id != deb.id) and (cred is None or eq_acc.id != cred.id)

        self.id = id
        self.name = name
        self.result_type = result_type
        self.deb = deb
        self.cred = cred

        # Equity is empty
        if result_type == 'G':
            assert cred is None
            self.cred = eq_acc
        elif result_type == 'L':
            assert deb is None
            self.deb = eq_acc

    def __str__(self):
        s = f"{self.name} [ID={self.id}]"
        s += f"\nResult Type: {result_type_dict[self.result_type]}"
        s += '\n'
        for i, aux in enumerate([self.deb, self.cred]):
            if aux is None:
                s += 'FREE'
            else:
                s += f"{aux.name} ({acc_type_dict[aux.acc_type]})"
            if i == 0:
                s += ' | '
        return s


class Movement:
    def __init__(
        self,
        am,
        mov_type: MovementType,
        deb=None,
        cred=None,
    ) -> None:
        self.am = am
        self.mov_type = mov_type
        if mov_type.deb is None:
            assert deb is not None and deb.acc_type != 'E'
        if mov_type.cred is None:
            assert cred is not None and cred.acc_type != 'E'
        self.deb = deb
        self.cred = cred
    
    def __str__(self):
        s = 'D\tAm\tC\n'
        s += f"{self.deb}\t{acct_format(self.am)}\t{self.cred}"
        return s

class Ledger:
    def __init__(self) -> None:
        self.movements = []

    def add_mov(self, mov: Movement):
        self.movements.append(mov)


class Index:
    def __init__(self) -> None:
        pass
