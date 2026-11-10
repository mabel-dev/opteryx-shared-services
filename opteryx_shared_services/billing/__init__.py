"""Billing rules every query front door enforces, in one place.

Previously copied by hand into control.opteryx, jobs.opteryx, flight.opteryx and
odata.opteryx. A divergence between those copies is not cosmetic: it enforces
an account against limits it no longer has, or lets one allowance be spent
twice, once at each door. One package, one version number to compare.

- ``plans`` -- ``effective_plan``: the plan actually in force for an account.
  The stored ``plan`` field is intent; never read it directly.
- ``accounts`` -- the ``billing_accounts/{id}`` read, cached for a minute and
  shared by the checks below, so they cost one Firestore read between them.
- ``block`` -- refuse work billed to an account that is suspended for
  non-payment or at its monthly spending cap.
- ``allowance`` -- the free tier's daily allowance: the standard rate cards,
  the ``billing_accounts/{account}/usage/{YYYYMMDD}`` counter every door counts
  onto, and the decision.

Pure rules and Firestore reads/writes only -- no web framework, no engine. How a
refusal is reported (a 402, a ``FlightUnauthorizedError``) and where a check
sits in the request path stay with each service. Every Firestore call takes a
client (or a factory for one) from the caller, so this package never chooses a
project or database.

Nothing is imported here; import the module you need::

    from opteryx_shared_services.billing import allowance, block, plans
"""
