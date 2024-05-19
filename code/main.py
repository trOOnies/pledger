import os
import yaml
from main_utils import (
    check_data, check_asserts, prompt_new_ledger, process_accounts
)
from data_io.loading import load_accounts, load_movement_types
from commands import Option, InputQuestion, see, info, close
from utils import print_sep_text


def main() -> None:
    with open("config.yaml", 'r') as f:
        try:
            cfg = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            print(exc)
            raise

    load_accounts(cfg)
    eq_acc = cfg['accounts'][cfg['eq_acc_id']]
    print(cfg['accounts'])
    print('Accounts loaded.')

    load_movement_types(cfg, eq_acc)
    print('Movement Types loaded.')

    # Data
    path = cfg['csv_folder'] + cfg['raw_data_csv']
    if os.path.exists(path):
        check_data(path)
    else:
        prompt_new_ledger(cfg)
        check_asserts(cfg)

        df = process_accounts(cfg)
        df.to_csv(path, index=False)
        print(f"DataFrame saved in {cfg['raw_data_csv']}.")

    # ---------------------------------------------------------------------------------------------------

    see_opt = Option('see', 'See ledger options.', see)
    info_opt = Option('info', 'Get info on all Pledger objects.', info)
    save_opt = Option('save', 'Save ledger to raw CSV.', lambda x: 1)  # TODO
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


if __name__ == "__main__":
    main()
