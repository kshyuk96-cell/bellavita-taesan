# Report-only host cost stop review

The owner requires an explicit recommendation to stop unnecessary DigitalOcean costs if the governance effort ultimately fails, is abandoned or no longer needs its host. `host_cost_review` makes this reporting rule reproducible without granting deletion or billing authority.

The ten checks cover: explicit final-outcome reminders; powered-off resources that can still be billed; retained extra resources; blocked versus final-failed work; stale/future provider observations; preservation before deletion; decimal-only conditional cost projections; invalid rates/clocks/bounds; no inferred refunds or stopped charges; and a no-network CLI path.

Use `scripts/review_governance_host_cost.py` with an explicitly reported outcome and resource state. A failed test or SSH attempt must not be entered as a final governance-failure decision. The tool does not query DigitalOcean, cancel resources, send notifications, create a recurring monitor or automatically delete anything. Provider observations remain reported rather than independently verified. When their time is stale or future-dated, current resources and billing require verification. An explicit final decision still retains the cost-stop reminder.

The projection is quoted hourly USD multiplied by a bounded future window, assuming one Droplet stays billable for that entire window. It is not an elapsed-cost calculation, invoice, monthly-cap calculation or refund estimate. Taxes, extras and provider rounding/minimums are not included. For example, a quote of USD 0.036/hour gives a conditional USD 0.864 over 24 hours. No deployment or spending authority follows from that number.

DigitalOcean's official [pricing guidance](https://docs.digitalocean.com/products/droplets/details/pricing/) states that powered-off bundled CPU Droplets remain billable and destruction ends Droplet billing. Its [destruction guidance](https://docs.digitalocean.com/products/droplets/how-to/destroy/) notes that associated resources are not automatically all destroyed and retained chargeable resources can continue billing. Necessary evidence and recovery material must be preserved before owner-authorized deletion. A refund request is separate and never guaranteed.

This feature is an operator reporting aid, not a claim that failure is being watched outside a running task. Existing scheduled development, sheet inspection and reserved deployments are unchanged. The overall operating qualification remains incomplete while connectivity and persistent credentials are unverified.
