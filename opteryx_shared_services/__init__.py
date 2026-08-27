"""Capabilities every Opteryx service needs, in one place.

Three seed capabilities, each of which was previously copied into eight service
repos and had silently diverged there:

- ``opteryx_shared_services.config`` -- the platform configuration client.
- ``opteryx_shared_services.logging`` -- structured logging and the audit wire
  format the fleet's log ingestion selects on.
- ``opteryx_shared_services.preflight`` -- startup dependency checks, which fail
  a Cloud Run revision rather than letting an instance that cannot serve replace
  one that can.

Nothing is imported here. A service that needs only configuration must not pay
for a Firestore, GCS or Secret Manager import it never uses, so each capability
is imported by name::

    from opteryx_shared_services.config import get_config
    from opteryx_shared_services.logging import get_logger
    from opteryx_shared_services.preflight import Preflight
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
