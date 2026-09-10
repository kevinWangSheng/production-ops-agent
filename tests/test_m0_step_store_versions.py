"""Invalid version identity must never enter a durable transaction."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts.m0.contracts import BudgetError
from scripts.m0.step_store import StepStore


@pytest.mark.parametrize("operation", ["accept", "new_run", "claim"])
@pytest.mark.parametrize(
    "versions", [{}, None, [], ["untyped"], {"state": ""}, {"": "v4"}, {"state": 4}]
)
def test_version_precondition_is_shared_and_pretransaction(operation, versions):
    def unexpected_transaction():
        pytest.fail("invalid version identity entered transaction")

    store = StepStore(SimpleNamespace(_transaction=unexpected_transaction))
    with pytest.raises(BudgetError, match="INVALID_INPUT"):
        if operation == "accept":
            store.accept(None, "intake", {}, versions)
        elif operation == "new_run":
            store.new_run(uuid4(), 0, None, {}, versions)
        else:
            store.claim(uuid4(), None, uuid4(), versions)
