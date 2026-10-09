"""Minimal, read-only GitLab REST API client (stdlib ``urllib`` only).

Endpoints used (GitLab REST API v4, verified against GitLab's API docs):

* ``GET /projects/:id/pipelines/:pipeline_id``
* ``GET /projects/:id/pipelines/:pipeline_id/jobs`` (retried jobs excluded by default)
* ``GET /projects/:id/jobs/:job_id``
* ``GET /projects/:id/jobs/:job_id/artifacts/*artifact_path``

The token is sent only to the configured GitLab host. Artifact downloads can
redirect through a CDN, so the token is stripped from any cross-host redirect.
The token is never logged or included in exceptions or ``repr``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

TOKEN_HEADER = "PRIVATE-TOKEN"


class GitLabError(Exception):
    """A GitLab API call failed. ``status`` is the HTTP status, or None for
    network errors and malformed responses."""

    def __init__(self, status: int | None, message: str) -> None:
        self.status = status
        super().__init__(f"GitLab API error ({status}): {message}")


class GitLabClient(Protocol):
    def get_pipeline(self, project_id: int, pipeline_id: int) -> dict[str, Any]: ...

    def list_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[dict[str, Any]]: ...

    def get_job(self, project_id: int, job_id: int) -> dict[str, Any]: ...

    def get_job_artifact(self, project_id: int, job_id: int, artifact_path: str) -> bytes: ...


class _NoTokenAcrossHosts(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise GitLabError(None, "refusing non-HTTPS redirect")
        if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header(TOKEN_HEADER.capitalize())
        return new


class HttpGitLabClient:
    MAX_ARTIFACT_BYTES = 1_000_000
    MAX_JSON_BYTES = 5_000_000
    MAX_JOB_PAGES = 10

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("GitLab URL must use https://")
        self._api = base_url.rstrip("/") + "/api/v4"
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoTokenAcrossHosts)

    def __repr__(self) -> str:
        return f"HttpGitLabClient({self._api!r}, token=<redacted>)"

    def _get(self, path: str, limit: int) -> bytes:
        request = urllib.request.Request(self._api + path, headers={TOKEN_HEADER: self._token})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                body = response.read(limit + 1)
        except urllib.error.HTTPError as err:
            raise GitLabError(err.code, err.reason) from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise GitLabError(None, type(err).__name__) from None
        if len(body) > limit:
            raise GitLabError(None, "response too large")
        return body

    def _json(self, path: str) -> Any:
        try:
            return json.loads(self._get(path, self.MAX_JSON_BYTES))
        except json.JSONDecodeError:
            raise GitLabError(None, "malformed JSON response") from None

    def get_pipeline(self, project_id: int, pipeline_id: int) -> dict[str, Any]:
        return self._json(f"/projects/{int(project_id)}/pipelines/{int(pipeline_id)}")

    def list_pipeline_jobs(self, project_id: int, pipeline_id: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for page in range(1, self.MAX_JOB_PAGES + 1):
            batch = self._json(
                f"/projects/{int(project_id)}/pipelines/{int(pipeline_id)}/jobs?per_page=100&page={page}"
            )
            if not isinstance(batch, list):
                raise GitLabError(None, "unexpected jobs response")
            jobs.extend(batch)
            if len(batch) < 100:
                break
        return jobs

    def get_job(self, project_id: int, job_id: int) -> dict[str, Any]:
        return self._json(f"/projects/{int(project_id)}/jobs/{int(job_id)}")

    def get_job_artifact(self, project_id: int, job_id: int, artifact_path: str) -> bytes:
        quoted = urllib.parse.quote(artifact_path, safe="/")
        return self._get(f"/projects/{int(project_id)}/jobs/{int(job_id)}/artifacts/{quoted}", self.MAX_ARTIFACT_BYTES)
