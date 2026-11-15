"""Compatibility import for the shared experiment runner utilities.

Controller entry points select backend-specific paths; this module has no
independent defaults, logger, scheduler, or mutable state.
"""

import sys
import run_funs as _shared

sys.modules[__name__] = _shared
