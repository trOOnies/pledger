import re
import pandas as pd
from utils import print_sep_text, tail_return
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
        df = tail_return(csv_path, 10, index_col='mov_id')
    elif re.match("see (last )*[0-9]+$", ans):
        pieces = ans.split(' ')
        if len(pieces) == 3:
            del pieces[1]
        nrows = int(pieces[1])
        del pieces

        if nrows > 0:
            df = tail_return(csv_path, nrows, index_col='mov_id')
        else:
            print('INPUT ERROR: nrows must be positive')
    elif re.match("see first [0-9]+$", ans):
        nrows = int(ans[ans.rfind(' ') + 1:])

        if nrows > 0:
            df = pd.read_csv(csv_path, nrows=nrows, index_col='mov_id')
        else:
            print('INPUT ERROR: nrows must be positive')
    elif ans == 'see all':
        df = pd.read_csv(csv_path)
    elif ans == 'see --help':
        print('- see: Quick tail N of ledger.')
        print('- see (last )[N]: Tail N of ledger.')
        print('- see first [N]: Head N of ledger.')
        print('- see all: Prints all movements. Not recommended for big ledgers.')
        return True
    else:
        print('INPUT ERROR. Use see --help if needed.')
        return True

    # Joins
    df['mov_type'] = df['mov_type_id'].map(cfg['df_movement_types'].name)
    df['deb'] = df['deb_id'].map(cfg['df_accounts'].name)
    df['cred'] = df['cred_id'].map(cfg['df_accounts'].name)
    print(df[[col for col in df.columns if not col.endswith('_id') and col != 'amount'] + ['amount']])
    return True


def info(ans, cfg):
    if ans == 'info --help':
        print('- info: Quick high level info of Accounts and Movement Types.')
        print('- info acc[=D]: Print Accounts high level info. You can also access a certain Account acc_id = D.')
        print('- info mt[=D]: Print Movement types high level info. You can also access a certain Movement Type mt_id = D.')
        return True
    if ans.startswith('info acc='):
        try:
            acc = cfg['accounts'].get(int(ans[ans.find('=')+1:]))
            assert acc is not None
            print(acc.__str__())
        except:
            print('ERROR: acc_id no reconocido.')
        return True
    if ans.startswith('info mt='):
        try:
            mt = cfg['movement_types'].get(int(ans[ans.find('=')+1:]))
            assert mt is not None
            print(mt.__str__())
        except:
            print('ERROR: acc_id no reconocido.')
        return True

    valid_input = False
    if ans == 'info' or ans == 'info acc':
        print_sep_text('', 50)
        print('ACCOUNTS')
        print(cfg['df_accounts'])
        valid_input = True
    if ans == 'info' or ans == 'info mt':
        print_sep_text('', 50)
        print('MOVEMENT TYPES')
        print(cfg['df_movement_types'][[col for col in cfg['df_movement_types'].columns if not col.endswith('_id')]])
        valid_input = True
    if valid_input:
        print_sep_text('', 50)
    else:
        print('INPUT ERROR. Use info --help if needed.')
    return True

def close(
    ans=None,
    cfg=None,
):
    print('Closing Pledger...')
    return False


# ---------------------------------------------------------------------------------------------------
