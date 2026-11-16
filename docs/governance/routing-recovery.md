# Routing and recovery diagnostics

`GovernanceRouting(core, controller=None).plan(token, projects)` reads the same registered core and optional continuous controller. Owners and managers must have access to every requested project. It does not invoke providers, poll a remote source, start a worker, approve a proposal, or retry an action.

A ready plan means the stored observations currently show distinct live members, available capacity, durable governance enrollment, a matching QA policy and registered provider, and a source observation within 300 seconds. The execution core still performs its own authorization and freshness checks. Source kind and origin alone do not prove a real remote fetch. Fleet host/template references describe registrations; they do not prove delivery or host health. Persistent host health remains `unmeasured`.

The recovery section reads durable leader and action records after restart. An expired relevant leader, an uncertain action, or a running action without a relevant live leader requires reconciliation. The plan does not replay these actions. Use the controller's exact reconciliation operation with the existing action key and current authorized manager; the controller and trusted host must verify the prior execution before any subsequent work. A registered owner proposal or business batch remains separate from technical acceptance and operational authorization.

Member results omit credential hashes. Portfolio views should project only readiness, reason codes, and eligible role counts when member identities are unnecessary.
