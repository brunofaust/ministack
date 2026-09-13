"""ECS_DOCKER_FLAGS: extra `docker run` flags for every ECS task container (unit, no endpoint)."""

import threading
from types import SimpleNamespace

import ministack.services.ecs as ecs_mod


def test_unset_flags_change_nothing(monkeypatch):
    monkeypatch.delenv("ECS_DOCKER_FLAGS", raising=False)
    assert ecs_mod._ecs_docker_flags() == ({}, {})


def test_env_entries_are_split_from_the_other_run_kwargs(monkeypatch):
    monkeypatch.setenv(
        "ECS_DOCKER_FLAGS",
        "-e GITHUB_API_URL=http://github -e APP_BASE_URL=http://app:8000 --add-host mocks:10.0.0.9",
    )
    env, kwargs = ecs_mod._ecs_docker_flags()
    assert env == {"GITHUB_API_URL": "http://github", "APP_BASE_URL": "http://app:8000"}
    assert kwargs == {"extra_hosts": {"mocks": "10.0.0.9"}}


def test_env_entries_override_the_task_definition_environment(monkeypatch):
    # The task definition says one thing (Terraform's dev value); the local stack's flag wins.
    monkeypatch.setenv("ECS_DOCKER_FLAGS", "-e FRONTEND_URL=http://localhost:3000")
    task_env = {"FRONTEND_URL": "https://dev.example.com", "LOG_LEVEL": "INFO"}
    flags_env, _ = ecs_mod._ecs_docker_flags()
    task_env.update(flags_env)
    assert task_env == {"FRONTEND_URL": "http://localhost:3000", "LOG_LEVEL": "INFO"}


def test_flags_reach_the_async_task_worker(monkeypatch):
    """The upstream async RunTask worker still applies every parsed flag."""
    started = threading.Event()
    calls = []

    class FakeContainers:
        def get(self, _name):
            raise RuntimeError("MiniStack container not discoverable in unit test")

        def run(self, image, **kwargs):
            calls.append((image, kwargs))
            started.set()
            return SimpleNamespace(id="flags-container")

    monkeypatch.setattr(
        ecs_mod,
        "_get_docker",
        lambda: SimpleNamespace(containers=FakeContainers()),
    )
    monkeypatch.setenv(
        "ECS_DOCKER_FLAGS",
        "-e FORK_ENDPOINT=http://mocks --network fork-net --add-host mocks:10.0.0.9",
    )
    ecs_mod._register_task_definition({
        "family": "docker-flags-worker",
        "containerDefinitions": [{
            "name": "worker",
            "image": "busybox",
            "environment": [{"name": "FORK_ENDPOINT", "value": "https://dev.example.com"}],
        }],
    })

    ecs_mod._run_task({
        "cluster": "docker-flags-worker",
        "taskDefinition": "docker-flags-worker",
    })

    assert started.wait(timeout=2)
    _image, kwargs = calls[0]
    assert kwargs["environment"]["FORK_ENDPOINT"] == "http://mocks"
    assert kwargs["network"] == "fork-net"
    assert kwargs["extra_hosts"] == {"mocks": "10.0.0.9"}
