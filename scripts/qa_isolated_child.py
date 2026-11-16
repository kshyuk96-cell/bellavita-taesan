"""Fixed sandbox entry point; no command/module/path supplied by a task."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from taesan_core.universal_qa import run_scenarios


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--suite', choices=['order-ledger-v1','universal-v1'], required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--seed', type=int, required=True)
    args = parser.parse_args()
    if args.suite == 'order-ledger-v1':
        from taesan_core.qa_order_ledger import suite, adapter
        scenarios, registrations, coverage = suite(), [adapter()], ['ledger-date-time-channel']
    else:
        from taesan_core.qa_fixtures import build_suite, adapters, REQUIRED_COVERAGE
        scenarios, registrations, coverage = build_suite(args.seed), adapters(), REQUIRED_COVERAGE
    report = run_scenarios(scenarios, registrations, source_revision=args.revision,
                           seed=args.seed, required_coverage=coverage)
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))

if __name__ == '__main__':
    main()
