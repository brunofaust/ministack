import os

import boto3
import pytest
from botocore.exceptions import ClientError

ENDPOINT = os.environ.get("MINISTACK_ENDPOINT", "http://localhost:4566").rstrip("/")
REGION = "us-east-1"


@pytest.fixture(scope="module")
def lf():
    return boto3.client(
        "lakeformation",
        endpoint_url=ENDPOINT,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name=REGION,
    )


ROLE = "arn:aws:iam::000000000000:role/lf-test-role"
DATABASE = {"Database": {"CatalogId": "000000000000:s3tablescatalog/bucket", "Name": "logs"}}


def test_grant_list_revoke_roundtrip(lf):
    lf.grant_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE, Permissions=["DESCRIBE"])
    listed = lf.list_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE)
    perms = listed["PrincipalResourcePermissions"]
    assert len(perms) == 1
    assert perms[0]["Permissions"] == ["DESCRIBE"]
    assert perms[0]["Resource"]["Database"]["Name"] == "logs"

    # Granting again on the same (principal, resource) merges instead of duplicating.
    lf.grant_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE, Permissions=["SELECT"])
    perms = lf.list_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE)["PrincipalResourcePermissions"]
    assert len(perms) == 1
    assert sorted(perms[0]["Permissions"]) == ["DESCRIBE", "SELECT"]

    lf.revoke_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE, Permissions=["DESCRIBE", "SELECT"])
    assert lf.list_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE)["PrincipalResourcePermissions"] == []


def test_revoke_without_grant_is_invalid_input(lf):
    with pytest.raises(ClientError) as excinfo:
        lf.revoke_permissions(
            Principal={"DataLakePrincipalIdentifier": ROLE},
            Resource={"Table": {"DatabaseName": "logs", "Name": "missing"}},
            Permissions=["SELECT"],
        )
    assert excinfo.value.response["Error"]["Code"] == "InvalidInputException"


def test_list_permissions_filters_by_resource_type(lf):
    table = {"Table": {"DatabaseName": "logs", "Name": "execution_logs"}}
    lf.grant_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=table, Permissions=["SELECT"])
    tables = lf.list_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, ResourceType="TABLE")["PrincipalResourcePermissions"]
    assert [p["Resource"]["Table"]["Name"] for p in tables] == ["execution_logs"]
    lf.revoke_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=table, Permissions=["SELECT"])


def test_data_lake_settings_roundtrip(lf):
    default = lf.get_data_lake_settings()["DataLakeSettings"]
    assert default["DataLakeAdmins"] == []
    assert default["CreateDatabaseDefaultPermissions"][0]["Principal"]["DataLakePrincipalIdentifier"] == "IAM_ALLOWED_PRINCIPALS"

    lf.put_data_lake_settings(DataLakeSettings={
        "DataLakeAdmins": [{"DataLakePrincipalIdentifier": ROLE}],
        "CreateDatabaseDefaultPermissions": [],
        "CreateTableDefaultPermissions": [],
    })
    settings = lf.get_data_lake_settings()["DataLakeSettings"]
    assert settings["DataLakeAdmins"] == [{"DataLakePrincipalIdentifier": ROLE}]
    assert settings["CreateDatabaseDefaultPermissions"] == []


def test_batch_grant_reports_per_entry_failures(lf):
    out = lf.batch_grant_permissions(Entries=[
        {"Id": "ok", "Principal": {"DataLakePrincipalIdentifier": ROLE}, "Resource": DATABASE, "Permissions": ["DESCRIBE"]},
        {"Id": "bad", "Principal": {"DataLakePrincipalIdentifier": ROLE}, "Resource": DATABASE, "Permissions": []},
    ])
    assert [f["RequestEntry"]["Id"] for f in out["Failures"]] == ["bad"]
    lf.revoke_permissions(Principal={"DataLakePrincipalIdentifier": ROLE}, Resource=DATABASE, Permissions=["DESCRIBE"])


@pytest.mark.parametrize("invalid_store", ([], "", 0, None), ids=("list", "string", "integer", "none"))
def test_bd_1133_restore_rejects_corrupt_snapshot_atomically(monkeypatch, invalid_store):
    """BD-1133: a corrupt final store cannot partially replace live state."""
    from ministack.core.responses import AccountScopedDict
    from ministack.services import lakeformation

    stores = {
        "grants": AccountScopedDict(),
        "settings": AccountScopedDict(),
        "resources": AccountScopedDict(),
        "lf_tags": AccountScopedDict(),
    }
    for key, store in stores.items():
        monkeypatch.setattr(lakeformation, f"_{key}", store)
        store[f"seed-{key}"] = {"store": key}

    original = lakeformation.get_state()
    corrupt = dict(original)
    corrupt["lf_tags"] = invalid_store

    with pytest.raises(TypeError, match="Invalid persisted Lake Formation lf_tags store"):
        lakeformation.restore_state(corrupt)

    retained = lakeformation.get_state()
    assert {key: store._data for key, store in retained.items()} == {
        key: store._data for key, store in original.items()
    }
