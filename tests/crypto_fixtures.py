"""Lightweight stand-ins for the SDK objects the crypto strategy reads.

The crypto path only ever touches a documented handful of attributes, so
hand-built doubles keep the tests deterministic and offline -- a strategy whose
whole point is a 30-second window cannot be tested against a live clock.
"""
from datetime import datetime, timedelta, timezone

# A fixed "now" so every window assertion is exact rather than racing the clock.
NOW = datetime(2026, 9, 12, 14, 0, 0, tzinfo=timezone.utc)


class Outcome:
    def __init__(self, label, token_id, price=None):
        self.label = label
        self.token_id = token_id
        self.price = price


class Outcomes:
    def __init__(self, yes, no):
        self.yes = yes
        self.no = no


class State:
    def __init__(self, start_date, end_date, closed=False, accepting_orders=True,
                 active=True, archived=False):
        self.start_date = start_date
        self.end_date = end_date
        self.closed = closed
        self.accepting_orders = accepting_orders
        self.active = active
        self.archived = archived


class Metrics:
    def __init__(self, volume=0.0, liquidity=0.0):
        self.volume = volume
        self.liquidity = liquidity


class EventRef:
    def __init__(self, slug):
        self.slug = slug


class FakeMarket:
    def __init__(self, id, slug, question, state, outcomes, metrics, events=()):
        self.id = id
        self.slug = slug
        self.question = question
        self.state = state
        self.outcomes = outcomes
        self.metrics = metrics
        self.events = events


def make_market(
    slug="bitcoin-up-or-down-2026-09-12-14-05",
    seconds_left=20,
    duration_seconds=300,
    market_id="1001",
    labels=("Up", "Down"),
    prices=(0.93, 0.07),
    tokens=("tok-up", "tok-down"),
    question=None,
    volume=0.0,
    liquidity=0.0,
    start="derive",
    now=NOW,
    naive_timestamps=False,
    events=(),
    **state_kwargs,
):
    """Builds one market ending `seconds_left` from `now`.

    `start="derive"` places the round start `duration_seconds` before the end,
    which is how a real fixed-length round looks. Pass start=None to model Gamma
    not hydrating a start timestamp.
    """
    end = now + timedelta(seconds=seconds_left)
    start_dt = (end - timedelta(seconds=duration_seconds)) if start == "derive" else start
    if naive_timestamps:
        end = end.replace(tzinfo=None)
        if start_dt is not None:
            start_dt = start_dt.replace(tzinfo=None)
    return FakeMarket(
        id=market_id,
        slug=slug,
        question=question if question is not None else f"{slug} Up or Down",
        state=State(start_dt, end, **state_kwargs),
        outcomes=Outcomes(
            Outcome(labels[0], tokens[0], prices[0]),
            Outcome(labels[1], tokens[1], prices[1]),
        ),
        metrics=Metrics(volume, liquidity),
        events=tuple(EventRef(e) for e in events),
    )


class Level:
    def __init__(self, price, size):
        self.price = price
        self.size = size


class FakeBook:
    """Order book shaped like the SDK's: bids ascending, asks descending, best last."""

    def __init__(self, best_bid=0.92, best_ask=0.94, ask_size=500.0, bid_size=500.0,
                 timestamp=NOW, min_order_size=0.0, tick_size=0.001, one_sided=None):
        self.bids = [] if one_sided == "no_bids" else [Level(best_bid - 0.05, bid_size), Level(best_bid, bid_size)]
        self.asks = [] if one_sided == "no_asks" else [Level(best_ask + 0.05, ask_size), Level(best_ask, ask_size)]
        self.timestamp = timestamp
        self.min_order_size = min_order_size
        self.tick_size = tick_size


class FakePage:
    def __init__(self, items):
        self.items = items


class FakeClient:
    """Public client double.

    `markets` is what list_markets pages over; `books` maps token id -> FakeBook.
    Every call is counted so the tests can assert the scan does not fetch an
    order book it had no reason to fetch.
    """

    def __init__(self, markets=(), books=None):
        self.markets = list(markets)
        self.books = dict(books or {})
        self.list_calls = []
        self.book_calls = []
        self.market_calls = []
        self.book_error = None

    def list_markets(self, **kwargs):
        self.list_calls.append(kwargs)
        return [FakePage(self.markets)]

    def get_order_book(self, token_id):
        self.book_calls.append(token_id)
        if self.book_error is not None:
            raise self.book_error
        book = self.books.get(token_id)
        if book is None:
            raise KeyError(f"no book fixture for {token_id}")
        return book

    def get_market(self, id):
        self.market_calls.append(id)
        for market in self.markets:
            if str(market.id) == str(id):
                return market
        raise KeyError(f"no market fixture for {id}")
