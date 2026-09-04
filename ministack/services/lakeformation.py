"""
AWS Lake Formation Service Emulator.
REST/JSON protocol — every operation is ``POST /<OperationName>``.

Supports:
  Permissions:  GrantPermissions, RevokePermissions, BatchGrantPermissions,
                BatchRevokePermissions, ListPermissions
  Settings:     GetDataLakeSettings, PutDataLakeSettings
  Resources:    RegisterResource, DeregisterResource, DescribeResource,
                ListResources
  Tags (LF-Tags): CreateLFTag, GetLFTag, UpdateLFTag, DeleteLFTag, ListLFTags

State is in-memory and scoped by account. Grants are matched on the exact
(principal, resource) pair so that Terraform's ``aws_lakeformation_permissions``
read (ListPermissions filtered by Principal + Resource) finds what it wrote.
"""

import json
import logging
import threading
from datetime import datetime, timezone

from ministack.core.responses import AccountScopedDict, get_account_id

logger = logging.getLogger(__name__)

_lock = threading.RLock()
# account -> list of grant dicts {"Principal", "Resource", "Permissions", "PermissionsWithGrantOption"}
_grants = AccountScopedDict()
# account -> DataLakeSettings dict
_settings = AccountScopedDict()
# account -> {resource_arn: resource_info}
_resources = AccountScopedDict()
# account -> {tag_key: [values]}
_lf_tags = AccountScopedDict()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _json_resp(status, body):
    return status, {"Content-Type": "application/json"}, json.dumps(body).encode()


def _error(status, code, message):
    return status, {"Content-Type": "application/json", "x-amzn-errortype": code}, json.dumps({"__type": code, "Message": message}).encode()


def _account_grants():
    acct = get_account_id()
    return _grants.setdefault(acct, [])


def _default_settings():
    return {
        "DataLakeAdmins": [],
        "ReadOnlyAdmins": [],
        "CreateDatabaseDefaultPermissions": [{"Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}, "Permissions": ["ALL"]}],
        "CreateTableDefaultPermissions": [{"Principal": {"DataLakePrincipalIdentifier": "IAM_ALLOWED_PRINCIPALS"}, "Permissions": ["ALL"]}],
        "Parameters": {},
        "TrustedResourceOwners": [],
        "AllowExternalDataFiltering": False,
        "AllowFullTableExternalDataAccess": False,
        "ExternalDataFilteringAllowList": [],
        "AuthorizedSessionTagValueList": [],
    }


def _normalize_resource(resource):
    """Return a canonical, JSON-comparable form of a Resource block."""
    if not isinstance(resource, dict):
        return {}
    out = {}
    for kind, spec in resource.items():
        if not isinstance(spec, dict):
            out[kind] = spec
            continue
        clean = {k: v for k, v in spec.items() if v not in (None, "", [], {})}
        clean.pop("CatalogId", None)
        out[kind] = clean
    return out


def _same_resource(a, b):
    return json.dumps(_normalize_resource(a), sort_keys=True) == json.dumps(_normalize_resource(b), sort_keys=True)


def _principal_id(principal):
    return (principal or {}).get("DataLakePrincipalIdentifier", "")


def _resource_type_of(resource):
    if not isinstance(resource, dict) or not resource:
        return None
    kind = next(iter(resource))
    return {"Catalog": "CATALOG", "Database": "DATABASE", "Table": "TABLE", "TableWithColumns": "TABLE",
            "DataLocation": "DATA_LOCATION", "LFTag": "LF_TAG", "LFTagPolicy": "LF_TAG_POLICY",
            "DataCellsFilter": "DATA_CELLS_FILTER"}.get(kind, kind.upper())


def _validate_grant(body):
    principal = body.get("Principal")
    resource = body.get("Resource")
    permissions = body.get("Permissions")
    if not _principal_id(principal):
        return _error(400, "InvalidInputException", "Principal.DataLakePrincipalIdentifier is required")
    if not isinstance(resource, dict) or not resource:
        return _error(400, "InvalidInputException", "Resource is required")
    if not isinstance(permissions, list) or not permissions:
        return _error(400, "InvalidInputException", "Permissions must be a non-empty list")
    return None


def _grant(body):
    err = _validate_grant(body)
    if err:
        return err
    principal = body["Principal"]
    resource = body["Resource"]
    permissions = list(dict.fromkeys(body["Permissions"]))
    grant_option = list(dict.fromkeys(body.get("PermissionsWithGrantOption") or []))
    with _lock:
        for entry in _account_grants():
            if _principal_id(entry["Principal"]) == _principal_id(principal) and _same_resource(entry["Resource"], resource):
                entry["Permissions"] = list(dict.fromkeys(entry["Permissions"] + permissions))
                entry["PermissionsWithGrantOption"] = list(dict.fromkeys(entry["PermissionsWithGrantOption"] + grant_option))
                entry["LastUpdated"] = _now_iso()
                return _json_resp(200, {})
        _account_grants().append({
            "Principal": principal,
            "Resource": resource,
            "Permissions": permissions,
            "PermissionsWithGrantOption": grant_option,
            "LastUpdated": _now_iso(),
            "LastUpdatedBy": f"arn:aws:iam::{get_account_id()}:root",
        })
    return _json_resp(200, {})


def _revoke(body):
    err = _validate_grant(body)
    if err:
        return err
    principal = body["Principal"]
    resource = body["Resource"]
    permissions = set(body["Permissions"])
    grant_option = set(body.get("PermissionsWithGrantOption") or [])
    with _lock:
        grants = _account_grants()
        for entry in grants:
            if _principal_id(entry["Principal"]) == _principal_id(principal) and _same_resource(entry["Resource"], resource):
                entry["Permissions"] = [p for p in entry["Permissions"] if p not in permissions]
                entry["PermissionsWithGrantOption"] = [p for p in entry["PermissionsWithGrantOption"] if p not in grant_option]
                if not entry["Permissions"] and not entry["PermissionsWithGrantOption"]:
                    grants.remove(entry)
                return _json_resp(200, {})
    return _error(400, "InvalidInputException", "No permissions revoked. Grantee has no permissions and nothing to revoke.")


def _batch(body, op):
    failures = []
    for i, entry in enumerate(body.get("Entries") or []):
        entry_id = entry.get("Id", str(i))
        status, _headers, payload = op(entry)
        if status != 200:
            err = json.loads(payload)
            failures.append({"RequestEntry": entry, "Error": {"ErrorCode": err.get("__type"), "ErrorMessage": err.get("Message")}})
            failures[-1]["RequestEntry"]["Id"] = entry_id
    return _json_resp(200, {"Failures": failures})


def _list_permissions(body):
    principal_filter = _principal_id(body.get("Principal"))
    resource_filter = body.get("Resource")
    type_filter = body.get("ResourceType")
    with _lock:
        entries = list(_account_grants())
    out = []
    for entry in entries:
        if principal_filter and _principal_id(entry["Principal"]) != principal_filter:
            continue
        if resource_filter and not _same_resource(entry["Resource"], resource_filter):
            continue
        if type_filter and _resource_type_of(entry["Resource"]) != type_filter:
            continue
        out.append({
            "Principal": entry["Principal"],
            "Resource": entry["Resource"],
            "Permissions": list(entry["Permissions"]),
            "PermissionsWithGrantOption": list(entry["PermissionsWithGrantOption"]),
            "LastUpdated": entry["LastUpdated"],
            "LastUpdatedBy": entry["LastUpdatedBy"],
        })
    return _json_resp(200, {"PrincipalResourcePermissions": out})


def _get_settings(body):
    with _lock:
        settings = _settings.get(get_account_id()) or _default_settings()
    return _json_resp(200, {"DataLakeSettings": settings})


def _put_settings(body):
    incoming = body.get("DataLakeSettings")
    if not isinstance(incoming, dict):
        return _error(400, "InvalidInputException", "DataLakeSettings is required")
    settings = _default_settings()
    settings.update(incoming)
    with _lock:
        _settings[get_account_id()] = settings
    return _json_resp(200, {})


def _register_resource(body):
    arn = body.get("ResourceArn")
    if not arn:
        return _error(400, "InvalidInputException", "ResourceArn is required")
    with _lock:
        _resources.setdefault(get_account_id(), {})[arn] = {
            "ResourceArn": arn,
            "RoleArn": body.get("RoleArn") or f"arn:aws:iam::{get_account_id()}:role/aws-service-role/lakeformation.amazonaws.com/AWSServiceRoleForLakeFormationDataAccess",
            "LastModified": _now_iso(),
            "WithFederation": bool(body.get("WithFederation", False)),
            "HybridAccessEnabled": bool(body.get("HybridAccessEnabled", False)),
        }
    return _json_resp(200, {})


def _deregister_resource(body):
    arn = body.get("ResourceArn")
    with _lock:
        if arn not in _resources.setdefault(get_account_id(), {}):
            return _error(400, "EntityNotFoundException", f"Resource {arn} is not registered")
        del _resources[get_account_id()][arn]
    return _json_resp(200, {})


def _describe_resource(body):
    arn = body.get("ResourceArn")
    with _lock:
        info = _resources.setdefault(get_account_id(), {}).get(arn)
    if info is None:
        return _error(400, "EntityNotFoundException", f"Resource {arn} is not registered")
    return _json_resp(200, {"ResourceInfo": info})


def _list_resources(body):
    with _lock:
        infos = list(_resources.setdefault(get_account_id(), {}).values())
    return _json_resp(200, {"ResourceInfoList": infos})


def _create_lf_tag(body):
    key = body.get("TagKey")
    values = body.get("TagValues") or []
    if not key or not values:
        return _error(400, "InvalidInputException", "TagKey and TagValues are required")
    with _lock:
        tags = _lf_tags.setdefault(get_account_id(), {})
        if key in tags:
            return _error(400, "AlreadyExistsException", f"Tag key {key} already exists")
        tags[key] = list(dict.fromkeys(values))
    return _json_resp(200, {})


def _get_lf_tag(body):
    key = body.get("TagKey")
    with _lock:
        values = _lf_tags.setdefault(get_account_id(), {}).get(key)
    if values is None:
        return _error(400, "EntityNotFoundException", f"Tag key {key} does not exist")
    return _json_resp(200, {"CatalogId": get_account_id(), "TagKey": key, "TagValues": values})


def _update_lf_tag(body):
    key = body.get("TagKey")
    with _lock:
        tags = _lf_tags.setdefault(get_account_id(), {})
        if key not in tags:
            return _error(400, "EntityNotFoundException", f"Tag key {key} does not exist")
        values = [v for v in tags[key] if v not in set(body.get("TagValuesToDelete") or [])]
        tags[key] = list(dict.fromkeys(values + list(body.get("TagValuesToAdd") or [])))
    return _json_resp(200, {})


def _delete_lf_tag(body):
    key = body.get("TagKey")
    with _lock:
        tags = _lf_tags.setdefault(get_account_id(), {})
        if key not in tags:
            return _error(400, "EntityNotFoundException", f"Tag key {key} does not exist")
        del tags[key]
    return _json_resp(200, {})


def _list_lf_tags(body):
    with _lock:
        tags = dict(_lf_tags.setdefault(get_account_id(), {}))
    return _json_resp(200, {"LFTags": [{"CatalogId": get_account_id(), "TagKey": k, "TagValues": v} for k, v in tags.items()]})


_OPERATIONS = {
    "GrantPermissions": _grant,
    "RevokePermissions": _revoke,
    "BatchGrantPermissions": lambda body: _batch(body, _grant),
    "BatchRevokePermissions": lambda body: _batch(body, _revoke),
    "ListPermissions": _list_permissions,
    "GetDataLakeSettings": _get_settings,
    "PutDataLakeSettings": _put_settings,
    "RegisterResource": _register_resource,
    "DeregisterResource": _deregister_resource,
    "DescribeResource": _describe_resource,
    "ListResources": _list_resources,
    "CreateLFTag": _create_lf_tag,
    "GetLFTag": _get_lf_tag,
    "UpdateLFTag": _update_lf_tag,
    "DeleteLFTag": _delete_lf_tag,
    "ListLFTags": _list_lf_tags,
}

# Path prefixes the router uses to recognise unsigned/odd requests.
PATH_PREFIXES = [f"/{name}" for name in _OPERATIONS]


async def handle_request(method, path, headers, body_bytes, query_params):
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except json.JSONDecodeError:
        return _error(400, "InvalidInputException", "Malformed JSON body")
    operation = path.strip("/").split("/")[0]
    handler = _OPERATIONS.get(operation)
    if handler is None or method != "POST":
        return _error(400, "InvalidInputException", f"Unsupported Lake Formation operation: {method} {path}")
    return handler(body)
