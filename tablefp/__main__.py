"""Allow running as `python -m tablefp.cli`."""

from tablefp.cli import cli
import sys

if __name__ == "__main__":
    cli(sys.argv[1:])