import re
import pandas as pd
from utils import tail_print
from utils import acct_format

class Option():
    """Question option"""
    def __init__(
        self,
        name: str,
        desc: str,
        action,
    ) -> None:
        self.name = name
        self.desc = desc
        self.action = action
    
    def do_action(self, ans, cfg):
        return self.action(ans, cfg)

class InputQuestion:
    """Questions iterable"""
    def __init__(
        self,
        question: str,
        options: list,
        cfg: dict,
    ) -> None:
        self.question = question
        self.options = options
        self.cfg = cfg

    def ask(self):
        go_on = True
        while go_on:
            print(self.question)
            for o in self.options:
                print(f"- {o.name}: {o.desc}")
            ans = input()
            for o in self.options:
                if ans.startswith(o.name):
                    go_on = o.do_action(ans, self.cfg)
                    print()
                    break
        print('CODE STOPPED SUCCESSFULLY.')

# ---------------------------------------------------------------------------------------------------

# COMMMANDS

def see(ans, cfg):
    csv_path = cfg['csv_folder'] + cfg['raw_data_csv']

    if ans == 'see':
        tail_print(csv_path, 10, index_col='mov_id')
    elif re.match("see (last )*[0-9]", ans):
        pieces = ans.split(' ')
        if len(pieces) == 3:
            del pieces[1]
        nrows = int(pieces[1])
        del pieces

        if nrows > 0:
            tail_print(csv_path, nrows, index_col='mov_id')
        else:
            print('INPUT ERROR: nrows must be positive')
    elif re.match("see first [0-9]", ans):
        nrows = int(ans[ans.rfind(' ') + 1:])

        if nrows > 0:
            print(pd.read_csv(csv_path, nrows=nrows, index_col='mov_id'))
        else:
            print('INPUT ERROR: nrows must be positive')
    elif ans == 'see all':
        print(pd.read_csv(csv_path))
    elif ans == 'see --help':
        print('- see: Quick tail N of ledger.')
        print('- see (last )[N]: Tail N of ledger.')
        print('- see first [N]: Head N of ledger.')
        print('- see all: Prints all movements. Not recommended for big ledgers.')
    else:
        print('INPUT ERROR. Use see --help if needed.')
    
    return True


def info(ans, cfg):
    if ans == 'info acc':
        print('Id', 'Acc', 'Balance')
        for acc in cfg['accounts'].values():
            print(acc.id, acc.name, acct_format(0))
    elif ans == 'info mt':
        print('Id', 'MT', 'Total')
        for acc in cfg['movement_types'].values():
            print(acc.id, acc.description, acct_format(0))
    elif ans == 'info --help':
        print('- info acc: Accounts.')
        print('- info mt: Movement types.')
    else:
        print('INPUT ERROR. Use info --help if needed.')
    
    return True


def close(ans, cfg):
    print('Closing Pledger...')
    return False


# ---------------------------------------------------------------------------------------------------
