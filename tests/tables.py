"""Definición de tablas para pruebas y servidor de desarrollo (igual que en Terraform)."""


def create_tables(ddb):
    ddb.create_table(
        TableName="kutnz-links", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"},
                              {"AttributeName": "owner_id", "AttributeType": "S"},
                              {"AttributeName": "created_at", "AttributeType": "N"}],
        KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[{
            "IndexName": "owner-index",
            "KeySchema": [{"AttributeName": "owner_id", "KeyType": "HASH"},
                          {"AttributeName": "created_at", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
    )
    ddb.create_table(
        TableName="kutnz-stats", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "code", "AttributeType": "S"},
                              {"AttributeName": "dim", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "code", "KeyType": "HASH"}, {"AttributeName": "dim", "KeyType": "RANGE"}],
    )
    ddb.create_table(
        TableName="kutnz-moderation", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "kind", "AttributeType": "S"},
                              {"AttributeName": "value", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "kind", "KeyType": "HASH"}, {"AttributeName": "value", "KeyType": "RANGE"}],
    )
