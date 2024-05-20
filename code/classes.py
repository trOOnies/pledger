from typing import Literal, Dict, Callable  # NewType

AccountType = Literal["A", "L", "E"]
AccountTypeFull = Literal["Asset", "Liability", "Equity"]
AccountTypeDict = Dict[AccountType, AccountTypeFull]

ResultType = Literal["G", "L", "T"]
ResultTypeFull = Literal["Gain", "Loss", "Transactional"]
ResultTypeDict = Dict[ResultType, ResultTypeFull]

OptionAction = Callable[[str, dict], bool]
