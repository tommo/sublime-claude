"""Entry point for `python -m grok_proxy`."""
import sys

from . import server

if __name__ == "__main__":
    sys.exit(server.main())
