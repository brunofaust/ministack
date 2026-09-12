"""Focused regressions for BusyDone-required AWS parity gaps."""

import base64
import hashlib
import hmac
import importlib
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest


def _payload(response):
    """Decode a MiniStack JSON response tuple."""
    return json.loads(response[2] or "{}")


def test_glue_reset_seeds_default_database():
    """Glue exposes AWS's default Data Catalog database after a reset."""
    from ministack.services import glue

    glue.reset()
    assert _payload(glue._get_database({"Name": "default"}))["Database"]["Name"] == "default"


def test_ses_configuration_set_event_destination_round_trip():
    """SES stores, describes, updates, and deletes event destinations."""
    from ministack.services import ses

    ses.reset()
    assert ses._create_configuration_set({"ConfigurationSet.Name": "cfg"})[0] == 200
    create = getattr(ses, "_create_configuration_set_event_destination")
    update = getattr(ses, "_update_configuration_set_event_destination")
    delete = getattr(ses, "_delete_configuration_set_event_destination")
    assert create({
        "ConfigurationSetName": "cfg",
        "EventDestination.Name": "events",
        "EventDestination.Enabled": "true",
        "EventDestination.MatchingEventTypes.member.1": "send",
        "EventDestination.SNSDestination.TopicARN": "arn:aws:sns:us-east-1:000000000000:events",
    })[0] == 200
    described = ses._describe_configuration_set({
        "ConfigurationSetName": "cfg",
        "ConfigurationSetAttributeNames.member.1": "eventDestinations",
    })[2].decode()
    assert "events" in described and "<Enabled>true</Enabled>" in described
    assert update({
        "ConfigurationSetName": "cfg",
        "EventDestination.Name": "events",
        "EventDestination.Enabled": "false",
        "EventDestination.MatchingEventTypes.member.1": "send",
        "EventDestination.SNSDestination.TopicARN": "arn:aws:sns:us-east-1:000000000000:events",
    })[0] == 200
    assert delete({"ConfigurationSetName": "cfg", "EventDestinationName": "events"})[0] == 200


def test_s3tables_seeds_maintenance_defaults():
    """S3 Tables exposes default bucket and table maintenance settings."""
    from ministack.services import s3tables

    bucket = getattr(s3tables, "_default_bucket_maintenance_configuration")()
    table = getattr(s3tables, "_default_table_maintenance_configuration")()
    assert set(bucket) == {"icebergUnreferencedFileRemoval"}
    assert set(table) == {"icebergCompaction", "icebergSnapshotManagement"}


def test_cloudfront_seeds_read_only_managed_policies():
    """CloudFront ships stable managed policy IDs and rejects mutation."""
    from ministack.services import cloudfront

    cloudfront.reset()
    policy_id = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    managed = getattr(cloudfront, "_managed_cache_policies")
    assert managed[policy_id]["Config"]["Name"] == "Managed-CachingDisabled"
    assert cloudfront._update_cache_policy(policy_id, {}, b"")[0] == 403


def test_s3vectors_control_and_data_plane_query():
    """S3 Vectors creates an index, stores vectors, and returns nearest matches."""
    vectors = importlib.import_module("ministack.services.s3vectors")
    vectors.reset()
    bucket_arn = _payload(vectors._create_vector_bucket({"vectorBucketName": "busy-vectors"}))["vectorBucketArn"]
    index_arn = _payload(vectors._create_index({
        "vectorBucketArn": bucket_arn,
        "indexName": "context",
        "dataType": "float32",
        "dimension": 2,
        "distanceMetric": "euclidean",
    }))["indexArn"]
    assert vectors._put_vectors({
        "indexArn": index_arn,
        "vectors": [
            {"key": "near", "data": {"float32": [0.0, 0.0]}, "metadata": {"kind": "ticket"}},
            {"key": "far", "data": {"float32": [10.0, 10.0]}, "metadata": {"kind": "ticket"}},
        ],
    })[0] == 200
    result = _payload(vectors._query_vectors({
        "indexArn": index_arn,
        "queryVector": {"float32": [1.0, 1.0]},
        "topK": 1,
        "returnDistance": True,
    }))
    assert result["vectors"][0]["key"] == "near"


def test_cognito_rejects_bad_confirmation_codes():
    """Cognito confirmation helpers reject missing or incorrect issued codes."""
    from ministack.services import cognito

    cognito.reset()
    pool_id = _payload(cognito._create_user_pool({"PoolName": "busy-codes"}))["UserPool"]["Id"]
    client_id = _payload(cognito._create_user_pool_client({
        "UserPoolId": pool_id,
        "ClientName": "busy-codes-client",
    }))["UserPoolClient"]["ClientId"]
    cognito._sign_up({"ClientId": client_id, "Username": "alice", "Password": "Password1!"})
    bad_signup = cognito._confirm_sign_up({
        "ClientId": client_id,
        "Username": "alice",
        "ConfirmationCode": "wrong",
    })
    assert _payload(bad_signup)["__type"] == "CodeMismatchException"
    cognito._forgot_password({"ClientId": client_id, "Username": "alice"})
    bad_reset = cognito._confirm_forgot_password({
        "ClientId": client_id,
        "Username": "alice",
        "ConfirmationCode": "wrong",
        "Password": "Changed1!",
    })
    assert _payload(bad_reset)["__type"] == "CodeMismatchException"


def test_cognito_rejects_invalid_refresh_token():
    """A recognized refresh flow rejects a token absent from the token store."""
    from ministack.services import cognito

    result, error = cognito._refresh_auth_result({"_users": {}}, "pool", "client", "garbage")
    assert result is None
    assert _payload(error)["__type"] == "NotAuthorizedException"

    oauth = cognito.handle_oauth2_token(
        "POST",
        "/oauth2/token",
        {"content-type": "application/x-www-form-urlencoded"},
        urlencode({"grant_type": "refresh_token", "refresh_token": "garbage"}).encode(),
        {},
    )
    assert oauth[0] == 400
    assert _payload(oauth)["error"] == "invalid_grant"


def test_bd_1135_cognito_rejects_unknown_oauth_grant_from_raw_form():
    """BD-1135: a nonempty unknown raw-form grant is unsupported."""
    from ministack.services import cognito

    oauth = cognito.handle_oauth2_token(
        "POST",
        "/oauth2/token",
        {"content-type": "application/x-www-form-urlencoded"},
        urlencode({"grant_type": "made-up"}).encode(),
        {},
    )
    assert oauth[0] == 400
    assert _payload(oauth)["error"] == "unsupported_grant_type"


@pytest.mark.parametrize("form", ({}, {"grant_type": ""}), ids=("missing", "blank"))
def test_bd_1135_cognito_rejects_absent_oauth_grant_from_raw_form(form):
    """BD-1135: a missing or blank raw-form grant is an invalid request."""
    from ministack.services import cognito

    oauth = cognito.handle_oauth2_token(
        "POST",
        "/oauth2/token",
        {"content-type": "application/x-www-form-urlencoded"},
        urlencode(form).encode(),
        {},
    )
    assert oauth[0] == 400
    assert _payload(oauth)["error"] == "invalid_request"


def test_cognito_validates_secret_hash():
    """SecretHash values are cryptographically checked."""
    from ministack.services import cognito

    secret = "client-secret"
    expected = base64.b64encode(hmac.new(secret.encode(), b"aliceclient-id", hashlib.sha256).digest()).decode()
    verify_hash = getattr(cognito, "_verify_secret_hash")
    assert verify_hash({"ClientSecret": secret}, "client-id", "alice", {"SecretHash": expected}) is None
    assert _payload(verify_hash({"ClientSecret": secret}, "client-id", "alice", {"SecretHash": "bad"}))["__type"] == "NotAuthorizedException"


def test_bd_1137_cognito_totp_accepts_current_and_adjacent_time_steps():
    """BD-1137: TOTP verification accepts the current step and its two neighbors."""
    from ministack.services import cognito

    secret = "JBSWY3DPEHPK3PXP"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cognito.time, "time", lambda: 1_700_000_000)
        codes = [cognito._totp_code(secret, offset) for offset in (-1, 0, 1)]
        assert all(cognito._totp_matches(secret, code) for code in codes)


class _FakeEcs:
    """Minimal ECS task-definition lookup used by the Step Functions regression."""

    @staticmethod
    def _describe_task_definition(_data):
        return 200, {}, json.dumps({"taskDefinition": {"containerDefinitions": [
            {"name": "app", "essential": True},
            {"name": "sidecar", "essential": False},
        ]}})


def test_stepfunctions_fails_on_essential_ecs_exit_only():
    """ecs:runTask.sync maps essential non-zero exits to States.TaskFailed."""
    from ministack.services import stepfunctions

    check = getattr(stepfunctions, "_raise_if_ecs_task_failed")
    task = {
        "taskDefinitionArn": "arn:task-definition/demo:1",
        "containers": [{"name": "sidecar", "exitCode": 9}, {"name": "app", "exitCode": 0}],
    }
    check(_FakeEcs, [task])
    task["containers"][1]["exitCode"] = 2
    with pytest.raises(stepfunctions._ExecutionError) as exc:
        check(_FakeEcs, [task])
    assert exc.value.error == "States.TaskFailed"


def test_rds_endpoint_summary_does_not_guess_missing_ports():
    """RDS admin inspection reports pending endpoints as null, never fabricated."""
    from ministack.services import rds

    rds._instances["pending-busy"] = {"DBInstanceIdentifier": "pending-busy", "DBInstanceStatus": "creating"}
    try:
        summary = getattr(rds, "rds_endpoints_summary")()
        entry = next(item for item in summary["instances"] if item["db_instance_identifier"] == "pending-busy")
        assert entry["host_mapped"] is None
        assert entry["in_network"] is None
    finally:
        rds._instances.pop("pending-busy", None)


def test_s3vectors_routes_by_signing_scope():
    """SigV4 s3vectors requests route to the S3 Vectors service."""
    from ministack.core.router import detect_service

    headers = {"authorization": "AWS4-HMAC-SHA256 Credential=test/20260829/us-east-1/s3vectors/aws4_request"}
    assert detect_service("POST", "/", headers, {}) == "s3vectors"


def test_s3vectors_rejects_foreign_index_arns_for_all_data_operations():
    """Index ARN identity is scoped to the active account and region."""
    from ministack.core.responses import request_scope
    from ministack.services import s3vectors

    s3vectors.reset()
    with request_scope("111111111111", "us-east-1"):
        bucket_arn = _payload(s3vectors._create_vector_bucket({"vectorBucketName": "scoped"}))["vectorBucketArn"]
        index_arn = _payload(s3vectors._create_index({
            "vectorBucketArn": bucket_arn,
            "indexName": "idx",
            "dataType": "float32",
            "dimension": 2,
            "distanceMetric": "euclidean",
        }))["indexArn"]
        foreign_arns = (
            index_arn.replace("111111111111", "222222222222"),
            index_arn.replace("us-east-1", "us-west-2"),
        )
        for foreign in foreign_arns:
            for operation, data in (
                (s3vectors._get_index, {"indexArn": foreign}),
                (s3vectors._delete_index, {"indexArn": foreign}),
                (s3vectors._put_vectors, {"indexArn": foreign, "vectors": [{"key": "x", "data": [1.0, 2.0]}]}),
                (s3vectors._query_vectors, {"indexArn": foreign, "queryVector": [1.0, 2.0], "topK": 1}),
            ):
                assert operation(data)[0] == 404
        assert s3vectors._get_index({"indexArn": index_arn})[0] == 200


def test_s3vectors_non_post_methods_never_mutate():
    """Every S3 Vectors operation rejects GET, PUT, and DELETE."""
    import asyncio

    from ministack.services import s3vectors

    s3vectors.reset()
    body = json.dumps({"vectorBucketName": "method-guard"}).encode()
    for method in ("GET", "PUT", "DELETE"):
        response = asyncio.run(s3vectors.handle_request(method, "/CreateVectorBucket", {}, body, {}))
        assert response[0] == 400
        assert not s3vectors._vector_buckets


def test_rds_endpoint_summary_is_request_scoped():
    """RDS endpoint discovery never leaks another account or region."""
    from ministack.core.responses import request_scope
    from ministack.services import rds

    rds.reset()
    with request_scope("111111111111", "us-east-1"):
        rds._instances["east-a"] = {"DBInstanceIdentifier": "east-a"}
    with request_scope("222222222222", "us-west-2"):
        rds._instances["west-b"] = {"DBInstanceIdentifier": "west-b"}
    with request_scope("111111111111", "us-east-1"):
        assert [entry["db_instance_identifier"] for entry in rds.rds_endpoints_summary()["instances"]] == ["east-a"]
    with request_scope("222222222222", "us-west-2"):
        assert [entry["db_instance_identifier"] for entry in rds.rds_endpoints_summary()["instances"]] == ["west-b"]


def test_ses_event_destination_route_rejects_invalid_shapes_without_mutation():
    """SES validates destination shape completely before storing it."""
    import asyncio

    from ministack.services import ses

    ses.reset()
    ses._create_configuration_set({"ConfigurationSet.Name": "cfg"})
    cases = (
        {},
        {"EventDestination.Name": "dest"},
        {"EventDestination.Name": "dest", "EventDestination.MatchingEventTypes.member.1": "send"},
        {
            "EventDestination.Name": "dest",
            "EventDestination.MatchingEventTypes.member.1": "send",
            "EventDestination.SNSDestination.TopicARN": "arn:aws:sns:us-east-1:000000000000:t",
            "EventDestination.KinesisFirehoseDestination.IAMRoleARN": "arn:aws:iam::000000000000:role/r",
            "EventDestination.KinesisFirehoseDestination.DeliveryStreamARN": "arn:aws:firehose:us-east-1:000000000000:deliverystream/d",
        },
        {
            "EventDestination.Name": "dest",
            "EventDestination.MatchingEventTypes.member.1": "send",
            "EventDestination.KinesisFirehoseDestination.IAMRoleARN": "arn:aws:iam::000000000000:role/r",
        },
        {
            "EventDestination.Name": "dest",
            "EventDestination.MatchingEventTypes.member.1": "send",
            "EventDestination.SNSDestination.TopicARN": "",
        },
        {
            "EventDestination.Name": "dest",
            "EventDestination.MatchingEventTypes.member.1": "send",
            "EventDestination.CloudWatchDestination.DimensionConfigurations.member.1.DimensionName": "dimension",
        },
    )
    for fields in cases:
        params = {"Action": "CreateConfigurationSetEventDestination", "ConfigurationSetName": "cfg", **fields}
        response = asyncio.run(ses.handle_request("POST", "/", {}, urlencode(params).encode(), {}))
        assert response[0] == 400
        assert not ses._configuration_sets["cfg"].get("EventDestinations")


class _LifecycleContainer:
    """Docker lifecycle double for essential-container transition tests."""

    def __init__(self, status, exit_code):
        self.status = status
        self.exit_code = exit_code
        self.stopped = False

    def reload(self):
        return None

    def wait(self):
        return {"StatusCode": self.exit_code}

    def stop(self, timeout=5):
        self.stopped = True
        self.status = "exited"


def test_ecs_essential_exit_stops_live_sidecars_and_fails_stepfunctions(monkeypatch):
    """An essential exit stops sidecars, completes the task, and fails runTask.sync."""
    from ministack.services import ecs, stepfunctions

    app = _LifecycleContainer("exited", 7)
    sidecar = _LifecycleContainer("running", 0)
    containers = {"app-id": app, "sidecar-id": sidecar}
    monkeypatch.setattr(ecs, "_get_docker", lambda: SimpleNamespace(
        containers=SimpleNamespace(get=lambda docker_id: containers[docker_id])
    ))
    task_containers = ecs._build_task_containers({"containerDefinitions": [
        {"name": "app", "image": "app", "essential": True},
        {"name": "sidecar", "image": "sidecar", "essential": False},
    ]}, [])
    task = {
        "lastStatus": "RUNNING",
        "desiredStatus": "RUNNING",
        "_docker_ids": ["app-id", "sidecar-id"],
        "containers": task_containers,
        "clusterArn": "arn:aws:ecs:us-east-1:000000000000:cluster/default",
    }
    ecs._maybe_mark_stopped(task)
    assert task["lastStatus"] == "STOPPED"
    assert sidecar.stopped is True
    assert all("_essential" not in item for item in ecs._sanitize(task)["containers"])
    with pytest.raises(stepfunctions._ExecutionError):
        stepfunctions._raise_if_ecs_task_failed(SimpleNamespace(), [task])


def test_ecs_nonessential_exit_does_not_stop_live_essential_container(monkeypatch):
    """A failed nonessential sidecar leaves its essential app task running."""
    from ministack.services import ecs

    app = _LifecycleContainer("running", 0)
    sidecar = _LifecycleContainer("exited", 9)
    docker_containers = {"app-id": app, "sidecar-id": sidecar}
    monkeypatch.setattr(ecs, "_get_docker", lambda: SimpleNamespace(
        containers=SimpleNamespace(get=lambda docker_id: docker_containers[docker_id])
    ))
    task_containers = ecs._build_task_containers({"containerDefinitions": [
        {"name": "app", "image": "app", "essential": True},
        {"name": "sidecar", "image": "sidecar", "essential": False},
    ]}, [])
    task = {
        "lastStatus": "RUNNING",
        "desiredStatus": "RUNNING",
        "_docker_ids": ["app-id", "sidecar-id"],
        "containers": task_containers,
        "clusterArn": "arn:aws:ecs:us-east-1:000000000000:cluster/default",
    }

    ecs._maybe_mark_stopped(task)

    assert task["lastStatus"] == "RUNNING"
    assert app.stopped is False
    assert ecs._sanitize(task)["containers"] == [
        {key: value for key, value in container.items() if not key.startswith("_")}
        for container in task_containers
    ]


def test_ecs_launch_exception_stops_task_for_stepfunctions(monkeypatch):
    """A Docker launch exception produces an immediate TaskFailedToStart task."""
    from ministack.services import ecs, stepfunctions

    class FailingContainers:
        def get(self, _name):
            raise RuntimeError("not found")

        def run(self, _image, **_kwargs):
            raise RuntimeError("launch failed")

    monkeypatch.setattr(ecs, "_get_docker", lambda: SimpleNamespace(containers=FailingContainers()))
    ecs.reset()
    ecs._register_task_definition({
        "family": "launch-failure",
        "containerDefinitions": [{"name": "app", "image": "broken", "essential": True}],
    })
    task = _payload(ecs._run_task({"cluster": "default", "taskDefinition": "launch-failure"}))["tasks"][0]
    assert task["lastStatus"] == "STOPPED"
    assert task["stopCode"] == "TaskFailedToStart"
    with pytest.raises(stepfunctions._ExecutionError):
        stepfunctions._raise_if_ecs_task_failed(SimpleNamespace(), [task])
