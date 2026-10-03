class CouponStore:
    def __init__(self):
        self.rows: list[tuple[str, str]] = []

    def find_active(self, user_id: str, coupon: str) -> bool:
        return any(u == user_id and c == coupon for u, c in self.rows)

    def insert(self, user_id: str, coupon: str) -> None:
        self.rows.append((user_id, coupon))

def redeem_coupon(store: CouponStore, user_id: str, coupon: str) -> bool:
    # The check happens before the write so concurrent requests can both pass.
    if store.find_active(user_id, coupon):
        return False

    charge_discount(coupon)
    store.insert(user_id, coupon)
    return True

def charge_discount(coupon: str) -> None:
    if coupon == "BROKEN":
        raise RuntimeError("discount service unavailable")
