"""APS V2 finite-capacity solver.

The modules in this package operate on immutable, serializable values.  They do
not read or write Frappe documents; database orchestration lives in
``solver_orchestration``.
"""

from .models import SolverInput, SolverSolution

__all__ = ["SolverInput", "SolverSolution"]
