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

The expanded suite uses **13 syntax variations per eligible record: 50,375 generated cases**. Exact expected-field matches: **50,361**. The remaining **14 strict differences** preserve information through nested-label removal or venue/address separation. They remain counted as mismatches. These are generated variations, not independently observed conversations.

The user authorized external invitation reads. **12 recorded raw orders referencing 9 unique actual pages** now parse with sender text matching all 12 references. All **10 available reference wedding dates and times** match independently stored sheet values. Another wedding has no reference date/time, so those values are not independently verified. Funeral departure is never substituted for delivery time. Replay uses saved real HTML and the vendor's read-only wedding-view response, not fabricated invitation metadata. No customer page or payload is published.

The earlier sender-only stub run (5 matched, 7 held) is retained as historical baseline in the JSON report; it is superseded by the real-page checks above. **36 unit tests** pass. No chat posting or sheet write was performed by this validation suite.

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
python validation/cached_invitation_checks.py /path/to/private /tmp/actual-page-report.json
```

The private input file must stay outside the repository. No customer raw text, names, addresses, phone numbers or invitation URLs are committed. Aggregate results are in `results-2026-11-14.json`.

This suite does not establish that arbitrary Kakao language is fully supported. Add genuinely observed, human-labeled failures separately from generated variations. Deployment, source-customer binding, invitation vendor support, UI capture and target-chat delivery still require integration validation in the test workbook and planning room.

## Deployment recheck

The 09:20 Asia/Seoul recheck found Render deploy `dep-dbrqg26q1p3s73e3lgmg` live at commit `b3a514387d1a086d6cdf9b38b1821010be924f3d`, finished 09:16:11. The public health endpoint returned `{"status":"ok"}`. This is a partial deployment with automatic ledger writes disabled. PR #19 remained open. PR #21 (`b43c6d484f093e3e4c0b9768dc7cf1ad540fb873`) separately corrects event/shipping time; its verified ledger changes were incorporated locally with the regional notice. No remote merge or deployment was performed here.

The user subsequently authorized invitation reads and feature-branch publication. Changes are submitted against PR #19’s branch for review, not as a production release. The live automatic-ledger-write guard must remain disabled until integration checks succeed. Customer identity binding, historical defaults, source-room capture and confirmed group delivery remain unverified integration work.
