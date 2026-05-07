"""PhotosReport — reports which Piwigo albums had photos updated in a date range."""
from __future__ import annotations

import json
import sys
import threading
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

# ── path: make PiwigoHelpers importable ───────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PiwigoHelpers import AlbumHierarchy
from PiwigoHelpers.DateUtils import parse_date
from PiwigoHelpers.CredentialStore import CredentialStore, CredentialError

# ── paths ─────────────────────────────────────────────────────────────────────
_HERE       = Path(__file__).resolve().parent
_STATE_FILE = _HERE / "PhotosReport State.json"
_REPORT_FILE = _HERE / "report.txt"
_REPORT_HTML = _HERE / "report.html"

_store = CredentialStore(_HERE, "PhotosReport Params.json")


# ── state helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_state(data: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── report logic (runs in background thread) ──────────────────────────────────

def _count_updates(client: AlbumHierarchy.PiwigoClient,
                   cat_id: int,
                   start: date,
                   end: date) -> int:
    """Return the number of images in *cat_id* whose date_available is in [start, end].

    Uses per_page=1 so only paging metadata is fetched — no image data is
    transferred, making this a cheap API call per album.
    """
    result = client._call("pwg.categories.getImages", {
        "cat_id":               cat_id,
        "per_page":             1,
        "page":                 0,
        "f_fields":             "id",
        "f_min_date_available": start.strftime("%Y-%m-%d 00:00:00"),
        "f_max_date_available": end.strftime("%Y-%m-%d 23:59:59"),
    })
    return int(result.get("paging", {}).get("total_count", 0))


def run_report(start: date,
               end: date,
               cutoff: int,
               status_cb,
               progress_cb=None) -> None:
    """Fetch data from Piwigo and write report.txt.  Runs in a worker thread."""
    creds  = _store.load_credentials()
    client = AlbumHierarchy.PiwigoClient(
        creds["url"], creds["username"], creds["password"],
        verify_ssl=creds.get("verify_ssl", True),
    )
    try:
        client.login(creds["username"], creds["password"])

        status_cb("Fetching album list…")
        albums = client.get_albums()
        _ignored = {"xxtest", "yytest", "ztest"}
        checkable = [a for a in albums
                     if int(a.get("nb_images", 0)) > 0
                     and a.get("name", "").lower() not in _ignored]
        total = len(checkable)
        if progress_cb:
            progress_cb(0, total)

        status_cb(f"{"#":>4}  {"Updates":>7}  Album")
        status_cb(f"{'-'*4}  {'-'*7}  {'-'*30}")

        rows          = []
        lesser        = []   # albums with 0 < updates < cutoff
        num           = 0
        checked       = 0
        total_photos  = 0
        total_updated = 0
        for album in checkable:
            cat_id    = int(album["id"])
            nb_direct = int(album.get("nb_images", 0))

            updates = _count_updates(client, cat_id, start, end)
            checked      += 1
            total_photos += nb_direct
            if progress_cb:
                progress_cb(checked, total)
            if updates > 0:
                num           += 1
                total_updated += updates
                status_cb(f"{num:>4}  {updates:>7}  {album.get('name', '')}")
            if updates >= cutoff:
                rows.append({
                    "cat_id":   cat_id,
                    "name":     album.get("name", ""),
                    "fullname": album.get("fullname", album.get("name", "")),
                    "updates":  updates,
                    "total":    nb_direct,
                })
            elif updates > 0:
                lesser.append(album.get("name", ""))

        rows.sort(key=lambda r: r["updates"], reverse=True)
        base_url = creds["url"].rstrip("/")
        _write_report(rows, start, end, cutoff, total, total_photos, total_updated)
        _write_html_report(rows, lesser, base_url, start, end, total, total_photos, total_updated)
        status_cb(f"Done — {len(rows)} album(s) written to {_REPORT_FILE.name} / {_REPORT_HTML.name}")

    finally:
        try:
            client.logout()
        except Exception:
            pass


def _write_report(rows: list, start: date, end: date, cutoff: int,
                  albums_checked: int, total_photos: int, total_updated: int) -> None:
    col_num  = max(len("#"),       len(str(len(rows))))
    col_upd  = max(len("Updates"), 7)
    col_name = max(len("Album"),   *(len(r["name"]) for r in rows) if rows else [0])

    header = f"{'#':>{col_num}}  {'Updates':>{col_upd}}  {'Album':<{col_name}}"
    sep    = f"{'-'*col_num}  {'-'*col_upd}  {'-'*col_name}"

    with _REPORT_FILE.open("w", encoding="utf-8") as f:
        f.write("PhotosReport\n")
        f.write(f"Date range : {start}  to  {end}\n")
        f.write(f"Cutoff     : {cutoff} update(s) minimum\n")
        f.write(f"Generated  : {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"Albums checked : {albums_checked:,}  |  "
                f"Total photos : {total_photos:,}  |  "
                f"Updated in period : {total_updated:,}\n")
        f.write("\n")

        if not rows:
            f.write("No albums met the cutoff threshold.\n")
            return

        f.write(header + "\n")
        f.write(sep + "\n")
        for i, r in enumerate(rows, 1):
            f.write(f"{i:>{col_num}}  {r['updates']:>{col_upd}}  {r['name']:<{col_name}}\n")
        f.write(sep + "\n")
        f.write(f"{len(rows)} album(s) listed.\n")


def _write_html_report(rows: list, lesser: list,
                       base_url: str, start: date, end: date,
                       albums_checked: int, total_photos: int, total_updated: int) -> None:
    def _lesser_line(names: list) -> str:
        if not names:
            return ""
        if len(names) == 1:
            return f"<li>Added a smaller number of photos to {names[0]}.</li>"
        body = ", ".join(names[:-1]) + f" and {names[-1]}"
        return f"<li>Added smaller numbers of photos to {body}.</li>"

    with _REPORT_HTML.open("w", encoding="utf-8") as f:
        f.write(f"<b>Photos added to the following albums:</b><br>\n")
        f.write(f"{albums_checked:,} albums checked, "
                f"{total_photos:,} total photos, "
                f"{total_updated:,} updated in this period<br>\n")
        f.write("<ul>\n")
        for r in rows:
            url = f"{base_url}/index.php?/category/{r['cat_id']}"
            f.write(f'<li><a href="{url}">{r["name"]}</a>,'
                    f' added {r["updates"]} photos</li>\n')
        lesser_line = _lesser_line(lesser)
        if lesser_line:
            f.write(f"{lesser_line}\n")
        f.write("</ul>\n")


# ── application ───────────────────────────────────────────────────────────────

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root   = root
        self._state = _load_state()
        self._busy  = False

        root.title("PhotosReport")
        root.resizable(False, False)
        self._build_ui()
        self._restore_state()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        fields = ttk.Frame(outer)
        fields.pack(fill=tk.X)

        def _field(label: str, row: int, width: int = 22) -> tk.StringVar:
            ttk.Label(fields, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=4
            )
            var = tk.StringVar()
            ttk.Entry(fields, textvariable=var, width=width).grid(
                row=row, column=1, sticky="w", pady=4
            )
            return var

        self._start_var  = _field("Start date:",           0)
        self._end_var    = _field("End date:",              1)
        self._cutoff_var = _field("Cutoff (min updates):",  2, width=8)

        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 6))

        btn_row = ttk.Frame(outer)
        btn_row.pack(fill=tk.X, pady=(0, 4))
        self._gen_btn = ttk.Button(btn_row, text="Generate Report",
                                   command=self._on_generate)
        self._gen_btn.pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row, text="Exit", command=self._on_close).pack(
            side=tk.RIGHT, padx=(4, 0)
        )
        self._progress_var = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self._progress_var,
                  anchor="center").pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Separator(outer, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(6, 6))

        log_frame = ttk.Frame(outer)
        log_frame.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL)
        self._log = tk.Text(
            log_frame, height=10, wrap=tk.WORD, state=tk.DISABLED,
            yscrollcommand=sb.set, font="TkDefaultFont",
        )
        sb.config(command=self._log.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.root.resizable(True, True)

    def _log_msg(self, msg: str) -> None:
        self._log.config(state=tk.NORMAL)
        self._log.insert(tk.END, msg + "\n")
        self._log.see(tk.END)
        self._log.config(state=tk.DISABLED)

    # ── state ─────────────────────────────────────────────────────────────────

    def _restore_state(self) -> None:
        geom = self._state.get("geometry")
        if geom:
            try:
                self.root.geometry(geom)
            except Exception:
                pass
        # Start date and cutoff are persisted; end date always defaults to today.
        self._start_var.set(self._state.get("start_date", ""))
        self._end_var.set(date.today().isoformat())
        self._cutoff_var.set(str(self._state.get("cutoff", 10)))

    def _persist_state(self) -> None:
        self._state["geometry"]   = self.root.geometry()
        self._state["start_date"] = self._start_var.get().strip()
        try:
            self._state["cutoff"] = int(self._cutoff_var.get().strip())
        except ValueError:
            pass
        _save_state(self._state)

    def _on_close(self) -> None:
        if self._busy:
            if not messagebox.askyesno(
                "Report Running",
                "A report is currently running. Exit anyway?",
                default=messagebox.NO,
            ):
                return
        self._persist_state()
        self.root.destroy()

    # ── generate ──────────────────────────────────────────────────────────────

    def _on_generate(self) -> None:
        if self._busy:
            return

        start_str  = self._start_var.get().strip()
        end_str    = self._end_var.get().strip()
        cutoff_str = self._cutoff_var.get().strip()

        start_dt = parse_date(start_str) if start_str else None
        if start_str and start_dt is None:
            messagebox.showerror("Invalid Date",
                                 f"Cannot parse start date: {start_str!r}")
            return
        if start_dt is None:
            start_dt = datetime(1900, 1, 1)

        end_dt = parse_date(end_str) if end_str else None
        if end_str and end_dt is None:
            messagebox.showerror("Invalid Date",
                                 f"Cannot parse end date: {end_str!r}")
            return
        if end_dt is None:
            end_dt = datetime.now()

        try:
            cutoff = int(cutoff_str)
            if cutoff < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Cutoff",
                                 "Cutoff must be a non-negative integer.")
            return

        start = start_dt.date()
        end   = end_dt.date()
        if end < start:
            messagebox.showerror("Invalid Range",
                                 "End date must not be before start date.")
            return

        self._busy = True
        self._gen_btn.config(state=tk.DISABLED)
        self._log.config(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.config(state=tk.DISABLED)
        self._log_msg("Connecting…")

        def status_cb(msg: str) -> None:
            self.root.after(0, lambda m=msg: self._log_msg(m))

        def progress_cb(checked: int, total: int) -> None:
            msg = f"{checked} of {total} albums checked"
            self.root.after(0, lambda m=msg: self._progress_var.set(m))

        def worker() -> None:
            try:
                run_report(start, end, cutoff, status_cb, progress_cb)
            except CredentialError as exc:
                self.root.after(0, lambda e=str(exc): messagebox.showerror(
                    "Credentials Not Found", e
                ))
                self.root.after(0, lambda: self._log_msg("Credentials error — see dialog."))
            except Exception as exc:
                self.root.after(
                    0, lambda e=str(exc): self._log_msg(f"Error: {e}")
                )
            finally:
                self.root.after(0, self._done)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self) -> None:
        self._busy  = False
        self._gen_btn.config(state=tk.NORMAL)
        self._progress_var.set("")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
