import asyncio
import logging
import time

import flet as ft
import flet_local_auth as fla

from otp.accounts import Account, parse_any
from otp.qr import decode_qr
from otp.scanner import scan_with_camera
from otp.storage import AccountStore

store = AccountStore()
THEME_PREF_KEY = "otapp.theme_mode"
LOCK_PREF_KEY = "otapp.app_lock"
RELOCK_AFTER = 30  # seconds in the background before the app locks again


class OTPTile:
    """One account card. Tap copies the code, swipe right for options, swipe left to delete."""

    def __init__(self, account: Account, app: "OTPApp"):
        self.account = account
        self.app = app
        self.name_text = ft.Text(account.name, size=14, color=ft.Colors.ON_PRIMARY_CONTAINER)
        self.otp_text = ft.Text(size=30, color=ft.Colors.ON_PRIMARY_CONTAINER)
        self.countdown_text = ft.Text(size=22, color=ft.Colors.PRIMARY)
        self.progress_ring = ft.ProgressRing(width=16, height=16, stroke_width=18)

        card = ft.Container(
            content=ft.Column(
                [
                    ft.Row([self.name_text], alignment=ft.MainAxisAlignment.CENTER),
                    ft.Stack(
                        height=44,
                        controls=[
                            ft.Container(self.otp_text, alignment=ft.Alignment.CENTER,
                                         left=0, right=0, top=0, bottom=0),
                            ft.Container(
                                ft.Row([self.countdown_text, self.progress_ring], tight=True, spacing=16),
                                alignment=ft.Alignment.CENTER_RIGHT,
                                padding=ft.Padding.only(right=16),
                                left=0, right=0, top=0, bottom=0,
                            ),
                        ],
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
            bgcolor=ft.Colors.PRIMARY_CONTAINER,
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
        self.file_picker = ft.FilePicker()
        self.prefs = ft.SharedPreferences()
        # Not available on web/Linux; the service raises there, so only create it where it works
        self.lock_supported = not page.web and page.platform != ft.PagePlatform.LINUX
        self.local_auth = fla.LocalAuthentication() if self.lock_supported else None
        self.lock_enabled = False
        self.locked = False
        self._authenticating = False
        self._hidden_at: float | None = None

    async def build(self):
        page = self.page
        page.title = "OTP App"
        page.theme = ft.Theme(color_scheme_seed=ft.Colors.LIGHT_BLUE)
        page.dark_theme = ft.Theme(color_scheme_seed=ft.Colors.LIGHT_BLUE)
        try:
            saved_mode = await self.prefs.get(THEME_PREF_KEY)
        except Exception:
            saved_mode = None
        page.theme_mode = ft.ThemeMode.DARK if saved_mode == "dark" else ft.ThemeMode.LIGHT
        self.theme_button = ft.IconButton(on_click=self.toggle_theme)
        self._sync_theme_button()
        if self.lock_supported:
            try:
                self.lock_enabled = await self.prefs.get(LOCK_PREF_KEY) == "on"
            except Exception:
                self.lock_enabled = False
        self.lock_button = ft.IconButton(on_click=self.toggle_lock, visible=self.lock_supported)
        self._sync_lock_button()
        page.on_app_lifecycle_state_change = self.on_lifecycle
        page.scroll = ft.ScrollMode.ADAPTIVE
        page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
        page.appbar = ft.AppBar(
            title=ft.Text("OTP App"),
            actions=[
                self.lock_button,
                self.theme_button,
            ],
        )
        page.floating_action_button = ft.FloatingActionButton(
            icon=ft.Icons.ADD, tooltip="Add account", on_click=lambda e: self.show_add_menu()
        )
        self.content = ft.Column(
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text("Tap to copy · swipe right for options · swipe left to delete",
                        size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                self.list_view,
            ],
        )
        self.lock_view = ft.Column(
            visible=False,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
            controls=[
                ft.Container(height=120),
                ft.Icon(ft.Icons.LOCK, size=64, color=ft.Colors.PRIMARY),
                ft.Text("OTP App is locked", size=18),
                ft.FilledButton("Unlock", icon=ft.Icons.FINGERPRINT, on_click=self.unlock),
            ],
        )
        page.add(self.content, self.lock_view)
        for account in self.accounts.values():
            self._add_tile(account)
        if self.lock_enabled:
            self.set_locked(True)
        page.update()
        page.run_task(self.tick)
        if self.lock_enabled:
            page.run_task(self.unlock)

    async def tick(self):
        while True:
            now = time.time()
            if not self.locked:
                for tile in self.tiles.values():
                    tile.refresh(now)
                self.page.update()
            await asyncio.sleep(1 - now % 1)

    # ---- theme ---------------------------------------------------------

    def _sync_theme_button(self):
        dark = self.page.theme_mode == ft.ThemeMode.DARK
        self.theme_button.icon = ft.Icons.LIGHT_MODE if dark else ft.Icons.DARK_MODE
        self.theme_button.tooltip = "Light theme" if dark else "Dark theme"

    async def toggle_theme(self, e=None):
        dark = self.page.theme_mode != ft.ThemeMode.DARK
        self.page.theme_mode = ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT
        self._sync_theme_button()
        self.page.update()
        try:
            await self.prefs.set(THEME_PREF_KEY, "dark" if dark else "light")
        except Exception:
            logging.exception("Failed to save theme preference")

    # ---- app lock ------------------------------------------------------

    def _sync_lock_button(self):
        self.lock_button.icon = ft.Icons.LOCK if self.lock_enabled else ft.Icons.LOCK_OPEN
        self.lock_button.tooltip = "App lock: on" if self.lock_enabled else "App lock: off"

    def set_locked(self, locked: bool):
        self.locked = locked
        if locked:
            # Don't leave account options/dialogs showing over the lock screen
            while self.page.pop_dialog():
                pass
        self.content.visible = not locked
        self.lock_view.visible = locked
        self.page.floating_action_button.visible = not locked
        self.lock_button.disabled = locked
        self.page.update()

    async def authenticate(self, reason: str) -> bool:
        """Shows the system prompt: fingerprint/face, or the phone's PIN/pattern/password."""
        if self._authenticating:
            return False
        self._authenticating = True
        try:
            return await self.local_auth.authenticate(
                reason,
                persist_across_backgrounding=True,
                android_messages=fla.AndroidAuthMessages(sign_in_title="OTP App"),
            )
        except fla.LocalAuthException as ex:
            if ex.code == fla.LocalAuthErrorCode.NO_CREDENTIALS_SET:
                self.snackbar("Set a screen lock (PIN, pattern or fingerprint) on your phone first", 4000)
            elif ex.code not in (fla.LocalAuthErrorCode.USER_CANCELED,
                                 fla.LocalAuthErrorCode.SYSTEM_CANCELED):
                self.snackbar(f"Authentication failed: {ex}", 4000)
            return False
        except Exception as ex:
            logging.exception("Authentication error")
            self.snackbar(f"Authentication failed: {ex}", 4000)
            return False
        finally:
            self._authenticating = False

    async def unlock(self, e=None):
        if await self.authenticate("Unlock to see your codes"):
            self.set_locked(False)

    async def toggle_lock(self, e=None):
        enable = not self.lock_enabled
        # Ask for the phone's lock both ways, so nobody else can turn it off
        if not await self.authenticate("Confirm to turn app lock " + ("on" if enable else "off")):
            return
        self.lock_enabled = enable
        self._sync_lock_button()
        self.page.update()
        try:
            await self.prefs.set(LOCK_PREF_KEY, "on" if enable else "off")
        except Exception:
            logging.exception("Failed to save app lock preference")
        self.snackbar("App lock on" if enable else "App lock off")

    def on_lifecycle(self, e: ft.AppLifecycleStateChangeEvent):
        # The auth prompt itself can pause the app; ignore that
        if not self.lock_enabled or self._authenticating:
            return
        if e.state == ft.AppLifecycleState.HIDE:
            self._hidden_at = time.monotonic()
        elif e.state in (ft.AppLifecycleState.SHOW, ft.AppLifecycleState.RESUME):
            hidden_at, self._hidden_at = self._hidden_at, None
            if hidden_at is not None and not self.locked and time.monotonic() - hidden_at > RELOCK_AFTER:
                self.set_locked(True)
                self.page.run_task(self.unlock)

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

    def show_add_menu(self):
        def pick(action):
            async def handler(e):
                if sheet.open:
                    self.page.pop_dialog()
                result = action()
                if asyncio.iscoroutine(result):
                    await result
            return handler

        sheet = ft.BottomSheet(
            ft.Container(
                padding=ft.Padding.only(bottom=16, top=8),
                content=ft.Column(
                    tight=True,
                    controls=[
                        ft.ListTile(title=ft.Text("Add account", weight=ft.FontWeight.BOLD)),
                        ft.ListTile(leading=ft.Icon(ft.Icons.QR_CODE_SCANNER),
                                    title=ft.Text("Scan QR with camera"),
                                    on_click=pick(self.scan_camera)),
                        ft.ListTile(leading=ft.Icon(ft.Icons.CONTENT_PASTE),
                                    title=ft.Text("Paste otpauth:// link or secret"),
                                    on_click=pick(self.show_paste_dialog)),
                        ft.ListTile(leading=ft.Icon(ft.Icons.IMAGE),
                                    title=ft.Text("Import QR from image"),
                                    on_click=pick(self.scan_image)),
                    ],
                ),
            ),
        )
        self.page.show_dialog(sheet)

    async def show_paste_dialog(self):
        field = ft.TextField(
            label="otpauth:// link or secret",
            autofocus=True,
            multiline=True,
            min_lines=1,
            max_lines=4,
        )

        async def paste_clipboard(e):
            field.value = (await ft.Clipboard().get()) or ""
            field.update()

        def add(e):
            text = field.value or ""
            self.page.pop_dialog()
            if text.strip():
                self.import_text(text)

        field.suffix = ft.IconButton(ft.Icons.CONTENT_PASTE, tooltip="Paste from clipboard",
                                     on_click=paste_clipboard)
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text("Add account"),
            content=ft.Container(field, width=320),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: self.page.pop_dialog()),
                ft.TextButton("Add", on_click=add),
            ],
        ))

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


async def main(page: ft.Page):
    await OTPApp(page).build()


if __name__ == "__main__":
    ft.run(main)
