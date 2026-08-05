import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from tablefp.cli import cli

if __name__ == "__main__":
    cli()
