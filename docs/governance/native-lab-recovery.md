# Narrow recovery of an unstarted native lab task

The actual subscription lab exposed an operator setup omission: the future-worker template had not been registered. The manager recorded an uncertain `start` before any hierarchy assignment, native reservation, session or process existed. Its admission reservation correctly remained uncertain. No model ran in that attempt.

`ContinuousHost.requeue_verified_unstarted(owner, action_key)` is a trusted local operator interface, not an HTTP endpoint or generic uncertain-job retry. It requires a live owner and the original manager/task/approval/configuration, an unchanged policy, current role/template/provider bindings, a clean approved source base, no execution-intent/effect records, required bounded diagnostics, an absent tick guard, and a leader explicitly released with zero expiry. Elapsed leases do not satisfy these checks.

The owner proof, queue transition and single-use admission proof consumption are atomic. Ordinary budget completion still preserves uncertainty. Attempt counters are retained; later execution must pass normal admission and retry limits. The recovery method never starts a transport or runs a model.

Historical runtime provenance remains a prerequisite verified independently by the trusted operator. Current `CodexHost` type alone cannot prove which runtime handled a previous request. The audit explicitly says `historical_runtime_auto_verified=False`. The isolated lab preserved its original private runner configuration pinned to commit `790263a85b97a02b3ebd7942b6c996a6e30a6e51` and the original no-effect attempt; this evidence must not be invented for other jobs. Unknown-runtime cases and all dispatch/execution uncertainties remain outside this narrow recovery claim.

Subscription enrollment uses the existing single canonical authority and add-only registration. Customer projects remain inactive; the synthetic lab has its own two role profiles and repository. Native execution, when attempted, consumes the existing subscription and is distinct from paid API-key calls. No persistent service or operational deployment follows from this lab.
