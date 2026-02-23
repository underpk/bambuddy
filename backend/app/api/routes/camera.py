"""Camera streaming API endpoints for Bambu Lab printers."""

import asyncio
import logging
import platform
import subprocess
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.auth import RequirePermissionIfAuthEnabled
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.printer import Printer
from backend.app.models.user import User
from backend.app.services.camera import (
    capture_camera_frame,
    generate_chamber_image_stream,
    get_camera_port,
    get_ffmpeg_path,
    is_chamber_image_model,
    read_next_chamber_frame,
    test_camera_connection,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/printers", tags=["camera"])


# ---------------------------------------------------------------------------
# Shared Stream Manager
#
# Problem: each HTTP client hitting /camera/stream used to spawn its own
# FFmpeg (or chamber-image) process.  Opening the page on a phone, a tablet
# and a desktop meant THREE ffmpeg processes decoding the same RTSP feed,
# tripling CPU usage.
#
# Solution: one producer (FFmpeg / chamber socket / external camera) per
# printer.  The producer writes the latest JPEG frame into a shared buffer.
# Each HTTP client is a lightweight consumer that polls the buffer at its
# own FPS and wraps the frame in MJPEG multipart format.
# ---------------------------------------------------------------------------


@dataclass
class _SharedStream:
    """State for a single printer's shared camera stream."""

    printer_id: int
    latest_frame: bytes | None = None
    frame_version: int = 0
    client_count: int = 0
    producer_task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)
    last_frame_time: float = 0.0
    stopping: bool = False


class _SharedStreamManager:
    """One FFmpeg process per printer, many HTTP viewers."""

    def __init__(self) -> None:
        self._streams: dict[int, _SharedStream] = {}
        self._lock = asyncio.Lock()

    # -- public API ----------------------------------------------------------

    async def get_or_create(
        self, printer_id: int, producer_factory
    ) -> _SharedStream:
        """Join an existing shared stream or start a new producer."""
        async with self._lock:
            stream = self._streams.get(printer_id)
            if stream is not None and not stream.stopping:
                stream.client_count += 1
                logger.info(
                    "Client joined shared stream for printer %s (clients: %d)",
                    printer_id,
                    stream.client_count,
                )
                return stream

            # Clean up the old stopping stream if present
            if stream is not None:
                await self._stop_stream(stream)

            # First viewer – spin up the producer
            stream = _SharedStream(printer_id=printer_id)
            self._streams[printer_id] = stream
            stream.client_count = 1
            stream.producer_task = asyncio.create_task(
                self._run_producer(stream, producer_factory)
            )
            logger.info("Started shared stream producer for printer %s", printer_id)
            return stream

    async def release(self, printer_id: int, stream: _SharedStream) -> None:
        """A consumer disconnected.  Stop the producer when nobody is left.

        The caller passes the exact stream it joined so that stale consumers
        from a replaced stream never decrement the wrong counter.
        """
        async with self._lock:
            current = self._streams.get(printer_id)
            if current is not stream:
                # Stream was already replaced – nothing to do
                logger.debug(
                    "Stale consumer released for printer %s (stream already replaced)",
                    printer_id,
                )
                return
            stream.client_count -= 1
            logger.info(
                "Client left shared stream for printer %s (clients: %d)",
                printer_id,
                stream.client_count,
            )
            if stream.client_count <= 0:
                await self._stop_stream(stream)
                self._streams.pop(printer_id, None)

    async def stop_printer(self, printer_id: int) -> bool:
        """Force-stop the stream for a printer (used by the stop endpoint)."""
        async with self._lock:
            stream = self._streams.get(printer_id)
            if not stream:
                return False
            await self._stop_stream(stream)
            self._streams.pop(printer_id, None)
            return True

    def get_stream(self, printer_id: int) -> _SharedStream | None:
        stream = self._streams.get(printer_id)
        return stream if stream and not stream.stopping else None

    def get_latest_frame(self, printer_id: int) -> bytes | None:
        stream = self._streams.get(printer_id)
        return stream.latest_frame if stream and stream.latest_frame else None

    def has_active_stream(self, printer_id: int) -> bool:
        stream = self._streams.get(printer_id)
        return stream is not None and not stream.stopping

    # -- internals -----------------------------------------------------------

    async def _stop_stream(self, stream: _SharedStream) -> None:
        stream.stopping = True
        if stream.producer_task and not stream.producer_task.done():
            stream.producer_task.cancel()
            try:
                await stream.producer_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Stopped shared stream for printer %s", stream.printer_id)

    async def _run_producer(self, stream: _SharedStream, producer_factory) -> None:
        """Consume raw JPEG frames from the producer and broadcast them.

        Automatically restarts the producer if frames stop arriving for
        STALE_TIMEOUT seconds (e.g. FFmpeg RTSP connection dropped silently).
        """
        STALE_TIMEOUT = 15  # seconds without a frame before restarting
        MAX_RESTARTS = 3
        restarts = 0

        while restarts <= MAX_RESTARTS and not stream.stopping:
            try:
                frame_count = 0
                async for frame in producer_factory():
                    if stream.stopping:
                        break
                    stream.latest_frame = frame
                    stream.last_frame_time = time.time()
                    stream.frame_version += 1
                    frame_count += 1

                # Producer generator exited normally
                if stream.stopping:
                    break
                if frame_count == 0:
                    logger.warning("Producer for printer %s yielded 0 frames", stream.printer_id)
                else:
                    logger.warning(
                        "Producer for printer %s ended after %d frames", stream.printer_id, frame_count
                    )

            except asyncio.CancelledError:
                logger.info("Producer cancelled for printer %s", stream.printer_id)
                break
            except Exception as e:
                logger.exception("Producer error for printer %s: %s", stream.printer_id, e)

            # If clients are still connected, retry after a short delay
            if stream.client_count > 0 and not stream.stopping:
                restarts += 1
                logger.info(
                    "Restarting producer for printer %s (attempt %d/%d, %d clients)",
                    stream.printer_id, restarts, MAX_RESTARTS, stream.client_count,
                )
                await asyncio.sleep(2)
            else:
                break

        stream.stopping = True
        logger.info("Producer ended for printer %s", stream.printer_id)


_stream_manager = _SharedStreamManager()


def cleanup_orphaned_ffmpeg() -> None:
    """Kill leftover ffmpeg processes from a previous server instance.

    When the server is killed (taskkill, crash, etc.), child ffmpeg processes
    survive as orphans.  This scans for ffmpeg processes whose command line
    contains our RTSP streaming signature and terminates them.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='ffmpeg.exe'\" | "
             "Where-Object { $_.CommandLine -like '*streaming/live*-f mjpeg*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        pids = [int(p.strip()) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
        if pids:
            logger.info("Cleaning up %d orphaned ffmpeg process(es): %s", len(pids), pids)
            for pid in pids:
                try:
                    handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
                    if handle:
                        ctypes.windll.kernel32.TerminateProcess(handle, 1)
                        ctypes.windll.kernel32.CloseHandle(handle)
                        logger.info("Killed orphaned ffmpeg PID %d", pid)
                except Exception:
                    pass
        else:
            logger.debug("No orphaned ffmpeg processes found")
    except Exception as e:
        logger.warning("Failed to cleanup orphaned ffmpeg: %s", e)


# ---------------------------------------------------------------------------
# Public helpers (imported by main.py, plate_detection, etc.)
# ---------------------------------------------------------------------------


def get_buffered_frame(printer_id: int) -> bytes | None:
    """Get the last buffered frame for a printer from an active shared stream."""
    return _stream_manager.get_latest_frame(printer_id)


def has_active_stream(printer_id: int) -> bool:
    """Check whether a shared stream is running for the given printer."""
    return _stream_manager.has_active_stream(printer_id)


async def get_printer_or_404(printer_id: int, db: AsyncSession) -> Printer:
    """Get printer by ID or raise 404."""
    result = await db.execute(select(Printer).where(Printer.id == printer_id))
    printer = result.scalar_one_or_none()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


# ---------------------------------------------------------------------------
# Raw-frame producers  (one instance per printer, managed by the manager)
# ---------------------------------------------------------------------------


async def _produce_chamber_frames(
    ip_address: str,
    access_code: str,
    model: str | None,
    fps: int = 5,
) -> AsyncGenerator[bytes, None]:
    """Yield raw JPEG frames from A1/P1 chamber-image protocol (port 6000)."""
    logger.info("Starting chamber producer for %s (model=%s, fps=%d)", ip_address, model, fps)

    connection = await generate_chamber_image_stream(ip_address, access_code, fps)
    if connection is None:
        logger.error("Failed to connect to chamber image stream for %s", ip_address)
        return

    reader, writer = connection
    try:
        frame_interval = 1.0 / fps if fps > 0 else 0.2
        last_frame_time = 0.0

        while True:
            frame = await read_next_chamber_frame(reader, timeout=30.0)
            if frame is None:
                logger.warning("Chamber image stream ended for %s", ip_address)
                break

            current_time = asyncio.get_event_loop().time()
            if current_time - last_frame_time < frame_interval:
                continue
            last_frame_time = current_time

            yield frame

    except asyncio.CancelledError:
        logger.info("Chamber producer cancelled for %s", ip_address)
    except Exception as e:
        logger.exception("Chamber producer error: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass
        logger.info("Chamber producer stopped for %s", ip_address)


async def _produce_rtsp_frames(
    ip_address: str,
    access_code: str,
    model: str | None,
    fps: int = 15,
) -> AsyncGenerator[bytes, None]:
    """Yield raw JPEG frames from an RTSP camera via a single FFmpeg process.

    On Windows, a dedicated reader thread pushes chunks into an asyncio.Queue.
    This avoids the problem where ``run_in_executor`` blocking reads cannot be
    cancelled, leaving zombie threads on the default thread pool.
    """
    import queue
    import threading

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        logger.error("ffmpeg not found – camera streaming requires ffmpeg")
        return

    port = get_camera_port(model)
    camera_url = f"rtsps://bblp:{access_code}@{ip_address}:{port}/streaming/live/1"

    cmd = [
        ffmpeg,
        "-rtsp_transport", "tcp",
        "-rtsp_flags", "prefer_tcp",
        "-timeout", "30000000",
        "-buffer_size", "1024000",
        "-max_delay", "500000",
        "-i", camera_url,
        "-vf", f"fps={fps},scale=720:-1",
        "-f", "mjpeg",
        "-q:v", "3",
        "-threads", "2",
        "-an",
        "-",
    ]

    logger.info("Starting RTSP producer for %s (model=%s, fps=%d)", ip_address, model, fps)

    process = None
    _is_windows = platform.system() == "Windows"

    try:
        if _is_windows:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        # Give ffmpeg a moment; bail on immediate failure
        await asyncio.sleep(0.5)
        returncode = process.poll() if _is_windows else process.returncode
        if returncode is not None:
            stderr_data = process.stderr.read() if _is_windows else await process.stderr.read()
            logger.error("ffmpeg failed immediately: %s", stderr_data.decode())
            return

        buffer = b""
        jpeg_start = b"\xff\xd8"
        jpeg_end = b"\xff\xd9"

        if _is_windows:
            # Dedicated reader thread → asyncio.Queue avoids unkillable executor reads
            data_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
            stop_reader = threading.Event()

            def _reader_thread():
                """Read stdout in a tight loop; push chunks or None on EOF/error."""
                try:
                    while not stop_reader.is_set():
                        chunk = process.stdout.read(8192)
                        if not chunk:
                            break
                        try:
                            data_queue.put(chunk, timeout=2)
                        except queue.Full:
                            # Consumer is too slow, drop chunk
                            pass
                except OSError:
                    pass
                finally:
                    data_queue.put(None)  # sentinel

            reader_thread = threading.Thread(target=_reader_thread, daemon=True)
            reader_thread.start()

            try:
                loop = asyncio.get_event_loop()
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            loop.run_in_executor(None, data_queue.get, True, 5),
                            timeout=10.0,
                        )
                    except TimeoutError:
                        # Check if ffmpeg is still alive
                        if process.poll() is not None:
                            logger.warning("RTSP ffmpeg exited (rc=%s) for %s", process.returncode, ip_address)
                        else:
                            logger.warning("RTSP producer read timeout for %s", ip_address)
                        break

                    if chunk is None:
                        logger.warning("RTSP stream ended for %s", ip_address)
                        break

                    buffer += chunk
                    while True:
                        start_idx = buffer.find(jpeg_start)
                        if start_idx == -1:
                            buffer = buffer[-2:] if len(buffer) > 2 else buffer
                            break
                        if start_idx > 0:
                            buffer = buffer[start_idx:]
                        end_idx = buffer.find(jpeg_end, 2)
                        if end_idx == -1:
                            break
                        frame = buffer[: end_idx + 2]
                        buffer = buffer[end_idx + 2 :]
                        yield frame
            finally:
                stop_reader.set()
        else:
            # Linux/Mac: use native async subprocess pipes
            while True:
                try:
                    chunk = await asyncio.wait_for(process.stdout.read(8192), timeout=10.0)
                    if not chunk:
                        logger.warning("RTSP stream ended (no more data)")
                        break

                    buffer += chunk
                    while True:
                        start_idx = buffer.find(jpeg_start)
                        if start_idx == -1:
                            buffer = buffer[-2:] if len(buffer) > 2 else buffer
                            break
                        if start_idx > 0:
                            buffer = buffer[start_idx:]
                        end_idx = buffer.find(jpeg_end, 2)
                        if end_idx == -1:
                            break
                        frame = buffer[: end_idx + 2]
                        buffer = buffer[end_idx + 2 :]
                        yield frame
                except TimeoutError:
                    logger.warning("RTSP producer read timeout")
                    break
                except asyncio.CancelledError:
                    break

    except FileNotFoundError:
        logger.error("ffmpeg not found")
    except asyncio.CancelledError:
        logger.info("RTSP producer cancelled for %s", ip_address)
    except Exception as e:
        logger.exception("RTSP producer error: %s", e)
    finally:
        if process:
            returncode = process.poll() if _is_windows else process.returncode
            if returncode is None:
                logger.info("Terminating ffmpeg process for %s", ip_address)
                try:
                    process.terminate()
                    if _is_windows:
                        try:
                            process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            logger.warning("ffmpeg didn't terminate gracefully, killing")
                            process.kill()
                            process.wait()
                    else:
                        try:
                            await asyncio.wait_for(process.wait(), timeout=2.0)
                        except (TimeoutError, asyncio.TimeoutError):
                            logger.warning("ffmpeg didn't terminate gracefully, killing")
                            process.kill()
                            await process.wait()
                except ProcessLookupError:
                    pass
                except OSError as e:
                    logger.warning("Error terminating ffmpeg: %s", e)
        logger.info("RTSP producer stopped for %s", ip_address)


# ---------------------------------------------------------------------------
# Stream endpoints
# ---------------------------------------------------------------------------


@router.get("/{printer_id}/camera/stream")
async def camera_stream(
    printer_id: int,
    request: Request,
    fps: int = 10,
    db: AsyncSession = Depends(get_db),
):
    """Stream live video from printer camera as MJPEG.

    Uses a **shared stream**: one FFmpeg / camera process per printer,
    multiple browser tabs / devices read from the same frame buffer.

    Args:
        printer_id: Printer ID
        fps: Target frames per second (default: 10, max: 30)
    """
    printer = await get_printer_or_404(printer_id, db)

    # Build the producer factory.  Only called if no stream is active yet.
    if printer.external_camera_enabled and printer.external_camera_url:
        fps = min(max(fps, 1), 15)
        _url = printer.external_camera_url
        _type = printer.external_camera_type
        _fps = fps

        async def producer_factory():
            from backend.app.services.external_camera import generate_raw_frames

            async for frame in generate_raw_frames(_url, _type, _fps):
                yield frame

    elif is_chamber_image_model(printer.model):
        fps = min(max(fps, 1), 5)
        _ip, _code, _model, _fps = printer.ip_address, printer.access_code, printer.model, fps

        async def producer_factory():
            async for frame in _produce_chamber_frames(_ip, _code, _model, _fps):
                yield frame

    else:
        fps = min(max(fps, 1), 30)
        _ip, _code, _model = printer.ip_address, printer.access_code, printer.model
        _producer_fps = min(fps, 15)  # cap producer to save CPU

        async def producer_factory():
            async for frame in _produce_rtsp_frames(_ip, _code, _model, _producer_fps):
                yield frame

    # Join (or create) the shared stream for this printer
    stream = await _stream_manager.get_or_create(printer_id, producer_factory)

    async def consume_stream():
        """Poll the shared buffer and yield MJPEG multipart chunks."""
        frame_interval = 1.0 / max(fps, 1)
        last_version = 0
        STALE_THRESHOLD = 10  # seconds without new frame = stale
        disconnect_check_counter = 0
        try:
            while not stream.stopping:
                # Check client disconnect every ~2 seconds to release resources
                disconnect_check_counter += 1
                if disconnect_check_counter >= int(2.0 / frame_interval):
                    disconnect_check_counter = 0
                    if await request.is_disconnected():
                        logger.info("Client disconnected from stream for printer %s", printer_id)
                        break

                if stream.frame_version != last_version and stream.latest_frame:
                    last_version = stream.frame_version
                    frame = stream.latest_frame
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                        b"\r\n" + frame + b"\r\n"
                    )
                elif stream.last_frame_time and (time.time() - stream.last_frame_time) > STALE_THRESHOLD:
                    # No new frames for too long – stream is stale, break so
                    # the browser reconnects and triggers a fresh producer
                    logger.warning(
                        "Stale stream detected for printer %s (no frames for %ds)",
                        printer_id, STALE_THRESHOLD,
                    )
                    break
                await asyncio.sleep(frame_interval)
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            try:
                await _stream_manager.release(printer_id, stream)
            except Exception:
                pass  # Best-effort cleanup during generator finalization

    return StreamingResponse(
        consume_stream(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.api_route("/{printer_id}/camera/stop", methods=["GET", "POST"])
async def stop_camera_stream(
    printer_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Stop the shared camera stream for a printer.

    Accepts both GET and POST (POST for sendBeacon compatibility).
    """
    stopped = await _stream_manager.stop_printer(printer_id)
    count = 1 if stopped else 0
    logger.info("Stopped %d camera stream(s) for printer %s", count, printer_id)
    return {"stopped": count}


@router.get("/{printer_id}/camera/snapshot")
async def camera_snapshot(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Capture a single frame from the printer camera.

    Returns a JPEG image.  If a shared stream is already running the
    buffered frame is returned instantly (no new FFmpeg process).

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    import tempfile
    from pathlib import Path

    # Fast path: return the latest buffered frame from an active shared stream
    buffered = get_buffered_frame(printer_id)
    if buffered:
        return Response(
            content=buffered,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Content-Disposition": f'inline; filename="snapshot_{printer_id}.jpg"',
            },
        )

    printer = await get_printer_or_404(printer_id, db)

    # Check for external camera first
    if printer.external_camera_enabled and printer.external_camera_url:
        from backend.app.services.external_camera import capture_frame

        frame_data = await capture_frame(printer.external_camera_url, printer.external_camera_type, timeout=15)
        if not frame_data:
            raise HTTPException(
                status_code=503,
                detail="Failed to capture frame from external camera.",
            )
        return Response(
            content=frame_data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Content-Disposition": f'inline; filename="snapshot_{printer_id}.jpg"',
            },
        )

    # Create temporary file for the snapshot
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        temp_path = Path(f.name)

    try:
        success = await capture_camera_frame(
            ip_address=printer.ip_address,
            access_code=printer.access_code,
            model=printer.model,
            output_path=temp_path,
            timeout=15,
        )

        if not success:
            raise HTTPException(
                status_code=503,
                detail="Failed to capture camera frame. Ensure printer is on and camera is enabled.",
            )

        # Read and return the image
        with open(temp_path, "rb") as f:
            image_data = f.read()

        return Response(
            content=image_data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Content-Disposition": f'inline; filename="snapshot_{printer_id}.jpg"',
            },
        )
    finally:
        # Clean up temp file
        if temp_path.exists():
            temp_path.unlink()


@router.get("/{printer_id}/camera/test")
async def test_camera(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Test camera connection for a printer.

    Returns success status and any error message.
    """
    printer = await get_printer_or_404(printer_id, db)

    result = await test_camera_connection(
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
    )

    return result


@router.get("/{printer_id}/camera/status")
async def camera_status(
    printer_id: int,
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Get the status of an active camera stream.

    Returns whether a stream is active and when the last frame was received.
    Used by the frontend to detect stalled streams and auto-reconnect.
    """
    stream = _stream_manager.get_stream(printer_id)

    if not stream:
        return {
            "active": False,
            "has_frames": False,
            "seconds_since_frame": None,
            "stream_uptime": None,
            "stalled": False,
        }

    current_time = time.time()
    seconds_since_frame = (
        current_time - stream.last_frame_time if stream.last_frame_time > 0 else None
    )
    stream_uptime = current_time - stream.started_at

    return {
        "active": True,
        "has_frames": stream.latest_frame is not None,
        "seconds_since_frame": seconds_since_frame,
        "stream_uptime": stream_uptime,
        "stalled": (
            stream_uptime > 5
            and (seconds_since_frame is None or seconds_since_frame > 10)
        ),
    }


@router.post("/{printer_id}/camera/external/test")
async def test_external_camera(
    printer_id: int,
    url: str,
    camera_type: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Test external camera connection.

    Args:
        printer_id: Printer ID (for authorization)
        url: Camera URL or USB device path to test
        camera_type: Camera type ("mjpeg", "rtsp", "snapshot", "usb")

    Returns:
        Dict with {success: bool, error?: str, resolution?: str}
    """
    # Verify printer exists (for authorization)
    await get_printer_or_404(printer_id, db)

    from backend.app.services.external_camera import test_connection

    return await test_connection(url, camera_type)


@router.get("/{printer_id}/camera/check-plate")
async def check_plate_empty(
    printer_id: int,
    plate_type: str | None = None,
    use_external: bool = False,
    include_debug_image: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Check if the build plate is empty using camera vision.

    Uses calibration-based difference detection - compares current frame
    to a reference image of the empty plate.

    IMPORTANT: Chamber light must be ON for reliable detection.

    Args:
        printer_id: Printer ID
        plate_type: Type of build plate (e.g., "High Temp Plate") for calibration lookup
        use_external: If True, prefer external camera over built-in
        include_debug_image: If True, return URL to annotated debug image

    Returns:
        Dict with detection results:
        - is_empty: bool - Whether plate appears empty
        - confidence: float - Confidence level (0.0 to 1.0)
        - difference_percent: float - How different from calibration reference
        - message: str - Human-readable result message
        - needs_calibration: bool - True if calibration is required
        - light_warning: bool - True if chamber light is off
    """
    from backend.app.services.plate_detection import (
        check_plate_empty as do_check,
        is_plate_detection_available,
    )
    from backend.app.services.printer_manager import printer_manager

    # Check printer exists first (before OpenCV check)
    printer = await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(
            status_code=503,
            detail="Plate detection not available. Install opencv-python-headless to enable.",
        )

    # Check chamber light status
    light_warning = False
    state = printer_manager.get_status(printer_id)
    if state and not state.chamber_light:
        light_warning = True

    from backend.app.services.plate_detection import PlateDetector

    # Build ROI tuple from printer settings if available
    roi = None
    if all(
        [
            printer.plate_detection_roi_x is not None,
            printer.plate_detection_roi_y is not None,
            printer.plate_detection_roi_w is not None,
            printer.plate_detection_roi_h is not None,
        ]
    ):
        roi = (
            printer.plate_detection_roi_x,
            printer.plate_detection_roi_y,
            printer.plate_detection_roi_w,
            printer.plate_detection_roi_h,
        )

    result = await do_check(
        printer_id=printer.id,
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
        plate_type=plate_type,
        include_debug_image=include_debug_image,
        external_camera_url=printer.external_camera_url if printer.external_camera_enabled else None,
        external_camera_type=printer.external_camera_type if printer.external_camera_enabled else None,
        use_external=use_external,
        roi=roi,
    )

    # Get reference count for the response
    detector = PlateDetector()
    ref_count = detector.get_calibration_count(printer.id)

    response = result.to_dict()
    response["light_warning"] = light_warning
    response["reference_count"] = ref_count
    response["max_references"] = detector.MAX_REFERENCES
    # Include current ROI in response
    if roi:
        response["roi"] = {"x": roi[0], "y": roi[1], "w": roi[2], "h": roi[3]}
    else:
        # Return default ROI
        response["roi"] = {"x": 0.15, "y": 0.35, "w": 0.70, "h": 0.55}

    # If debug image requested and available, encode as base64 data URL
    if include_debug_image and result.debug_image:
        import base64

        b64_image = base64.b64encode(result.debug_image).decode("utf-8")
        response["debug_image_url"] = f"data:image/jpeg;base64,{b64_image}"

    return response


@router.post("/{printer_id}/camera/plate-detection/calibrate")
async def calibrate_plate_detection(
    printer_id: int,
    label: str | None = None,
    use_external: bool = False,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Calibrate plate detection by capturing a reference image of the empty plate.

    The plate MUST be empty when calling this endpoint. The captured image
    will be used as the reference for future detection comparisons.

    Supports up to 5 reference images per printer. When adding a 6th, the oldest
    is automatically removed.

    IMPORTANT: Chamber light should be ON for calibration.

    Args:
        printer_id: Printer ID
        label: Optional label for this reference (e.g., "High Temp Plate", "Wham Bam")
        use_external: If True, prefer external camera over built-in

    Returns:
        Dict with:
        - success: bool - Whether calibration succeeded
        - message: str - Status message
        - index: int - The reference slot used (0-4)
    """
    from backend.app.services.plate_detection import (
        calibrate_plate,
        is_plate_detection_available,
    )
    from backend.app.services.printer_manager import printer_manager

    # Check printer exists first (before OpenCV check)
    printer = await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(
            status_code=503,
            detail="Plate detection not available. Install opencv-python-headless to enable.",
        )

    # Check chamber light - warn but don't block
    state = printer_manager.get_status(printer_id)
    light_warning = state and not state.chamber_light

    success, message, index = await calibrate_plate(
        printer_id=printer.id,
        ip_address=printer.ip_address,
        access_code=printer.access_code,
        model=printer.model,
        label=label,
        external_camera_url=printer.external_camera_url if printer.external_camera_enabled else None,
        external_camera_type=printer.external_camera_type if printer.external_camera_enabled else None,
        use_external=use_external,
    )

    if light_warning and success:
        message += " (Warning: Chamber light was off)"

    return {"success": success, "message": message, "index": index}


@router.delete("/{printer_id}/camera/plate-detection/calibrate")
async def delete_plate_calibration(
    printer_id: int,
    plate_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Delete the plate detection calibration for a printer and plate type.

    Args:
        printer_id: Printer ID
        plate_type: Type of build plate (if None, deletes legacy non-plate-specific calibration)

    Returns:
        Dict with:
        - success: bool - Whether deletion succeeded
        - message: str - Status message
    """
    from backend.app.services.plate_detection import (
        delete_calibration,
        is_plate_detection_available,
    )

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(
            status_code=503,
            detail="Plate detection not available. Install opencv-python-headless to enable.",
        )

    deleted = delete_calibration(printer_id, plate_type)
    plate_msg = f" for '{plate_type}'" if plate_type else ""

    return {
        "success": deleted,
        "message": f"Calibration deleted{plate_msg}" if deleted else f"No calibration found{plate_msg}",
    }


@router.get("/{printer_id}/camera/plate-detection/status")
async def get_plate_detection_status(
    printer_id: int,
    plate_type: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Check plate detection status for a printer and plate type.

    Returns:
        Dict with:
        - available: bool - Whether OpenCV is installed
        - calibrated: bool - Whether printer has calibration for this plate type
        - plate_type: str - The plate type queried
        - chamber_light: bool - Whether chamber light is on
        - message: str - Status message
    """
    from backend.app.services.plate_detection import (
        get_calibration_status,
        is_plate_detection_available,
    )
    from backend.app.services.printer_manager import printer_manager

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        return {
            "available": False,
            "calibrated": False,
            "plate_type": plate_type,
            "chamber_light": False,
            "message": "OpenCV not installed",
        }

    # Get chamber light status
    state = printer_manager.get_status(printer_id)
    chamber_light = state.chamber_light if state else False

    status = get_calibration_status(printer_id, plate_type)
    status["chamber_light"] = chamber_light

    return status


@router.get("/{printer_id}/camera/plate-detection/references")
async def get_plate_references(
    printer_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Get all calibration references for a printer with metadata.

    Returns list of references with index, label, timestamp, and thumbnail URL.
    """
    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    references = detector.get_references(printer_id)

    # Add thumbnail URLs
    for ref in references:
        ref["thumbnail_url"] = (
            f"/api/v1/printers/{printer_id}/camera/plate-detection/references/{ref['index']}/thumbnail"
        )

    return {
        "references": references,
        "max_references": detector.MAX_REFERENCES,
    }


@router.get("/{printer_id}/camera/plate-detection/references/{index}/thumbnail")
async def get_reference_thumbnail(
    printer_id: int,
    index: int,
    db: AsyncSession = Depends(get_db),
):
    """Get thumbnail image for a calibration reference.

    Note: Unauthenticated - loaded via <img> tags which can't send auth headers.
    """
    from fastapi.responses import Response

    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    thumbnail = detector.get_reference_thumbnail(printer_id, index)

    if thumbnail is None:
        raise HTTPException(404, "Reference not found")

    return Response(content=thumbnail, media_type="image/jpeg")


@router.put("/{printer_id}/camera/plate-detection/references/{index}")
async def update_reference_label(
    printer_id: int,
    index: int,
    label: str,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Update the label for a calibration reference."""
    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    success = detector.update_reference_label(printer_id, index, label)

    if not success:
        raise HTTPException(404, "Reference not found")

    return {"success": True, "index": index, "label": label}


@router.delete("/{printer_id}/camera/plate-detection/references/{index}")
async def delete_reference(
    printer_id: int,
    index: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermissionIfAuthEnabled(Permission.CAMERA_VIEW),
):
    """Delete a specific calibration reference."""
    from backend.app.services.plate_detection import PlateDetector, is_plate_detection_available

    # Verify printer exists first (before OpenCV check)
    await get_printer_or_404(printer_id, db)

    if not is_plate_detection_available():
        raise HTTPException(503, "Plate detection not available")

    detector = PlateDetector()
    success = detector.delete_reference(printer_id, index)

    if not success:
        raise HTTPException(404, "Reference not found")

    return {"success": True, "message": "Reference deleted"}
