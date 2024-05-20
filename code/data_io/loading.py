import pickle
import pandas as pd
from pledger import Account, MovementType


def load_accounts(cfg: dict) -> None:
    cfg["dfs"] = {}
    try:
        cfg["acc"] = pickle.load(cfg['pkl_folder'] + cfg['accounts_pkl'])
    except Exception:
        path = cfg["folders"]["ref_csv"] + 'ref_accounts.csv'
        cfg["acc"] = {}
        for _, row in pd.read_csv(path).iterrows():
            cfg["acc"][row['id']] = Account(
                id=row['id'],
                name=row['name'],
                full_name=row['full_name'],
                acc_type=row['acc_type'],
                description=row['description'],
            )
        cfg["dfs"]["acc"] = pd.read_csv(path, index_col='id')
    # Fill empty and keep eq_acc
    cfg["dfs"]["acc"]['description'] = cfg["dfs"]["acc"]['description'].fillna('-')


def load_movement_types(
    cfg: dict,
    eq_acc: Account,
) -> None:
    try:
        cfg["mov_types"] = pickle.load(cfg['pkl_folder'] + cfg['movement_types_pkl'])
    except Exception:
        path = cfg["folders"]["ref_csv"] + 'ref_movement_types.csv'

        cfg["mov_types"] = {}
        for _, row in pd.read_csv(path).iterrows():
            cfg["mov_types"][row['id']] = MovementType(
                id=row['id'],
                name=row['name'],
                result_type=row['result_type'],
                eq_acc=eq_acc,
                deb=cfg["acc"].get(row['deb_id']),
                cred=cfg["acc"].get(row['cred_id']),
            )
        cfg["dfs"]["mov_types"] = pd.read_csv(path, index_col='id')

        def fill_eq(row, col, result_type):
            if row['result_type'] == result_type:
                return cfg['eq_acc_id']
            else:
                return row[col]

        cfg["dfs"]["mov_types"]['cred_id'] = cfg["dfs"]["mov_types"].apply(fill_eq, args=('cred_id', 'G',), axis=1)
        cfg["dfs"]["mov_types"]['deb_id'] = cfg["dfs"]["mov_types"].apply(fill_eq, args=('deb_id', 'L',), axis=1)

        # Joins
        cfg["dfs"]["mov_types"]['deb'] = cfg["dfs"]["mov_types"]['deb_id'].map(cfg["dfs"]["acc"].name)
        cfg["dfs"]["mov_types"]['cred'] = cfg["dfs"]["mov_types"]['cred_id'].map(cfg["dfs"]["acc"].name)
        # Fill empty
        cfg["dfs"]["mov_types"]['deb'] = cfg["dfs"]["mov_types"]['deb'].fillna('-')
        cfg["dfs"]["mov_types"]['cred'] = cfg["dfs"]["mov_types"]['cred'].fillna('-')
