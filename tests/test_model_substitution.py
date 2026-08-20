"""Output is attributed to the model that generated it, never to the one
that was asked for.

MEASURED, not hypothesised. LM Studio answers a request for a model it
does not have with HTTP 200 and whatever is currently loaded. On this
machine a request for `definitely/not-a-real-model-xyz` was answered by
`google/gemma-4-26b-a4b-qat`, with no error, no warning, and a perfectly
ordinary-looking completion.

Before this, `stats['model']` was set to the REQUESTED id before the call
went out and was never corrected afterwards, even though the response body
names the model that actually answered. Any per-model outcome comparison
built on that field was comparing a fiction: rows labelled with a model
that never ran.

The requested and actual identities are now both preserved, the mismatch is
flagged, and attribution follows reality. A caller that requires a specific
model can act on `model_substituted`; one that just wants any local model
gets its answer, with provenance that admits what produced it.
"""
import unittest
from unittest.mock import MagicMock, patch


def _attribute(requested, served):
    """Run the REAL reconciliation, not a re-implementation of it.

    An earlier version of this file drove the whole HTTP path to reach the
    same three lines, which made the test about transport and tripped the
    hermetic-network guard. The neighbouring test_model_attribution goes
    the other way and re-implements the capture inside the test -- that one
    would pass even if the production code were deleted. This calls the
    function that actually runs in production.
    """
    from lib.lmstudio import record_model_attribution
    stats = {"requested_model": requested, "model": requested}
    return record_model_attribution(stats, served, "lmstudio")


class AttributionFollowsTheResponseTests(unittest.TestCase):

    def test_a_matching_model_is_not_flagged(self):
        s = _attribute("model-a", "model-a")
        self.assertEqual(s["requested_model"], "model-a")
        self.assertEqual(s["actual_model"], "model-a")
        self.assertFalse(s["model_substituted"])
        self.assertEqual(s["model"], "model-a")

    def test_a_substituted_model_is_flagged(self):
        s = _attribute("model-requested", "model-actual")
        self.assertTrue(s["model_substituted"],
                        "a different model answered and nothing said so")
        self.assertEqual(s["requested_model"], "model-requested")
        self.assertEqual(s["actual_model"], "model-actual")

    def test_output_is_attributed_to_what_actually_ran(self):
        """THE HEADLINE. Never label a row with a model that did not
        generate it."""
        s = _attribute("model-requested", "model-actual")
        self.assertEqual(
            s["model"], "model-actual",
            "output was attributed to the REQUESTED model, so per-model "
            "outcome comparison would be measuring a fiction")

    def test_both_identities_survive_so_the_swap_is_auditable(self):
        s = _attribute("asked", "answered")
        self.assertEqual(s["requested_model"], "asked")
        self.assertEqual(s["actual_model"], "answered")

    def test_a_substitution_is_timestamped(self):
        s = _attribute("asked", "answered")
        self.assertIn("model_substitution_at", s)
        self.assertNotIn("model_substitution_at",
                         _attribute("same", "same"))

    def test_a_caller_can_detect_the_mismatch_without_string_compare(self):
        """A caller with a model contract acts on the flag, not on its own
        re-derivation of the comparison."""
        s = _attribute("contract-model", "something-else")
        self.assertIs(s["model_substituted"], True)


class TheProviderStayingSilentIsNotTreatedAsAgreementTests(unittest.TestCase):

    def test_a_response_without_a_model_field_does_not_claim_a_match(self):
        """A provider that names no model has not confirmed anything. It
        must not read as 'requested == served'."""
        s = _attribute("asked", None)
        self.assertIsNone(s["actual_model"])
        self.assertFalse(s["model_substituted"])
        # And attribution must not silently keep claiming the requested id
        # as though it were confirmed.
        self.assertEqual(s["requested_model"], "asked")


class TheProductionPathUsesThisFunctionTests(unittest.TestCase):
    """A pure function nobody calls proves nothing."""

    def test_call_lm_studio_records_attribution_through_it(self):
        import inspect
        from lib import lmstudio as LM
        # The reconciliation happens on the RESPONSE path, which is
        # _call_openai_compat -- call_lm_studio only dispatches to it.
        body = inspect.getsource(LM._call_openai_compat)
        self.assertIn("record_model_attribution(stats, served", body,
                      "the OpenAI-compatible response path no longer routes "
                      "attribution through record_model_attribution")


if __name__ == "__main__":
    unittest.main()
