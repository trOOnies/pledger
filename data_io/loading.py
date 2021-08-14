import pandas as pd
import pickle
from pledger import Account, MovementType

def load_accounts(
    cfg: dict,
):
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
        cfg['df_accounts'] = pd.read_csv(
            cfg['ref_csv_folder'] + 'ref_accounts.csv',
            index_col='acc_id',
        )
    # Fill empty and keep eq_acc
    cfg['df_accounts']['description'] = cfg['df_accounts']['description'].fillna('-')


def load_movement_types(
    cfg: dict,
    eq_acc: Account,
):
    try:
        cfg['movement_types'] = pickle.load(cfg['pkl_folder'] + cfg['movement_types_pkl'])
    except:
        cfg['movement_types'] = {}
        for _, row in pd.read_csv(cfg['ref_csv_folder'] + 'ref_movement_types.csv').iterrows():
            cfg['movement_types'][row['mov_type_id']] = MovementType(
                id=row['mov_type_id'],
                name=row['name'],
                result_type=row['result_type'],
                eq_acc=eq_acc,
                deb=cfg['accounts'].get(row['deb_id']),
                cred=cfg['accounts'].get(row['cred_id']),
            )
        cfg['df_movement_types'] = pd.read_csv(
            cfg['ref_csv_folder'] + 'ref_movement_types.csv',
            index_col='mov_type_id',
        )
        def fill_eq(row, col, result_type):
            if row['result_type'] == result_type:
                return cfg['eq_acc_id']
            else:
                return row[col]
        cfg['df_movement_types']['cred_id'] = cfg['df_movement_types'].apply(fill_eq, args=('cred_id','G',), axis=1)
        cfg['df_movement_types']['deb_id'] = cfg['df_movement_types'].apply(fill_eq, args=('deb_id','L',), axis=1)

        # Joins
        cfg['df_movement_types']['deb'] = cfg['df_movement_types']['deb_id'].map(cfg['df_accounts'].name)
        cfg['df_movement_types']['cred'] = cfg['df_movement_types']['cred_id'].map(cfg['df_accounts'].name)
        # Fill empty
        cfg['df_movement_types']['deb'] = cfg['df_movement_types']['deb'].fillna('-')
        cfg['df_movement_types']['cred'] = cfg['df_movement_types']['cred'].fillna('-')
