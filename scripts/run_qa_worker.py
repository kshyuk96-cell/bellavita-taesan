"""Run one approved QA job. Host config and private credential file are required."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager_bridge.control import secret
from manager_bridge.qa_runtime import QAProfile, QARegistry
from manager_bridge.qa_worker import QAClient, QAWorker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--token-file',type=Path,required=True)
    parser.add_argument('--job',required=True)
    args = parser.parse_args()
    # File is a trusted operator input, never an inbox attachment or child output.
    config = json.loads(args.config.read_text())
    registry = QARegistry([QAProfile(**row) for row in config['profiles']])
    stop = threading.Event()
    for sig in (signal.SIGINT,signal.SIGTERM):
        signal.signal(sig,lambda *_:stop.set())
    try:
        result = QAWorker(QAClient(config['manager_url'],secret(args.token_file)),registry,stop=stop).run_once(args.job)
    except Exception:
        result = dict(state='configuration_or_connection_unverified',retry_performed=False)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['state']=='accepted' else 2

if __name__ == '__main__':
    raise SystemExit(main())
