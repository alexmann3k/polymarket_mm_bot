import unittest
from types import SimpleNamespace

from models import Position
from risk import MarketHealth, RiskManager
from helpers import book, config, market


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.config = config()
        self.health = MarketHealth(self.config)
        self.end = market().end_date

    def update(self, bid=.40, ask=.44):
        return self.health.update((book(bid=bid, ask=ask), book("n")), self.end)[0]

    def test_flat_green(self):
        for _ in range(8):
            self.assertEqual(self.update(), "GREEN")
        self.assertLessEqual(len(self.health.history), 3)

    def test_yellow_recovers_after_two_stable_snapshots(self):
        self.update()
        self.assertEqual(self.update(.45, .49), "YELLOW")
        self.assertEqual(self.update(.45, .49), "YELLOW")
        self.assertEqual(self.update(.45, .49), "GREEN")

    def test_persistent_movement_red(self):
        self.update()
        self.update(.45, .49)
        self.assertEqual(self.update(.50, .54), "YELLOW")
        self.assertEqual(self.update(.55, .59), "RED")

    def test_spread_collapse_immediate(self):
        self.update()
        self.assertEqual(self.update(.44, .449), "RED")
        self.assertEqual(self.health.reason, "spread_collapse")

    def test_red_is_terminal(self):
        self.update(.44, .449)
        self.assertEqual(self.update(), "RED")

    def test_only_recovery_allows_narrow_spread_without_two_sided_edge(self):
        books = (book(bid=.42, ask=.429), book("n", bid=.42, ask=.429))
        self.assertEqual(MarketHealth(self.config).update(books, self.end)[0], "RED")
        self.assertEqual(self.health.update(books, self.end, inventory_reduction=True)[0], "GREEN")

    def test_recovery_narrow_spread_does_not_mask_other_health_limits(self):
        from dataclasses import replace
        narrow = book(bid=.42, ask=.429)
        for invalid in (book("n", bid=.96, ask=.97), book("n", bid=.40, ask=.55),
                        book("n", bid=.44, ask=.44), replace(book("n"), bids=())):
            with self.subTest(book=invalid):
                health = MarketHealth(self.config)
                self.assertEqual(health.update((narrow, invalid), self.end, inventory_reduction=True)[0], "RED")


class TestInventoryRisk(unittest.TestCase):
    def narrow_targets(self, position, token="a-n", price=.41, size=5, **changes):
        from orders import TrackedOrder
        return RiskManager(config(**changes)).targets(market(),
            (book("a-y", bid=.42, ask=.429), book("a-n", bid=.42, ask=.429)), position,
            SimpleNamespace(positions={"a": position}),
            [TrackedOrder("resting", token, "BUY", price, size, 0, "condition-a")])

    def test_narrow_spread_preserves_only_permitted_recovery_buy(self):
        for position, token in ((Position(5, 0, 100, .46), "a-n"),
                                (Position(0, 5, 100, None, .46), "a-y")):
            with self.subTest(token=token):
                self.assertEqual(self.narrow_targets(position, token), {(token, "BUY"): (.41, .01, 5)})
        self.assertFalse(self.narrow_targets(Position(5, 5, 100, .46, .46)))
        self.assertFalse(self.narrow_targets(Position(0, 5, 100, None, .46)))

    def test_narrow_recovery_still_rejects_unsafe_resting_quotes(self):
        self.assertFalse(self.narrow_targets(Position(5, 0, 100, .60)))  # Pair cap .39 < .41.
        self.assertFalse(self.narrow_targets(Position(5, 0, 100)))  # Unknown cost.
        self.assertFalse(self.narrow_targets(Position(5, 0, 100, .46), price=.44))  # Crossing.
        self.assertFalse(self.narrow_targets(Position(5, 0, 100, .46), price=.415))  # Off tick.
        self.assertFalse(self.narrow_targets(Position(5, 0, 0, .46)))  # Cash.
        self.assertFalse(self.narrow_targets(Position(5, 0, 100, .46), max_total_capital=9))
        self.assertFalse(self.narrow_targets(Position(10, 5, 100, .46), max_position_per_side=5))
        self.assertFalse(self.narrow_targets(Position(1, 0, 100, .46)))  # Cannot retain excess size.

    def targets(self, position, **changes):
        cfg = config(**changes)
        inventory = SimpleNamespace(positions={"a": position})
        return RiskManager(cfg).targets(market(), (book("a-y"), book("a-n")),
                                         position, inventory, [])

    def test_balanced_both_sides(self):
        self.assertEqual(len(self.targets(Position(5, 5, 100))), 4)

    def test_no_short_selling(self):
        result = self.targets(Position(0, 0, 100))
        self.assertEqual({side for _, side in result}, {"BUY"})

    def test_yes_heavy_only_buys_no(self):
        result = self.targets(Position(10, 5, 100, .46, .46))
        self.assertEqual(set(result), {("a-n", "BUY")})

    def test_no_heavy_only_buys_yes(self):
        result = self.targets(Position(5, 10, 100, .46, .46))
        self.assertEqual(set(result), {("a-y", "BUY")})

    def test_hard_limit(self):
        result = self.targets(Position(10, 10, 100))
        self.assertTrue(all(side == "SELL" for _, side in result))

    def test_cash_shared_between_bids(self):
        result = self.targets(Position(0, 0, 3))
        self.assertEqual(len(result), 1)

    def test_total_capital_not_per_order(self):
        result = self.targets(Position(0, 0, 100), max_total_capital=7)
        self.assertEqual(len(result), 1)

    def test_old_markets_consume_capital(self):
        cfg = config(max_total_capital=20)
        p = Position(0, 0, 100)
        inv = SimpleNamespace(positions={"a": p, "old": Position(10, 10)})
        self.assertEqual(RiskManager(cfg).targets(market(), (book("a-y"), book("a-n")), p, inv, []), {})
