# Offline order validation

Base under test: PR #19 commit `a5349a95dd17d2d9a0eefdd060805fc7b02e70bd`. These changes are prepared on a separate branch; they are not a deployment.

## Evidence

The test workbook supplied 3,951 sampled ledger records. Of those, 3,875 were funeral, wedding or general-event records with a location and sender. Five syntax forms produced **19,375 ledger-derived messages**. They are generated input variations, not 19,375 independently observed Kakao conversations.

| Check | Before | After |
| --- | ---: | ---: |
| Exact expected-field matches | 6,107 / 19,375 | 19,369 / 19,375 |
| Strict mismatches after fixes | — | 6 |
| Information-preserving field normalization among those mismatches | — | 6 |
| Synthetic regional-address cases | — | 5,100 / 5,100 |
| Ambiguous/conflicting region cases held | — | 2,000 / 2,000 |
| Invalid delivery date/time cases held | — | 1,000 / 1,000 |

The six strict mismatches involve a nested recipient label and a combined venue/address cell. Their content is retained after label removal or field separation; they remain visible as strict mismatches rather than being removed from the denominator.

Only **12 recorded raw Kakao messages** were available. A sender-only check with *stubbed invitation metadata* matched five and held seven. Six held inputs use unsupported invitation hosts; one has invitation narrative and a sender signature requiring more context. These are not seven successful interpretations. Live invitation extraction and end-to-end chat delivery were not tested. External reads of these customer-specific URLs were blocked by automatic approval review; permission was requested and has not been assumed.

## What changed

- Preserve multiline sender inscriptions, messages and addresses.
- Accept compact/spaced labels, fullwidth colons, label-on-previous-line inputs, and delimited inline labels.
- Reject conflicting repeated fields instead of silently choosing the last value.
- Do not interpret a URL printed in an inscription/message/address as an event invitation.
- Ignore standalone greetings when selecting an unlabeled sender.
- Add a regional delivery notice to order reports and ledger notes.
- Align with PR #21: column C is event time, never shipping time. Keep separately provided shipping time in notes and preserve event time during staff edits.

Regional alert scope defaults to **outside Seoul/Gyeonggi/Incheon**, pending the user's clarification. `TAESAN_REGIONAL_SCOPE=outside_gyeonggi` narrows the exempt area to Gyeonggi only. The code reads location fields, not sender/customer names. Ambiguous `광주시`, `고성군`, venue names without addresses, and conflicting regions stay unknown. The notice requests delivery/installation/fee review; it never adds a fee or assigns a driver.

## Run

```sh
python -m unittest discover -s tests -v
python validation/edge_cases.py /tmp/edge-report.json
python validation/mass_orders.py --source /path/to/private/source-orders.json --report /tmp/order-report.json
python validation/raw_sender_checks.py /path/to/private/source-orders.json /tmp/raw-report.json
```

The private input file must stay outside the repository. No customer raw text, names, addresses, phone numbers or invitation URLs are committed. Aggregate results are in `results-2026-11-14.json`.

This suite does not establish that arbitrary Kakao language is fully supported. Add genuinely observed, human-labeled failures separately from generated variations. Deployment, source-customer binding, invitation vendor support, UI capture and target-chat delivery still require integration validation in the test workbook and planning room.

## Deployment recheck

The 09:20 Asia/Seoul recheck found Render deploy `dep-dbrqg26q1p3s73e3lgmg` live at commit `b3a514387d1a086d6cdf9b38b1821010be924f3d`, finished 09:16:11. The public health endpoint returned `{"status":"ok"}`. This is a partial deployment with automatic ledger writes disabled. PR #19 remained open. PR #21 (`b43c6d484f093e3e4c0b9768dc7cf1ad540fb873`) separately corrects event/shipping time; its verified ledger changes were incorporated locally with the regional notice. No remote merge or deployment was performed here.

Branch upload was rejected by automatic approval review because external-repository upload authorization/trust was not confirmed. A user decision was requested. The local commits and review patch are retained; no workaround upload was attempted.
