"""Punto de entrada: `python3 compiler/sourc.py <comando>`.

Se ejecuta como script y no como paquete, así que se ajusta sys.path para que
`from sourc import ...` funcione igual en los dos casos. En la Fase 8 esto
desaparece: el compilador estará escrito en Source.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sourc.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
