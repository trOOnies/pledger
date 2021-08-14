import pandas as pd
import yaml
import os
from data_io.loading import load_accounts, load_movement_types
from commands import Option, InputQuestion
from commands import see, info, close
from utils import print_sep_text

with open("config.yaml", 'r') as f:
    try:
        cfg = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(exc)

# Accounts
load_accounts(cfg)
eq_acc = cfg['accounts'][cfg['eq_acc_id']]
print(cfg['accounts'])
print('Accounts loaded.')

# Movement Types
load_movement_types(cfg, eq_acc)
print('Movement Types loaded.')

# Data
if os.path.exists(cfg['csv_folder'] + cfg['raw_data_csv']):
    # Check not empty
    with open(cfg['csv_folder'] + cfg['raw_data_csv']) as f:
        i = 0
        for line in f:
            i += 1
            if i >= 2:
                break
    if i < 2:
        raise ValueError('The file is empty.')

    # Check if data makes sense
    with open(cfg['csv_folder'] + cfg['raw_data_csv']) as f:
        for line in f:
            pass
else:
    print("The file doesn't exist.")
    go_on = True
    while go_on:
        ans = input(f"Do you want to start a new ledger at {cfg['raw_data_csv']}? (y/n)\n")
        if ans.lower() == 'y':
            go_on = False
        elif ans.lower() == 'n':
            close()
        else:
            print("Error: Input must be y/n.")
    
    # There can only be 1 Equity Account, and it must be the id eq_acc_id
    assert cfg['accounts'][cfg['eq_acc_id']].acc_type == 'E'
    assert all(acc.acc_type != 'E' or acc.id == cfg['eq_acc_id'] for acc in cfg['accounts'].values())
    # The Initial G & L movements must be id 2000 and 2001 respectively
    initial_ids = [2000, 2001]
    assert [cfg['movement_types'][id].description for id in initial_ids] == ['Initial (G)', 'Initial (L)']
    assert [cfg['movement_types'][id].result_type for id in initial_ids] == ['G', 'L']
    assert cfg['movement_types'][initial_ids[0]].deb is None
    assert cfg['movement_types'][initial_ids[0]].cred.id == cfg['eq_acc_id']
    assert cfg['movement_types'][initial_ids[1]].deb.id == cfg['eq_acc_id']
    assert cfg['movement_types'][initial_ids[1]].cred is None


    # All accounts
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
                    values += [initial_ids[0], acc.id, cfg['eq_acc_id']]
                else:
                    values += [initial_ids[1], cfg['eq_acc_id'], acc.id]
                values.append(ans)

                # Append row
                df2 = dict(zip(cols, values))
                df = df.append(df2, ignore_index=True)
                i += 1
            except:
                print("Error: Input must be numeric.")
    
    df = df.astype(dict(zip(cols, (len(cols)-1)*[int] + [float])))
    df.to_csv(cfg['csv_folder'] + cfg['raw_data_csv'], index=False)
    print(f"DataFrame saved in {cfg['raw_data_csv']}.")

# ---------------------------------------------------------------------------------------------------

see_opt = Option('see', 'See ledger options.', see)
info_opt = Option('info', 'Get info on all Pledger objects.', info)
save_opt = Option('save', 'Save ledger to raw CSV.', lambda x: 1)
close_opt = Option('close', 'Close program.', close)

print_sep_text('PLEDGER v0.0.0')

my_q = InputQuestion(
    'What would you like to do?',
    [
        see_opt,
        info_opt,
        save_opt,
        close_opt,
    ],
    cfg,
)

my_q.ask()
