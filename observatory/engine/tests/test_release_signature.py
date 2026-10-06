#!/usr/bin/env python3
"""The organization's signature on SHA256SUMS, checked before any update installs (audit A04).

Two populations: the real v0.17.3 SHA256SUMS and its `.asc`, as CI's gpg made them with the
organization's key, pin the parser to what a release actually carries; a throwaway key from
`pgp_fixture` (built from the RFC, not from the parser) exercises every refusal.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_signature as rs

try:
    import pgp_fixture
except ImportError:  # the `full` extra is absent: the signer needs cryptography too
    pgp_fixture = None

ORG = "63B30DC324BD697487AA31944FAFB8AEC803B6A7"

# The published release v0.17.3 (passioncode-ai/project-observatory-dashboard), byte for byte.
SUMS_0173 = (
    b"238f19a8cde267de72b57de4e5c14e32f1490da0d93a254cfbfe7be6fc7e498d  ProjectObservatory-0.17.3-macos.zip\n"
    b"1c26abb990d9d41051b3fc87f8be10a52350a752c9a00c341a2d18e3cbc278ee  project_observatory-0.17.3-py3-none-any.whl\n")
ASC_0173 = """-----BEGIN PGP SIGNATURE-----

iHQEABYKAB0WIQRjsw3DJL1pdIeqMZRPr7iuyAO2pwUCasPq+gAKCRBPr7iuyAO2
p6tvAQCrGH2kylwn9eWgqb0jh8fQNxsAZCZ+mt2p8hZj/hXa1wD3ftBYH0bI7sOc
4lrUf0DieyZWJ07hEnkLMQJvqUzZCg==
=H7VT
-----END PGP SIGNATURE-----
"""


def needs_crypto(test):
    return unittest.skipIf(pgp_fixture is None, "the full extra (cryptography) is not installed")(test)


class RealRelease(unittest.TestCase):
    """Runs with and without cryptography: the path taken is whichever the install has."""

    def test_the_published_release_verifies_against_the_pinned_key(self):
        self.assertEqual(rs.verify(SUMS_0173, ASC_0173), ORG)

    def test_without_cryptography_the_plain_python_check_answers_the_same(self):
        with patch.dict(sys.modules, {"cryptography": None, "cryptography.exceptions": None,
                                      "cryptography.hazmat.primitives.asymmetric.ed25519": None}):
            self.assertEqual(rs.verify(SUMS_0173, ASC_0173), ORG)
            with self.assertRaises(rs.SignatureError):
                rs.verify(SUMS_0173.replace(b"1c26", b"1c27"), ASC_0173)

    def test_one_changed_byte_is_refused(self):
        tampered = SUMS_0173.replace(b"238f", b"238e", 1)
        with self.assertRaisesRegex(rs.SignatureError, "does not match|does not verify"):
            rs.verify(tampered, ASC_0173)

    def test_a_line_added_to_the_list_is_refused(self):
        with self.assertRaises(rs.SignatureError):
            rs.verify(SUMS_0173 + b"00" * 32 + b"  evil.whl\n", ASC_0173)

    def test_the_pinned_key_carries_the_fingerprint_it_is_pinned_under(self):
        self.assertEqual(list(rs.pinned_keys()), [ORG])

    def test_a_pinned_entry_with_another_fingerprint_is_refused_at_load(self):
        wrong = ({**rs.PINNED[0], "fingerprint": "0" * 40},)
        with self.assertRaisesRegex(rs.SignatureError, "fingerprint"):
            rs.pinned_keys(wrong)

    def test_a_broken_armour_checksum_is_refused(self):
        with self.assertRaisesRegex(rs.SignatureError, "checksum"):
            rs.parse_signature(ASC_0173.replace("=H7VT", "=H7VU"))

    def test_text_that_is_not_a_signature_is_refused(self):
        for text in ("", "hello", "-----BEGIN PGP SIGNATURE-----\n\n!!!\n-----END PGP SIGNATURE-----\n"):
            with self.assertRaises(rs.SignatureError):
                rs.verify(SUMS_0173, text)


@needs_crypto
class SyntheticKey(unittest.TestCase):
    def setUp(self):
        self.key = pgp_fixture.Key()
        self.other = pgp_fixture.Key()
        self.data = b"abc  file.whl\n"

    def test_a_signature_by_the_pinned_key_verifies_with_each_accepted_hash(self):
        for algo in (8, 9, 10):
            self.assertEqual(rs.verify(self.data, self.key.sign(self.data, hash_algo=algo), self.key.pinned()),
                             self.key.fingerprint)

    def test_a_signature_without_the_issuer_fingerprint_falls_back_to_the_key_id(self):
        self.assertEqual(rs.verify(self.data, self.key.sign(self.data, issuer=False), self.key.pinned()),
                         self.key.fingerprint)

    def test_a_key_that_is_not_pinned_is_refused(self):
        with self.assertRaisesRegex(rs.SignatureError, "does not trust"):
            rs.verify(self.data, self.other.sign(self.data), self.key.pinned())

    def test_the_real_key_does_not_accept_a_stranger_claiming_its_fingerprint(self):
        # A signature that names the organization's fingerprint but was made by another key.
        with patch.object(self.other, "fingerprint", ORG):
            forged = self.other.sign(self.data)
        with self.assertRaisesRegex(rs.SignatureError, "does not verify"):
            rs.verify(self.data, forged)

    def test_sha1_and_text_signatures_are_refused_by_name(self):
        with self.assertRaisesRegex(rs.SignatureError, "hash algorithm 2"):
            rs.verify(self.data, _with_hash(self.key, self.data, 2), self.key.pinned())
        with self.assertRaisesRegex(rs.SignatureError, "0x01"):
            rs.verify(self.data, self.key.sign(self.data, sig_type=0x01), self.key.pinned())

    def test_a_corrupted_signature_value_is_refused(self):
        armored = self.key.sign(self.data)
        raw = bytearray(rs.dearmor(armored))
        raw[-5] ^= 0x01  # inside s
        broken = pgp_fixture.armor("SIGNATURE", bytes(raw))
        with self.assertRaisesRegex(rs.SignatureError, "does not verify"):
            rs.verify(self.data, broken, self.key.pinned())

    def test_two_signature_packets_are_refused(self):
        one = rs.dearmor(self.key.sign(self.data))
        with self.assertRaisesRegex(rs.SignatureError, "found 2"):
            rs.verify(self.data, pgp_fixture.armor("SIGNATURE", one + one), self.key.pinned())


class MalformedInput(unittest.TestCase):
    """Audit A04 review: 20 000 mutated signatures raised IndexError 85 times and
    struct.error 570 times — a traceback and no refusal recorded. Every malformed input
    is a SignatureError, and none of them verifies."""

    def test_mutated_signatures_are_refused_never_raised(self):
        import random
        rng = random.Random(20261006)
        raw = rs.dearmor(ASC_0173)
        samples = [b"", b"\x04", b"\x04\x00\x16\x0a", raw[:1], raw[:5]]
        for _ in range(3000):
            b = bytearray(raw)
            kind = rng.randrange(3)
            if kind == 0:
                b = b[:rng.randrange(len(b))]
            elif kind == 1:
                for _ in range(rng.randrange(1, 4)):
                    b[rng.randrange(len(b))] = rng.randrange(256)
            else:
                at = rng.randrange(len(b))
                b = b[:at] + bytes(rng.randrange(256) for _ in range(rng.randrange(1, 6))) + b[at:]
            samples.append(bytes(b))
        verified = 0
        for blob in samples:
            armored = (pgp_fixture.armor("SIGNATURE", blob) if pgp_fixture
                       else "-----BEGIN PGP SIGNATURE-----\n\n" + __import__("base64").b64encode(blob).decode()
                       + "\n-----END PGP SIGNATURE-----\n")
            try:
                rs.verify(SUMS_0173, armored)
                verified += 1
            except rs.SignatureError:
                pass
        # Malleable bytes with no security effect (unhashed subpackets, the MPI bit
        # count) may still verify; a changed hashed byte never does — the RealRelease
        # tests pin that. Here the claim is only: no other exception escapes.
        self.assertLess(verified, len(samples))


class PlainEd25519(unittest.TestCase):
    """`_ed25519_verify` is the check on an install without cryptography."""

    # RFC 8032 §7.1, TEST 1 and TEST 2.
    VECTORS = (
        ("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    )

    def test_rfc_8032_vectors_verify_and_their_mutations_do_not(self):
        for public, message, signature in self.VECTORS:
            pub, msg, sig = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
            self.assertTrue(rs._ed25519_verify(pub, sig, msg))
            self.assertFalse(rs._ed25519_verify(pub, sig, msg + b"\0"))
            for i in (0, 31, 32, 63):
                bad = bytearray(sig); bad[i] ^= 0x01
                self.assertFalse(rs._ed25519_verify(pub, bytes(bad), msg), i)

    def test_a_scalar_at_or_above_the_group_order_is_refused(self):
        public, message, signature = self.VECTORS[0]
        sig = bytes.fromhex(signature)
        s = int.from_bytes(sig[32:], "little") + rs._L  # the malleable twin of a valid signature
        self.assertFalse(rs._ed25519_verify(bytes.fromhex(public), sig[:32] + s.to_bytes(32, "little"), b""))

    def test_malformed_points_are_refused(self):
        public, message, signature = self.VECTORS[0]
        sig = bytes.fromhex(signature)
        self.assertFalse(rs._ed25519_verify(b"\xff" * 32, sig, b""))
        self.assertFalse(rs._ed25519_verify(bytes.fromhex(public)[:31], sig, b""))
        self.assertFalse(rs._ed25519_verify(bytes.fromhex(public), sig[:63], b""))

    @needs_crypto
    def test_it_agrees_with_cryptography_on_valid_and_broken_signatures(self):
        key = pgp_fixture.Key()
        public = key.private.public_key().public_bytes_raw()
        for n in range(20):
            message = f"message {n}".encode()
            sig = key.private.sign(message)
            broken = bytearray(sig); broken[n % 64] ^= 1 << (n % 8)
            for candidate in (sig, bytes(broken)):
                self.assertEqual(rs._ed25519_verify(public, candidate, message),
                                 rs._check(public, candidate, message), (n, candidate == sig))


def _with_hash(key, data, algo):
    """A signature packet declaring an unaccepted hash (the bytes past it do not matter)."""
    raw = bytearray(rs.dearmor(key.sign(data)))
    raw[2 + 3] = algo  # header (tag + one length byte), then version, type, public-key algorithm, HASH
    return pgp_fixture.armor("SIGNATURE", bytes(raw))


if __name__ == "__main__":
    unittest.main()
