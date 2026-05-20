"""CLI integration tests for the `hyperi-ci init-gitops` subcommand."""

from __future__ import annotations

from typer.testing import CliRunner

from hyperi_ci.cli import app

runner = CliRunner()


def test_cli_init_gitops_creates_repo(tmp_path):
    result = runner.invoke(app, ["init-gitops", str(tmp_path / "gitops")])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "gitops" / "README.md").exists()
    assert (tmp_path / "gitops" / ".github" / "workflows" / "validate.yaml").exists()


def test_cli_init_gitops_substitutes_org(tmp_path):
    result = runner.invoke(
        app,
        ["init-gitops", str(tmp_path / "gitops"), "--org", "my-org"],
    )

    assert result.exit_code == 0, result.output
    codeowners = (tmp_path / "gitops" / "CODEOWNERS").read_text(encoding="utf-8")
    assert "@my-org/platform" in codeowners


def test_cli_init_gitops_exits_2_on_non_empty_without_force(tmp_path):
    target = tmp_path / "gitops"
    target.mkdir()
    (target / "existing.txt").write_text("x")

    result = runner.invoke(app, ["init-gitops", str(target)])

    assert result.exit_code == 2
