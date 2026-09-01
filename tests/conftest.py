import boto3
import pytest
from moto import mock_aws


@pytest.fixture
def dynamo():
    """A moto-mocked DynamoDB client with the three project tables created."""
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="wave_state",
            KeySchema=[{"AttributeName": "wave_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "wave_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="decision_log",
            KeySchema=[
                {"AttributeName": "wave_id", "KeyType": "HASH"},
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "wave_id", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="step_ledger",
            KeySchema=[{"AttributeName": "step_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "step_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client
