"""Hashnode publishing through its GraphQL API (personal access token in Keychain)."""
from __future__ import annotations

import httpx

from .. import secrets
from ..http import make_client as _http_client
from ..runner.protocol import Agent, AgentError, Step

API = "https://gql.hashnode.com"

ME = """query Me { me { username publications(first: 5) { edges { node { id title } } } } }"""

PUBLISH = """mutation Publish($input: PublishPostInput!) {
  publishPost(input: $input) { post { url slug } }
}"""


class HashnodeAgent(Agent):
    name = "hashnode"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}
        self.token = self.settings.get("hashnode_token") or secrets.get_text("hashnode_token")
        self.publication_id: str | None = self.settings.get("publication_id")

    def steps(self, task: dict) -> list[Step]:
        if not self.token:
            raise AgentError("policy", "hashnode_token_missing")
        if not (task.get("prompt") or "").strip():
            raise AgentError("policy", "empty_text")
        return [Step("publication"), Step("publish")]

    def perform(self, step: Step, ctx: dict) -> str | None:
        if step.kind == "publication":
            self.publication_id = self.publication_id or self._first_publication()
            return self.publication_id
        if step.kind == "publish":
            return self._publish(ctx["task"])
        raise AgentError("policy", f"unknown_step:{step.kind}")

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        client = _http_client()
        try:
            response = client.post(
                API,
                json={"query": query, "variables": variables or {}},
                headers={"Authorization": self.token},
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise AgentError("transient", f"hashnode:{type(exc).__name__}") from exc
        if response.status_code in (301, 302, 307, 308):
            raise AgentError(
                "policy",
                "hashnode_api_paid_only: free GraphQL access was retired on 2026-05-13; "
                "a Pro plan on the publication is required",
            )
        if response.status_code in (401, 403):
            raise AgentError("policy", f"hashnode_auth_{response.status_code}")
        if response.status_code == 402:
            raise AgentError("policy", "hashnode_payment_required")
        if response.status_code >= 500:
            raise AgentError("transient", f"hashnode_http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentError("transient", "hashnode_bad_json") from exc
        if payload.get("errors"):
            message = str(payload["errors"][0].get("message", "graphql_error"))[:200]
            raise AgentError("policy", f"hashnode_error:{message}")
        return payload.get("data") or {}

    def _first_publication(self) -> str | None:
        data = self._graphql(ME)
        edges = (((data.get("me") or {}).get("publications") or {}).get("edges") or [])
        if not edges:
            return self.settings.get("publication_id")
        return edges[0]["node"]["id"]

    def _publish(self, task: dict) -> str:
        title = (task.get("title") or "AgentCal").strip()[:200]
        text = (task.get("prompt") or "").strip()
        payload = {"title": title, "contentMarkdown": text}
        if self.publication_id:
            payload["publicationId"] = self.publication_id
        data = self._graphql(PUBLISH, {"input": payload})
        post = (data.get("publishPost") or {}).get("post") or {}
        url = post.get("url")
        if not url:
            raise AgentError("transient", "hashnode_missing_url")
        return url
