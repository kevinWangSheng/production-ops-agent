"""Construction-contract tests for DurableToolLedger that need no real
PostgreSQL server. Its actual charge/usage behaviour against a live
DurableStore is covered under real PostgreSQL in
tests/integration/test_m1_tool_budget_postgres.py.
"""

import pytest

from opspilot.tools.ledger import DurableToolLedger


def test_max_operations_has_no_default_and_must_be_supplied_explicitly():
    """Bot review finding: max_operations previously defaulted to the frozen
    global ceiling (20), so a caller that forgot to pass a Run's own,
    possibly narrower QueryScope.max_operations would silently have the
    durable ledger enforce the wrong (wider) cap instead. Making it a
    required keyword forces every caller -- present and future -- to make
    an explicit choice instead of silently inheriting the global default.
    """

    with pytest.raises(TypeError, match="max_operations"):
        DurableToolLedger(store=None, lease=None)
