"""Cron agent service-tier propagation."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from cron.scheduler import run_job


def test_run_job_forwards_fast_mode_to_openai_agent(tmp_path: Path) -> None:
    """A config-level Fast preference must reach the actual cron AIAgent."""
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "provider": "openai-codex",
                    "default": "gpt-5.6-sol",
                },
                "agent": {
                    "reasoning_effort": "xhigh",
                    "service_tier": "fast",
                },
                "cron": {"preflight": False},
            }
        )
    )
    job = {
        "id": "service-tier-test",
        "name": "service tier test",
        "prompt": "reply ok",
        "model": "gpt-5.6-sol",
        "provider": "openai-codex",
        "base_url": None,
    }
    fake_db = MagicMock()

    with (
        patch("cron.scheduler._hermes_home", tmp_path),
        patch("cron.scheduler._get_hermes_home", return_value=tmp_path),
        patch("cron.scheduler._resolve_origin", return_value=None),
        patch("hermes_cli.env_loader.load_hermes_dotenv"),
        patch("hermes_cli.env_loader.reset_secret_source_cache"),
        patch("hermes_state.SessionDB", return_value=fake_db),
        patch("tools.mcp_tool.discover_mcp_tools", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://example.invalid/v1",
                "provider": "openai-codex",
                "requested_provider": "openai-codex",
                "api_mode": "codex_responses",
            },
        ),
        patch("run_agent.AIAgent") as agent_cls,
    ):
        agent_cls.return_value.run_conversation.return_value = {
            "final_response": "ok",
        }
        success, _output, final_response, error = run_job(job)

    assert success is True
    assert final_response == "ok"
    assert error is None
    kwargs = agent_cls.call_args.kwargs
    assert kwargs["model"] == "gpt-5.6-sol"
    assert kwargs["provider"] == "openai-codex"
    assert kwargs["reasoning_config"]["effort"] == "xhigh"
    assert kwargs["service_tier"] == "priority"
    assert kwargs["request_overrides"] == {"service_tier": "priority"}
