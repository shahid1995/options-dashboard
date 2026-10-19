class AccountStore:
    def __init__(self, balances: dict[str, int]):
        self.balances = balances

    def debit(self, account_id: str, amount: int) -> None:
        if self.balances.get(account_id, 0) < amount:
            raise ValueError("insufficient funds")
        self.balances[account_id] -= amount

    def credit(self, account_id: str, amount: int) -> None:
        self.balances[account_id] = self.balances.get(account_id, 0) + amount

    def commit(self) -> None:
        pass

def transfer(store: AccountStore, source: str, destination: str, amount: int) -> None:
    store.debit(source, amount)
    store.commit()

    # External settlement may fail after the debit is committed.
    settle_credit(store, destination, amount)

def settle_credit(store: AccountStore, destination: str, amount: int) -> None:
    if destination.startswith("blocked-"):
        raise RuntimeError("destination rejected by settlement provider")
    store.credit(destination, amount)
    store.commit()
