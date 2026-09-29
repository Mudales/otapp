"""Live camera QR scanner shown in a dialog."""

import asyncio
import logging
import time

import flet as ft
import flet_camera as fc

from otp.qr import decode_qr

FRAME_INTERVAL = 0.25  # seconds between decoded frames


def _is_otp(text: str) -> bool:
    return text.lower().startswith(("otpauth://", "otpauth-migration://"))


async def scan_with_camera(page: ft.Page) -> str | None:
    """Opens the camera and returns the first OTP QR payload seen, or None if cancelled."""
    result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    busy = False
    last_frame = 0.0

    status = ft.Text("Point the camera at a QR code", text_align=ft.TextAlign.CENTER)

    def finish(value: str | None):
        if not result.done():
            result.set_result(value)

    async def handle_image(data: bytes):
        nonlocal busy, last_frame
        if busy or result.done() or time.monotonic() - last_frame < FRAME_INTERVAL:
            return
        busy = True
        try:
            texts = await asyncio.to_thread(decode_qr, data)
        except Exception:
            logging.exception("QR decode failed")
            texts = []
        finally:
            busy = False
            last_frame = time.monotonic()

        otp_texts = [t for t in texts if _is_otp(t)]
        if otp_texts:
            finish(otp_texts[0])
        elif texts and status.value != "QR found, but it isn't an OTP code":
            status.value = "QR found, but it isn't an OTP code"
            status.update()

    async def on_stream_image(e: fc.CameraImageEvent):
        await handle_image(e.bytes)

    camera = fc.Camera(
        expand=True,
        on_stream_image=on_stream_image,
        content=ft.Container(
            alignment=ft.Alignment.CENTER,
            content=ft.Container(
                width=200,
                height=200,
                border=ft.Border.all(3, ft.Colors.WHITE_70),
                border_radius=16,
            ),
        ),
    )
    dialog = ft.AlertDialog(
        title=ft.Text("Scan QR code"),
        content=ft.Column(
            tight=True,
            controls=[
                ft.Container(
                    content=camera,
                    width=320,
                    height=400,
                    border_radius=12,
                    clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    bgcolor=ft.Colors.BLACK,
                ),
                status,
            ],
        ),
        actions=[ft.TextButton("Cancel", on_click=lambda e: finish(None))],
        on_dismiss=lambda e: finish(None),
    )
    page.show_dialog(dialog)

    streaming = False
    try:
        # The camera control may not be mounted on the client right after show_dialog()
        for attempt in range(5):
            try:
                cameras = await camera.get_available_cameras()
                break
            except Exception as ex:
                if attempt == 4 or "CameraException" in str(ex):
                    raise
                await asyncio.sleep(0.3)
        if not cameras:
            raise RuntimeError("No camera found on this device")

        back = next(
            (c for c in cameras if c.lens_direction == fc.CameraLensDirection.BACK),
            cameras[0],
        )
        await camera.initialize(
            back,
            fc.ResolutionPreset.MEDIUM,
            enable_audio=False,
            image_format_group=fc.ImageFormatGroup.JPEG,
        )

        if await camera.supports_image_streaming():
            await camera.start_image_stream()
            streaming = True
            return await result

        # No frame streaming (e.g. some web browsers): poll still pictures instead
        while not result.done():
            await handle_image(await camera.take_picture())
            await asyncio.sleep(FRAME_INTERVAL)
        return result.result()
    except Exception as ex:
        logging.exception("Camera scan failed")
        if "CameraAccessDenied" in str(ex):
            raise RuntimeError("Camera permission denied - allow it in the app settings") from ex
        raise RuntimeError(f"Camera error: {ex}") from ex
    finally:
        finish(None)
        if streaming:
            try:
                await camera.stop_image_stream()
            except Exception:
                pass
        if dialog.open:
            page.pop_dialog()
