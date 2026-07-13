"""RFC 9457 problem responses."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from select_fuzz.api.contracts import Problem, ProblemField


@dataclass(slots=True)
class ApiProblem(Exception):
    status: int
    code: str
    title: str
    detail: str
    errors: tuple[ProblemField, ...] = ()


def response(request: Request, problem: ApiProblem) -> JSONResponse:
    request_id = getattr(request.state, "request_id", uuid4().hex)
    body = Problem(
        type=f"urn:select-fuzz:problem:{problem.code}",
        title=problem.title,
        status=problem.status,
        detail=problem.detail,
        instance=str(request.url.path),
        request_id=request_id,
        errors=list(problem.errors),
    )
    return JSONResponse(
        status_code=problem.status,
        content=body.model_dump(mode="json"),
        media_type="application/problem+json",
        headers={"X-Request-ID": request_id},
    )


async def validation_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RequestValidationError):  # pragma: no cover - registration contract
        raise error
    fields: list[ProblemField] = []
    for item in error.errors():
        location = item.get("loc", ())
        pointer = "/" + "/".join(str(part) for part in location)
        fields.append(ProblemField(pointer=pointer, message=str(item.get("msg", "invalid value"))))
    return response(
        request,
        ApiProblem(422, "validation", "Validation failed", "The request is invalid.", tuple(fields)),
    )


async def api_problem_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ApiProblem):  # pragma: no cover - registration contract
        raise error
    return response(request, error)
