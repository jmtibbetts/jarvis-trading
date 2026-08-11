"""Stable identity rules for replacing same-side signals without hiding hedges."""


def direction_side(direction: str | None) -> str:
    return "short" if "short" in str(direction or "").lower() else "long"


def signal_identity(symbol: str | None, direction: str | None,
                    user_id: str = "local") -> tuple[str, str, str]:
    return (str(user_id or "local"), str(symbol or "").upper().strip(), direction_side(direction))

