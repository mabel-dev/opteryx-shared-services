"""Capabilities every Opteryx service needs, in one place.

Capabilities, each of which was previously copied into eight service repos and
had silently diverged there:

- ``opteryx_shared_services.config`` -- the platform configuration client.
- ``opteryx_shared_services.logging`` -- structured logging and the audit wire
  format the fleet's log ingestion selects on.
- ``opteryx_shared_services.preflight`` -- startup dependency checks, which fail
  a Cloud Run revision rather than letting an instance that cannot serve replace
  one that can.
- ``opteryx_shared_services.audit`` -- request audit middleware, and the payload
  shape the fleet's log ingestion reads.
- ``opteryx_shared_services.billing`` -- the billing rules every query front door
  enforces: the effective plan, the suspended/spend-cap block and the free
  tier's daily allowance.
- ``opteryx_shared_services.platform_mode`` -- the maintenance switch every
  service reads, and the matrix of what each one refuses in each mode.

Nothing is imported here. A service that needs only configuration must not pay
for a Firestore, GCS or Secret Manager import it never uses, so each capability
is imported by name::

    from opteryx_shared_services.config import get_config
    from opteryx_shared_services.logging import get_logger
    from opteryx_shared_services.preflight import Preflight
    from opteryx_shared_services.audit import AuditMiddleware
    from opteryx_shared_services.billing import allowance, block, plans
    from opteryx_shared_services import platform_mode
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version

try:
    # Read from the installed distribution rather than restated here, so the
    # one place a version is written is pyproject.toml. A second copy is a
    # second thing to forget, which is the failure this package exists to fix.
    __version__ = version("opteryx-shared-services")
except PackageNotFoundError:  # running from a source checkout, not installed
    __version__ = "0.0.0+source"

__all__ = ["__version__"]
