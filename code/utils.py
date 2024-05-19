import csv
import pandas as pd
from collections import deque
from io import StringIO


def acct_format(val) -> str:
    return str(val).format(
        lambda f: '${:>10}'.format(
            ('({:,.0f})'if f < 0 else '{:,.0f}').format(f)
        )
    )


def print_sep_text(
    s: str,
    total: int = 100,
    sep: str = '-',
) -> None:
    slack = total - len(s)
    half_slack = int(slack / 2)
    print(half_slack * sep + s + ((len(s) + 1) % 2) * sep + half_slack * sep)


def tail_return(
    csv_path: str,
    nrows: int,
    index_col=None,
) -> pd.DataFrame:
    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        q = deque(f, nrows)
    return pd.read_csv(StringIO(''.join(q)), names=header, index_col=index_col)
