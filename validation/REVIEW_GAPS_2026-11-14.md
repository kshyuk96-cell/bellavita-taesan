# Additional review without Windows access

Implemented and tested locally:
- Require exactly one supported current invitation for link-only customer default enrichment. Empty commands and unsupported/multiple links cannot consume a historical default.
- Negative title cues (exclude, not this, cancel) are held before positive variant selection.
- An existing title variant ID cannot be silently transferred to another customer.
- Recognize explicit quantity labels. Non-single or ambiguous quantities are held before invitation network reads; a single quantity label cannot leak into the sender inscription.

Evidence: 62 unit tests pass. 5,600 synthetic context checks pass (200 bound defaults, 2,200 customer isolation holds, 2,000 negative-instruction holds, 1,200 quantity holds). The existing 50,375 generated syntax variations still yield 50,361 exact matches and 14 information-preserving normalization differences. Replayed actual invitation data still matches 12 sender references and 10 available wedding date/time references. These are parser checks, not confirmed sheet/chat delivery.

Still open: identifying free-form quantities; cancellations and amendments as durable order operations; receipt/source-event based duplicate detection. Current intake keys derive from canonical message text globally, so identical legitimate repeat orders and the same content forwarded by different employees require explicit source-event handling. Do not change that identity scheme without migration/replay tests, as old keys must remain discoverable.

No external upload, merge, deployment, sheet write or Kakao send was attempted in this review. Prior external upload approval remains unresolved. Keep these changes local pending publication authorization.
