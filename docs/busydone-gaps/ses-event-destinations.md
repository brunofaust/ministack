# Gap: SES configuration-set event destinations return `400 InvalidAction`

## What

MiniStack's classic SES emulator (`ministack/services/ses.py`) implemented
configuration sets (`CreateConfigurationSet`/`DescribeConfigurationSet`/
`DeleteConfigurationSet`/`ListConfigurationSets`) but not the event-destination
operation family that attaches a publish target (SNS / CloudWatch / Kinesis
Firehose) to a configuration set. This change adds:

- `CreateConfigurationSetEventDestination`
- `UpdateConfigurationSetEventDestination`
- `DeleteConfigurationSetEventDestination`
- `DescribeConfigurationSet` extended to return the stored `EventDestinations`
  when the caller requests the `eventDestinations` attribute
  (`ConfigurationSetAttributeNames.member.N=eventDestinations`), matching real
  AWS's opt-in-per-attribute behavior.

## Why classic SES (`ses.py`), not SESv2 (`ses_v2.py`)

MiniStack ships both a v1 (`ses.py`, classic Query API) and v2
(`ses_v2.py`, REST/JSON) SES emulator. The consuming Terraform module,
`infra/modules/ses/main.tf` in the busydone repo, declares:

```hcl
resource "aws_ses_event_destination" "sns" {
  name                   = "sns-events"
  configuration_set_name = aws_ses_configuration_set.main.name
  enabled                = true
  matching_types         = ["bounce", "complaint", "delivery"]

  sns_destination {
    topic_arn = aws_sns_topic.ses_events.arn
  }
}
```

`aws_ses_event_destination` is the AWS provider's **classic SES v1** resource
(the v2 equivalent is `aws_sesv2_configuration_set_event_destination`, backed
by SESv2's `CreateConfigurationSetEventDestination` REST endpoint under
`/v2/email/configuration-sets/{name}/event-destinations`, an entirely
different wire shape). The provider issues the classic Query API action
`CreateConfigurationSetEventDestination` against the `ses` service, which is
exactly the "Unknown action" MiniStack was rejecting. Implementation therefore
went into `ses.py` only; `ses_v2.py` was not touched.

## Verbatim before-error

Against the unmodified `v1.4.17` image (`ministack-local:dev`, container
`ms-ses-before`, port 14574):

```
$ aws ses create-configuration-set-event-destination \
    --configuration-set-name before-cs \
    --event-destination '{"Name":"sns-events","Enabled":true,"MatchingEventTypes":["bounce"],"SNSDestination":{"TopicARN":"arn:aws:sns:us-east-1:000000000000:t"}}' \
    --endpoint-url http://localhost:14574

An error occurred (InvalidAction) when calling the CreateConfigurationSetEventDestination operation: Unknown action: CreateConfigurationSetEventDestination
```

## Wire shapes (source of truth: botocore, read-only)

Read from the busydone repo's installed venv (never modified):
`/Users/bfaust/Repos/busydone/.venv/lib/python3.14/site-packages/botocore/data/ses/2010-12-01/service-2.json.gz`
(botocore 1.43.56). Relevant shapes: `CreateConfigurationSetEventDestinationRequest`,
`UpdateConfigurationSetEventDestinationRequest`,
`DeleteConfigurationSetEventDestinationRequest`, `EventDestination`,
`SNSDestination`, `CloudWatchDestination`, `CloudWatchDimensionConfiguration`,
`KinesisFirehoseDestination`, `DescribeConfigurationSetResponse`.

An `EventDestination` carries `Name` + `MatchingEventTypes` (required) +
`Enabled`, plus exactly one of three mutually-exclusive nested sub-shapes per
AWS's own documentation ("you must provide one, and only one, destination"):

| Sub-shape | Status |
|---|---|
| `SNSDestination` (`TopicARN`) | **Implemented and verified** — the only destination the consuming Terraform module uses |
| `CloudWatchDestination` (`DimensionConfigurations[]`: `DimensionName`/`DimensionValueSource`/`DefaultDimensionValue`) | Implemented (parse/store/echo, same code path as SNS) but **not exercised by any test** — stubbed in the sense that it has no driving verification |
| `KinesisFirehoseDestination` (`IAMRoleARN`, `DeliveryStreamARN`) | Implemented (parse/store/echo) but **not exercised by any test** — same caveat |

## Files / functions changed

`ministack/services/ses.py`:

- `handlers{}` dispatch dict (was L134-161): added 3 entries —
  `CreateConfigurationSetEventDestination`, `UpdateConfigurationSetEventDestination`,
  `DeleteConfigurationSetEventDestination` — following the file's existing
  `"Action": _handler_fn` pattern exactly.
- New functions: `_create_configuration_set_event_destination`,
  `_update_configuration_set_event_destination`,
  `_delete_configuration_set_event_destination`,
  `_parse_event_destination` (parses the dotted Query-API form params into a
  dict matching the `EventDestination` shape), `_event_destination_xml`
  (serializes one destination back to the Query API's XML `<member>` shape).
- `_describe_configuration_set`: now reads
  `ConfigurationSetAttributeNames.member.N` from the request and, only when
  `eventDestinations` is among them, appends an `<EventDestinations>` block —
  matching real AWS's per-attribute opt-in instead of always returning it.
- Module docstring's v1 action list updated to list the three new actions.

## Storage / multi-tenancy

Event destinations are stored as a plain `dict` **nested inside each
configuration set's own value**, and that value already lives inside the
pre-existing `_configuration_sets` `AccountRegionScopedDict`. No new
module-level store was introduced, so:

- destinations inherit the existing per-account/per-region isolation for
  free (they're just another key on an already-scoped value), and
- `reset()`'s existing `_configuration_sets.clear()` clears them too — no
  new call needed.

## Error semantics

| Condition | Error code | AWS shape it mirrors |
|---|---|---|
| Configuration set doesn't exist (create/update/delete) | `ConfigurationSetDoesNotExist` (400) | `ConfigurationSetDoesNotExistException` — same code already used by `_delete_configuration_set`/`_describe_configuration_set` |
| Create with a destination name that already exists on that config set | `EventDestinationAlreadyExists` (400) | `EventDestinationAlreadyExistsException` |
| Update/delete a destination name that doesn't exist | `EventDestinationDoesNotExist` (400) | `EventDestinationDoesNotExistException` |

No generic 500s — every failure path returns a specific, AWS-shaped error
code via the file's existing `_error()` helper.

## Verbatim verification

Built and ran standalone, own container/image, port 14573 (not the repo's
compose stack, not their test suite):

```
$ docker build --build-arg MINISTACK_VERSION=v1.4.17 -t ministack-ses-gap:dev .
[...]
naming to docker.io/library/ministack-ses-gap:dev done

$ docker run -d --name ms-ses -p 14573:4566 ministack-ses-gap:dev
```

Create configuration set + event destination:

```
$ aws ses create-configuration-set --configuration-set '{"Name":"gap-test-cs"}' \
    --endpoint-url http://localhost:14573
(no output = success)

$ aws ses create-configuration-set-event-destination \
    --configuration-set-name gap-test-cs \
    --event-destination '{"Name":"sns-events","Enabled":true,"MatchingEventTypes":["bounce","complaint","delivery"],"SNSDestination":{"TopicARN":"arn:aws:sns:us-east-1:000000000000:gap-test-topic"}}' \
    --endpoint-url http://localhost:14573
(no output = success)
```

Describe returns the destination:

```
$ aws ses describe-configuration-set --configuration-set-name gap-test-cs \
    --configuration-set-attribute-names eventDestinations \
    --endpoint-url http://localhost:14573
{
  ConfigurationSet: { Name: "gap-test-cs" }
  EventDestinations:
  [
    {
      Enabled: true
      MatchingEventTypes: ["bounce", "complaint", "delivery"]
      Name: "sns-events"
      SNSDestination: { TopicARN: "arn:aws:sns:us-east-1:000000000000:gap-test-topic" }
    },
  ]
}
```

Duplicate create is rejected with the right error code:

```
$ aws ses create-configuration-set-event-destination --configuration-set-name gap-test-cs \
    --event-destination '{"Name":"sns-events", ...}' --endpoint-url http://localhost:14573
An error occurred (EventDestinationAlreadyExists) when calling the CreateConfigurationSetEventDestination operation: Event destination sns-events already exists
```

Update round-trips:

```
$ aws ses update-configuration-set-event-destination \
    --configuration-set-name gap-test-cs \
    --event-destination '{"Name":"sns-events","Enabled":false,"MatchingEventTypes":["bounce","complaint"],"SNSDestination":{"TopicARN":"arn:aws:sns:us-east-1:000000000000:gap-test-topic-updated"}}' \
    --endpoint-url http://localhost:14573
(no output = success)

$ aws ses describe-configuration-set --configuration-set-name gap-test-cs \
    --configuration-set-attribute-names eventDestinations --endpoint-url http://localhost:14573
{
  ConfigurationSet: { Name: "gap-test-cs" }
  EventDestinations:
  [
    {
      Enabled: false
      MatchingEventTypes: ["bounce", "complaint"]
      Name: "sns-events"
      SNSDestination: { TopicARN: "arn:aws:sns:us-east-1:000000000000:gap-test-topic-updated" }
    },
  ]
}
```

Delete round-trips, and a second delete correctly 400s:

```
$ aws ses delete-configuration-set-event-destination --configuration-set-name gap-test-cs \
    --event-destination-name sns-events --endpoint-url http://localhost:14573
(no output = success)

$ aws ses describe-configuration-set --configuration-set-name gap-test-cs \
    --configuration-set-attribute-names eventDestinations --endpoint-url http://localhost:14573
{ "ConfigurationSet": { "Name": "gap-test-cs" }, "EventDestinations": [] }

$ aws ses delete-configuration-set-event-destination --configuration-set-name gap-test-cs \
    --event-destination-name sns-events --endpoint-url http://localhost:14573
An error occurred (EventDestinationDoesNotExist) when calling the DeleteConfigurationSetEventDestination operation: Event destination sns-events does not exist
```

Container `ms-ses` (and the throwaway `ms-ses-before` used only to capture
the pre-fix error) were stopped and removed after verification
(`docker rm -f ms-ses ms-ses-before`); confirmed no `ms-ses*` containers
remain (`docker ps -a --filter name=ms-ses` returns empty).

## Upstream-PR notes

- Existing service modification, not a new service — per `CONTRIBUTING.md`
  this does not require an issue filed first.
- **Tests still need writing before this is submitted upstream.** Neither
  `tests/test_ses.py` (existing `test_ses_configuration_set_crud` /
  `test_ses_configuration_set_v2` coverage is create/describe/delete of the
  *configuration set itself*, not event destinations) nor a new test module
  currently exercises any of the three new actions or the extended
  `DescribeConfigurationSet` output. Minimum bar for a real PR: create →
  duplicate-create error → describe → update → describe → delete →
  double-delete error, mirroring the CLI round-trip above, plus one
  multi-tenant isolation test (two accounts each create a `sns-events`
  destination on same-named configuration sets and must not see each
  other's).
- CloudWatch and Kinesis Firehose sub-shapes are implemented but have zero
  test coverage — call this out explicitly in the PR description rather
  than presenting them as equally verified to SNS.
- No Dockerfile or `pyproject.toml` changes — no new-dependency issue-first
  gate applies.
