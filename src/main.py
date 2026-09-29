import asyncio
import logging
import time
from datetime import date

import flet as ft
import flet_local_auth as fla
import flet_secure_storage as fss

from otp.accounts import Account, build_migration_uris, migration_compatible, parse_any
from otp.backup import MIN_PASSWORD_LENGTH, BackupError, decrypt_backup, encrypt_backup, merge_restored
from otp.qr import decode_qr, make_qr_png
from otp.scanner import scan_with_camera
from otp.storage import AccountStore

THEME_PREF_KEY = "otapp.theme_mode"
LOCK_PREF_KEY = "otapp.app_lock"
RELOCK_AFTER = 30  # seconds in the background before the app locks again
NEXT_CODE_AT = 5  # show the upcoming code during the last N seconds
CARD_WIDTH = 350


class OTPTile:
    """One account card. Tap copies the code, swipe right for options, swipe left to delete."""

    def __init__(self, account: Account, app: "OTPApp"):
        self.account = account
        self.app = app
        self.name_text = ft.Text(account.name, size=14, color=ft.Colors.ON_PRIMARY_CONTAINER)
        self.otp_text = ft.Text(size=30, color=ft.Colors.ON_PRIMARY_CONTAINER)
        self.countdown_text = ft.Text(size=22, color=ft.Colors.PRIMARY)
        # Solid "pie": stroke drawn inside a fixed 24px box so it never overlaps the countdown
        self.progress_ring = ft.ProgressRing(
            width=24, height=24, stroke_width=12, stroke_align=-1, padding=0,
            size_constraints=ft.BoxConstraints(min_width=24, min_height=24, max_width=24, max_height=24),
        )
        self.next_text = ft.Text(size=14, color=ft.Colors.ON_PRIMARY_CONTAINER)
        self.next_code = ft.Column(
            [ft.Text("next", size=10, color=ft.Colors.ON_PRIMARY_CONTAINER), self.next_text],
            spacing=0,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            opacity=0.6,
            visible=False,
        )
        self.drag_handle = ft.ReorderableDragHandle(
            content=ft.Icon(ft.Icons.DRAG_INDICATOR, size=18, color=ft.Colors.ON_PRIMARY_CONTAINER),
            mouse_cursor=ft.MouseCursor.GRAB,
            opacity=0.5,
        )

        def fill(control, **kwargs):
            return ft.Container(control, left=0, right=0, top=0, bottom=0, **kwargs)

        card = ft.Container(
            content=ft.Stack([
                fill(ft.Column(
                    [
                        ft.Row([self.name_text], alignment=ft.MainAxisAlignment.CENTER),
                        ft.Stack(
                            height=44,
                            controls=[
                                fill(self.next_code, alignment=ft.Alignment.CENTER_LEFT,
                                     padding=ft.Padding.only(left=16)),
                                fill(self.otp_text, alignment=ft.Alignment.CENTER),
                                fill(
                                    ft.Row([self.countdown_text, self.progress_ring],
                                           tight=True, spacing=8),
                                    alignment=ft.Alignment.CENTER_RIGHT,
                                    padding=ft.Padding.only(right=16),
                                ),
                            ],
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.CENTER,
                )),
                ft.Container(self.drag_handle, top=6, right=6),
            ]),
            ink=True,
            border_radius=10,
            width=CARD_WIDTH,
            height=100,
            bgcolor=ft.Colors.PRIMARY_CONTAINER,
            on_click=self.copy_code,
            on_long_press=lambda e: self.app.show_options(self),
        )
        self.dismissible = ft.Dismissible(
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
        # The list stretches items to full width; keep the card centered at its own width
        # Padding outside the Dismissible gives the gap between cards (ReorderableListView ignores spacing)
        self.control = ft.Container(self.dismissible, alignment=ft.Alignment.TOP_CENTER,
                                    padding=ft.Padding.only(bottom=10), data=self)
        self.refresh()

    @staticmethod
    def _swipe_background(color, icon, label, alignment):
        return ft.Container(
            bgcolor=color,
            border_radius=10,
            width=CARD_WIDTH,
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
        totp = self.account.totp()
        self.otp_text.value = totp.at(now)
        self.countdown_text.value = str(remaining)
        self.progress_ring.value = remaining / period
        self.next_code.visible = remaining <= NEXT_CODE_AT
        if self.next_code.visible:
            self.next_text.value = totp.at(now + period)

    def matches(self, query: str) -> bool:
        return query in self.account.name.lower() or query in self.account.issuer.lower()

    async def copy_code(self, e=None):
        await ft.Clipboard().set(self.otp_text.value)
        self.app.snackbar("OTP copied to clipboard")

    async def on_confirm_dismiss(self, e: ft.DismissibleDismissEvent):
        if e.direction == ft.DismissDirection.START_TO_END:
            # Swipe right never removes the tile, it only opens the options sheet
            await self.dismissible.confirm_dismiss(False)
            self.app.show_options(self)
        else:
            await self.dismissible.confirm_dismiss(await self.app.confirm_delete(self.account))


class OTPApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.accounts: dict[str, Account] = {}
        self.load_failed = False
        self._save_lock = asyncio.Lock()
        self.list_view = ft.ReorderableListView(
            show_default_drag_handles=False,
            expand=True,
            padding=ft.Padding.only(bottom=80),  # room for the + button
            on_reorder=self.on_reorder,
        )
        self.search_field = ft.TextField(
            hint_text="Search",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            border_radius=15,
            width=CARD_WIDTH,
            on_change=lambda e: self.apply_filter(),
        )
        self.empty_text = ft.Text(color=ft.Colors.ON_SURFACE_VARIANT, visible=False)
        self.file_picker = ft.FilePicker()
        self.prefs = ft.SharedPreferences()
        mobile = not page.web and page.platform.is_mobile()
        # Keystore/Keychain-backed storage on phones. Never let it silently wipe itself if it
        # can't decrypt: losing every account is worse than showing an error.
        self.store = AccountStore(
            secure=fss.SecureStorage(android_options=fss.AndroidOptions(reset_on_error=False))
            if mobile else None
        )
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
        page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
        self.menu_button = ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            tooltip="More",
            items=[
                ft.PopupMenuItem(content=ft.Text("Back up to file"), icon=ft.Icons.SAVE,
                                 on_click=self.backup_to_file),
                ft.PopupMenuItem(content=ft.Text("Restore from backup"), icon=ft.Icons.RESTORE,
                                 on_click=self.restore_from_file),
                ft.PopupMenuItem(content=ft.Text("Export to Google Authenticator"),
                                 icon=ft.Icons.QR_CODE_2, on_click=self.export_all_qr),
            ],
        )
        page.appbar = ft.AppBar(
            title=ft.Text("OTP App"),
            actions=[self.lock_button, self.theme_button, self.menu_button],
        )
        page.floating_action_button = ft.FloatingActionButton(
            icon=ft.Icons.ADD, tooltip="Add account", on_click=lambda e: self.show_add_menu()
        )
        self.content = ft.Column(
            expand=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                self.search_field,
                ft.Text("Tap to copy · swipe right for options · swipe left to delete",
                        size=11, color=ft.Colors.ON_SURFACE_VARIANT),
                self.empty_text,
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
        page.add(ft.Column([self.content, self.lock_view], expand=True,
                           horizontal_alignment=ft.CrossAxisAlignment.CENTER))

        await self.load_accounts()
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
                for tile in self.tiles():
                    tile.refresh(now)
                self.page.update()
            await asyncio.sleep(1 - now % 1)

    # ---- storage -------------------------------------------------------

    async def load_accounts(self):
        try:
            accounts = await self.store.load()
        except Exception as ex:
            logging.exception("Failed to load accounts")
            # Don't save anything this session, or we'd overwrite whatever is stored
            self.load_failed = True
            self.page.show_dialog(ft.AlertDialog(
                title=ft.Text("Couldn't read your accounts"),
                content=ft.Text(
                    f"{ex}\n\nNothing was deleted. Changes won't be saved until the app can "
                    "read its storage again. Restore from a backup if this keeps happening."
                ),
                actions=[ft.TextButton("OK", on_click=lambda e: self.page.pop_dialog())],
            ))
            accounts = {}
        for account in accounts.values():
            self.list_view.controls.append(OTPTile(account, self).control)
        self._sync_accounts()

    def schedule_save(self):
        self.page.run_task(self._save)

    async def _save(self):
        if self.load_failed:
            self.snackbar("Not saved: the app couldn't read its storage at startup", 4000)
            return
        # Serialize saves; each one writes the latest state
        async with self._save_lock:
            try:
                await self.store.save(self.accounts)
            except Exception as ex:
                logging.exception("Failed to save accounts")
                self.snackbar(f"Couldn't save: {ex}", 5000)

    # ---- list ----------------------------------------------------------

    def tiles(self) -> list[OTPTile]:
        return [c.data for c in self.list_view.controls]

    def _sync_accounts(self):
        """The list order is the source of truth for account order."""
        self.accounts = {t.account.name: t.account for t in self.tiles()}
        self.apply_filter()

    def apply_filter(self):
        query = (self.search_field.value or "").strip().lower()
        shown = 0
        for tile in self.tiles():
            tile.control.visible = not query or tile.matches(query)
            # Reordering a filtered list would be confusing (and indexes wouldn't line up)
            tile.drag_handle.visible = not query
            shown += tile.control.visible
        self.empty_text.visible = shown == 0
        self.empty_text.value = "No matches" if query else "No accounts yet. Tap + to add one."
        self.page.update()

    def on_reorder(self, e: ft.OnReorderEvent):
        item = self.list_view.controls.pop(e.old_index)
        self.list_view.controls.insert(e.new_index, item)
        self._sync_accounts()
        self.schedule_save()

    def move_to_top(self, tile: OTPTile):
        self.list_view.controls.remove(tile.control)
        self.list_view.controls.insert(0, tile.control)
        self._sync_accounts()
        self.schedule_save()

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
        self.menu_button.disabled = locked
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

    async def confirm_sensitive(self, reason: str) -> bool:
        """Secrets leave the app (QR, backup): ask for the phone's lock if app lock is on."""
        return not self.lock_enabled or await self.authenticate(reason)

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

    def add_accounts(self, accounts: list[Account], skipped: int = 0) -> int:
        added = existing = 0
        for account in accounts:
            if account.name in self.accounts:
                existing += 1
                continue
            self.list_view.controls.append(OTPTile(account, self).control)
            self.accounts[account.name] = account
            added += 1
        if added:
            self._sync_accounts()
            self.schedule_save()

        parts = []
        if added:
            parts.append(f"Added {added} account{'s' if added > 1 else ''}")
        if existing:
            parts.append(f"{existing} already added")
        if skipped:
            parts.append(f"{skipped} counter-based (HOTP) skipped")
        self.snackbar(", ".join(parts) or "Nothing to add")
        return added

    def import_text(self, text: str):
        try:
            accounts, skipped = parse_any(text)
        except (ValueError, IndexError) as ex:
            self.snackbar(f"Invalid OTP: {ex}", 4000)
            return
        self.add_accounts(accounts, skipped)

    def remove(self, tile: OTPTile):
        if tile.control in self.list_view.controls:
            self.list_view.controls.remove(tile.control)
        self._sync_accounts()
        self.schedule_save()

    def rename(self, tile: OTPTile, new_name: str):
        new_name = new_name.strip()
        if not new_name or new_name == tile.account.name:
            return
        if new_name in self.accounts:
            self.snackbar("An account with that name already exists")
            return
        tile.account.name = new_name
        tile.name_text.value = new_name
        self._sync_accounts()
        self.schedule_save()

    async def _pick_file_bytes(self, **kwargs) -> tuple[str, bytes] | None:
        files = await self.file_picker.pick_files(with_data=True, **kwargs)
        if not files:
            return None
        f = files[0]
        data = f.bytes
        if data is None and f.path:
            with open(f.path, "rb") as fh:
                data = fh.read()
        return (f.name, data) if data else None

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

    async def ask_password(self, title: str, intro: str, confirm: bool) -> str | None:
        answer = asyncio.get_running_loop().create_future()
        password = ft.TextField(label="Password", password=True, can_reveal_password=True,
                                autofocus=True)
        repeat = ft.TextField(label="Repeat password", password=True, can_reveal_password=True,
                              visible=confirm)

        def close(value):
            if not answer.done():
                answer.set_result(value)
            if dialog.open:
                self.page.pop_dialog()

        def submit(e):
            value = password.value or ""
            password.error = repeat.error = None
            if confirm and len(value) < MIN_PASSWORD_LENGTH:
                password.error = f"At least {MIN_PASSWORD_LENGTH} characters"
            elif confirm and value != repeat.value:
                repeat.error = "Passwords don't match"
            elif not value:
                password.error = "Enter the password"
            else:
                close(value)
                return
            dialog.update()

        password.on_submit = submit if not confirm else (lambda e: repeat.focus())
        repeat.on_submit = submit
        dialog = ft.AlertDialog(
            title=ft.Text(title),
            content=ft.Column([ft.Text(intro, size=13), password, repeat], tight=True, width=320),
            actions=[
                ft.TextButton("Cancel", on_click=lambda e: close(None)),
                ft.TextButton("OK", on_click=submit),
            ],
            on_dismiss=lambda e: close(None),
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

        def top(e):
            close()
            self.move_to_top(tile)

        async def show_qr(e):
            close()
            await self.show_account_qr(tile.account)

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
                        ft.ListTile(leading=ft.Icon(ft.Icons.VERTICAL_ALIGN_TOP),
                                    title=ft.Text("Move to top"), on_click=top),
                        ft.ListTile(leading=ft.Icon(ft.Icons.QR_CODE_2),
                                    title=ft.Text("Show QR code"), on_click=show_qr),
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

    # ---- export & backup -----------------------------------------------

    async def _show_qr_dialog(self, title: str, uris: list[str], note: str):
        """Shows one or more QR codes with previous/next buttons."""
        images = await asyncio.to_thread(lambda: [make_qr_png(u) for u in uris])
        index = 0
        image = ft.Image(src=images[0], width=280, height=280, gapless_playback=True)
        counter = ft.Text(text_align=ft.TextAlign.CENTER)
        prev_btn = ft.IconButton(ft.Icons.CHEVRON_LEFT, tooltip="Previous")
        next_btn = ft.IconButton(ft.Icons.CHEVRON_RIGHT, tooltip="Next")

        def show(i):
            nonlocal index
            index = i
            image.src = images[i]
            counter.value = f"QR code {i + 1} of {len(images)}"
            prev_btn.disabled = i == 0
            next_btn.disabled = i == len(images) - 1

        prev_btn.on_click = lambda e: (show(index - 1), dialog.update())
        next_btn.on_click = lambda e: (show(index + 1), dialog.update())
        show(0)
        multi = len(images) > 1
        dialog = ft.AlertDialog(
            title=ft.Text(title),
            content=ft.Column(
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    # White background so the code scans in dark mode too
                    ft.Container(image, bgcolor=ft.Colors.WHITE, border_radius=8),
                    ft.Row([prev_btn, counter, next_btn], alignment=ft.MainAxisAlignment.CENTER,
                           visible=multi),
                    ft.Text(note, size=12, color=ft.Colors.ON_SURFACE_VARIANT, width=280),
                ],
            ),
            actions=[ft.TextButton("Done", on_click=lambda e: self.page.pop_dialog())],
        )
        self.page.show_dialog(dialog)

    async def show_account_qr(self, account: Account):
        if not await self.confirm_sensitive("Confirm to show the QR code"):
            return
        await self._show_qr_dialog(
            account.name,
            [account.to_uri()],
            "Scan with any authenticator app. Anyone who sees this code can generate your codes.",
        )

    async def export_all_qr(self, e=None):
        accounts = list(self.accounts.values())
        if not accounts:
            self.snackbar("No accounts to export")
            return
        if not await self.confirm_sensitive("Confirm to export your accounts"):
            return
        uris = build_migration_uris(accounts)
        skipped = [a.name for a in accounts if not migration_compatible(a)]
        if not uris:
            self.snackbar("None of these accounts can be exported to Google Authenticator", 4000)
            return
        note = ("In Google Authenticator: Transfer accounts → Import accounts, then scan "
                "each code. Anyone who sees these codes can generate your codes.")
        if skipped:
            note += ("\n\nNot included (Google Authenticator only supports 30-second codes): "
                     + ", ".join(skipped) + ". Use \"Show QR code\" on those accounts instead.")
        await self._show_qr_dialog("Export to Google Authenticator", uris, note)

    async def backup_to_file(self, e=None):
        accounts = list(self.accounts.values())
        if not accounts:
            self.snackbar("No accounts to back up")
            return
        if not await self.confirm_sensitive("Confirm to back up your accounts"):
            return
        password = await self.ask_password(
            "Back up to file",
            "The backup is encrypted with this password. You'll need it to restore, and it "
            "can't be recovered if you forget it.",
            confirm=True,
        )
        if password is None:
            return
        try:
            data = await asyncio.to_thread(encrypt_backup, accounts, password)
            path = await self.file_picker.save_file(
                dialog_title="Save backup",
                file_name=f"otapp-backup-{date.today():%Y-%m-%d}.otapp",
                src_bytes=data,
            )
        except Exception as ex:
            logging.exception("Backup failed")
            self.snackbar(f"Backup failed: {ex}", 5000)
            return
        if path is None and not self.page.web:
            return  # cancelled (on web the browser downloads it and returns no path)
        self.snackbar(f"Backed up {len(accounts)} account{'s' if len(accounts) > 1 else ''}", 3000)

    async def restore_from_file(self, e=None):
        picked = await self._pick_file_bytes(dialog_title="Choose a backup file")
        if not picked:
            return
        name, data = picked
        password = await self.ask_password(
            "Restore from backup", f"Enter the password for {name}.", confirm=False
        )
        if password is None:
            return
        try:
            restored = await asyncio.to_thread(decrypt_backup, data, password)
        except BackupError as ex:
            self.snackbar(str(ex), 4000)
            return

        to_add, already = merge_restored(list(self.accounts.values()), restored)
        if to_add:
            self.add_accounts(to_add)
        else:
            self.snackbar(f"All {already} accounts in the backup are already here", 3000)

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
