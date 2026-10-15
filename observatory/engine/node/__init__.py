"""The agent-network node: the client half of fabric-relay/0.1 (RM-21, OBS-107).

Ports of the contract kit (`passioncode-ai/fabric-agent-contract`, `src/canonical.ts`, `src/p256.ts`, `src/relay.ts`)
checked against the contract's own vectors in `fabric/node-vectors/`. Signing and verification need the engine's `full`
extra (`cryptography`); key validation and canonical JSON are the standard library alone.
"""

import logging as _logging

# A library logs; the program that runs it decides where the lines go (no output when it configured nothing).
_logging.getLogger('observatory.node').addHandler(_logging.NullHandler())
