from dispatcher.steps import DynamoDbLedger, InMemoryLedger


def test_in_memory_ledger_first_write_wins():
    lg = InMemoryLedger()
    assert lg.get(("w1", "s1")) is None
    assert lg.put(("w1", "s1"), {"n": 1}) is True
    assert lg.put(("w1", "s1"), {"n": 2}) is False
    assert lg.get(("w1", "s1")) == {"n": 1}


def test_dynamo_ledger_conditional_write(dynamo):
    lg = DynamoDbLedger("step_ledger", client=dynamo)
    assert lg.get(("w1", "s1")) is None
    assert lg.put(("w1", "s1"), {"done": True}) is True
    assert lg.put(("w1", "s1"), {"done": False}) is False  # concurrent writer loses
    assert lg.get(("w1", "s1")) == {"done": True}
