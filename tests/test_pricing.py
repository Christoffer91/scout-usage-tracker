import unittest
from decimal import Decimal

from scout_usage_tracker.pricing import credits_from_nano, estimate_costs, recalculate_nano, verification


class PricingTests(unittest.TestCase):
    def test_exact_conversion_and_negative_adjustment(self):
        self.assertEqual(credits_from_nano(1), Decimal("0.000000001"))
        self.assertEqual(credits_from_nano(-500000000), Decimal("-0.5"))

    def test_recalculation_precision_and_tolerance(self):
        state, value, _ = recalculate_nano('[{"tokenCount":"3","costPerBatch":"0.1","batchSize":"2"}]')
        self.assertEqual(state, "calculated")
        self.assertEqual(value, Decimal("0.15"))
        self.assertEqual(verification(0, '[{"tokenCount":"1","costPerBatch":"0.5","batchSize":"1"}]')[0], "verified")
        self.assertEqual(verification(0, '[{"tokenCount":"1","costPerBatch":"0.5000001","batchSize":"1"}]')[0], "mismatch")

    def test_unquoted_high_precision_json_numbers_remain_exact(self):
        state, value, _ = recalculate_nano(
            '[{"tokenCount":3,"costPerBatch":0.123456789123456789123456789,"batchSize":7}]'
        )
        self.assertEqual(state, "calculated")
        self.assertEqual(value, Decimal(3) * Decimal("0.123456789123456789123456789") / Decimal(7))

    def test_json_boolean_is_not_numeric(self):
        self.assertEqual(recalculate_nano('[{"tokenCount":true,"costPerBatch":2,"batchSize":1}]')[0], "invalid_json")

    def test_invalid_missing_zero_batch_and_nonfinite(self):
        self.assertEqual(recalculate_nano(None)[0], "missing_json")
        self.assertEqual(recalculate_nano("{")[0], "invalid_json")
        self.assertEqual(recalculate_nano('[{"tokenCount":1,"costPerBatch":2,"batchSize":0}]')[0], "invalid_json")
        self.assertEqual(recalculate_nano('[{"tokenCount":"NaN","costPerBatch":2,"batchSize":1}]')[0], "invalid_json")

    def test_unknown_model_suppresses_total(self):
        result = estimate_costs({"known": Decimal("1"), "unknown": Decimal("2")}, {"known": "0.5"})
        self.assertEqual(result["per_model_usd"]["known"], Decimal("0.5"))
        self.assertIsNone(result["per_model_usd"]["unknown"])
        self.assertIsNone(result["total_usd"])

    def test_official_default_credit_rate_prices_every_model(self):
        result = estimate_costs(
            {"gpt-5.6-luna": Decimal("2"), "future-model": Decimal("3")},
            {}, {"code": "EUR", "usd_rate": "0.9"}, default_rate="0.01",
        )
        self.assertEqual(result["per_model_usd"]["gpt-5.6-luna"], Decimal("0.02"))
        self.assertEqual(result["per_model_usd"]["future-model"], Decimal("0.03"))
        self.assertEqual(result["total_usd"], Decimal("0.05"))
        self.assertEqual(result["secondary_currency_code"], "EUR")
        self.assertEqual(result["total_secondary"], Decimal("0.045"))
