"""Antithesis workload library for the PXC harness.

Every Antithesis SDK assertion call in this project lives in ``oracles.py``,
with its property name as an inline constant string literal. Nothing else in
this package -- and nothing in the test command files under
``antithesis/test/pxc/`` -- may call the SDK assertion API.

Two reasons, both hard requirements rather than style:

1. Assertion cataloging statically scans ``/opt/antithesis/catalog`` (a one-hop
   symlink to ``/opt/antithesis/workload``) before any run. Anything under
   ``/opt/antithesis/test/`` is OUTSIDE that tree, so an assertion written in a
   command file is never cataloged -- and an uncataloged reach claim that is
   never hit is invisible rather than failing, which would silently destroy the
   only signal this workload has for whether it reaches its targets.
2. Keeping all property names in one file makes project-wide uniqueness
   auditable by eye.
"""
