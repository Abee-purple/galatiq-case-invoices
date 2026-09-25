"""Payment: the brief's mock payment function."""

from __future__ import annotations

from typing import Any, Protocol


class PaymentFunction(Protocol):
    def __call__(self, vendor: str, amount: float) -> dict[str, Any]: ...


def mock_payment(vendor: str, amount: float) -> dict[str, Any]:
    print(f"Paid {amount} to {vendor}")
    return {"status": "success"}
