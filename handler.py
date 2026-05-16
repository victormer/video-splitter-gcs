import runpod
from pydantic import ValidationError

from main import SplitRequest, process_split_request


def handler(job: dict) -> dict:
    try:
        request = SplitRequest.model_validate(job.get("input") or {})
        return process_split_request(request).model_dump()
    except ValidationError as error:
        raise ValueError(error.json()) from error


runpod.serverless.start({"handler": handler})
