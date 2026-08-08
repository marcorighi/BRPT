"""Dependency-free smoke tests for the BRPT engine."""

import unittest

from brpt import brpt


class BRPTSmokeTests(unittest.TestCase):
    def test_small_primes(self):
        for n in (2, 3, 5, 7, 11, 17, 97):
            with self.subTest(n=n):
                self.assertTrue(brpt(n))

    def test_small_composites(self):
        for n in (1, 4, 6, 9, 15, 21, 25, 49):
            with self.subTest(n=n):
                self.assertFalse(brpt(n))

    def test_known_base2_pseudoprimes_are_rejected(self):
        # 341 is a base-2 Fermat pseudoprime; 561, 1105 and 1729 are
        # Carmichael numbers. These are regression checks, not an exhaustive
        # validation campaign.
        for n in (341, 561, 1105, 1729):
            with self.subTest(n=n):
                self.assertFalse(brpt(n))


if __name__ == "__main__":
    unittest.main()
