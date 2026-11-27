"""Per-request usage and its pricing source."""
import math


def usd_amount(value):
    if type(value) in (int, float) and 0 <= value <= 1_000_000 and math.isfinite(value):
        return float(value)
    return None


class Usage(tuple):
    """A token pair with optional vendor pricing metadata."""

    def __new__(cls, values, amount=None, source=None, request_id=None):
        valid = tuple(type(value) is int and 0 <= value <= 2**31 - 1 for value in values)
        instance = super().__new__(cls, (value if ok else 0 for value, ok in zip(values, valid, strict=True)))
        instance.reported = all(valid)
        instance.amount = usd_amount(amount)
        instance.source = source if instance.amount is not None else None
        instance.request_id = request_id if isinstance(request_id, str) and 0 < len(request_id) <= 240 else None
        return instance

    def price(self, settings):
        if self.amount is not None:
            return self.amount, self.source
        if settings["kind"] in ("demo", "local") and not (settings["input_rate"] or settings["output_rate"]):
            return 0, "local"
        estimated = (self[0] * settings["input_rate"] + self[1] * settings["output_rate"]) / 1_000_000
        if self.reported and settings.get("rate_source") == "azure_retail":
            return estimated, "azure_retail"
        source = "token_rates" if self.reported and (settings["kind"] == "local" or (
            settings["input_rate"] > 0 and settings["output_rate"] > 0)) else "unpriced"
        return estimated, source


class MeteredModelError(RuntimeError):
    def __init__(self, message, usage, *, budget_reached=False):
        super().__init__(message)
        self.usage = usage
        self.budget_reached = budget_reached
