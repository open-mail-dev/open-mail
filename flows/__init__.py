from dataclasses import dataclass, field
from datetime import timedelta


@dataclass(frozen=True)
class Step:
    n: int
    delay: timedelta
    marketing: bool = True


@dataclass(frozen=True)
class Flow:
    name: str
    steps: list
    exit_on: set = field(default_factory=set)
    # who may receive it: "subscribed" (marketing consent), "not_unsubscribed" (anyone not opted out / suppressed) or
    # "requested" (they asked for this mail: only a bounce / complaint / import suppression stops it)
    audience: str = "subscribed"


from flows import abandoned_checkout, back_in_stock, browse_abandonment, cart_reminder, post_purchase, welcome, winback  # noqa: E402

FLOWS = {f.name: f for f in (welcome.FLOW, abandoned_checkout.FLOW, browse_abandonment.FLOW, cart_reminder.FLOW,
                                 post_purchase.FLOW, winback.FLOW, back_in_stock.FLOW)}
