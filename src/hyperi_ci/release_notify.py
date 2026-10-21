# Project:   HyperI CI
# File:      src/hyperi_ci/release_notify.py
# Purpose:   Tell someone a release shipped, or that it died
#
# License:   BUSL-1.1 - HYPERI PTY LIMITED
# Copyright: (c) 2026 HYPERI PTY LIMITED
"""Close the loop after a release, the way semantic-release's github plugin does.

That plugin's ``success`` and ``fail`` steps are not loaded (issue #37), so
this replaces them. Three notifications, all idempotent because a re-run is
normal:

* **success** -- one comment per issue or PR referenced by the commits in the
  release, naming the version, and the version's own failure issue closed when
  a retry shipped it.
* **failure** -- one open issue per broken version, with no gate label so it
  holds the next cut until someone triages it.
* **commit-back-failed** -- the release shipped but ``release-commit`` could not
  push ``VERSION`` and ``CHANGELOG.md`` back to main. The job stays green. One
  open issue per repo carries it, gated ``next-patch``, with a comment per
  later version, because the cause is repo configuration, not the version.

Slack is off unless ``notify.slack.webhook_env`` names an environment variable
holding a webhook URL.
"""

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from hyperi_ci.commit_range import last_version_tag
from hyperi_ci.common import announce, error, info, run_cmd, success, warn
from hyperi_ci.config import CIConfig
from hyperi_ci.curl_config import config_line
from hyperi_ci.fork_version import check_fork
from hyperi_ci.gh import gh_api
from hyperi_ci.labels import expected_labels

# `#123`, but not a colour literal (`#abc`) or a trailing digit of a word.
_ISSUE_REF = re.compile(r"(?:^|[\s(\[,])#(\d+)\b")

# Plain vX.Y.Z only -- a prerelease sorts above its own release under -v:refname.
_RELEASE_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# Marks our own comments so a re-run recognises them. Invisible when rendered.
_MARKER = "<!-- hyperi-ci:release-notify -->"

_FAILURE_TITLE = "Release v{version} failed"

COMMIT_BACK_TITLE = "Release commit-back to main is failing"
COMMIT_BACK_LABEL = "release-commit-back"

# The release shipped, so the fix is wanted soon but holds no cut. A failed
# release carries no gate label, which leaves it untriaged and gating.
COMMIT_BACK_GATE = "next-patch"

# Set by the release workflow to the identity its pushes used: `app` or `github-actions`.
IDENTITY_ENV = "HYPERCI_RELEASE_IDENTITY"

_KEY_MISSING = (
    "The release App key did not reach this job, so the run pushed as "
    "`github-actions`, which no ruleset can exempt. The `GH_APP_PRIVATE_KEY` org "
    "secret reaches every hyperi-io repo, so the caller workflow is dropping it: "
    "where it passes an explicit `secrets:` list, add `GH_APP_PRIVATE_KEY` to it, "
    "or use `secrets: inherit`."
)

_BYPASS_MISSING = (
    "The run pushed as the release bot, `hypersec-ci-bot`, and GitHub still "
    "refused it, so the branch has a rule the bot cannot bypass. It is either a "
    "ruleset or classic branch protection. A ruleset shows in the repo's "
    "Settings > Rules > Insights: add the `hypersec-ci-bot` app to its bypass "
    "list with mode Always, or drop the rule. Classic protection shows under "
    "Settings > Branches: add the app to `Allow specified actors to bypass "
    "required pull requests`. The App key is not the problem."
)


def refusal_advice(identity: str | None = None) -> str:
    """Say why GitHub refused a release push to a branch, and what fixes it.

    Args:
        identity: ``app`` or ``github-actions``, the identity the push used.
            Defaults to :data:`IDENTITY_ENV`. Anything else gives both causes.

    Returns:
        The cause and the fix as one paragraph.

    """
    if identity is None:
        identity = os.environ.get(IDENTITY_ENV, "")
    if identity == "app":
        return _BYPASS_MISSING
    if identity == "github-actions":
        return _KEY_MISSING
    return (
        "The run's *Report the release identity* step says who pushed. As "
        f"`github-actions`: {_KEY_MISSING} As `hypersec-ci-bot`: {_BYPASS_MISSING}"
    )


def _api(args: list[str], *, body: dict | None = None) -> dict | list | None:
    """Call `gh api`, returning the parsed response or None on failure."""
    return _call(args, body=body)[0]


def _call(
    args: list[str], *, body: dict | None = None
) -> tuple[dict | list | None, str]:
    """Call `gh api`, returning the parsed response and, on failure, why.

    Returns:
        ``(response, "")`` on success, ``(None, reason)`` when the call failed
        or its output was not JSON.

    """
    outcome = gh_api(args, body=body)
    return outcome.data, outcome.reason


def _ensure_label(repo: str, name: str) -> None:
    """Create manifest label ``name`` on ``repo`` when it is missing.

    Filing an issue with an unknown label gets GitHub's default colour and no
    description, which ``labels --check`` would then report as drift.
    """
    label = next(entry for entry in expected_labels() if entry.name == name)
    existing, _ = _call([f"repos/{repo}/labels/{quote(name, safe='')}"])
    if isinstance(existing, dict):
        return
    created, why = _call(
        ["-X", "POST", f"repos/{repo}/labels"],
        body={
            "name": label.name,
            "color": label.color,
            "description": label.description,
        },
    )
    if created is None:
        warn(f"release-notify: could not create the {name} label ({why})")


def _previous_tag(version: str, *, cwd: str | None = None) -> str | None:
    """Find the release tag before ``v{version}``, which bounds the release's commits.

    Only a plain ``vX.Y.Z`` bounds the range, because a prerelease sorts above
    its own release under ``-v:refname``. ``v{version}`` itself stays a candidate
    whatever its shape, so a prerelease can find its position in the list. When
    the tag is not in the clone (``tag-head`` made it through the API) the newest
    plain tag is the previous one, as earlier tags arrive with the checkout.
    """
    result = run_cmd(
        ["git", "tag", "--list", "v[0-9]*", "--sort=-v:refname"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if result.returncode != 0:
        return None
    tags = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    current = f"v{version}"
    candidates = [tag for tag in tags if _RELEASE_TAG_RE.match(tag) or tag == current]
    if current in candidates:
        after = candidates[candidates.index(current) + 1 :]
        return after[0] if after else None
    return candidates[0] if candidates else None


def _remote_tag_commit(repo: str, tag: str) -> tuple[str | None, str]:
    """Resolve ``tag`` to its commit through the GitHub API.

    ``tag-head`` creates its tag there, so the job's clone never holds it. An
    annotated tag is dereferenced once, as the release workflow's tag guard does.

    Returns:
        ``(sha, "")``, or ``(None, reason)`` when GitHub has no such tag or could
        not be asked.

    """
    ref, why = _call([f"repos/{repo}/git/ref/tags/{tag}"])
    obj = ref.get("object") if isinstance(ref, dict) else None
    if not isinstance(obj, dict) or not obj.get("sha"):
        return None, why or f"gh api returned no object for {tag}"
    sha = str(obj["sha"])
    if obj.get("type") != "tag":
        return sha, ""
    annotated, why = _call([f"repos/{repo}/git/tags/{sha}"])
    target = annotated.get("object") if isinstance(annotated, dict) else None
    if not isinstance(target, dict) or not target.get("sha"):
        return None, why or f"gh api returned no target for annotated tag {tag}"
    return str(target["sha"]), ""


def _release_ref(
    version: str, *, repo: str | None = None, cwd: str | None = None
) -> str | None:
    """Name the commit ``v{version}`` points at, for git to read a range up to.

    The local tag when the clone holds it, else the commit GitHub says it points
    at. ``tag-head`` makes its tag through the API, so a forced-bump, fork or
    tag-less first release has no local tag. A commit the clone lacks fails the
    git log that follows, which warns.

    Returns:
        ``v{version}``, a commit sha, or None after a warning when neither the
        clone nor GitHub knows the tag.

    """
    tag = f"v{version}"
    local = run_cmd(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
        capture=True,
        check=False,
        cwd=cwd,
    )
    if local.returncode == 0 and local.stdout.strip():
        return tag
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if repo:
        sha, why = _remote_tag_commit(repo, tag)
    else:
        sha, why = None, "GITHUB_REPOSITORY is not set"
    if sha:
        return sha
    announce(
        f"{tag} is not in this clone and GitHub could not resolve it ({why}), "
        "so no issue or PR is told about this release.",
        "hyperi-ci release-notify found no tag",
    )
    return None


def referenced_issues(
    version: str, *, repo: str | None = None, cwd: str | None = None
) -> list[int]:
    """Issue and PR numbers referenced by the commits in this release.

    Reads the commit range since the previous tag, including a squash-merge's
    ``(#123)`` suffix. A fork walks only its own first-parent history, because
    upstream's sync-merged commits carry upstream's issue numbers.

    Args:
        version: Bare version being released.
        repo: ``owner/name`` to ask for a tag the clone lacks. Defaults to
            ``GITHUB_REPOSITORY``.
        cwd: Repository to read. Defaults to the current directory.

    Returns:
        The numbers, sorted. Empty after a warning when the tag cannot be
        resolved or its range cannot be read.

    """
    ref = _release_ref(version, repo=repo, cwd=cwd)
    if ref is None:
        return []
    root = Path(cwd) if cwd else Path.cwd()
    fork = check_fork(root).fork
    if fork:
        previous = last_version_tag(first_parent=True, cwd=root, rev=f"{ref}^")
    else:
        previous = _previous_tag(version, cwd=cwd)
    span = f"{previous}..{ref}" if previous else ref
    log = ["git", "log", "--format=%B"]
    if fork:
        log.append("--first-parent")
    result = run_cmd([*log, span], capture=True, check=False, cwd=cwd)
    if result.returncode != 0:
        announce(
            f"git could not read {span} ({result.stderr.strip() or 'no detail'}), "
            "so no issue or PR is told about this release.",
            "hyperi-ci release-notify found no commits",
        )
        return []
    numbers = {int(match) for match in _ISSUE_REF.findall(result.stdout)}
    return sorted(numbers)


def mentions_version(text: str, version: str) -> bool:
    """Check whether ``text`` names ``v{version}`` and not a longer version.

    A substring test reads ``v1.2.30`` and ``v1.2.3-beta.1`` as ``v1.2.3``.

    Args:
        text: Issue or comment body.
        version: Bare version, without the leading ``v``.

    Returns:
        True when ``v{version}`` appears as a whole version.

    """
    pattern = rf"v{re.escape(version)}(?![0-9A-Za-z.-]*[0-9A-Za-z])"
    return re.search(pattern, text) is not None


def _already_commented(repo: str, number: int, version: str) -> bool:
    """Check whether a previous run already announced this version here."""
    comments = _api([f"repos/{repo}/issues/{number}/comments", "--paginate"])
    if not isinstance(comments, list):
        return False
    return any(
        _MARKER in str(comment.get("body", ""))
        and mentions_version(str(comment.get("body", "")), version)
        for comment in comments
    )


def failure_issue_numbers(issues: object, version: str) -> list[int]:
    """Numbers of the open failure issues that belong to ``version``.

    Args:
        issues: The decoded `GET /issues` response, or anything else when the
            call failed.
        version: Bare version, without the leading ``v``.

    Returns:
        Matching issue numbers, empty when ``issues`` is not a list.

    """
    if not isinstance(issues, list):
        return []
    title = _FAILURE_TITLE.format(version=version)
    numbers: list[int] = []
    for issue in issues:
        if not isinstance(issue, dict) or "number" not in issue:
            continue
        if str(issue.get("title", "")) == title:
            numbers.append(int(issue["number"]))
    return numbers


def _open_failure_issues(repo: str) -> object:
    """Open issues carrying the release-failure label, or None on an API error."""
    return _api(
        [
            "-X",
            "GET",
            f"repos/{repo}/issues",
            "-f",
            "state=open",
            "-f",
            "labels=release-failure",
        ]
    )


def _close_resolved_failures(repo: str, version: str) -> None:
    """Close the failure issue a successful retry of ``version`` has resolved.

    Without this the tracker keeps reporting a release that is on the registry.
    """
    numbers = failure_issue_numbers(_open_failure_issues(repo), version)
    for number in numbers:
        comment = (
            f"{_MARKER}\nA later run shipped **v{version}** -- "
            f"https://github.com/{repo}/releases/tag/v{version}"
        )
        _api(
            ["-X", "POST", f"repos/{repo}/issues/{number}/comments"],
            body={"body": comment},
        )
        closed = _api(
            ["-X", "PATCH", f"repos/{repo}/issues/{number}"],
            body={"state": "closed", "state_reason": "completed"},
        )
        if closed:
            info(f"release-notify: closed #{number}, v{version} shipped on a retry")
        else:
            warn(f"release-notify: could not close #{number} -- close it by hand")


def notify_success(
    *, version: str, repo: str | None = None, cwd: str | None = None
) -> int:
    """Comment on every issue and PR carried by this release.

    Also closes this version's own failure issue, when an earlier attempt
    opened one and a retry is what shipped it.

    Returns:
        0 always -- a notification that fails must never fail a release that
        already shipped.

    """
    version = version.removeprefix("v").strip()
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        warn("release-notify: GITHUB_REPOSITORY not set -- skipping")
        return 0

    _close_resolved_failures(repo, version)

    numbers = referenced_issues(version, repo=repo, cwd=cwd)
    if not numbers:
        info(f"release-notify: no issues or PRs referenced by v{version}")
        return 0

    body = (
        f"{_MARKER}\nReleased in **v{version}** -- "
        f"https://github.com/{repo}/releases/tag/v{version}"
    )
    posted = 0
    for number in numbers:
        if _already_commented(repo, number, version):
            continue
        if _api(
            ["-X", "POST", f"repos/{repo}/issues/{number}/comments"],
            body={"body": body},
        ):
            posted += 1
        else:
            # A #123 in a commit message may be a reference to another repo,
            # or an issue since deleted.
            info(f"release-notify: could not comment on #{number} -- skipping")
    success(f"release-notify: announced v{version} on {posted} issue(s)/PR(s)")
    return 0


def _open_failure_issue(repo: str, version: str, run_url: str) -> int | None:
    """Existing open failure issue for this version, or a newly created one."""
    title = _FAILURE_TITLE.format(version=version)
    existing = failure_issue_numbers(_open_failure_issues(repo), version)
    if existing:
        return existing[0]

    body = (
        f"{_MARKER}\n"
        f"The release of **v{version}** failed.\n\n"
        f"- Run: {run_url or 'see the Actions tab'}\n"
        f"- The tag and the registry artefact may disagree -- check both before "
        f"re-running.\n\n"
        f"Retry with `hyperi-ci publish --version {version}` once the cause is "
        f"fixed, or `hyperi-ci publish --bump patch` to ship past it."
    )
    created = _api(
        ["-X", "POST", f"repos/{repo}/issues"],
        body={"title": title, "body": body, "labels": ["release-failure"]},
    )
    if isinstance(created, dict) and "number" in created:
        return int(created["number"])
    return None


def notify_failure(*, version: str, repo: str | None = None, run_url: str = "") -> int:
    """Open (or reuse) a tracker issue for a release that broke.

    Returns:
        0 always -- the release already failed; this must not add a second
        failure on top of it.

    """
    version = version.removeprefix("v").strip()
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not version:
        warn("release-notify: repository or version unknown -- skipping")
        return 0

    number = _open_failure_issue(repo, version, run_url)
    if number is None:
        error(f"release-notify: could not record the v{version} failure as an issue")
        return 0
    success(f"release-notify: v{version} failure tracked in #{number}")
    return 0


def commit_back_issue(issues: object) -> dict | None:
    """The open commit-back issue among ``issues``, matched on its exact title.

    Args:
        issues: The decoded `GET /issues` response, or anything else when the
            call failed.

    Returns:
        The first matching issue, or None.

    """
    if not isinstance(issues, list):
        return None
    for issue in issues:
        if not isinstance(issue, dict) or "number" not in issue:
            continue
        if str(issue.get("title", "")) == COMMIT_BACK_TITLE:
            return issue
    return None


def _commit_back_body(repo: str, version: str, run_url: str) -> str:
    """Body of a new commit-back issue, first seen on ``version``."""
    run = run_url or "see the Actions tab"
    return (
        f"{_MARKER}\n"
        f"**v{version} shipped, but main was not updated.**\n\n"
        f"- The tag, the GitHub Release and the registry uploads are done: "
        f"https://github.com/{repo}/releases/tag/v{version}\n"
        f"- `VERSION`, `CHANGELOG.md` and any `release.stamp_paths` files on "
        f"main were NOT updated, so they still describe an earlier release.\n"
        f"- Run: {run}\n\n"
        f"Nothing needs re-publishing. This is not a failed release.\n\n"
        f"**Cause and fix.** {refusal_advice()} The *Commit rendered release "
        f"artefacts* step carries GitHub's own refusal.\n\n"
        f"The next release then commits `VERSION` and `CHANGELOG.md` back. "
        f"Close this issue once one does.\n\n"
        f"Later releases that hit the same refusal add a comment here rather "
        f"than a new issue."
    )


def notify_commit_back_failed(
    *, version: str, repo: str | None = None, run_url: str = ""
) -> int:
    """Record a shipped release whose commit-back to main was refused.

    Opens the repo's commit-back issue, or comments on the open one when
    this version is not on it yet.

    Returns:
        0 always -- the release shipped, and this step exists so its job can
        stay green.

    """
    version = version.removeprefix("v").strip()
    repo = repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or not version:
        warn("release-notify: repository or version unknown -- skipping")
        return 0

    issues, why = _call(
        [
            "-X",
            "GET",
            f"repos/{repo}/issues",
            "-f",
            "state=open",
            "-f",
            f"labels={COMMIT_BACK_LABEL}",
        ]
    )
    if not isinstance(issues, list):
        # An unanswered lookup cannot tell "no issue yet" from "one is open".
        warn(
            f"release-notify: could not list the open {COMMIT_BACK_LABEL} issues "
            f"({why or 'the response was not a list'}), so v{version}'s "
            "commit-back failure is not recorded rather than risk a duplicate"
        )
        return 0
    existing = commit_back_issue(issues)
    if existing is None:
        _ensure_label(repo, COMMIT_BACK_GATE)
        created = _api(
            ["-X", "POST", f"repos/{repo}/issues"],
            body={
                "title": COMMIT_BACK_TITLE,
                "body": _commit_back_body(repo, version, run_url),
                "labels": [COMMIT_BACK_LABEL, COMMIT_BACK_GATE],
            },
        )
        if isinstance(created, dict) and "number" in created:
            success(
                f"release-notify: v{version} commit-back failure tracked in "
                f"#{created['number']}"
            )
        else:
            error(f"release-notify: could not open the v{version} commit-back issue")
        return 0

    number = int(existing["number"])
    if mentions_version(str(existing.get("body", "")), version) or (
        _already_commented(repo, number, version)
    ):
        info(f"release-notify: #{number} already records v{version}")
        return 0

    comment = (
        f"{_MARKER}\nv{version} shipped too, and its commit-back to main "
        f"also failed. Run: {run_url or 'see the Actions tab'}\n\n"
        f"{refusal_advice()}"
    )
    if _api(
        ["-X", "POST", f"repos/{repo}/issues/{number}/comments"],
        body={"body": comment},
    ):
        success(f"release-notify: v{version} commit-back failure added to #{number}")
    else:
        error(f"release-notify: could not comment on #{number} for v{version}")
    return 0


def notify_slack(config: CIConfig, *, text: str) -> int:
    """Post to Slack, if a webhook has been configured for this project.

    The webhook URL is a secret read from the environment variable named by
    ``notify.slack.webhook_env``, never from config, and passed to curl on stdin,
    never argv. Unset means no Slack.
    """
    variable = str(config.setting("notify.slack.webhook_env") or "")
    if not variable:
        return 0
    webhook = os.environ.get(variable, "")
    if not webhook:
        warn(f"release-notify: {variable} names no webhook -- skipping Slack")
        return 0

    # -f fails on a rejected webhook. No retry flags: a retried POST posts twice.
    result = run_cmd(
        [
            "curl",
            "-fsS",
            "-X",
            "POST",
            "-K",
            "-",
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps({"text": text}),
        ],
        capture=True,
        check=False,
        stdin_text=config_line("url", webhook),
    )
    if result.returncode != 0:
        warn("release-notify: Slack post failed")
        return 0
    success("release-notify: posted to Slack")
    return 0
