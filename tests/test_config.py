"""Tests for configuration: site lists, token resolution, env loading."""

from pathlib import Path

import pytest

from jira_mcp import config
from jira_mcp.config import (
    ConfigError,
    Settings,
    Site,
    find_site,
    load_settings,
    parse_sites,
    site_from_url,
)

ACME = Site("acme", "https://acme.atlassian.net")
OTHER = Site("other", "https://other.atlassian.net")
ENV_KEYS = (
    "ATLASSIAN_EMAIL",
    "ATLASSIAN_SITE_URL",
    "ATLASSIAN_CLOUD_ID",
    *config.TOKEN_KEYS,
    config.JIRA_WRITE_TOKEN_ENV,
    "JIRA_WRITE_PROJECTS",
)


def make_settings(
    *sites: Site,
    api_token: str | None = None,
    jira_api_token: str | None = None,
) -> Settings:
    return Settings(
        email="ada@x.io",
        sites=sites or (ACME, OTHER),
        cloud_id=None,
        env_file_found=True,
        api_token=api_token,
        jira_api_token=jira_api_token,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> pytest.MonkeyPatch:
    """A clean environment that never reads the developer's real .env."""
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATLASSIAN_EMAIL", "ada@x.io")
    monkeypatch.setenv("ATLASSIAN_SITE_URL", ACME.url)
    return monkeypatch


def test_single_site_url_is_one_site() -> None:
    assert parse_sites("https://acme.atlassian.net/") == (ACME,)


def test_comma_separated_sites_keep_order_and_get_names() -> None:
    sites = parse_sites(" acme.atlassian.net , https://other.atlassian.net/, ")
    assert sites == (ACME, OTHER)


def test_custom_domain_is_named_by_full_host() -> None:
    assert parse_sites("https://jira.corp.example")[0].name == "jira.corp.example"


@pytest.mark.parametrize("raw", ["", " , ", "acme.atlassian.net,ACME.atlassian.net"])
def test_empty_or_duplicate_sites_are_rejected(raw: str) -> None:
    with pytest.raises(ConfigError):
        parse_sites(raw)


@pytest.mark.parametrize(
    "ref", ["other", " Other ", "other.atlassian.net", "https://other.atlassian.net/"]
)
def test_find_site_accepts_name_host_or_url(ref: str) -> None:
    assert find_site(make_settings(), ref) == OTHER


def test_find_site_lists_configured_sites_when_unknown() -> None:
    with pytest.raises(ValueError, match=r"acme .*other"):
        find_site(make_settings(), "nope")


def test_site_from_url_matches_host() -> None:
    url = "https://other.atlassian.net/wiki/spaces/ENG/pages/1/T"
    assert site_from_url(make_settings(), url) == OTHER
    assert site_from_url(make_settings(), "123456") is None


def test_site_from_url_rejects_unconfigured_host_only_with_several_sites() -> None:
    url = "https://third.atlassian.net/wiki/spaces/ENG/pages/1/T"
    with pytest.raises(ValueError, match="not a configured site"):
        site_from_url(make_settings(), url)
    assert site_from_url(make_settings(ACME), url) is None


def test_shared_token_serves_both_products() -> None:
    settings = make_settings(api_token="shared")
    assert settings.jira_token == ("shared", "ATLASSIAN_API_TOKEN")
    assert settings.confluence_token == ("shared", "ATLASSIAN_API_TOKEN")


def test_product_tokens_take_precedence_over_shared() -> None:
    settings = make_settings(api_token="shared", jira_api_token="j")
    assert settings.jira_token == ("j", "JIRA_API_TOKEN")
    assert settings.confluence_token == ("shared", "ATLASSIAN_API_TOKEN")


def test_missing_product_token_names_both_variables() -> None:
    settings = make_settings(jira_api_token="j")
    with pytest.raises(ConfigError, match="CONFLUENCE_API_TOKEN.*ATLASSIAN_API_TOKEN"):
        _ = settings.confluence_token


def test_load_settings_with_only_scoped_tokens(env: pytest.MonkeyPatch) -> None:
    env.setenv("ATLASSIAN_SITE_URL", f"{ACME.url},{OTHER.url}")
    env.setenv("JIRA_API_TOKEN", "j")
    env.setenv("CONFLUENCE_API_TOKEN", "c")
    settings = load_settings()
    assert settings.sites == (ACME, OTHER)
    assert settings.default_site == ACME
    assert settings.api_token is None
    assert settings.jira_token.value == "j"
    assert settings.confluence_token.value == "c"


def test_load_settings_requires_some_token(env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="No API token configured"):
        load_settings()


def test_load_settings_requires_site(env: pytest.MonkeyPatch) -> None:
    env.delenv("ATLASSIAN_SITE_URL")
    env.setenv("ATLASSIAN_API_TOKEN", "t")
    with pytest.raises(ConfigError, match="ATLASSIAN_SITE_URL"):
        load_settings()


def test_cloud_id_override_is_rejected_with_several_sites(
    env: pytest.MonkeyPatch,
) -> None:
    env.setenv("ATLASSIAN_API_TOKEN", "t")
    env.setenv("ATLASSIAN_CLOUD_ID", "cid")
    assert load_settings().cloud_id == "cid"
    env.setenv("ATLASSIAN_SITE_URL", f"{ACME.url},{OTHER.url}")
    with pytest.raises(ConfigError, match="ATLASSIAN_CLOUD_ID"):
        load_settings()


def test_load_settings_reads_write_options(env: pytest.MonkeyPatch) -> None:
    env.setenv("JIRA_API_TOKEN", "j")
    env.setenv("JIRA_WRITE_API_TOKEN", "w")
    env.setenv("JIRA_WRITE_PROJECTS", "ac, plat")
    settings = load_settings()
    assert settings.jira_write_projects == frozenset({"AC", "PLAT"})
    assert settings.write_token.source == "JIRA_WRITE_API_TOKEN"


def test_write_token_alone_does_not_satisfy_reads(env: pytest.MonkeyPatch) -> None:
    env.setenv("JIRA_WRITE_API_TOKEN", "w")
    with pytest.raises(ConfigError, match="No API token"):
        load_settings()
