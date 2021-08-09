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
        self.description=description
    
    def __str__(self):
        s = f"{self.full_name} ({self.name})\n"
        s += f"Acc. ID: {self.id}\n"
        s += f"Account type: {acc_type_dict[self.acc_type]}\n"
        s += self.description
        return s


class MovementType:
    def __init__(
        self,
        id,
        description,
        result_type,
        deb=None,
        cred=None,
    ) -> None:
        self.id = id
        self.description = description
        assert result_type in result_type_dict.keys()
        self.result_type = result_type
        self.deb = deb
        self.cred = cred
        if result_type == 'G':
            assert self.cred is not None and self.cred.acc_type == 'E'
        elif result_type == 'L':
            assert self.deb is not None and self.deb.acc_type == 'E'
        if result_type in ['G', 'T'] and self.deb is not None:
            assert self.deb.acc_type != 'E'
        if result_type in ['L', 'T'] and self.cred is not None:
            assert self.cred.acc_type != 'E'

    def __str__(self):
        s = f"{self.id} ({result_type_dict[self.result_type]})\n"
        for i, aux in enumerate([self.deb, self.cred]):
            if aux is None:
                s += 'FREE'
            else:
                s += f"{aux.id} ({acc_type_dict[aux.acc_type]})"
            if i == 0:
                s += ' | '
        s += '\n'
        s += self.description
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
