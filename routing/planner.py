"""Dynamic route planning independent of index construction and retrieval execution."""

from typing import Protocol, TypeVar

from pydantic import BaseModel

from routing.prompts import route_prompt
from routing.schemas import RoutePlan


SchemaT = TypeVar("SchemaT", bound=BaseModel)


class RouteModelClient(Protocol):
    """The existing guarded model client interface required by routing."""

    def complete_model(self, prompt: str, schema: type[SchemaT]) -> SchemaT: ...


def plan_route(
    *,
    query: str,
    criterion: str,
    titles: list[dict[str, object]],
    methods: dict[str, str],
    client: RouteModelClient,
) -> RoutePlan:
    """Return one schema-validated route plan using the caller's guarded client."""
    prompt = route_prompt(query, criterion, titles, methods)
    return client.complete_model(prompt, RoutePlan)
