# Customer QA requirements from current source

These are source-bound test declarations and independent expected oracles. No customer test, package script, application, database, browser, payment or message was executed to prepare them. Every formula and scenario remains **unmeasured**, with `business_admitted=false` and no customer UI verification.

| Project | Inspected source revision | Source-backed requirements |
|---|---|---|
| Bellavita Shop | `d5047f3345b757edfcfbd81a73c609c0392007dd` | Server price and options, refunds, cash receipt components, fulfillment ordering, replay and origin checks |
| Funeral Association | `42cfeedee9e0fbc1ffc90603b86d4fb5ed0e9e84` | Server price snapshots, duplicate orders, proof-gated state changes, family privacy and installation schedule boundaries |
| Bellavita ERP, recovered Sites v28 | `13f78626e415c67ce2d07424f5ba5c1351666581` | Customer price, quantity settlement, nonnegative stock, finance roles and replay, fee sequencing and parcel/installation separation |
| Hope Sunrise, recovered Sites v37 | `985a918f55f3cd2fbf0445d6ef38c761332577d2` | Adult/child/extra pricing, stock and state restrictions, private APIs, duplicate orders, sandbox club allocation and reward recovery |

Older Sites exports must be labeled `stale` and cannot register review commands against these pins. A source snapshot does not itself establish live service connectivity or deployment status.

## Exact command inventory

`manager_bridge.customer_qa_plan.COMMANDS` is the canonical registry. Commands are fixed argument lists; package scripts and customer configuration are never evaluated by the inspection function.

| Project | Label | Exact argv |
|---|---|---|
| Shop | `orders` | `/usr/bin/python3 -m unittest discover -s tests -p test_shop.py` |
| Shop | `operations` | `/usr/bin/python3 -m unittest discover -s tests -p test_order_operations.py` |
| Shop | `settlement` | `/usr/bin/python3 -m unittest discover -s tests -p test_shop_delivery_suite.py` |
| Funeral | `unit` | `/usr/bin/node --import tsx --test tests/validation.test.ts tests/scenario-matrix.test.ts` |
| Funeral | `orders-review` | `/usr/bin/node node_modules/@playwright/test/cli.js test tests/browser/orders.spec.ts` |
| ERP | `business` | `/usr/bin/node --test tests/business.test.js tests/settlement.test.js` |
| ERP | `finance` | `/usr/bin/node --test tests/finance.test.js` |
| Hope | `orders` | `/usr/bin/node --test tests/orders.test.mjs` |
| Hope | `club-ledger` | `/usr/bin/node --test tests/club.test.mjs tests/club-reward-settlement.test.mjs` |

Runtime prerequisites are separate from discovering these commands. Shop needs its Python dependencies, isolated SQLite and local HTTP fixtures with outbound providers mocked. Funeral needs its Node/tsx dependency resolution, isolated local PostgreSQL, Next review server and Playwright runtime. ERP and Hope need Node with `node:sqlite` and isolated database helpers. The declared `/usr/bin/node` is absent on the current cloud host. Its installed Node binary is outside the runner's mounted paths and exceeds the current runtime-file size bound; the runner's memory budget is also unverified for Node. These are target-image declarations, with `runtime_verified=false` and `execution_ready=false`, rather than an execution-ready profile. No binary was copied or engine limit changed for this plan.

Customer code may run only through the registered fixed review runner and verified isolation; declaring a command does not attest these prerequisites.

## Required calculation oracles

These 14 definitions include inputs, literal expected values, units, rounding rules and an exact source-file hash/reference in the frozen plan. The source assertions were read; their execution has not been observed.

| Project | Required formula | Frozen independent expected oracle |
|---|---|---|
| Shop | `price-plus-option` | `99000 + 10000 = 109000` KRW, from `tests/test_shop.py` |
| Shop | `refund-remaining` | `93500 - 10000 - 5000 = 78500` KRW, from `tests/test_shop.py` |
| Shop | `cash-target` | `93500 - 10000 = 83500` KRW, from `tests/test_order_operations.py` |
| Shop | `cash-tax-exempt` | Configured `TG02`: tax is `0`, from `tests/test_order_operations.py` |
| Shop | `cash-tax-conservation` | Configured `TG01`: supply plus tax equals original `93500`, from `tests/test_order_operations.py` |
| Shop | `vat-taxable-split` | Configured `TG01`, inclusive total `93500`: supply `85000`, tax `8500`. Independently evaluated literal arithmetic for reviewed `cash_receipts.py` integer rule `(total+5)//11`; no floating point and no tax assumption for unconfigured goods |
| Funeral | `server-price-snapshot` | Receipt retains catalog `123000` KRW despite client price `1`, from `tests/browser/orders.spec.ts` |
| Funeral | `installation-due-boundary` | Exactly `21600000` milliseconds (six hours) is `soon`, not `planned`, from `tests/validation.test.ts` |
| ERP | `customer-quantity-total` | `2 * 95000 = 190000` KRW, from `tests/settlement.test.js` |
| ERP | `stock-nonnegative` | Stock `3`, withdrawal `-1`: stock `2`; insufficient stock is rejected, from `tests/business.test.js` |
| Hope | `trip-total` | Lines `[2000000,500000,300000,40000,20000]` sum to `2860000` KRW, from `tests/orders.test.mjs` |
| Hope | `free-child-total` | Two adults at `1000000`, configured free child: `2000000` KRW, from `tests/orders.test.mjs` |
| Hope | `club-first-allocation` | Sandbox initial fee `75000` allocates `DP50000 + BP25000`; no real redemption/settlement claim, from `tests/club.test.mjs` |
| Hope | `reward-recovery` | Original sandbox reward `30000`, already spent `20000`: recover only `10000`; retain spent review amount and create no real debt, from `tests/club-reward-settlement.test.mjs` |

The reviewed scope contains no source-backed tax-component feature contract for ERP, Hope or Funeral. Their tax assessment is **not_assessed**, rather than passed or an invented mandatory VAT requirement. Any later actual tax feature must add its reviewed specification, required formulas and independent observations before business acceptance.

## Required risk groups and state criteria

All four plans require P0 price integrity, authority separation and idempotency, plus P1 order/workflow states. Shop, ERP and Hope additionally require P0 financial conservation. Shop requires P0 configured tax components; Funeral requires the source-backed P1 installation schedule group.

The plans contain 31 scenario definitions. Shop refuses skipping directly to delivery, allows the confirmed/shipping/delivered sequence, snapshots server prices, and keeps refunds and settlement amounts consistent. Funeral refuses received-to-installed bypasses, requires production/install proof, preserves one order under replay and hides internal notes from family receipts. ERP confirms fees before payment and keeps parcel completion from overwriting installation status; finance role checks and concurrent approvals cannot duplicate obligations or claim actual transfers. Hope cannot claim a paid order state, accepts only exact server totals and keeps replay from duplicating orders; club rules remain explicitly sandbox rules.

Missing or changed required assertions/formulas produce explicit `not_available` blockers for business requirements. A passing process exit, a large test count, a skipped test, a prepared profile or an owner command registration cannot replace independently measured scenario/formula coverage.

## Interface and admission boundary

```python
plan = inspect_customer_qa_plan(
    project, physical_checkout,
    expected_source_sha=trusted_current_sha,
    config_fingerprint=registered_project_fingerprint,
)
contract = register_customer_review_commands(
    owner_token, registry, plan,
    contract_version="review-v1", provider_identity="registered-independent-review",
)
```

Inspection hashes only bounded pinned source references and uses the existing hardened Git reader. It refuses physical-path symlinks and source drift, refuses executable Git filters and disables hooks/fsmonitor, and rechecks HEAD/clean status after reading. Registration authorizes the owner before and after inspection, rechecks the current source and project configuration, and durably preserves the exact plan/command digest per version. It returns the existing `ProjectQAContract`; it neither starts a review nor registers a passing business report.

`definition_ready` means that the required definitions are present. `mechanical_registration_ready` means that source-bound command preparation is available. Neither means execution succeeded. Missing business oracles may block business acceptance while mechanical development-copy preparation continues. A future trusted customer observation adapter must supply actual measured values for `plan.expected_contract`, and normal universal QA admission must enforce the required groups/formulas before any business acceptance claim.
