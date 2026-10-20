# xTool F2 Ultra & F1 Ultra Camera Bridge for LightBurn

A lightweight, zero-dependency bridge that connects the internal overhead camera and lighting controls of the **xTool F2 Ultra** (and **F1 Ultra**) directly to **LightBurn** and any standard web browser or video pipeline.

---

## 📖 Overview & Why This Is Needed

xTool F-series machines (F2 Ultra UV, F2 Ultra Dual Galvo, and F1 Ultra) do not expose standard video streaming protocols like RTSP, HTTP/MJPEG, or USB UVC webcams. Instead, the machine runs an encrypted TLS WebSocket service on port `28900`. Furthermore, without software control over the internal front and rear LED fill lights, camera captures default to pitch-black images.

This bridge acts as a local translator running on your PC:
1. It connects to the laser machine over local Wi-Fi / Ethernet via encrypted WebSockets.
2. It authenticates and powers on the internal chamber LED fill lights so the bed is properly illuminated.
3. It downloads high-resolution camera frames using xTool's native binary framing protocol.
4. It exposes standard HTTP endpoints (`/stream.mjpg` and `/snap.jpg`) that LightBurn and browsers can consume natively.

---

## ⚡ Key Features

- **Zero External Dependencies:** Built 100% with the Python Standard Library (`socket`, `ssl`, `http.server`, `json`, `struct`). No `pip install`, no OpenCV, and no compilation required.
- **Integrated Fill Light Control:** Full software control over the enclosure's front and rear LED arrays (0–100%) so bed photos are sharp and properly exposed.
- **Fast High-Resolution Capture:** Captures high-res bed photos directly via xTool's native binary protocol in ~1–2 seconds.
- **Multi-Threaded Server:** Multiple clients (browser dashboard, LightBurn, OBS) can connect simultaneously without hanging or blocking each other.
- **Multiple Output Modes:**
  - **Live Stream (MJPEG):** `http://localhost:8080/stream.mjpg`
  - **Individual Snapshot:** `http://localhost:8080/snap.jpg`
  - **Interactive Web Dashboard:** `http://localhost:8080/` (live preview, light brightness slider, snapshot trigger)
  - **Light Control API:** `http://localhost:8080/api/light?val=100`
- **Silent Background Operation:** Includes one-click launchers for both visible console logs and silent background execution (no terminal windows).

---

## 🚀 Quick Start (Windows)

### 1. Launch the Bridge
In the project folder, you have two launch options:

- **Visible Console Window:** Double-click **`start_bridge.bat`** (shows logs and real-time capture times).
- **Silent Background Run:** Double-click **`start_bridge_hidden.vbs`** (runs invisibly in the background with zero terminal popup).
- **To Stop the Bridge:** Double-click **`stop_bridge.bat`**.

### 2. Verify in Browser
Open **`http://localhost:8080/`** in any web browser. You will see:
- A live view of the laser bed.
- Fill light brightness slider (Off, 50%, 100%).
- Manual "Take Snapshot" trigger and connection status.

---

## 📐 Connecting to LightBurn

> [!IMPORTANT]
> The bridge server runs locally on your PC. In LightBurn, use **`localhost`** (or your PC's local IP, e.g. `192.168.178.29`), **NOT** the laser's IP address (`192.168.178.39`).

### Method A: LightBurn 2.1+ (Native Network / IP Camera)

1. Open **LightBurn**.
2. Open the **Camera Control** window (`Window` > `Camera Control` or `Cameras`).
3. Click **Add Camera** (or select **Camera Type: Network**).
4. Enter either of these URLs:
   ```text
   http://localhost:8080/stream.mjpg
   ```
   *(Or `http://localhost:8080/snap.jpg` for snapshot mode).*
5. LightBurn will display the camera overlay.
6. **One-Time Setup:** Once added, LightBurn permanently saves this URL into your device profile (`prefs.ini`). You never need to enter it again!
7. Run LightBurn's **Lens Calibration** and **Camera Alignment** wizards to calibrate the camera to your workspace.

---

### Method B: Older LightBurn Versions (< 2.1) via OBS Virtual Camera

1. Open [OBS Studio](https://obsproject.com/).
2. Under *Sources*, click **`+`** > select **Browser**.
3. Set the URL to `http://localhost:8080/stream.mjpg`.
4. Click **Start Virtual Camera** in OBS.
5. In LightBurn's **Camera Control** window, select **OBS Virtual Camera** from the dropdown.

---

## 💡 Lighting Control & APIs

- **Via Web Dashboard:** Go to `http://localhost:8080/` to adjust brightness with the slider or click `Off`, `50%`, or `100%`.
- **Via URL / API:**
  - Turn off lights: `http://localhost:8080/api/light?val=0`
  - Set to 50%: `http://localhost:8080/api/light?val=50`
  - Set to 100%: `http://localhost:8080/api/light?val=100`
- **Via Startup Flag:**
  ```bash
  python bridge.py --light 100
  ```

---

## ⚙️ Command-Line Options

```bash
python bridge.py --help
```

| Flag | Default | Description |
| :--- | :--- | :--- |
| `--ip` | `192.168.178.39` | IP address of your xTool machine |
| `--port` | `8080` | Local HTTP port for the bridge server |
| `--interval` | `2.5` | Auto-snapshot background interval in seconds (`0` for on-demand only) |
| `--light` | `100` | Initial fill light brightness percentage (0–100) |
| `--camera` | `main` | Camera lens: `main` (wide overhead) or `deep` (macro) |

---

## 🔬 Protocol & Hardware Specification for Developers

If you are a developer or working with the LightBurn software team to implement native support, refer to:

👉 **[`xTool_F2_Ultra_LightBurn_Integration_Spec.md`](xTool_F2_Ultra_LightBurn_Integration_Spec.md)**

This document contains:
- Complete TLS WebSocket handshake and authentication specifications.
- Byte-level layouts of the 10-byte binary packet framing (`0xBA 0xBE`).
- CRC-16 ARC lookup tables and algorithms.
- Motorized Z-axis autofocus (`auto_start`, `coord`, `goTo`) control commands.
- Factory calibration matrix download endpoints (`fileType: 3`).
