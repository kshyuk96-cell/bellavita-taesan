"""Private nonproduction manager process. Credentials are provisioned externally."""
import argparse
from pathlib import Path
from .server import make_server
from .store import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--ready-file',type=Path,required=True)
    args=parser.parse_args()
    if not args.database.is_file() or args.database.is_symlink() or args.ready_file.exists():raise ValueError('existing dedicated database and fresh readiness path required')
    server=make_server(Store(args.database),port=0)
    # Only loopback port, never tokens, in this readiness file.
    args.ready_file.write_text(str(server.server_address[1]))
    try:server.serve_forever()
    finally:server.server_close()

if __name__=='__main__':main()
