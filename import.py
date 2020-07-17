import calendar
import csv
import dataclasses
import enum
import json
import os
import re
import sys
from collections import OrderedDict, defaultdict
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

import arrow
import dacite
import funcy
import requests
import toml

YNAB_TRANSACTION_FIELDS = [
    # Which account is this entry for?
    "Account",
    # Colored flags
    "Flag",
    # Ignored
    "Check Number",
    # Transaction date
    "Date",
    # Who did you pay / get money from / which account did you transfer money to?
    "Payee",
    # Concatenation of Master and Sub category
    "Category",
    # Category group
    "Master Category",
    # Actual category
    "Sub Category",
    # Notes
    "Memo",
    # Amount if positive
    "Outflow",
    # Amount if negative
    "Inflow",
    # Reconciled?
    "Cleared",
    # Ignored
    "Running Balance",
]

YNAB_BUDGET_FIELDS = [
    # Which month does this apply to? In format: MMMM YYYY
    "Month",
    # Concatenation of Master and Sub category
    "Category",
    # Category group
    "Master Category",
    # Actual category
    "Sub Category",
    # Amount budgets
    "Budgeted",
    # Total outflows in this budget-month - ignored for the purpose of import
    "Outflows",
    # Current balance in this budget - ignored for the purpose of import
    "Category Balance",
]


@dataclasses.dataclass(frozen=True)
class YNABTransaction:
    """
    Source data from YNAB register (refer to YNAB_TRANSACTION_FIELDS for docs)
    """

    account: str
    flag: str
    date: arrow.Arrow
    payee: str
    category: str
    master_category: str
    sub_category: str
    memo: str
    outflow: Decimal
    inflow: Decimal
    cleared: str
    running_balance: Decimal


@dataclasses.dataclass(frozen=True)
class YNABBudget:
    """
    Source data from YNAB budget (refer to YNAB_BUDGET_FIELDS for docs)
    """

    month: arrow.Arrow
    category: str
    master_category: str
    sub_category: str
    budgeted: Decimal
    outflows: Decimal
    category_balance: Decimal

    @property
    def is_hidden(self):
        return self.master_category == "Hidden Categories"

    @property
    def is_pre_ynab(self):
        return self.category.startswith("Pre-YNAB Debt")


@dataclasses.dataclass(frozen=True)
class Config:
    """
    Application configuration
    """

    @dataclasses.dataclass(frozen=True)
    class Account:
        class Role(enum.Enum):
            credit_card = "credit_card"
            default = "default"
            savings = "savings"
            cash = "cash"

        # Mapping of Account Name to foreign currency code (ISO 4217)
        # will use default currency if not specified
        currency: str = ""

        # Account roles (credit_card / cash / savings)
        role: Role = Role.default

        # Monthly bill payment date (if not specified will be inferred from transfer transactions)
        monthly_payment_date: str = ""

        # Mark account as inactive after import
        inactive: bool = False

    # All of your YNAB accounts - only need to add here if there's some customization to be done.
    accounts: Dict[str, Account] = dataclasses.field(default_factory=dict)

    # Mapping of currency conversion fallback if foreign amount can't be automatically determined
    currency_conv_fallback: Dict[str, Decimal] = dataclasses.field(default_factory=dict)

    # default currency
    currency: str = "USD"

    date_format: str = "MM/DD/YYYY"

    # Mapping for renaming payees
    payee_mapping: Dict[str, str] = dataclasses.field(default_factory=dict)

    # Mapping for concatenated budget names to budget name
    budget_mapping: Dict[str, str] = dataclasses.field(default_factory=dict)

    skip_budget_limits_import: bool = False

    # Which YNAB field to use for category?
    # Category: Everyday Expenses:Household
    # Master Category: Everyday Expenses
    # Sub Category: Household
    category_field: str = "Sub Category"

    # Which YNAB field to use for budget?
    # Category: Everyday Expenses:Household
    # Master Category: Everyday Expenses
    # Sub Category: Household
    budget_field: str = "Sub Category"

    # Should memo field be used to fill in description? It will be moved to Notes otherwise
    memo_to_description: bool = True

    # What value should be used if a description can't be determined automatically?
    # This is also the value that'll get used if `memo_to_description` is false.
    empty_description: str = "(empty description)"


@dataclasses.dataclass
class ImportData:
    """
    Processed data to be imported into Firefly
    """

    @dataclasses.dataclass(frozen=True)
    class Account:
        class Role(enum.Enum):
            credit_card = "ccAsset"
            default = "defaultAsset"
            savings = "savingAsset"
            cash = "cashWalletAsset"

        name: str
        opening_date: arrow.Arrow
        monthly_payment_date: Optional[arrow.Arrow] = None
        role: Role = Role.default
        opening_balance: Decimal = Decimal(0)
        currency_code: str = "USD"

    @dataclasses.dataclass(frozen=True)
    class TransactionGroup:
        @dataclasses.dataclass(frozen=True)
        class TransactionMetadata:
            date: arrow.Arrow
            amount: Decimal
            description: str
            notes: str
            tags: List[str]
            reconciled: bool
            # this field is used to de-duplicate transactions with the same amount
            external_id: str

        @dataclasses.dataclass(frozen=True)
        class Withdrawal(TransactionMetadata):
            account: str
            payee: str
            budget: str
            category: str

        @dataclasses.dataclass(frozen=True)
        class Deposit(TransactionMetadata):
            account: str
            payee: str
            budget: str
            category: str

        @dataclasses.dataclass(frozen=True)
        class Transfer(TransactionMetadata):
            from_account: str
            to_account: str
            # set if exactly one of: from, to is foreign currency account
            foreign_amount: Decimal = None

        title: str = ""
        transactions: List[Union[Withdrawal, Deposit, Transfer]] = dataclasses.field(default_factory=list)

    @dataclasses.dataclass(frozen=True)
    class Budget:
        name: str
        active: bool = True

    @dataclasses.dataclass(frozen=True)
    class BudgetHistory:
        name: str
        amount: Decimal
        start: arrow.Arrow
        end: arrow.Arrow

    asset_accounts: List[Account] = dataclasses.field(default_factory=list)
    revenue_accounts: List[str] = dataclasses.field(default_factory=list)
    expense_accounts: List[str] = dataclasses.field(default_factory=list)

    categories: Set[str] = dataclasses.field(default_factory=set)
    budgets: Dict[str, Budget] = dataclasses.field(default_factory=dict)
    budget_history: List[BudgetHistory] = dataclasses.field(default_factory=list)

    transaction_groups: List[TransactionGroup] = dataclasses.field(default_factory=list)

    # not used for imports - only for verification
    # map of month to map of account name to running balance at the *end* of that month
    running_balances: Dict[arrow.Arrow, Dict[str, Decimal]] = dataclasses.field(
        default_factory=lambda: defaultdict(dict)
    )


@dataclasses.dataclass
class FireflyData:
    """
    Data in firefly - mostly primary keys (ids) for various objects
    """

    currencies: Dict[str, int] = dataclasses.field(default_factory=dict)
    categories: Dict[str, int] = dataclasses.field(default_factory=dict)
    budgets: Dict[str, dict] = dataclasses.field(default_factory=dict)
    budget_limits: Dict[Tuple[str, arrow.Arrow, arrow.Arrow], dict] = dataclasses.field(default_factory=dict)
    available_budgets: Dict[Tuple[arrow.Arrow, arrow.Arrow], dict] = dataclasses.field(default_factory=dict)
    asset_accounts: Dict[str, dict] = dataclasses.field(default_factory=dict)
    revenue_accounts: Dict[str, dict] = dataclasses.field(default_factory=dict)
    expense_accounts: Dict[str, dict] = dataclasses.field(default_factory=dict)


def create_transaction():
    pass


AMOUNT_RE = re.compile(r"^[^0-9]*([0-9,\.]+)$")

DUPLICATE_TX_RE = re.compile(r"^Duplicate of transaction #([0-9]+)\.$")


def _to_amount(s: str) -> Decimal:
    m = AMOUNT_RE.match(s)
    assert m, f"Invalid value with no amount: |{s}|"
    return Decimal(m.group(1).replace(",", ""))


def _ynab_field_name(s: str) -> str:
    return s.lower().replace(" ", "_")


def _is_transfer(tx: YNABTransaction) -> bool:
    return "Transfer : " in tx.payee


def _transfer_account(tx: YNABTransaction) -> str:
    if " / Transfer : " in tx.payee:
        return tx.payee.split(" / ")[1].split(" : ")[1]
    return tx.payee.split(" : ")[1]


def _correct_transfer_to_from(tx: YNABTransaction) -> YNABTransaction:
    """
    Amount in Firefly needs to be +ve. For transfer transactions, this means tx.account always needs to be the
    account from which money goes out. Since YNAB transactions could have it the wrong way around, this method is
    used to correct such transactions.
    """
    assert _is_transfer(tx)

    account_from_payee = _transfer_account(tx)

    if tx.outflow > 0:
        return dataclasses.replace(tx, payee=account_from_payee)

    real_from_account = account_from_payee
    real_to_account = tx.account

    return dataclasses.replace(
        tx,
        account=real_from_account,
        payee=real_to_account,
        running_balance=Decimal(-1),
        outflow=tx.inflow,
        inflow=tx.outflow,
    )


def _firefly_compare(obj: dict, firefly_obj: dict) -> bool:
    if isinstance(obj, arrow.Arrow):
        return obj.format("YYYY-MM-DD") == firefly_obj
    elif isinstance(obj, Decimal) and firefly_obj is not None:
        return obj == Decimal(str(firefly_obj))
    elif firefly_obj is None:
        return obj == 0
    return obj == firefly_obj


def _firefly_needs_update(obj: dict, firefly_obj: dict) -> bool:
    needs_update = False
    for k, v in obj.items():
        if not _firefly_compare(v, firefly_obj["attributes"].get(k)):
            needs_update = True
            break
    return needs_update


def _firefly_create_transaction_errors(
    response: requests.Response,
) -> Tuple[Dict[int, int], Dict[int, Dict[str, List[str]]], Dict[str, List[str]]]:
    """
    Interpret firefly create transaction errors
    :return: Tuple of (duplicate_errors, other_tx_errors, other_errors)
        duplicate_errors is a dict of transaction index to duplicate transaction id
        other_tx_errors is a dict of transaction index to dict of field to list of errors
        other_errors is a dict of other fields to list of errors
    """

    dup_errors = {}
    other_tx_errors = defaultdict(dict)
    other_errors = {}
    # sample response:
    # b'{"message":"The given data was invalid.","errors":{"transactions.0.description":["Duplicate of transaction #7995."]}}'
    for field, field_errors in response.json()["errors"].items():
        split = field.split(".")
        field = split[0]
        if field != "transactions":
            other_errors[field] = field_errors
        else:
            tx_idx = int(split[1])
            child_field = split[2]
            if len(field_errors) == 1 and (m := DUPLICATE_TX_RE.match(field_errors[0])):
                assert tx_idx not in other_tx_errors
                dup_errors[tx_idx] = int(m.group(1))
            else:
                assert tx_idx not in dup_errors
                other_tx_errors[tx_idx][child_field] = field_errors
    return dup_errors, other_tx_errors, other_errors


class ProgressBar:
    """
    Call in a loop to create terminal progress bar
    """

    def __init__(
        self,
        total: int,
        prefix: str = "",
        suffix: str = "",
        decimals: int = 1,
        length: int = 100,
        fill: str = "█",
        print_end: str = "\r",
    ):
        """
        Create new progress bar. Once created, simply call .print() once every iteration.

        :param total: total iterations
        :param prefix: prefix string
        :param suffix: suffix string
        :param decimals: positive number of decimals in percent complete
        :param length: character length of bar
        :param fill: bar fill character
        :param print_end: end character (e.g. "\r", "\r\n")
        """
        self.total = total
        self.prefix = prefix
        self.suffix = suffix
        self.decimals = decimals
        self.length = length
        self.fill = fill
        self.print_end = print_end

    def print(self, iteration: int) -> None:
        """
        Call this method once for every iteration

        :param iteration: current iteration
        """
        percent = 100 * (iteration / float(self.total))
        # percent = ("{0:." + str(decimals) + "f}").format(100 * (iteration / float(total)))
        filled_length = int(self.length * iteration // self.total)
        bar = self.fill * filled_length + "-" * (self.length - filled_length)
        print(f"\r{self.prefix} |{bar}| {percent:0.{self.decimals}f}% {self.suffix}", end=self.print_end)
        # Print New Line on Complete
        if iteration == self.total:
            print()


class FireflySession(requests.Session):
    def __init__(self, firefly_url: str, firefly_token: str):
        self.firefly_url = firefly_url
        self.firefly_token = firefly_token

        super().__init__()

        self.headers["Accept"] = "application/json"
        self.headers["Authorization"] = f"Bearer {self.firefly_token}"
        self.headers["Content-Type"] = "application/json"

    @staticmethod
    def _json_default(obj):
        if isinstance(obj, Decimal):
            int_val = int(obj)
            if int_val == obj:
                return int_val
            else:
                return float(obj)
        elif isinstance(obj, arrow.Arrow):
            assert not any((obj.hour, obj.minute, obj.second, obj.microsecond))
            return obj.format("YYYY-MM-DD")
        raise TypeError

    def request(self, method, url, **kwargs) -> requests.Response:
        if not url.startswith("https://"):
            url = f"{self.firefly_url}{url}"
        print_failures = True
        if "print_failures" in kwargs:
            print_failures = kwargs.pop("print_failures")
        if "json" in kwargs:
            kwargs["data"] = json.dumps(kwargs.pop("json"), default=self._json_default)
        response = super().request(method, url, **kwargs)
        if not response.ok:
            if print_failures and (method in ["PUT", "POST"] or response.status_code == 500):
                print(response.json())
            response.raise_for_status()
        return response

    def get_all_pages(self, url, params=None, **kwargs) -> dict:
        response = self.get(url, params=params, **kwargs)

        all_response = {"data": response.json()["data"]}

        while (
            response.json()["meta"]["pagination"]["current_page"]
            != response.json()["meta"]["pagination"]["total_pages"]
        ):
            if not params:
                params = {}
            params["page"] = response.json()["meta"]["pagination"]["current_page"] + 1

            response = self.get(url, params=params, **kwargs)

            all_response["data"].extend(response.json()["data"])

        return all_response


class Importer:
    MEMO_RE = re.compile(r"^.*([A-Z]{3})\s+([0-9,\\.]+)(K)?(;.*)?$")

    def __init__(
        self,
        firefly_url: str,
        firefly_token: str,
        config_path: str,
        register_path: str,
        budget_path: str,
        filter_min_date: str,
        filter_max_date: str,
    ):
        firefly_url = firefly_url.rstrip("/")

        self.filter_min_date, self.filter_max_date = None, None
        if filter_min_date:
            self.filter_min_date = arrow.get(filter_min_date)
        if filter_max_date:
            self.filter_max_date = arrow.get(filter_max_date)

        self.config = dacite.from_dict(
            Config, toml.load(config_path), config=dacite.Config(cast=[Decimal, Config.Account.Role], strict=True)
        )
        print(f"Loaded config for import into {firefly_url}")

        self.register_path = register_path
        self.budget_path = budget_path

        self.data = ImportData()
        self.firefly_data = FireflyData()

        self.all_transactions: List[YNABTransaction] = None
        self.all_budgets: List[YNABBudget] = None

        self._import_tag = f"import-{arrow.now().format('YYYY-MM-DDTHH-MM')}"

        self._session = FireflySession(firefly_url, firefly_token)

        self._cache_dir = Path(".cache")
        self._cache_path = self._cache_dir / "firefly_data.json"

    def run(self, dry_run: bool = False):
        if not dry_run:
            self._verify_connection()

        self._read_ynab_data()

        self._process_budgets()
        self._process_accounts()
        self._process_transactions()

        if not dry_run:
            self._load_cache()
            self._create_currencies()
            self._create_categories()
            self._create_budgets()
            self._create_budget_limits()
            self._create_available_budgets()
            self._create_asset_accounts()
            self._create_payee_accounts("revenue")
            self._create_payee_accounts("expense")
            self._create_transactions()

    def _acc_config(self, acc: str) -> Config.Account:
        return self.config.accounts.get(acc, Config.Account())

    def _is_foreign(self, acc: str) -> bool:
        return self._acc_config(acc).currency and self._acc_config(acc).currency != self.config.currency

    def _parse_date(self, dt: str) -> arrow.Arrow:
        return arrow.get(dt, self.config.date_format)

    def _payee(self, tx: YNABTransaction) -> str:
        return self.config.payee_mapping.get(tx.payee.strip(), tx.payee.strip())

    def _amount(self, tx: YNABTransaction) -> Decimal:
        # exactly one of these would be non-zero
        amount = tx.outflow or tx.inflow
        if not self._is_foreign(tx.account):
            return amount

        # for foreign accounts, try to use memo to find real value at the time of transaction
        m = self.MEMO_RE.match(tx.memo)
        foreign_currency_code = self._acc_config(tx.account).currency
        if m and m.group(1) == self._acc_config(tx.account).currency:
            # use memo - yaay!
            return Decimal(m.group(2).replace(",", "")) * (-1 if tx.inflow > 0 else 1)

        conv_fallback = self.config.currency_conv_fallback.get(foreign_currency_code)
        if not conv_fallback:
            raise ValueError(
                f"Unable to determine foreign amount for {tx}. Memo must be of form: [CURRENCY CODE] "
                f"[AMOUNT]. Alternatively, set config.currency_conv_fallback."
            )
        return amount * conv_fallback

    def _category(self, tx: Union[YNABTransaction, YNABBudget]) -> str:
        return getattr(tx, _ynab_field_name(self.config.category_field))

    def _budget(self, tx: Union[YNABTransaction, YNABBudget]) -> str:
        budget = getattr(tx, _ynab_field_name(self.config.budget_field))
        if tx.master_category == "Hidden Categories":
            budget = budget.split("`")[1].strip() + " (hidden)"
        budget = budget.strip()
        return self.config.budget_mapping.get(tx.category, budget)

    def _description(self, tx: YNABTransaction) -> str:
        if not self.config.memo_to_description:
            return self.config.empty_description
        memo = tx.memo.strip()
        if "(Split" in memo:
            memo = memo.split(")")[1].strip()
        # for foreign transactions, memo _may_ contain foreign currency info
        if self._is_foreign(tx.account) and (m := self.MEMO_RE.match(tx.memo)) and m.group(4):
            memo = m.group(4).strip()
        return memo or self.config.empty_description

    def _notes(self, tx: YNABTransaction) -> str:
        if not self.config.memo_to_description:
            return tx.memo.strip()
        return ""

    def _tags(self, tx: YNABTransaction) -> List[str]:
        tags = [self._import_tag]
        if tx.flag:
            tags.append(tx.flag)
        return tags

    def _transfer_foreign_amount(self, tx: YNABTransaction) -> Decimal:
        # should be the "corrected" version already
        assert "Transfer : " not in tx.payee

        to_account = tx.payee
        # From foreign account to default currency account --
        if self._is_foreign(tx.account) and not self._is_foreign(to_account):
            # Amount is already computed in `self._amount` above using foreign currency code
            # Outflow / Inflow corresponds to default currency
            return tx.outflow or tx.inflow
        # From default currency account to foreign account
        elif not self._is_foreign(tx.account) and self._is_foreign(to_account):
            foreign_currency_code = self._acc_config(to_account).currency
            conv_fallback = self.config.currency_conv_fallback.get(foreign_currency_code)
            fallback_amount = None
            if conv_fallback:
                # Amount is default currency
                fallback_amount = (tx.outflow or tx.inflow) * conv_fallback

            # for foreign accounts, try to use memo to find real value at the time of transaction
            m = self.MEMO_RE.match(tx.memo)
            if m and m.group(1) == self._acc_config(to_account).currency:
                # use memo - yaay!
                amount = Decimal(m.group(2).replace(",", ""))
                # thousand multiplier: 1K => 1,000
                if m.group(2) == "K":
                    amount *= 1000
                if fallback_amount:
                    # verify that amount doesn't mismatch from fallback by more than 20%
                    # this accounts for 20% fluctuation in currency conversion
                    assert abs(amount - fallback_amount) / fallback_amount <= 0.20
                return amount

            if not fallback_amount:
                raise ValueError(
                    f"Unable to determine foreign amount for {tx}. Memo must be of form: [CURRENCY CODE] "
                    f"[AMOUNT]. Alternatively, set config.currency_conv_fallback."
                )
            return fallback_amount

        elif self._is_foreign(tx.account) and self._is_foreign(to_account):
            assert (
                self._acc_config(tx.account).currency == self._acc_config(to_account).currency
            ), f"Can't handle transaction between two different foreign accounts: {tx}"

    def _read_ynab_data(self):
        assert not self.all_transactions and not self.all_budgets, "Already read!"
        with open(self.register_path) as f:
            reader = csv.DictReader(f, fieldnames=YNAB_TRANSACTION_FIELDS)
            # skip header
            next(reader)
            all_transactions = list(reader)
        self.all_transactions = [
            YNABTransaction(
                account=tx["Account"],
                flag=tx["Flag"],
                date=self._parse_date(tx["Date"]),
                payee=tx["Payee"],
                category=tx["Category"],
                master_category=tx["Master Category"],
                sub_category=tx["Sub Category"],
                memo=tx["Memo"],
                outflow=_to_amount(tx["Outflow"]),
                inflow=_to_amount(tx["Inflow"]),
                cleared=tx["Cleared"],
                running_balance=_to_amount(tx["Running Balance"]),
            )
            for tx in all_transactions
        ]
        self.all_transactions = sorted(
            # YNAB4 display sorts same date transactions by highest inflow first
            self.all_transactions,
            key=lambda tx: (tx.date, tx.outflow - tx.inflow),
        )
        print(f"Loaded {len(self.all_transactions)} transactions")

        with open(self.budget_path) as f:
            reader = csv.DictReader(f, fieldnames=YNAB_BUDGET_FIELDS)
            # skip header
            next(reader)
            all_budgets = list(reader)
        self.all_budgets = [
            YNABBudget(
                month=arrow.get(bg["Month"], "MMMM YYYY"),
                category=bg["Category"],
                master_category=bg["Master Category"],
                sub_category=bg["Sub Category"],
                budgeted=_to_amount(bg["Budgeted"]),
                outflows=_to_amount(bg["Outflows"]),
                category_balance=_to_amount(bg["Category Balance"]),
            )
            for bg in all_budgets
        ]
        print(f"Loaded {len(self.all_budgets)} budgets")

    def _process_budgets(self):
        self.data.categories = list(
            {self._category(bg) for bg in self.all_budgets if self._category(bg) and not bg.is_hidden}
        )
        # Two special categories for income
        self.data.categories.extend(["Available this month", "Available next month"])
        self.data.budgets.update(
            {
                budget: ImportData.Budget(name=budget, active=not hidden)
                for budget, hidden in {
                    (self._budget(bg), bg.is_hidden)
                    for bg in self.all_budgets
                    if not bg.is_pre_ynab and self._budget(bg)
                }
            }
        )
        # Two special budgets for managing YNAB Rule #4 - use last month's income
        self.data.budgets.update(
            {
                budget: ImportData.Budget(name=budget, active=True)
                for budget in ["Available next month", "Available this month"]
            }
        )
        self.data.budget_history = [
            ImportData.BudgetHistory(
                name=self._budget(bg),
                amount=bg.budgeted,
                start=bg.month,
                end=bg.month.replace(day=calendar.monthrange(bg.month.year, bg.month.month)[1]),
            )
            for bg in self.all_budgets
            if not bg.is_pre_ynab and self._budget(bg) and bg.budgeted
        ]

    def _process_accounts(self):
        account_names = {tx.account for tx in self.all_transactions}
        for acc in self.config.accounts:
            assert acc in account_names, f"Unknown account with no transactions in config: |{acc}|"

        starting_balances: Dict[str, Tuple[arrow.Arrow, Decimal]] = {
            tx.account: (tx.date, tx.inflow - tx.outflow)
            for tx in self.all_transactions
            if tx.payee == "Starting Balance"
        }

        for acc in account_names:
            start_date, balance = starting_balances[acc]
            account_config = self._acc_config(acc)
            role = ImportData.Account.Role[account_config.role.name]
            monthly_payment_date = None
            if role is ImportData.Account.Role.credit_card:
                if account_config.monthly_payment_date:
                    try:
                        monthly_payment_date = arrow.get(account_config.monthly_payment_date)
                    except arrow.ParserError:
                        try:
                            monthly_payment_date = arrow.get(
                                account_config.monthly_payment_date, self.config.date_format
                            )
                        except arrow.ParserError:
                            raise ValueError(f"Unable to parse date: |{account_config.monthly_payment_date}|")
                else:
                    try:
                        monthly_payment_date = next(
                            tx.date for tx in self.all_transactions if _is_transfer(tx) and _transfer_account(tx) == acc
                        )
                    except StopIteration:
                        print(f"[WARN] Couldn't figure out monthly payment date for {acc}, defaulting to 01/01")
                        monthly_payment_date = arrow.get("2020-01-01")  # year doesn't matter
            account = ImportData.Account(
                name=acc,
                opening_date=start_date,
                monthly_payment_date=monthly_payment_date,
                role=role,
                opening_balance=balance,
            )
            self.data.asset_accounts.append(account)

        self.data.revenue_accounts = list(
            {self._payee(tx) for tx in self.all_transactions if tx.inflow > 0 and not _is_transfer(tx)}
        )
        self.data.expense_accounts = list(
            {self._payee(tx) for tx in self.all_transactions if tx.outflow > 0 and not _is_transfer(tx)}
        )
        print(
            f"Configured account data for {len(self.data.asset_accounts)} asset accounts, "
            f"{len(self.data.revenue_accounts)} revenue accounts, and {len(self.data.expense_accounts)} "
            f"expense accounts"
        )

    def _process_transactions(self):
        print(f"Transactions will be tagged with {self._import_tag}")

        for tx in reversed(self.all_transactions):
            tx_month = tx.date.replace(day=1)
            if (tx_month, tx.account) not in self.data.running_balances:
                self.data.running_balances[tx_month][tx.account] = tx.running_balance

        transactions = funcy.remove(
            lambda tx: (tx.inflow == 0 and tx.outflow == 0) or tx.payee == "Starting Balance", self.all_transactions
        )
        splits, non_splits = funcy.split(lambda tx: "(Split " in tx.memo, transactions)
        splits_grouped = OrderedDict()
        for tx in splits:
            split_group_key = (tx.account, tx.date, tx.running_balance)
            if split_group_key not in splits_grouped:
                splits_grouped[split_group_key] = []
            splits_grouped[split_group_key].append(tx)
        # process splits first because in case of transfers, we want to split version of Transfer rather than the other
        all_tx_grouped = list(splits_grouped.values())
        all_tx_grouped.extend([[tx] for tx in non_splits])

        # used to de-dup transactions because YNAB will double-log every transfer
        # map key: tuple of accounts (sorted by name), date, abs(outflow - inflow)
        # map value: int - how many times this was seen. Max = 2
        transfers_seen_map: Dict[Tuple[Tuple[str], arrow.Arrow, Decimal]] = {}

        withdrawals_count = deposits_count = transfers_count = 0

        for tx_group in all_tx_grouped:
            transaction_group = ImportData.TransactionGroup(
                title=self.config.empty_description if len(tx_group) > 0 else ""
            )

            for tx in tx_group:
                budget = self._budget(tx)
                if budget:
                    assert budget in self.data.budgets, f"Unable to process transaction with unknown budget: |{budget}|"

                    # ignore hidden categories
                    if self.data.budgets[budget].active:
                        category = self._category(tx)
                        assert (
                            not category or category in self.data.categories
                        ), f"Unable to process transaction with unknown category: |{category}|"
                else:
                    category = ""

                # running balance would be different for two accounts on the same day
                # for the same amount, thus allowing us to de-duplicate them
                # could hash this value but hardly worth it
                external_id = str(tx.running_balance)

                if _is_transfer(tx):
                    tx = _correct_transfer_to_from(tx)

                    date = tx.date
                    transfer_seen_map_key = (
                        tuple(sorted([tx.account, tx.payee])),
                        date,
                        abs(tx.outflow - tx.inflow),
                    )
                    if transfers_seen_map.get(transfer_seen_map_key, 0) % 2 == 1:
                        transfers_seen_map[transfer_seen_map_key] += 1
                        continue
                    transfers_seen_map[transfer_seen_map_key] = 1
                    transfer = ImportData.TransactionGroup.Transfer(
                        from_account=tx.account,
                        to_account=tx.payee,
                        date=date,
                        amount=self._amount(tx),
                        description=self._description(tx),
                        foreign_amount=self._transfer_foreign_amount(tx),
                        notes=self._notes(tx),
                        tags=self._tags(tx),
                        reconciled=tx.cleared == "R",
                        external_id=external_id,
                    )
                    transaction_group.transactions.append(transfer)
                    transfers_count += 1
                elif tx.outflow > 0:
                    withdrawal = ImportData.TransactionGroup.Withdrawal(
                        account=tx.account,
                        date=tx.date,
                        payee=self._payee(tx),
                        amount=self._amount(tx),
                        description=self._description(tx),
                        budget=budget,
                        category=category,
                        notes=self._notes(tx),
                        tags=self._tags(tx),
                        reconciled=tx.cleared == "R",
                        external_id=external_id,
                    )
                    transaction_group.transactions.append(withdrawal)
                    withdrawals_count += 1
                elif tx.inflow > 0:
                    deposit = ImportData.TransactionGroup.Deposit(
                        account=tx.account,
                        date=tx.date,
                        payee=self._payee(tx),
                        amount=self._amount(tx),
                        description=self._description(tx),
                        budget=budget,
                        category=category,
                        notes=self._notes(tx),
                        tags=self._tags(tx),
                        reconciled=tx.cleared == "R",
                        external_id=external_id,
                    )
                    transaction_group.transactions.append(deposit)
                    deposits_count += 1
                else:
                    raise ValueError(f"dunno how to handle this transaction of unknown kind: |{tx}|")
            # there may be transaction groups with only one transfer the counterpart of which was already picked
            # so ignore these zero-transaction groups
            if transaction_group.transactions:
                self.data.transaction_groups.append(transaction_group)
        print(
            f"Configured transaction data for {deposits_count} deposits and {withdrawals_count} withdrawals, and "
            f"{transfers_count} transfers in a total of {len(self.data.transaction_groups)} groups."
        )

    def _verify_connection(self) -> None:
        response = self._session.get("/api/v1/about/user")

        print(f"Authenticated successfully as {response.json()['data']['attributes']['email']}")

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return

        self.firefly_data = dacite.from_dict(
            FireflyData, json.loads(self._cache_path.read_text()), config=dacite.Config(strict=True)
        )

    def _update_cache(self) -> None:
        if not self._cache_dir.exists():
            self._cache_dir.mkdir(parents=True)
        with open(self._cache_path, "w") as f:
            json.dump(dataclasses.asdict(self.firefly_data), f)

    def _create_currencies(self) -> None:
        # check cache
        if self.firefly_data.currencies:
            return
        response = self._session.get_all_pages("/api/v1/currencies")
        self.firefly_data.currencies = {d["attributes"]["code"]: int(d["id"]) for d in response["data"]}
        currencies = {d["attributes"]["code"]: d for d in response["data"]}
        currencies_to_keep = [
            "EUR",  # Firefly recommends keeping this always
            self.config.currency,
        ]
        currencies_to_keep.extend([account.currency for account in self.config.accounts.values() if account.currency])
        for code, data in currencies.items():
            if code == self.config.currency:
                if not data["attributes"]["default"]:
                    self._session.post(f"/api/v1/currencies/{code}/default")
            if code in [self.config.currency, "INR", "EUR"]:
                if not data["attributes"]["enabled"]:
                    self._session.post(f"/api/v1/currencies/{code}/enable")
            elif data["attributes"]["enabled"]:
                self._session.post(f"/api/v1/currencies/{code}/disable")
        self._update_cache()

    def _create_budgets(self) -> None:
        # check cache
        if self.firefly_data.budgets:
            return
        response = self._session.get_all_pages("/api/v1/budgets")

        for data in response["data"]:
            self.firefly_data.budgets[data["attributes"]["name"]] = data

        for budget in self.data.budgets.values():
            data = {
                "name": budget.name,
                "active": budget.active,
            }

            try:
                if budget.name in self.firefly_data.budgets:
                    if _firefly_needs_update(data, self.firefly_data.budgets[budget.name]):
                        self._session.put(
                            f"/api/v1/budgets/{self.firefly_data.budgets[budget.name]['id']}", json=data,
                        )
                else:
                    response = self._session.post("/api/v1/budgets", json=data)
                    self.firefly_data.budgets[budget.name] = response.json()["data"]
            except requests.HTTPError as e:
                if e.response.status_code == 500:
                    # ignore, because Firefly seems to do the right thing but fail still!?
                    pass
                else:
                    raise
        self._update_cache()
        print(f"Created {len(self.firefly_data.budgets)} budgets")

    def _create_budget_limits(self) -> None:
        if self.config.skip_budget_limits_import:
            print("Skipping budget limits import as requested")
            return

        # check cache
        if self.firefly_data.budget_limits:
            return

        for budget, budget_data in self.firefly_data.budgets.items():
            response = self._session.get_all_pages(f"/api/v1/budgets/{budget_data['id']}/limits")

            for data in response["data"]:
                self.firefly_data.budget_limits[
                    (budget, arrow.get(data["attributes"]["start"]), arrow.get(data["attributes"]["end"]))
                ] = data

        for bg_hist in self.data.budget_history:
            data = {
                "budget_id": int(self.firefly_data.budgets[bg_hist.name]["id"]),
                "start": bg_hist.start,
                "end": bg_hist.end,
                "amount": bg_hist.amount,
            }

            firefly_data = self.firefly_data.budget_limits.get((bg_hist.name, bg_hist.start, bg_hist.end))
            if firefly_data:
                if _firefly_needs_update(data, firefly_data):
                    self._session.put(
                        f"/api/v1/budgets/limits/{firefly_data['id']}", json=data,
                    )
            else:
                response = self._session.post(f"/api/v1/budgets/{data['budget_id']}/limits", json=data)
                self.firefly_data.budget_limits[(bg_hist.name, bg_hist.start, bg_hist.end)] = response.json()["data"]
        self._update_cache()
        print(f"Created {len(self.firefly_data.budget_limits)} budget limits")

    def _create_available_budgets(self) -> None:
        print(
            "SKIPPED creating available budgets since this requires more complex calculation based on previous months "
            "in-flow and such"
        )

    def _create_categories(self) -> None:
        # check cache
        if self.firefly_data.categories:
            return
        response = self._session.get_all_pages("/api/v1/categories")

        for data in response["data"]:
            self.firefly_data.categories[data["attributes"]["name"]] = int(data["id"])

        for category in self.data.categories:
            if category in self.firefly_data.categories:
                continue

            response = self._session.post("/api/v1/categories", json={"name": category})
            self.firefly_data.categories[category] = int(response.json()["data"]["id"])
        self._update_cache()
        print(f"Created {len(self.firefly_data.categories)} categories")

    def _create_asset_accounts(self, ignore_cache: bool = False) -> None:
        # check cache
        if not ignore_cache and self.firefly_data.asset_accounts:
            return

        response = self._session.get_all_pages("/api/v1/accounts?type=asset")

        for data in response["data"]:
            self.firefly_data.asset_accounts[data["attributes"]["name"]] = data

        for account in self.data.asset_accounts:
            data = {
                "name": account.name,
                "active": True,
                "type": "asset",
                "account_role": account.role.value,
                "currency_id": self.firefly_data.currencies[
                    self.config.accounts.get(account.name, Config.Account()).currency or self.config.currency
                ],
                "include_net_worth": True,
            }
            if account.opening_balance:
                # firefly iii will not update date unless balance is non-zero
                data.update({"opening_balance": account.opening_balance, "opening_balance_date": account.opening_date})
            if account.role is ImportData.Account.Role.credit_card:
                data["credit_card_type"] = "monthlyFull"
                data["monthly_payment_date"] = account.monthly_payment_date

            if account.name in self.firefly_data.asset_accounts:
                if _firefly_needs_update(data, self.firefly_data.asset_accounts[account.name]):
                    self._session.put(
                        f"/api/v1/accounts/{self.firefly_data.asset_accounts[account.name]['id']}", json=data,
                    )
            else:
                response = self._session.post("/api/v1/accounts", json=data)
                self.firefly_data.asset_accounts[account.name] = response.json()["data"]
        self._update_cache()
        print(f"Created {len(self.firefly_data.asset_accounts)} asset accounts")

    def _create_payee_accounts(self, account_type: str) -> None:
        assert account_type in ("revenue", "expense")

        # e.g. self.data.revenue_accounts
        accounts = getattr(self.data, f"{account_type}_accounts")

        # e.g. self.firefly_data.revenue_accounts
        firefly_data = getattr(self.firefly_data, f"{account_type}_accounts")

        # check cache
        if firefly_data:
            return

        response = self._session.get_all_pages(f"/api/v1/accounts?type={account_type}")

        for data in response["data"]:
            firefly_data[data["attributes"]["name"]] = data

        for account in accounts:
            data = {
                "name": account,
                "active": True,
                "type": account_type,
                "include_net_worth": True,
            }

            if account in firefly_data:
                if _firefly_needs_update(data, firefly_data[account]):
                    self._session.put(
                        f"/api/v1/accounts/{firefly_data[account]['id']}", json=data,
                    )
            else:
                response = self._session.post("/api/v1/accounts", json=data)
                firefly_data[account] = response.json()["data"]["id"]
        self._update_cache()
        print(f"Created {len(firefly_data)} {account_type} accounts")

    def _verify_running_balance(self, month: arrow.Arrow) -> None:
        """
        Before moving to next month verify running balance to match data against YNAB
        """
        # re-fetch asset accounts
        self._create_asset_accounts(ignore_cache=True)

        for account, balance in self.data.running_balances[month].items():
            if self._is_foreign(account):
                continue
            firefly_balance = self.firefly_data.asset_accounts[account]["attributes"]["running_balance"]
            if balance != firefly_balance:
                raise ValueError(
                    f"Running balance for {account} doesn't match. YNAB says: {self.config.currency} {balance}, Firefly"
                    f"says: {self.config.currency} {firefly_balance}"
                )

    def _create_transactions(self) -> None:
        imported = ignored = 0
        total = len(self.data.transaction_groups)
        progress_bar = ProgressBar(total, prefix="Import progress:", suffix="Complete", length=50)
        output = StringIO()
        current_month = self.data.transaction_groups[0].transactions[0].date.replace(day=1)
        try:
            for tx_group in self.data.transaction_groups:
                tx_month = self.data.transaction_groups[0].transactions[0].date.replace(day=1)
                if tx_month != current_month:
                    self._verify_running_balance(current_month)
                    current_month = tx_month
                    # abort after first month
                    raise ValueError
                if (self.filter_max_date and tx_group.transactions[0].date > self.filter_max_date) or (
                    self.filter_min_date and tx_group.transactions[0].date < self.filter_min_date
                ):
                    ignored += 1
                    progress_bar.print(imported + ignored)
                    continue
                transactions_data = []
                for tx in tx_group.transactions:
                    tx_data = {
                        "type": tx.__class__.__name__.lower(),
                        "date": tx.date,
                        "amount": tx.amount,
                        "description": tx.description,
                        "tags": tx.tags,
                        "notes": tx.notes,
                        "reconciled": tx.reconciled,
                        "external_id": tx.external_id,
                    }
                    if isinstance(tx, ImportData.TransactionGroup.Deposit):
                        tx_data.update(
                            {
                                "source_id": int(self.firefly_data.revenue_accounts[tx.payee]["id"]),
                                "destination_id": int(self.firefly_data.asset_accounts[tx.account]["id"]),
                            }
                        )
                        if tx.budget:
                            tx_data["budget_id"] = int(self.firefly_data.budgets[tx.budget]["id"])
                        if tx.category:
                            tx_data["category_id"] = self.firefly_data.categories[tx.category]
                    elif isinstance(tx, ImportData.TransactionGroup.Withdrawal):
                        tx_data.update(
                            {
                                "source_id": int(self.firefly_data.asset_accounts[tx.account]["id"]),
                                "destination_id": int(self.firefly_data.expense_accounts[tx.payee]["id"]),
                            }
                        )
                        if tx.budget:
                            tx_data["budget_id"] = int(self.firefly_data.budgets[tx.budget]["id"])
                        if tx.category:
                            tx_data["category_id"] = self.firefly_data.categories[tx.category]
                    elif isinstance(tx, ImportData.TransactionGroup.Transfer):
                        tx_data.update(
                            {
                                "source_id": int(self.firefly_data.asset_accounts[tx.from_account]["id"]),
                                "destination_id": int(self.firefly_data.asset_accounts[tx.to_account]["id"]),
                            }
                        )
                        if tx.foreign_amount:
                            tx_data.update({"foreign_amount": tx.foreign_amount})
                    transactions_data.append(tx_data)
                data = {
                    "error_if_duplicate_hash": True,
                    "apply_rules": False,
                    "group_title": tx_group.title,
                    "transactions": transactions_data,
                }
                try:
                    response = self._session.post("/api/v1/transactions", json=data, print_failures=False,)
                    print(f"Created transaction #{response.json()['data']['id']}", file=output)
                except requests.HTTPError as e:
                    # check for duplicate transaction "failure"
                    if e.response.status_code == 422:
                        dup_errors, other_tx_errors, other_errors = _firefly_create_transaction_errors(e.response)
                        if other_tx_errors or other_errors:
                            print(e.response.json(), file=output)
                            raise
                        for tx_idx, dup_id in dup_errors.items():
                            print(f"Ignoring transaction duplicate of #{dup_id}", file=output)
                    else:
                        print(e.response.json(), file=output)
                        raise
                imported += 1
                if imported > 200:
                    raise ValueError
                progress_bar.print(imported + ignored)
        finally:
            output.flush()
            print(output.getvalue())


def main() -> None:
    firefly_url = os.environ["FIREFLY_III_URL"]
    firefly_token = os.environ["FIREFLY_III_ACCESS_TOKEN"]
    importer = Importer(firefly_url, firefly_token, sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    importer.run()


if __name__ == "__main__":
    main()
