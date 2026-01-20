"""
Live GUI for the mock SmartPDU (Tkinter) with a 2x24 outlet grid and power heat map.

Layout:
- 2 columns x 24 rows (48 outlets)
- Outlet numbering: top-to-bottom, left column then right column:
    left column:  1..24
    right column: 25..48
If you want a different numbering (e.g., serpentine), adjust outlet_to_row_col().

Coloring:
- Cell fill is a heat map based on PowerOUTLETn (W):
    low power -> blue
    mid power -> yellow
    high power -> red
- If outlet is OFF/Disabled, the tile is desaturated and marked OFF.

Requirements:
  pip install requests
  (Tkinter usually included; on minimal Linux you may need python3-tk)

Run:
  python pdu_live_gui_heatmap.py --base-url http://127.0.0.1:8000 --pdu-id 2 --user admin --password 123456789 --refresh 1.0
"""

from __future__ import annotations

import argparse
import queue
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import requests
import tkinter as tk
from tkinter import ttk


@dataclass
class OutletData:
    outlet: int
    state: str                    # "Enabled"/"Disabled" typically
    power_w: Optional[float]      # W
    energy_kwh: Optional[float]   # kWh


class SmartPDUClient:
    def __init__(self, base_url: str, pdu_id: str, username: str, password: str, timeout_s: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.pdu_id = pdu_id
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._session.auth = (username, password)
        self.timeout_s = timeout_s

    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        r = self._session.get(url, timeout=self.timeout_s)
        if r.status_code >= 400:
            try:
                body = r.json()
            except Exception:
                body = r.text
            raise RuntimeError(f"GET {path} -> HTTP {r.status_code}: {body}")
        return r.json()

    def get_outlet(self, outlet: int) -> dict:
        return self._get(f"/redfish/v1/PowerEquipment/RackPDUs/{self.pdu_id}/Outlets/{outlet}")

    def get_sensor(self, sensor_id: str) -> dict:
        return self._get(f"/redfish/v1/PowerEquipment/RackPDUs/{self.pdu_id}/Sensors/{sensor_id}")

    def get_outlet_data(self, outlet: int) -> OutletData:
        o = self.get_outlet(outlet)
        status_state = None
        if isinstance(o.get("Status"), dict):
            status_state = o["Status"].get("State")
        state = status_state or o.get("State") or "Unknown"

        p = self.get_sensor(f"PowerOUTLET{outlet}")
        e = self.get_sensor(f"EnergyOUTLET{outlet}")

        power_w = p.get("Reading")
        energy_kwh = e.get("Reading")

        power_w = float(power_w) if power_w is not None else None
        energy_kwh = float(energy_kwh) if energy_kwh is not None else None

        return OutletData(outlet=outlet, state=state, power_w=power_w, energy_kwh=energy_kwh)

    def get_all_outlets_data(self, outlet_count: int = 48) -> Dict[int, OutletData]:
        data: Dict[int, OutletData] = {}
        for n in range(1, outlet_count + 1):
            data[n] = self.get_outlet_data(n)
        return data


def state_to_on(state: str) -> bool:
    s = (state or "").strip().lower()
    if s in {"enabled", "on", "up"}:
        return True
    if s in {"disabled", "off", "down"}:
        return False
    return True


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02x}{g:02x}{b:02x}"


def heat_color(power_w: Optional[float], p_min: float, p_max: float, off: bool) -> str:
    """
    Simple 3-stop gradient:
      t=0.0  -> blue  (low)
      t=0.5  -> yellow(mid)
      t=1.0  -> red   (high)
    OFF tiles are desaturated.
    """
    if power_w is None:
        # unknown -> gray
        base = (220, 220, 220)
        return rgb_to_hex(*base)

    if p_max <= p_min:
        t = 0.0
    else:
        t = (power_w - p_min) / (p_max - p_min)
    t = clamp(t, 0.0, 1.0)

    # blue -> yellow
    if t <= 0.5:
        tt = t / 0.5
        r = int(lerp(40, 255, tt))
        g = int(lerp(90, 235, tt))
        b = int(lerp(220, 80, tt))
    else:
        # yellow -> red
        tt = (t - 0.5) / 0.5
        r = int(lerp(255, 220, tt))
        g = int(lerp(235, 60, tt))
        b = int(lerp(80, 40, tt))

    if off:
        # desaturate toward light gray
        r = int(lerp(r, 235, 0.65))
        g = int(lerp(g, 235, 0.65))
        b = int(lerp(b, 235, 0.65))

    return rgb_to_hex(r, g, b)


def fmt_power(power_w: Optional[float]) -> str:
    if power_w is None:
        return "P: n/a"
    if power_w >= 1000.0:
        return f"P: {power_w/1000.0:.2f} kW"
    return f"P: {power_w:.0f} W"


def fmt_energy(energy_kwh: Optional[float]) -> str:
    if energy_kwh is None:
        return "E: n/a"
    return f"E: {energy_kwh:.3f} kWh"


def outlet_to_row_col(outlet: int) -> Tuple[int, int]:
    """
    2x24 layout.
    left col: 1..24 (top->bottom)
    right col: 25..48 (top->bottom)
    Returns (row, col) with row in [0..23], col in [0..1]
    """
    if 1 <= outlet <= 24:
        return outlet - 1, 0
    if 25 <= outlet <= 48:
        return outlet - 25, 1
    raise ValueError("outlet out of range")


class PDUGUI(tk.Tk):
    def __init__(
        self,
        client: SmartPDUClient,
        pdu_id: str,
        outlet_count: int = 48,
        cols: int = 2,
        rows: int = 24,
        refresh_s: float = 1.0,
        heat_min_w: float = 0.0,
        heat_max_w: float = 300.0,
        autoscale: bool = True,
    ):
        super().__init__()
        self.client = client
        self.pdu_id = pdu_id
        self.outlet_count = outlet_count
        self.cols = cols
        self.rows = rows
        self.refresh_s = refresh_s
        self.heat_min_w = heat_min_w
        self.heat_max_w = heat_max_w
        self.autoscale = autoscale

        self.title(f"SmartPDU Live Heat Map (PDU {pdu_id})")
        self.geometry("900x900")

        self._stop_event = threading.Event()
        self._q: "queue.Queue[Tuple[str, float, object]]" = queue.Queue()
        # message types:
        # ("data", ts, Dict[int, OutletData])
        # ("err", ts, str)

        self._build_ui()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

        self.after(80, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=10)

        self.status_var = tk.StringVar(value="Starting...")
        self.error_var = tk.StringVar(value="")
        self.scale_var = tk.StringVar(value="")

        ttk.Label(top, text=f"PDU {self.pdu_id}", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(top, text="Refresh now", command=self._refresh_now).pack(side=tk.RIGHT)
        ttk.Button(top, text="Quit", command=self._on_close).pack(side=tk.RIGHT, padx=(0, 8))

        mid = ttk.Frame(self)
        mid.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(mid, textvariable=self.status_var).pack(side=tk.LEFT)
        ttk.Label(mid, textvariable=self.scale_var).pack(side=tk.RIGHT)

        err = ttk.Frame(self)
        err.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 10))
        ttk.Label(err, textvariable=self.error_var, foreground="red").pack(side=tk.LEFT)

        self.canvas = tk.Canvas(self, bg="white")
        self.canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Geometry
        self.cell_w = 380
        self.cell_h = 32
        self.margin = 20
        self.header_h = 50

        self._cells: Dict[int, Dict[str, int]] = {}
        self._draw_static()

    def _draw_static(self) -> None:
        self.canvas.delete("all")

        panel_w = self.margin * 2 + self.cols * self.cell_w
        panel_h = self.margin * 2 + self.header_h + self.rows * self.cell_h

        x0, y0 = 20, 20
        x1, y1 = x0 + panel_w, y0 + panel_h

        self.canvas.create_rectangle(x0, y0, x1, y1, outline="black", width=3)
        self.canvas.create_rectangle(x0 + 5, y0 + 5, x1 - 5, y0 + 5 + self.header_h, outline="black", width=2)
        self.canvas.create_text(
            (x0 + x1) / 2,
            y0 + 5 + self.header_h / 2,
            text="Outlet Power Heat Map (live)",
            font=("Segoe UI", 16, "bold"),
        )

        self._cells.clear()
        for outlet in range(1, self.outlet_count + 1):
            row, col = outlet_to_row_col(outlet)
            cx = x0 + self.margin + col * self.cell_w
            cy = y0 + self.margin + self.header_h + row * self.cell_h

            rect = self.canvas.create_rectangle(
                cx,
                cy,
                cx + self.cell_w,
                cy + self.cell_h,
                outline="black",
                width=1,
                fill="#eeeeee",
            )
            num = self.canvas.create_text(
                cx + 8, cy + self.cell_h / 2, anchor="w", text=f"{outlet:02d}", font=("Segoe UI", 10, "bold")
            )
            txt = self.canvas.create_text(
                cx + 60,
                cy + self.cell_h / 2,
                anchor="w",
                text="P: ...   E: ...",
                font=("Segoe UI", 9),
            )
            state = self.canvas.create_text(
                cx + self.cell_w - 10,
                cy + self.cell_h / 2,
                anchor="e",
                text="...",
                font=("Segoe UI", 10, "bold"),
            )

            self._cells[outlet] = {"rect": rect, "num": num, "txt": txt, "state": state}

        # Legend
        lx = x0 + 10
        ly = y1 + 10
        # (Legend drawn inside canvas; ensure space is present by scrolling if needed)
        # Keep legend near top-left inside header instead for simplicity:
        self.canvas.create_text(x0 + 12, y0 + 8, anchor="nw", text="Blue=low  Yellow=mid  Red=high", font=("Segoe UI", 9))

    def _apply_data(self, ts: float, data: Dict[int, OutletData]) -> None:
        self.error_var.set("")
        self.status_var.set(f"Last update: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}   (refresh {self.refresh_s:.2f}s)")

        # Determine heat map range
        powers = [od.power_w for od in data.values() if od.power_w is not None and od.power_w >= 0]
        if self.autoscale and powers:
            # Use 5th..95th percentile-ish via sorted slice to reduce outlier impact
            sp = sorted(powers)
            lo_i = max(0, int(len(sp) * 0.05) - 1)
            hi_i = min(len(sp) - 1, int(len(sp) * 0.95))
            p_min = float(sp[lo_i])
            p_max = float(sp[hi_i])
            # Ensure some span
            if p_max - p_min < 10.0:
                p_max = p_min + 10.0
        else:
            p_min = self.heat_min_w
            p_max = self.heat_max_w

        self.scale_var.set(f"Heat scale: {p_min:.0f} W .. {p_max:.0f} W" + (" (auto)" if self.autoscale else ""))

        for outlet, od in data.items():
            cell = self._cells.get(outlet)
            if not cell:
                continue

            is_on = state_to_on(od.state)
            fill = heat_color(od.power_w, p_min, p_max, off=not is_on)

            self.canvas.itemconfigure(cell["rect"], fill=fill)
            self.canvas.itemconfigure(cell["txt"], text=f"{fmt_power(od.power_w)}   {fmt_energy(od.energy_kwh)}")
            self.canvas.itemconfigure(cell["state"], text=("ON" if is_on else "OFF"), fill=("green" if is_on else "red"))

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            start = time.time()
            try:
                data = self.client.get_all_outlets_data(self.outlet_count)
                self._q.put(("data", time.time(), data))
            except Exception as e:
                self._q.put(("err", time.time(), str(e)))

            elapsed = time.time() - start
            wait = max(0.05, self.refresh_s - elapsed)
            self._stop_event.wait(wait)

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, ts, payload = self._q.get_nowait()
                if kind == "err":
                    self.error_var.set(str(payload))
                    self.status_var.set(f"Update failed: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}")
                else:
                    self._apply_data(ts, payload)  # type: ignore[arg-type]
        except queue.Empty:
            pass

        self.after(80, self._drain_queue)

    def _refresh_now(self) -> None:
        def one_shot():
            try:
                data = self.client.get_all_outlets_data(self.outlet_count)
                self._q.put(("data", time.time(), data))
            except Exception as e:
                self._q.put(("err", time.time(), str(e)))

        threading.Thread(target=one_shot, daemon=True).start()

    def _on_close(self) -> None:
        self._stop_event.set()
        self.destroy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--pdu-id", default="2")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="123456789")
    ap.add_argument("--refresh", type=float, default=1.0)
    ap.add_argument("--autoscale", action="store_true", help="Autoscale heat map from observed power values")
    ap.add_argument("--pmin", type=float, default=0.0, help="Fixed heat map min W (if not autoscale)")
    ap.add_argument("--pmax", type=float, default=300.0, help="Fixed heat map max W (if not autoscale)")
    args = ap.parse_args()

    client = SmartPDUClient(args.base_url, args.pdu_id, args.user, args.password)

    gui = PDUGUI(
        client=client,
        pdu_id=args.pdu_id,
        outlet_count=48,
        cols=2,
        rows=24,
        refresh_s=max(0.2, args.refresh),
        heat_min_w=args.pmin,
        heat_max_w=args.pmax,
        autoscale=bool(args.autoscale),
    )
    gui.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
