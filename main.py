import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, HTTPException
from google.cloud import storage
from google.oauth2 import service_account
from pydantic import AliasChoices, BaseModel, ConfigDict, Field


app = FastAPI(title="Video Chunker Worker")

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class SplitRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    uri: str
    clippingId: Annotated[str, Field(min_length=1)]
    splittingSeconds: Annotated[int, Field(gt=0)]
    overlappingSeconds: Annotated[int, Field(ge=0)] = 0
    startSeconds: Annotated[
        int | None,
        Field(ge=0, validation_alias=AliasChoices("startSeconds", "start")),
    ] = None
    endSeconds: Annotated[
        int | None,
        Field(gt=0, validation_alias=AliasChoices("endSeconds", "end")),
    ] = None


class ClipMetadata(BaseModel):
    start: int
    end: int
    size: int


class SplitResponse(BaseModel):
    clips: list[str]
    metadata: list[ClipMetadata]


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    if uri.startswith("gs://"):
        path = uri[5:]
    else:
        parsed = urlparse(uri)
        if parsed.scheme != "https" or parsed.netloc != "storage.googleapis.com":
            raise ValueError("uri must be gs://... or https://storage.googleapis.com/...")
        path = unquote(parsed.path.lstrip("/"))

    if "/" not in path:
        raise ValueError("uri must include a bucket and object path")

    bucket_name, blob_name = path.split("/", 1)
    if not bucket_name or not blob_name:
        raise ValueError("uri must include a bucket and object path")

    return bucket_name, blob_name


def validate_clipping_id(clipping_id: str) -> None:
    if not SAFE_ID.fullmatch(clipping_id):
        raise ValueError("clippingId can only contain letters, numbers, _ and -")


def validate_split_parameters(splitting_seconds: int, overlapping_seconds: int) -> None:
    if overlapping_seconds >= splitting_seconds:
        raise ValueError("overlappingSeconds must be lower than splittingSeconds")


def validate_global_range(
    start_seconds: int | None,
    end_seconds: int | None,
    total_duration: float,
) -> tuple[int, int]:
    if (start_seconds is None) != (end_seconds is None):
        raise ValueError("startSeconds and endSeconds must be provided together")

    total_duration_ceiled = math.ceil(total_duration)
    if start_seconds is None or end_seconds is None:
        return 0, total_duration_ceiled

    if end_seconds <= start_seconds:
        raise ValueError("endSeconds must be greater than startSeconds")

    if start_seconds >= total_duration_ceiled:
        raise ValueError("startSeconds must be lower than the video duration")

    if end_seconds > total_duration_ceiled:
        raise ValueError("endSeconds must be lower than or equal to the video duration")

    return start_seconds, end_seconds


def get_storage_client() -> storage.Client:
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        credentials_info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(
            credentials_info
        )
        return storage.Client(
            credentials=credentials,
            project=credentials.project_id,
        )

    return storage.Client()


def output_blob_name(input_blob_name: str, clipping_id: str, clip_id: str) -> str:
    source_dir = os.path.dirname(input_blob_name)
    prefix = f"{source_dir}/" if source_dir else ""
    return f"{prefix}clipping/{clipping_id}/{clip_id}.mp4"


def probe_duration_seconds(input_path: str) -> float:
    command = [
        "ffprobe",
        "-loglevel",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        input_path,
    ]

    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = json.loads(result.stdout)
    return float(payload["format"]["duration"])


def calculate_clip_ranges(
    total_duration: float,
    splitting_seconds: int,
    overlapping_seconds: int,
) -> list[tuple[int, int]]:
    step = splitting_seconds - overlapping_seconds
    total_duration_ceiled = math.ceil(total_duration)
    ranges: list[tuple[int, int]] = []

    start = 0
    while start < total_duration_ceiled:
        end = min(start + splitting_seconds, total_duration_ceiled)
        ranges.append((start, end))
        if end >= total_duration_ceiled:
            break
        start += step

    return ranges


def write_clip(input_path: str, output_path: str, start: int, end: int) -> None:
    duration = end - start
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        str(start),
        "-i",
        input_path,
        "-t",
        str(duration),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-sn",
        "-dn",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        output_path,
    ]

    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def subprocess_error_message(error: subprocess.CalledProcessError) -> str:
    if isinstance(error.stderr, bytes):
        return error.stderr.decode("utf-8", errors="replace")

    return str(error.stderr or error)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/split", response_model=SplitResponse)
def split(request: SplitRequest) -> SplitResponse:
    try:
        return process_split_request(request)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=str(error)) from error


def process_split_request(request: SplitRequest) -> SplitResponse:
    try:
        validate_clipping_id(request.clippingId)
        validate_split_parameters(request.splittingSeconds, request.overlappingSeconds)
        bucket_name, input_blob_name = parse_gcs_uri(request.uri)
    except ValueError as error:
        raise ValueError(str(error)) from error

    bucket = get_storage_client().bucket(bucket_name)
    result_uris: list[str] = []
    metadata: list[ClipMetadata] = []

    with tempfile.TemporaryDirectory() as temp_dir:
        input_path = os.path.join(temp_dir, "input.mp4")
        output_dir = os.path.join(temp_dir, "clips")
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        try:
            bucket.blob(input_blob_name).download_to_filename(input_path)
            range_start, range_end = validate_global_range(
                start_seconds=request.startSeconds,
                end_seconds=request.endSeconds,
                total_duration=probe_duration_seconds(input_path),
            )
            clip_ranges = calculate_clip_ranges(
                total_duration=range_end - range_start,
                splitting_seconds=request.splittingSeconds,
                overlapping_seconds=request.overlappingSeconds,
            )

            for index, (start, end) in enumerate(clip_ranges):
                clip_id = f"{index:06d}"
                local_clip_path = os.path.join(output_dir, f"{clip_id}.mp4")
                source_start = range_start + start
                source_end = range_start + end
                write_clip(
                    input_path=input_path,
                    output_path=local_clip_path,
                    start=source_start,
                    end=source_end,
                )

                output_name = output_blob_name(
                    input_blob_name=input_blob_name,
                    clipping_id=request.clippingId,
                    clip_id=clip_id,
                )

                bucket.blob(output_name).upload_from_filename(
                    local_clip_path,
                    content_type="video/mp4",
                )

                result_uris.append(f"gs://{bucket_name}/{output_name}")
                metadata.append(
                    ClipMetadata(
                        start=source_start,
                        end=source_end,
                        size=os.path.getsize(local_clip_path),
                    )
                )
        except subprocess.CalledProcessError as error:
            stderr = subprocess_error_message(error)
            raise RuntimeError(f"ffmpeg failed: {stderr}") from error

    return SplitResponse(clips=result_uris, metadata=metadata)
