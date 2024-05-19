import pandas as pd
from commands import close

INITIAL_IDS = [2000, 2001]


def check_data(path: str):
    # Check not empty
    with open(path) as f:
        i = 0
        for line in f:
            i += 1
            if i >= 2:
                break
    if i < 2:
        raise ValueError('The file is empty.')

    # Check if data makes sense
    with open(path) as f:
        for line in f:
            pass


def prompt_new_ledger(cfg) -> None:
    print("The file doesn't exist.")
    go_on = True
    while go_on:
        ans = input(
            f"Do you want to start a new ledger at {cfg['raw_data_csv']}? (y/n)\n"
        )
        if ans.lower() == 'y':
            go_on = False
        elif ans.lower() == 'n':
            close()
        else:
            print("Error: Input must be y/n.")


def check_asserts(cfg) -> None:
    # There can only be 1 Equity Account, and it must be the id eq_acc_id
    assert cfg['accounts'][cfg['eq_acc_id']].acc_type == 'E'
    assert all(
        acc.acc_type != 'E' or acc.id == cfg['eq_acc_id']
        for acc in cfg['accounts'].values()
    )

    # The Initial G & L movements must be id 2000 and 2001 respectively
    assert [cfg['movement_types'][id].description for id in INITIAL_IDS] == ['Initial (G)', 'Initial (L)']
    assert [cfg['movement_types'][id].result_type for id in INITIAL_IDS] == ['G', 'L']
    assert cfg['movement_types'][INITIAL_IDS[0]].deb is None
    assert cfg['movement_types'][INITIAL_IDS[0]].cred.id == cfg['eq_acc_id']
    assert cfg['movement_types'][INITIAL_IDS[1]].deb.id == cfg['eq_acc_id']
    assert cfg['movement_types'][INITIAL_IDS[1]].cred is None


def process_accounts(cfg: dict) -> pd.DataFrame:
    cols = ['mov_id', 'uot', 'mov_type_id', 'deb_id', 'cred_id', 'amount']
    df = pd.DataFrame(columns=cols)
    print('Please input the initial values of the available accounts:')

    i = 0
    for acc in cfg['accounts'].values():
        if acc.acc_type == 'E':
            continue
        go_on = True
        while go_on:
            ans = input(f"{acc.full_name} ({acc.name}): ")
            try:
                ans = float(ans)
                go_on = False

                # New row
                values = [i, 0]
                if acc.acc_type == 'A':
                    values += [INITIAL_IDS[0], acc.id, cfg['eq_acc_id']]
                else:
                    values += [INITIAL_IDS[1], cfg['eq_acc_id'], acc.id]
                values.append(ans)

                # Append row
                df = df.append(dict(zip(cols, values)), ignore_index=True)
                i += 1
            except Exception:
                print("Error: Input must be numeric.")

    df = df.astype(
        dict(zip(cols, (len(cols)-1)*[int] + [float]))
    )
    return df
