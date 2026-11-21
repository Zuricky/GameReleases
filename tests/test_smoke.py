"""Import and configuration sanity checks for the package skeleton."""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import gamereleases
from gamereleases.config import Settings
from gamereleases.domain import models
from gamereleases.domain.models import GuildConfig, PlatformPreset, StoreKind

FORBIDDEN_DOMAIN_IMPORTS = {"discord", "httpx", "sqlite3", "aiohttp", "requests"}


def _all_module_names() -> list[str]:
    return [
        info.name
        for info in pkgutil.walk_packages(
            gamereleases.__path__, prefix=f"{gamereleases.__name__}."
        )
    ]


def test_version_is_exposed() -> None:
    assert gamereleases.__version__


@pytest.mark.parametrize("module_name", _all_module_names())
def test_every_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_domain_has_no_io_dependencies() -> None:
    domain_dir = Path(models.__file__).parent
    for path in domain_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                roots = {node.module.split(".")[0]}
            else:
                continue
            assert not roots & FORBIDDEN_DOMAIN_IMPORTS, f"{path.name} imports {roots}"


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("TIMEZONE", raising=False)

    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert settings.discord_token.get_secret_value() == "discord-token"
    assert settings.twitch_client_secret.get_secret_value() == "client-secret"
    assert "client-secret" not in repr(settings)
    assert settings.log_level == "DEBUG"
    assert settings.db_path == Path("data/game_releases.db")
    assert settings.timezone == "Europe/Rome"
    assert settings.tzinfo.key == "Europe/Rome"


def test_settings_reject_unknown_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
    monkeypatch.setenv("TWITCH_CLIENT_ID", "client-id")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")

    with pytest.raises(ValueError, match="unknown time zone"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_store_priority_follows_declaration_order() -> None:
    ordered = sorted(StoreKind, key=lambda kind: kind.priority)
    assert ordered[0] is StoreKind.STEAM
    assert ordered[-1] is StoreKind.IGDB


def test_guild_config_defaults_and_bounds() -> None:
    config = GuildConfig(guild_id=1)
    assert config.panel_count == 5
    assert config.platform_preset is PlatformPreset.ALL

    with pytest.raises(ValueError):
        GuildConfig(guild_id=1, panel_count=0)
    with pytest.raises(ValueError):
        GuildConfig(guild_id=1, panel_count=11)
