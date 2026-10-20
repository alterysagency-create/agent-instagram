"""Explicit live Anthropic smoke test; importing this file makes no paid call."""
from check_connections import main

if __name__ == '__main__':
    import sys
    sys.argv.append('--anthropic')
    raise SystemExit(main())
