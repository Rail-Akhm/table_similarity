#!/usr/bin/env python3
"""Direct entry point for tablefp CLI - no installation required.

Usage:
    python run_tablefp.py --help
    python run_tablefp.py index --config config/config.yaml
    python run_tablefp.py search fields.xlsx --config config/config.yaml
"""

import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from tablefp.cli import cli

if __name__ == "__main__":
    cli()