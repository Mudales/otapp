import asyncio
import logging
import time

import flet as ft

from otp.accounts import Account, parse_any
from otp.qr import decode_qr
from otp.scanner import scan_with_camera
from otp.storage import AccountStore

store = AccountStore()


class OTPTile:
    """One account card. Tap copies the code, swipe right for options, swipe left to delete."""

    def __init__(self, account: Account, app: "OTPApp"):
        self.account = account
        self.app = app
        self.name_text = ft.Text(account.name, size=14)
        self.otp_text = ft.Text(size=30)
        self.countdown_text = ft.Text(size=30, color=ft.Colors.DEEP_PURPLE_500)
        self.progress_ring = ft.ProgressRing(width=16, height=16, stroke_width=18)

        card = ft.Container(
            content=ft.Column(
                [
                    ft.Row([self.name_text], alignment=ft.MainAxisAlignment.CENTER),
                    ft.Row(
                        [self.otp_text, self.countdown_text, self.progress_ring],
                        alignment=ft.MainAxisAlignment.CENTER,
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ink=True,
            margin=1,
            border_radius=10,
            width=350,
            height=100,
            alignment=ft.Alignment.CENTER,
            bgcolor=ft.Colors.LIGHT_BLUE_100,
            on_click=self.copy_code,
            on_long_press=lambda e: self.app.show_options(self),
        )
        self.control = ft.Dismissible(
            content=card,
            dismiss_direction=ft.DismissDirection.HORIZONTAL,
            background=self._swipe_background(
                ft.Colors.BLUE_400, ft.Icons.MORE_HORIZ, "Options", ft.MainAxisAlignment.START
            ),
            secondary_background=self._swipe_background(
                ft.Colors.RED_400, ft.Icons.DELETE, "Delete", ft.MainAxisAlignment.END
            ),
            dismiss_thresholds={
                ft.DismissDirection.START_TO_END: 0.25,
                ft.DismissDirection.END_TO_START: 0.4,
            },
            on_confirm_dismiss=self.on_confirm_dismiss,
            on_dismiss=lambda e: self.app.remove(self),
        )
        self.refresh()

    @staticmethod
    def _swipe_background(color, icon, label, alignment):
        return ft.Container(
            bgcolor=color,
            border_radius=10,
            width=350,
            padding=ft.Padding.symmetric(horizontal=20),
            content=ft.Row(
                [ft.Icon(icon, color=ft.Colors.WHITE), ft.Text(label, color=ft.Colors.WHITE)],
                alignment=alignment,
            ),
        )

    def refresh(self, now: float | None = None):
        now = time.time() if now is None else now
        period = self.account.period
        remaining = period - int(now) % period
        self.otp_text.value = self.account.totp().at(now)
        self.countdown_text.value = str(remaining)
        self.progress_ring.value = remaining / period

    async def copy_code(self, e=None):
        await ft.Clipboard().set(self.otp_text.value)
        self.app.snackbar("OTP copied to clipboard")

    async def on_confirm_dismiss(self, e: ft.DismissibleDismissEvent):
        if e.direction == ft.DismissDirection.START_TO_END:
            # Swipe right never removes the tile, it only opens the options sheet
            await self.control.confirm_dismiss(False)
            self.app.show_options(self)
        else:
            await self.control.confirm_dismiss(await self.app.confirm_delete(self.account))


class OTPApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.accounts = store.load()
        self.tiles: dict[str, OTPTile] = {}
        self.list_view = ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6
        )
        self.text_field = ft.TextField(
            hint_text="Add otp", width=280, border_radius=15, on_submit=self.add_from_text
        )
        self.file_picker = ft.FilePicker()

    def build(self):
        page = self.page
        page.title = "OTP App"
        page.theme_mode = ft.ThemeMode.LIGHT
        page.scroll = ft.ScrollMode.ADAPTIVE
        page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
        page.appbar = ft.AppBar(
            title=ft.Text("OTP App"),
            actions=[
                ft.IconButton(ft.Icons.QR_CODE_SCANNER, tooltip="Scan with camera",
                              on_click=self.scan_camera),
                ft.IconButton(ft.Icons.IMAGE, tooltip="Import QR from image",
                              on_click=self.scan_image),
            ],
        )
        page.add(
            ft.Row(
                [self.text_field,
                 ft.FloatingActionButton(icon=ft.Icons.ADD, on_click=self.add_from_text)],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            ft.Text("Tap to copy · swipe right for options · swipe left to delete",
                    size=11, color=ft.Colors.GREY_600),
            self.list_view,
        )
        for account in self.accounts.values():
            self._add_tile(account)
        page.update()
        page.run_task(self.tick)

    async def tick(self):
        while True:
            now = time.time()
            for tile in self.tiles.values():
                tile.refresh(now)
            self.page.update()
            await asyncio.sleep(1 - now % 1)

    # ---- helpers -------------------------------------------------------

    def snackbar(self, text: str, duration: int = 2000):
        self.page.show_dialog(ft.SnackBar(ft.Text(text), duration=duration))

    def _add_tile(self, account: Account):
        tile = OTPTile(account, self)
        self.tiles[account.name] = tile
        self.list_view.controls.append(tile.control)

    def import_text(self, text: str):
        try:
            accounts, skipped = parse_any(text)
        except (ValueError, IndexError) as ex:
            self.snackbar(f"Invalid OTP: {ex}", 4000)
            return

        added = existing = 0
        for account in accounts:
            if account.name in self.accounts:
                existing += 1
                continue
            self.accounts[account.name] = account
            self._add_tile(account)
            added += 1
        store.save(self.accounts)
        self.page.update()

        parts = []
        if added:
            parts.append(f"Added {added} account{'s' if added > 1 else ''}")
        if existing:
            parts.append(f"{existing} already added")
        if skipped:
            parts.append(f"{skipped} counter-based (HOTP) skipped")
        self.snackbar(", ".join(parts) or "Nothing to add")

    def remove(self, tile: OTPTile):
        self.accounts.pop(tile.account.name, None)
        self.tiles.pop(tile.account.name, None)
        if tile.control in self.list_view.controls:
            self.list_view.controls.remove(tile.control)
        store.save(self.accounts)
        self.page.update()

    def rename(self, tile: OTPTile, new_name: str):
        new_name = new_name.strip()
        old_name = tile.account.name
        if not new_name or new_name == old_name:
            return
        if new_name in self.accounts:
            self.snackbar("An account with that name already exists")
            return
        # Rebuild the dict so the account keeps its position
        self.accounts = {
            (new_name if k == old_name else k): v for k, v in self.accounts.items()
        }
        self.tiles = {(new_name if k == old_name else k): v for k, v in self.tiles.items()}
        tile.account.name = new_name
        tile.name_text.value = new_name
        store.save(self.accounts)
        self.page.update()

    # ---- dialogs -------------------------------------------------------

    async def confirm_delete(self, account: Account) -> bool:
        answer = asyncio.get_running_loop().create_future()

        def close(value: bool):
            if not answer.done():
                answer.set_result(value)
            if dialog.open:
                self.page.pop_dialog()

        dialog = ft.AlertDialog(
            title=ft.Text("Confirm Remove"),
            content=ft.Text(f"Are you sure you want to remove the OTP for '{account.name}'?"),
            actions=[
                ft.TextButton("Yes", on_click=lambda e: close(True)),
                ft.TextButton("No", on_click=lambda e: close(False)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda e: close(False),
        )
        self.page.show_dialog(dialog)
        return await answer

    def show_options(self, tile: OTPTile):
        def close():
            if sheet.open:
                self.page.pop_dialog()

        async def copy(e):
            close()
            await tile.copy_code()

        def rename(e):
            close()
            self.show_rename(tile)

        async def delete(e):
            close()
            if await self.confirm_delete(tile.account):
                self.remove(tile)

        acc = tile.account
        sheet = ft.BottomSheet(
            ft.Container(
                padding=ft.Padding.only(bottom=16, top=8),
                content=ft.Column(
                    tight=True,
                    controls=[
                        ft.ListTile(
                            title=ft.Text(acc.name, weight=ft.FontWeight.BOLD),
                            subtitle=ft.Text(
                                f"{acc.algorithm} · {acc.digits} digits · {acc.period}s"
                            ),
                        ),
                        ft.ListTile(leading=ft.Icon(ft.Icons.COPY), title=ft.Text("Copy code"),
                                    on_click=copy),
                        ft.ListTile(leading=ft.Icon(ft.Icons.EDIT), title=ft.Text("Rename"),
                                    on_click=rename),
                        ft.ListTile(leading=ft.Icon(ft.Icons.DELETE, color=ft.Colors.RED),
                                    title=ft.Text("Delete", color=ft.Colors.RED),
                                    on_click=delete),
                    ],
                ),
            ),
        )
        self.page.show_dialog(sheet)

    def show_rename(self, tile: OTPTile):
        field = ft.TextField(value=tile.account.name, autofocus=True, label="Name")

        def save(e):
            self.page.pop_dialog()
            self.rename(tile, field.value)

        field.on_submit = save
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text("Rename"),
            content=field,
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: self.page.pop_dialog()),
                ft.TextButton("Save", on_click=save),
            ],
        ))

    # ---- add sources ---------------------------------------------------

    def add_from_text(self, e=None):
        text = self.text_field.value or ""
        self.text_field.value = ""
        if text.strip():
            self.import_text(text)
        else:
            self.page.update()

    async def scan_image(self, e=None):
        files = await self.file_picker.pick_files(
            dialog_title="Choose a QR code image",
            file_type=ft.FilePickerFileType.IMAGE,
            allow_multiple=True,
            with_data=True,
        )
        if not files:
            return
        found = False
        for f in files:
            data = f.bytes
            if data is None and f.path:
                with open(f.path, "rb") as fh:
                    data = fh.read()
            if not data:
                continue
            try:
                texts = await asyncio.to_thread(decode_qr, data)
            except Exception as ex:
                logging.exception("Failed to read %s", f.name)
                self.snackbar(f"Couldn't read {f.name}: {ex}", 4000)
                continue
            for text in texts:
                found = True
                self.import_text(text)
        if not found:
            self.snackbar("No QR code found in the image", 3000)

    async def scan_camera(self, e=None):
        try:
            text = await scan_with_camera(self.page)
        except RuntimeError as ex:
            self.snackbar(str(ex), 5000)
            return
        if text:
            self.import_text(text)


def main(page: ft.Page):
    OTPApp(page).build()


if __name__ == "__main__":
    ft.run(main)
