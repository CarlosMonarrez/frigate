"""Tapo camera SD-card recording APIs."""

import asyncio
import ipaddress
import logging
import os
import secrets
import time
import uuid
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pathvalidate import sanitize_filename
from pydantic import BaseModel
from pytapo import Tapo
from pytapo.media_stream.downloader import Downloader
from starlette.background import BackgroundTask

from frigate.api.auth import require_role
from frigate.api.defs.request.tapo_recordings_body import (
    TapoDownloadRequest,
    TapoRecordingsRequest,
    TapoRecordingSelection,
)
from frigate.api.defs.tags import Tags
from frigate.const import EXPORT_DIR

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=[Tags.export],
    dependencies=[Depends(require_role(["admin"]))],
)

_download_locks: dict[str, asyncio.Lock] = {}
_download_jobs: dict[str, "TapoDownloadJob"] = {}
_running_download_tasks: set[asyncio.Task[None]] = set()
_DOWNLOAD_JOB_TTL = 60 * 60


@dataclass
class TapoDownloadJob:
    """In-memory status for a camera recording download."""

    id: str
    date: date
    status: Literal["running", "completed", "failed"] = "running"
    completed: int = 0
    total: int = 0
    download_url: str | None = None
    archive_path: Path | None = None
    error: str | None = None
    expires_at: float = 0


class TapoCamera(BaseModel):
    """A configured camera with a private RTSP address."""

    name: str
    display_name: str


class TapoRecording(BaseModel):
    """One recording on a camera's SD card."""

    camera_name: str
    start_time: int
    end_time: int
    display_start_time: int
    display_end_time: int


class TapoRecordingsResponse(BaseModel):
    """Recordings found on selected cameras."""

    recordings: list[TapoRecording]


class TapoDownloadResponse(BaseModel):
    """The ID of a newly started download job."""

    job_id: str


class TapoDownloadJobResponse(BaseModel):
    """Progress and download details for a background job."""

    id: str
    status: Literal["running", "completed", "failed"]
    completed: int
    total: int
    download_url: str | None
    error: str | None


def _get_camera_host(request: Request, camera_name: str) -> str:
    """Find a configured camera's private IP from its RTSP inputs."""
    camera = request.app.frigate_config.cameras.get(camera_name)
    if camera is None:
        raise HTTPException(status_code=404, detail="Camera is not configured")

    for camera_input in camera.ffmpeg.inputs:
        stream_url = str(camera_input.path)
        try:
            parsed_url = urlsplit(stream_url)
        except ValueError:
            continue
        if parsed_url.scheme not in {"rtsp", "rtsps"} or not parsed_url.hostname:
            continue

        try:
            address = ipaddress.ip_address(parsed_url.hostname)
        except ValueError:
            continue

        if (
            address.version == 4
            and address.is_private
            and not address.is_loopback
            and not address.is_link_local
            and not address.is_multicast
            and not address.is_unspecified
            and not address.is_reserved
        ):
            return str(address)

    raise HTTPException(
        status_code=400,
        detail="Camera has no private RTSP IP address in its Frigate config",
    )


def _get_recording_times(recordings: object) -> list[tuple[int, int]]:
    """Flatten pytapo's keyed recording results into start/end pairs."""
    times: set[tuple[int, int]] = set()
    if not isinstance(recordings, list):
        return []

    for result in recordings:
        if not isinstance(result, dict):
            continue
        for recording in result.values():
            if not isinstance(recording, dict):
                continue
            try:
                start_time = int(recording["startTime"])
                end_time = int(recording["endTime"])
            except (KeyError, TypeError, ValueError):
                continue
            if 0 < start_time < end_time:
                times.add((start_time, end_time))

    return sorted(times)


async def _connect_to_camera(
    host: str,
    username: str,
    camera_password: str,
    cloud_password: str,
) -> Tapo:
    """Connect to a camera without blocking the API event loop."""
    return await asyncio.to_thread(
        Tapo,
        host,
        username,
        camera_password,
        cloudPassword=cloud_password,
    )


async def _close_camera(tapo: Tapo) -> None:
    """Close pytapo's HTTP session outside the API event loop."""
    try:
        await asyncio.to_thread(tapo.close)
    except Exception:
        logger.debug("Unable to close a Tapo camera session")


async def _fetch_recordings(
    request: Request,
    camera_name: str,
    recording_date: date,
    username: str,
    camera_password: str,
    cloud_password: str,
) -> tuple[Tapo, list[tuple[int, int]], int]:
    """Connect and list one camera's recordings for the selected date."""
    host = _get_camera_host(request, camera_name)
    tapo: Tapo | None = None
    try:
        tapo = await _connect_to_camera(
            host,
            username,
            camera_password,
            cloud_password,
        )
        raw_recordings = await asyncio.to_thread(
            tapo.getRecordings,
            recording_date.strftime("%Y%m%d"),
        )
        time_correction = await asyncio.to_thread(tapo.getTimeCorrection)
    except Exception:
        if tapo is not None:
            await _close_camera(tapo)
        logger.warning("Unable to list SD-card recordings for camera %s", camera_name)
        raise HTTPException(
            status_code=502,
            detail="Unable to access this camera's SD-card recordings. Check its connectivity and Tapo credentials.",
        ) from None

    return tapo, _get_recording_times(raw_recordings), int(time_correction)


@router.get("/tapo/cameras", response_model=list[TapoCamera])
async def get_tapo_cameras(request: Request) -> list[TapoCamera]:
    """Return configured cameras that have a private RTSP address."""
    cameras: list[TapoCamera] = []
    for camera_name, camera in request.app.frigate_config.cameras.items():
        try:
            _get_camera_host(request, camera_name)
        except HTTPException:
            continue
        cameras.append(
            TapoCamera(
                name=camera_name,
                display_name=camera.name or camera_name,
            )
        )

    return cameras


@router.post("/tapo/recordings", response_model=TapoRecordingsResponse)
async def list_tapo_recordings(
    body: TapoRecordingsRequest,
    request: Request,
) -> TapoRecordingsResponse:
    """List recordings saved on selected Tapo cameras for one date."""
    if len(set(body.camera_names)) != len(body.camera_names):
        raise HTTPException(status_code=400, detail="Duplicate camera names")

    results: list[TapoRecording] = []
    for camera_name in body.camera_names:
        tapo, recordings, time_correction = await _fetch_recordings(
            request,
            camera_name,
            body.date,
            body.username,
            body.camera_password.get_secret_value(),
            body.cloud_password.get_secret_value(),
        )
        try:
            results.extend(
                TapoRecording(
                    camera_name=camera_name,
                    start_time=start_time,
                    end_time=end_time,
                    display_start_time=start_time + time_correction,
                    display_end_time=end_time + time_correction,
                )
                for start_time, end_time in recordings
            )
        finally:
            await _close_camera(tapo)

    results.sort(key=lambda recording: recording.display_start_time)
    return TapoRecordingsResponse(recordings=results)


@router.post(
    "/tapo/recordings/download",
    response_model=TapoDownloadResponse,
    status_code=202,
)
async def download_tapo_recordings(
    body: TapoDownloadRequest,
    request: Request,
) -> TapoDownloadResponse:
    """Start a background download of selected camera recordings."""
    selected_ranges: set[tuple[str, int, int]] = set()
    for recording in body.recordings:
        if recording.start_time >= recording.end_time:
            raise HTTPException(status_code=400, detail="Invalid recording range")
        _get_camera_host(request, recording.camera_name)
        selected_range = (
            recording.camera_name,
            recording.start_time,
            recording.end_time,
        )
        if selected_range in selected_ranges:
            raise HTTPException(status_code=400, detail="Duplicate recording")
        selected_ranges.add(selected_range)

    _cleanup_expired_jobs()
    job_id = secrets.token_urlsafe(24)
    job = TapoDownloadJob(id=job_id, date=body.date, total=len(body.recordings))
    _download_jobs[job_id] = job
    task = asyncio.create_task(_run_tapo_download_job(job, body, request))
    _running_download_tasks.add(task)
    task.add_done_callback(_running_download_tasks.discard)
    return TapoDownloadResponse(job_id=job_id)


async def _run_tapo_download_job(
    job: TapoDownloadJob,
    body: TapoDownloadRequest,
    request: Request,
) -> None:
    """Download clips and update in-memory progress for the UI."""
    try:
        await _download_tapo_recordings_for_job(job, body, request)
    except HTTPException as error:
        job.status = "failed"
        job.error = str(error.detail)
        job.expires_at = time.time() + _DOWNLOAD_JOB_TTL
    except Exception:
        logger.warning("Tapo SD-card recording download failed")
        job.status = "failed"
        job.error = "Download failed. Check camera connectivity, SD card availability, and Tapo credentials."
        job.expires_at = time.time() + _DOWNLOAD_JOB_TTL


async def _download_tapo_recordings_for_job(
    job: TapoDownloadJob,
    body: TapoDownloadRequest,
    request: Request,
) -> None:
    """Download selected clips, keep MP4 copies, and package a temporary ZIP."""
    root = Path(EXPORT_DIR).resolve()
    grouped_recordings: dict[str, list[TapoRecordingSelection]] = defaultdict(list)
    for recording in body.recordings:
        grouped_recordings[recording.camera_name].append(recording)

    downloaded_files: list[tuple[Path, str]] = []
    for camera_name, selected_recordings in grouped_recordings.items():
        camera_lock = _download_locks.setdefault(camera_name, asyncio.Lock())
        async with camera_lock:
            tapo, available_recordings, time_correction = await _fetch_recordings(
                request,
                camera_name,
                body.date,
                body.username,
                body.camera_password.get_secret_value(),
                body.cloud_password.get_secret_value(),
            )
            available = set(available_recordings)
            camera_dir = (
                root / "tapo" / sanitize_filename(camera_name) / body.date.isoformat()
            ).resolve()
            if not camera_dir.is_relative_to(root):
                raise HTTPException(status_code=400, detail="Invalid camera path")
            await asyncio.to_thread(camera_dir.mkdir, parents=True, exist_ok=True)

            try:
                for selected in selected_recordings:
                    recording_range = (selected.start_time, selected.end_time)
                    if (
                        selected.camera_name != camera_name
                        or recording_range not in available
                    ):
                        raise HTTPException(
                            status_code=400,
                            detail="A selected recording is no longer available on the camera",
                        )

                    filename = f"{selected.start_time}-{selected.end_time}.mp4"
                    output_path = camera_dir / filename
                    if not output_path.is_file() or output_path.stat().st_size == 0:
                        partial_name = f".{uuid.uuid4().hex}.partial.mp4"
                        try:
                            downloader = Downloader(
                                tapo,
                                selected.start_time,
                                selected.end_time,
                                time_correction,
                                f"{camera_dir}{os.sep}",
                                None,
                                False,
                                50,
                                fileName=partial_name,
                            )
                            async for _status in downloader.download():
                                pass
                            partial_path = camera_dir / partial_name
                            if (
                                not partial_path.is_file()
                                or partial_path.stat().st_size == 0
                            ):
                                raise OSError("The camera returned an empty recording")
                            await asyncio.to_thread(partial_path.replace, output_path)
                        except Exception:
                            logger.warning(
                                "Unable to download an SD-card recording from camera %s",
                                camera_name,
                            )
                            raise HTTPException(
                                status_code=502,
                                detail="Unable to download a recording from the camera",
                            ) from None
                        finally:
                            (camera_dir / partial_name).unlink(missing_ok=True)

                    archive_name = (
                        f"{sanitize_filename(camera_name)}/"
                        f"{body.date.isoformat()}/{filename}"
                    )
                    downloaded_files.append((output_path, archive_name))
                    job.completed += 1
            finally:
                await _close_camera(tapo)

    if not downloaded_files:
        raise HTTPException(status_code=404, detail="No recordings were selected")

    archive_dir = (root / "tapo" / "downloads" / body.date.isoformat()).resolve()
    if not archive_dir.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Invalid download path")
    await asyncio.to_thread(archive_dir.mkdir, parents=True, exist_ok=True)
    archive_path = archive_dir / f"tapo-recordings-{uuid.uuid4().hex[:12]}.zip"

    def create_archive() -> None:
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_STORED) as archive:
            for source_path, archive_name in downloaded_files:
                archive.write(source_path, archive_name)

    await asyncio.to_thread(create_archive)
    job.status = "completed"
    job.archive_path = archive_path
    job.download_url = f"tapo/downloads/{job.id}/file"
    job.expires_at = time.time() + _DOWNLOAD_JOB_TTL


def _cleanup_expired_jobs() -> None:
    """Remove expired job state and temporary archives."""
    now = time.time()
    for job_id, job in list(_download_jobs.items()):
        if job.expires_at and job.expires_at <= now:
            if job.archive_path is not None:
                job.archive_path.unlink(missing_ok=True)
            del _download_jobs[job_id]


@router.get(
    "/tapo/downloads/{job_id}",
    response_model=TapoDownloadJobResponse,
)
async def get_tapo_download_status(job_id: str) -> TapoDownloadJobResponse:
    """Return progress for a Tapo recording download job."""
    _cleanup_expired_jobs()
    job = _download_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Download job expired")

    return TapoDownloadJobResponse(
        id=job.id,
        status=job.status,
        completed=job.completed,
        total=job.total,
        download_url=job.download_url,
        error=job.error,
    )


@router.get("/tapo/downloads/{job_id}/file")
async def get_tapo_download_file(job_id: str) -> FileResponse:
    """Stream a completed archive through the browser's native download flow."""
    _cleanup_expired_jobs()
    job = _download_jobs.get(job_id)
    if job is None or job.status != "completed" or job.archive_path is None:
        raise HTTPException(status_code=404, detail="Download archive is unavailable")

    return FileResponse(
        job.archive_path,
        media_type="application/zip",
        filename=f"tapo-recordings-{job.date.isoformat()}.zip",
        background=BackgroundTask(
            _remove_archive_after_download,
            job_id,
            job.archive_path,
        ),
    )


def _remove_archive_after_download(job_id: str, archive_path: Path) -> None:
    """Free temporary ZIP storage after the browser finishes downloading."""
    job = _download_jobs.get(job_id)
    if job is not None and job.archive_path == archive_path:
        job.archive_path = None
        job.download_url = None
    archive_path.unlink(missing_ok=True)
