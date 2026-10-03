from dataclasses import dataclass

@dataclass(frozen=True)
class Order:
    id: str
    total: int

def list_orders() -> list[Order]:
    return [Order("A", 100), Order("B", 200)]

def orders_response() -> dict:
    orders = list_orders()
    return {
        "items": [order.__dict__ for order in orders],
        "count": len(orders),
    }

def client_total(response: list[dict]) -> int:
    return sum(order["total"] for order in response)
