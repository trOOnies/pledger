import pandas as pd
import pickle
import yaml
from pledger import Account, MovementType
from commands import Option, InputQuestion
from commands import see, info, close
from utils import print_sep_text

with open("config.yaml", 'r') as f:
    try:
        cfg = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        print(exc)

# Standard definitions
try:
    cfg['accounts'] = pickle.load(cfg['pkl_folder'] + cfg['accounts_pkl'])
except:
    cfg['accounts'] = {}
    for _, row in pd.read_csv(cfg['ref_csv_folder'] + 'ref_accounts.csv').iterrows():
        cfg['accounts'][row['acc_id']] = Account(
            id=row['acc_id'],
            name=row['name'],
            full_name=row['full_name'],
            acc_type=row['acc_type'],
            description=row['description'],
        )
try:
    cfg['movement_types'] = pickle.load(cfg['pkl_folder'] + cfg['movement_types_pkl'])
except:
    cfg['movement_types'] = {}
    for _, row in pd.read_csv(cfg['ref_csv_folder'] + 'ref_movement_types.csv').iterrows():
        cfg['movement_types'][row['mov_type_id']] = MovementType(
            id=row['mov_type_id'],
            description=row['description'],
            result_type=row['result_type'],
            deb=cfg['accounts'].get(row['deb_id']),
            cred=cfg['accounts'].get(row['cred_id']),
        )

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
