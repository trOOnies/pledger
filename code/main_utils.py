import pandas as pd
from commands import close

INITIAL_IDS = {
    "G": 2000,
    "L": 2001
}


def process_accounts(cfg: dict, path: str) -> pd.DataFrame:
    cols = ['mov_id', 'uot', 'mov_type_id', 'deb_id', 'cred_id', 'amount']
    df = pd.DataFrame(columns=cols)
    print('Please input the initial values of the available accounts:')

    i = 0
    for acc in cfg["acc"].values():
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
                    values += [INITIAL_IDS["G"], acc.id, cfg['eq_acc_id']]
                else:
                    values += [INITIAL_IDS["L"], cfg['eq_acc_id'], acc.id]
                values.append(ans)

                # Append row
                df = df.append(dict(zip(cols, values)), ignore_index=True)
                i += 1
            except Exception:
                print("- [ERROR] Input must be numeric.")

    df = df.astype(
        dict(zip(cols, (len(cols)-1)*[int] + [float]))
    )
    print(df)

    acc_init = df[["mov_type_id", "deb_id", "cred_id", "amount"]].copy()
    acc_init["acc_id"] = acc_init.apply(
        lambda row: row.deb_id
        if row.mov_type_id == INITIAL_IDS["G"]
        else row.cred_id,
        axis=1
    )
    acc_init = acc_init[["acc_id", "amount"]].rename({"acc_id": "id"}, axis=1).astype({"id": int})
    acc_init.to_csv(path, index=False)

    return df


def check_data(path: str):
    # Check not empty
    # TODO
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
            pass  # TODO


def check_asserts(cfg) -> None:
    # There can only be 1 Equity Account, and it must be the id eq_acc_id
    assert cfg["acc"][cfg['eq_acc_id']].acc_type == 'E'
    assert all(
        acc.acc_type != 'E' or acc.id == cfg['eq_acc_id']
        for acc in cfg["acc"].values()
    )

    # The Initial G & L movements must be id 2000 and 2001 respectively
    assert [cfg["mov_types"][id].name for id in INITIAL_IDS.values()] == ['Initial (G)', 'Initial (L)']
    assert [cfg["mov_types"][id].result_type for id in INITIAL_IDS.values()] == ['G', 'L']
    assert cfg["mov_types"][INITIAL_IDS["G"]].deb is None
    assert cfg["mov_types"][INITIAL_IDS["G"]].cred.id == cfg['eq_acc_id']
    assert cfg["mov_types"][INITIAL_IDS["L"]].deb.id == cfg['eq_acc_id']
    assert cfg["mov_types"][INITIAL_IDS["L"]].cred is None
