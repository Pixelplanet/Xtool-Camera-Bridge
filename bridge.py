#!/usr/bin/env python3
"""
xTool F2 Ultra / F1 Ultra Camera Bridge for LightBurn
Zero external dependencies - Built 100% with the Python Standard Library.
"""

import sys
import os
import time
import json
import ssl
import socket
import struct
import base64
import threading
import argparse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# --- xTool Framing & CRC-16 ARC ---
HV_TABLE = [
    0,49345,49537,320,49921,960,640,49729,50689,1728,1920,51009,1280,50625,50305,1088,
    52225,3264,3456,52545,3840,53185,52865,3648,2560,51905,52097,2880,51457,2496,2176,51265,
    55297,6336,6528,55617,6912,56257,55937,6720,7680,57025,57217,8000,56577,7616,7296,56385,
    5120,54465,54657,5440,55041,6080,5760,54849,53761,4800,4992,54081,4352,53697,53377,4160,
    61441,12480,12672,61761,13056,62401,62081,12864,13824,63169,63361,14144,62721,13760,13440,62529,
    15360,64705,64897,15680,65281,16320,16000,65089,64001,15040,15232,64321,14592,63937,63617,14400,
    10240,59585,59777,10560,60161,11200,10880,59969,60929,11968,12160,61249,11520,60865,60545,11328,
    58369,9408,9600,58689,9984,59329,59009,9792,8704,58049,58241,9024,57601,8640,8320,57409,
    40961,24768,24960,41281,25344,41921,41601,25152,26112,42689,42881,26432,42241,26048,25728,42049,
    27648,44225,44417,27968,44801,28608,28288,44609,43521,27328,27520,43841,26880,43457,43137,26688,
    30720,47297,47489,31040,47873,31680,31360,47681,48641,32448,32640,48961,32000,48577,48257,31808,
    46081,29888,30080,46401,30464,47041,46721,30272,29184,45761,45953,29504,45313,29120,28800,45121,
    20480,37057,37249,20800,37633,21440,21120,37441,38401,22208,22400,38721,21760,38337,38017,21568,
    39937,23744,23936,40257,24320,40897,40577,24128,23040,39617,39809,23360,39169,22976,22656,38977,
    34817,18624,18816,35137,19200,35777,35457,19008,19968,36545,36737,20288,36097,19904,19584,35905,
    17408,33985,34177,17728,34561,18368,18048,34369,33281,17088,17280,33601,16640,33217,32897,16448
]

def crc16(data: bytes, init: int = 0) -> int:
    n = init & 0xFFFF
    for b in data:
        n = (HV_TABLE[(n ^ b) & 0xFF] ^ (n >> 8)) & 0xFFFF
    return n

def build_xtool_frame(payload: bytes, protocol_type: int = 4, cal_crc: bool = True) -> bytes:
    length = len(payload)
    header = bytearray(10)
    header[0] = 0xBA
    header[1] = 0xBE
    header[2] = (length >> 16) & 0xFF
    header[3] = (length >> 8) & 0xFF
    header[4] = length & 0xFF
    header[5] = (protocol_type & 127) | (0 if cal_crc else 128)
    p_crc = crc16(payload) if cal_crc else 0
    header[6] = (p_crc >> 8) & 0xFF
    header[7] = p_crc & 0xFF
    h_crc = crc16(bytes(header[:8]))
    header[8] = (h_crc >> 8) & 0xFF
    header[9] = h_crc & 0xFF
    return bytes(header) + payload

def make_ws_binary_frame(payload: bytes) -> bytes:
    mask_key = os.urandom(4)
    length = len(payload)
    frame = bytearray()
    frame.append(0x82)  # FIN=1, binary opcode=2
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack('>H', length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack('>Q', length))
    frame.extend(mask_key)
    masked = bytearray(length)
    for i in range(length):
        masked[i] = payload[i] ^ mask_key[i % 4]
    frame.extend(masked)
    return bytes(frame)

def parse_ws_frame(data: bytearray):
    if len(data) < 2:
        return None, 0
    b0 = data[0]
    b1 = data[1]
    opcode = b0 & 0x0F
    is_masked = (b1 & 0x80) != 0
    payload_len = b1 & 0x7F
    offset = 2
    if payload_len == 126:
        if len(data) < offset + 2:
            return None, 0
        payload_len = struct.unpack('>H', data[offset:offset+2])[0]
        offset += 2
    elif payload_len == 127:
        if len(data) < offset + 8:
            return None, 0
        payload_len = struct.unpack('>Q', data[offset:offset+8])[0]
        offset += 8
    if is_masked:
        if len(data) < offset + 4 + payload_len:
            return None, 0
        mask = data[offset:offset+4]
        offset += 4
        raw = data[offset:offset+payload_len]
        unmasked = bytearray(len(raw))
        for i in range(len(raw)):
            unmasked[i] = raw[i] ^ mask[i % 4]
        payload = bytes(unmasked)
    else:
        if len(data) < offset + payload_len:
            return None, 0
        payload = bytes(data[offset:offset+payload_len])
    return (opcode, payload), offset + payload_len


class XToolCameraClient:
    """Manages secure communication with the xTool laser camera and fill lights."""
    def __init__(self, laser_ip: str, camera_name: str = "main", initial_brightness: int = 100):
        self.laser_ip = laser_ip
        self.camera_name = camera_name
        self.current_brightness = initial_brightness
        self.lock = threading.Lock()
        self.latest_image = None
        self.latest_timestamp = 0
        self.last_error = None
        self.snap_count = 0
        self.is_capturing = False

    def _create_ws_socket(self, session_id: str, func_name: str):
        ctx = ssl._create_unverified_context()
        raw_sock = socket.create_connection((self.laser_ip, 28900), timeout=5)
        ss = ctx.wrap_socket(raw_sock, server_hostname="device.xtooldevice.com")
        key = base64.b64encode(os.urandom(16)).decode('utf-8')
        req = (
            f"GET /websocket?id={session_id}&function={func_name} HTTP/1.1\r\n"
            f"Host: device.xtooldevice.com:28900\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: null\r\n\r\n"
        )
        ss.sendall(req.encode('utf-8'))
        resp = ss.recv(4096)
        if b"101 Switching Protocols" not in resp:
            raise ConnectionError(f"WebSocket handshake failed: {resp[:100]}")
        return ss

    def _send_inst_cmd(self, sock, cmd_obj):
        json_bytes = json.dumps(cmd_obj).encode('utf-8')
        frame = build_xtool_frame(json_bytes, protocol_type=4, cal_crc=True)
        sock.sendall(make_ws_binary_frame(frame))

    def _recv_inst_msg(self, sock, timeout=5.0):
        sock.settimeout(timeout)
        buf = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionResetError("Connection closed by laser")
            buf.extend(chunk)
            while True:
                res, consumed = parse_ws_frame(buf)
                if res is None:
                    break
                buf = buf[consumed:]
                opcode, payload = res
                if len(payload) >= 10 and payload[0] == 0xBA and payload[1] == 0xBE:
                    body = payload[10:]
                    return json.loads(body.decode('utf-8', errors='ignore'))

    def _send_file_req(self, sock, channel: int, offset: int, window_size: int):
        req = bytearray(10)
        req[0] = 1  # FILE_REQUEST
        req[1] = channel
        req[2] = (offset >> 32) & 0xFF
        req[3] = (offset >> 24) & 0xFF
        req[4] = (offset >> 16) & 0xFF
        req[5] = (offset >> 8) & 0xFF
        req[6] = offset & 0xFF
        req[7] = (window_size >> 16) & 0xFF
        req[8] = (window_size >> 8) & 0xFF
        req[9] = window_size & 0xFF
        frame = build_xtool_frame(bytes(req), protocol_type=33, cal_crc=True)
        sock.sendall(make_ws_binary_frame(frame))

    def set_lights(self, brightness_percent: int) -> bool:
        """
        Sets front and back fill lights.
        brightness_percent: 0 (off) to 100 (max)
        xTool formula maps 1..100 -> 20..255
        """
        b = max(0, min(100, int(brightness_percent)))
        if b == 0:
            raw_val = 0
        else:
            raw_val = round((b - 1) * 235 / 99 + 20)
            raw_val = max(20, min(255, raw_val))

        ws_inst = None
        try:
            session_id = f"light-{int(time.time()*1000)}"
            ws_inst = self._create_ws_socket(session_id, "instruction")

            # Auth
            self._send_inst_cmd(ws_inst, {
                "type": "request",
                "method": "GET",
                "url": "/v1/user/parity",
                "transactionId": 1,
                "params": {},
                "data": {
                    "userID": "mk-guest",
                    "userKey": "bWFrZWJsb2NrLXh0b29s",
                    "timezone": "Europe/Berlin"
                }
            })
            self._recv_inst_msg(ws_inst, timeout=5.0)

            # Set light
            self._send_inst_cmd(ws_inst, {
                "type": "request",
                "method": "PUT",
                "url": "/v1/peripheral/param",
                "transactionId": 2,
                "params": {"type": "fill_light"},
                "data": {
                    "action": "set_bri",
                    "front": raw_val,
                    "back": raw_val
                }
            })
            resp = self._recv_inst_msg(ws_inst, timeout=5.0)
            if resp.get("code") == 0:
                self.current_brightness = b
                print(f"[xTool Bridge] Fill lights set to {b}% (value: {raw_val})")
                return True
            else:
                print(f"[xTool Bridge] Failed to set lights: {resp}")
                return False
        except Exception as e:
            print(f"[xTool Bridge Error] Light control failed: {e}")
            return False
        finally:
            if ws_inst:
                try: ws_inst.close()
                except: pass

    def capture_snapshot(self) -> bytes:
        """Connects, snaps photo, downloads JPEG, and caches it."""
        with self.lock:
            self.is_capturing = True
            ws_inst = None
            ws_file = None
            try:
                t0 = time.time()
                session_id = f"cam-{int(time.time()*1000)}"
                ws_inst = self._create_ws_socket(session_id, "instruction")
                ws_file = self._create_ws_socket(session_id, "file_stream")

                # 1. Parity Auth
                self._send_inst_cmd(ws_inst, {
                    "type": "request",
                    "method": "GET",
                    "url": "/v1/user/parity",
                    "transactionId": 1,
                    "params": {},
                    "data": {
                        "userID": "mk-guest",
                        "userKey": "bWFrZWJsb2NrLXh0b29s",
                        "timezone": "Europe/Berlin"
                    }
                })
                auth_resp = self._recv_inst_msg(ws_inst, timeout=5.0)
                if auth_resp.get("code") != 0:
                    raise PermissionError(f"Laser auth failed: {auth_resp}")

                # 2. Camera Snap
                self._send_inst_cmd(ws_inst, {
                    "type": "request",
                    "method": "GET",
                    "url": "/v1/camera/snap",
                    "transactionId": 2,
                    "params": {"name": self.camera_name},
                    "data": {}
                })
                snap_resp = self._recv_inst_msg(ws_inst, timeout=10.0)
                if snap_resp.get("code") != 0:
                    raise RuntimeError(f"Snapshot failed: {snap_resp}")
                remote_filename = snap_resp["data"]["filename"]

                # 3. Download Request
                self._send_inst_cmd(ws_inst, {
                    "type": "request",
                    "method": "PUT",
                    "url": "/v1/filetransfer/download",
                    "transactionId": 3,
                    "params": {},
                    "data": {
                        "filetype": 5,
                        "filename": remote_filename,
                        "digesttype": 1,
                        "channel": 1,
                        "packetsize": 1048576
                    }
                })
                dl_resp = self._recv_inst_msg(ws_inst, timeout=5.0)
                if dl_resp.get("code") != 0:
                    raise RuntimeError(f"File transfer download request failed: {dl_resp}")
                filesize = dl_resp["data"]["filesize"]

                # 4. Request binary chunks over file_stream
                self._send_file_req(ws_file, channel=1, offset=0, window_size=filesize)
                file_data = bytearray(filesize)
                received = 0
                file_buf = bytearray()
                ws_file.settimeout(10.0)

                while received < filesize:
                    chunk = ws_file.recv(65536)
                    if not chunk:
                        break
                    file_buf.extend(chunk)
                    while True:
                        res, consumed = parse_ws_frame(file_buf)
                        if res is None:
                            break
                        file_buf = file_buf[consumed:]
                        opcode, payload = res
                        if len(payload) >= 10 and payload[0] == 0xBA and payload[1] == 0xBE:
                            p_body = payload[10:]
                            if len(p_body) >= 7 and p_body[0] == 129:  # FILE_DATA
                                offset = (p_body[2] << 32) | (p_body[3] << 24) | (p_body[4] << 16) | (p_body[5] << 8) | p_body[6]
                                content = p_body[7:]
                                file_data[offset:offset+len(content)] = content
                                received += len(content)

                if received < filesize:
                    raise IOError(f"Truncated file: got {received}/{filesize} bytes")

                jpeg_bytes = bytes(file_data)
                self.latest_image = jpeg_bytes
                self.latest_timestamp = time.time()
                self.snap_count += 1
                self.last_error = None
                dt = time.time() - t0
                print(f"[xTool Bridge] Snapshot #{self.snap_count} captured ({len(jpeg_bytes)/1024:.1f} KB in {dt:.2f}s)")
                return jpeg_bytes

            except Exception as e:
                self.last_error = str(e)
                print(f"[xTool Bridge Error] Capture failed: {e}")
                raise
            finally:
                self.is_capturing = False
                if ws_inst:
                    try: ws_inst.close()
                    except: pass
                if ws_file:
                    try: ws_file.close()
                    except: pass


# Global client instance
camera_client: XToolCameraClient = None
auto_snap_interval: float = 3.0


class BridgeHTTPHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence routine snapshot requests to keep terminal clean
        if any(ep in args[0] for ep in ["/snap.jpg", "/stream.mjpg", "/api/status"]):
            return
        super().log_message(format, *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ["/", "/index.html"]:
            self.handle_index()
        elif path == "/snap.jpg":
            self.handle_snapshot()
        elif path == "/stream.mjpg":
            self.handle_stream()
        elif path == "/api/snap":
            self.handle_api_snap()
        elif path == "/api/light":
            self.handle_api_light(query)
        elif path == "/api/status":
            self.handle_api_status()
        else:
            self.send_error(404, "Not Found")

    def handle_index(self):
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>xTool F2 Ultra - LightBurn Camera Bridge</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    :root {{
      --bg: #0f172a;
      --card: #1e293b;
      --card-alt: #162032;
      --text: #f8fafc;
      --accent: #38bdf8;
      --accent-hover: #0ea5e9;
      --success: #22c55e;
      --border: #334155;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
      padding: 24px 16px;
    }}
    .container {{
      max-width: 960px;
      width: 100%;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: var(--card);
      padding: 18px 24px;
      border-radius: 12px;
      border: 1px solid var(--border);
    }}
    h1 {{ font-size: 1.35rem; font-weight: 700; color: var(--accent); }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 0.875rem;
      background: #064e3b;
      color: #a7f3d0;
      padding: 6px 14px;
      border-radius: 9999px;
      font-weight: 500;
    }}
    .viewer {{
      background: #000;
      border-radius: 12px;
      overflow: hidden;
      border: 1px solid var(--border);
      position: relative;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 480px;
    }}
    .viewer img {{
      max-width: 100%;
      height: auto;
      display: block;
      object-fit: contain;
    }}
    .controls {{
      display: flex;
      gap: 16px;
      background: var(--card);
      padding: 16px 20px;
      border-radius: 12px;
      border: 1px solid var(--border);
      align-items: center;
      flex-wrap: wrap;
    }}
    .light-control {{
      display: flex;
      align-items: center;
      gap: 12px;
      background: var(--card-alt);
      padding: 10px 16px;
      border-radius: 8px;
      border: 1px solid var(--border);
    }}
    button {{
      background: var(--accent);
      color: #0f172a;
      border: none;
      padding: 9px 16px;
      font-weight: 600;
      border-radius: 8px;
      cursor: pointer;
      transition: background 0.2s;
    }}
    button:hover {{ background: var(--accent-hover); }}
    button:disabled {{ opacity: 0.6; cursor: not-allowed; }}
    button.btn-sec {{
      background: #334155;
      color: #f8fafc;
      font-size: 0.85rem;
      padding: 6px 12px;
    }}
    button.btn-sec:hover {{ background: #475569; }}
    .info-card {{
      background: var(--card);
      padding: 20px 24px;
      border-radius: 12px;
      border: 1px solid var(--border);
      font-size: 0.925rem;
      line-height: 1.6;
    }}
    .url-box {{
      background: #090d16;
      border: 1px dashed var(--accent);
      color: var(--accent);
      padding: 10px 14px;
      border-radius: 6px;
      font-family: monospace;
      margin: 8px 0;
      user-select: all;
    }}
    ol {{ padding-left: 20px; margin-top: 8px; }}
    li {{ margin-bottom: 6px; }}
    input[type=range] {{
      accent-color: var(--accent);
      cursor: pointer;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>xTool F2 Ultra Camera Bridge</h1>
        <small style="color: #94a3b8;">Laser IP: {camera_client.laser_ip} | Bridge Port: 8080</small>
      </div>
      <div class="status-badge" id="bridge-status">
        <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e;"></span>
        Connected
      </div>
    </header>

    <div class="viewer">
      <img id="cam-view" src="/snap.jpg?t={time.time()}" alt="Camera Feed">
    </div>

    <div class="controls">
      <button id="snap-btn" onclick="takeSnap()">📸 Take Snapshot</button>
      
      <!-- Light Controls -->
      <div class="light-control">
        <span style="font-size: 1.1rem;">💡</span>
        <label for="light-range" style="font-weight: 500; font-size: 0.9rem;">Fill Lights:</label>
        <input type="range" id="light-range" min="0" max="100" value="{camera_client.current_brightness}" oninput="updateLightLabel(this.value)" onchange="setLight(this.value)">
        <span id="light-val" style="min-width: 42px; font-weight: 600; color: var(--accent);">{camera_client.current_brightness}%</span>
        <button class="btn-sec" onclick="setLight(0)">Off</button>
        <button class="btn-sec" onclick="setLight(50)">50%</button>
        <button class="btn-sec" onclick="setLight(100)">100%</button>
      </div>

      <label style="display: flex; align-items: center; gap: 8px; margin-left: auto; font-size: 0.9rem;">
        <input type="checkbox" id="auto-refresh" checked onchange="toggleAutoRefresh(this.checked)">
        Auto Refresh Bed (every 2s)
      </label>
    </div>

    <div class="info-card">
      <h3 style="margin-bottom: 10px; color: var(--accent);">How to connect LightBurn</h3>
      <ol>
        <li>Open <b>LightBurn</b> (version 2.1+ recommended).</li>
        <li>Open <b>Window &gt; Camera Control</b>.</li>
        <li>Click <b>Add Camera</b> (or configure camera type as <b>Network / IP Camera</b>).</li>
        <li>Enter either of these URLs:</li>
        <div class="url-box">http://localhost:8080/stream.mjpg</div>
        <div class="url-box">http://localhost:8080/snap.jpg</div>
        <li>LightBurn will immediately detect and display the camera feed!</li>
      </ol>
    </div>
  </div>

  <script>
    let refreshTimer = null;

    function refreshFrame() {{
      const img = document.getElementById('cam-view');
      const tempImg = new Image();
      tempImg.onload = () => {{
        img.src = tempImg.src;
      }};
      tempImg.src = '/snap.jpg?t=' + Date.now();
    }}

    function toggleAutoRefresh(enable) {{
      if (refreshTimer) clearInterval(refreshTimer);
      if (enable) {{
        refreshTimer = setInterval(refreshFrame, 2000);
      }}
    }}

    function takeSnap() {{
      const btn = document.getElementById('snap-btn');
      btn.disabled = true;
      btn.innerText = 'Capturing...';
      fetch('/api/snap')
        .then(r => r.json())
        .then(data => {{
          refreshFrame();
        }})
        .catch(err => alert('Capture error: ' + err))
        .finally(() => {{
          btn.disabled = false;
          btn.innerText = '📸 Take Snapshot';
        }});
    }}

    function updateLightLabel(val) {{
      document.getElementById('light-val').innerText = val + '%';
    }}

    function setLight(val) {{
      document.getElementById('light-range').value = val;
      updateLightLabel(val);
      fetch('/api/light?val=' + val)
        .then(r => r.json())
        .then(data => {{
          // Immediately refresh picture with new lighting
          setTimeout(refreshFrame, 600);
        }})
        .catch(err => console.error('Light error:', err));
    }}

    // Start auto-refresh
    toggleAutoRefresh(true);
  </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode('utf-8'))))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def handle_snapshot(self):
        # If no image cached yet, take one
        if not camera_client.latest_image:
            try:
                camera_client.capture_snapshot()
            except Exception as e:
                self.send_error(500, f"Capture failed: {e}")
                return

        img = camera_client.latest_image
        if not img:
            self.send_error(503, "No image available yet")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(img)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(img)

    def handle_stream(self):
        # Multipart MJPEG stream with multi-threading and immediate flush
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()

        last_ts = 0
        try:
            while True:
                # If image exists and updated
                if camera_client.latest_image and camera_client.latest_timestamp != last_ts:
                    last_ts = camera_client.latest_timestamp
                    img = camera_client.latest_image
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(img)}\r\n\r\n".encode())
                    self.wfile.write(img)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def handle_api_snap(self):
        try:
            img = camera_client.capture_snapshot()
            resp = json.dumps({"success": True, "size": len(img), "timestamp": time.time()})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp.encode('utf-8'))
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode('utf-8'))

    def handle_api_light(self, query):
        val = 100
        if "val" in query and query["val"]:
            try:
                val = int(query["val"][0])
            except:
                pass
        success = camera_client.set_lights(val)
        resp = json.dumps({"success": success, "brightness": camera_client.current_brightness})
        self.send_response(200 if success else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp.encode('utf-8'))

    def handle_api_status(self):
        resp = json.dumps({
            "laser_ip": camera_client.laser_ip,
            "snap_count": camera_client.snap_count,
            "latest_timestamp": camera_client.latest_timestamp,
            "brightness": camera_client.current_brightness,
            "is_capturing": camera_client.is_capturing,
            "last_error": camera_client.last_error
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp.encode('utf-8'))


def background_snap_worker(interval: float):
    """Background worker to keep the snapshot updated periodically."""
    print(f"[xTool Bridge] Background auto-snap worker started (interval: {interval}s)")
    while True:
        try:
            camera_client.capture_snapshot()
        except Exception as e:
            time.sleep(3.0)
        time.sleep(max(1.0, interval))


def main():
    global camera_client, auto_snap_interval
    sys.stdout.reconfigure(encoding='utf-8')

    parser = argparse.ArgumentParser(description="xTool F2 Ultra / F1 Ultra Camera Bridge for LightBurn")
    parser.add_argument("--ip", default="192.168.178.39", help="IP address of the xTool machine")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port to serve camera on (default: 8080)")
    parser.add_argument("--interval", type=float, default=2.5, help="Auto snapshot interval in seconds (default: 2.5s, 0 for on-demand)")
    parser.add_argument("--camera", default="main", choices=["main", "deep"], help="Camera lens to use (default: main)")
    parser.add_argument("--light", type=int, default=100, help="Fill light brightness percentage 0-100 (default: 100)")
    args = parser.parse_args()

    camera_client = XToolCameraClient(laser_ip=args.ip, camera_name=args.camera, initial_brightness=args.light)
    auto_snap_interval = args.interval

    print("=" * 65)
    print("   xTool F2 Ultra / F1 Ultra Camera Bridge for LightBurn")
    print("=" * 65)
    print(f"[*] Target Laser IP:    {args.ip}:28900")
    print(f"[*] Camera Selection:   {args.camera}")
    print(f"[*] Fill Light:         {args.light}%")
    print(f"[*] Auto-Snap:          {args.interval}s interval" if args.interval > 0 else "[*] Auto-Snap: Disabled (on-demand)")
    print(f"[*] Web Dashboard:      http://localhost:{args.port}/")
    print(f"[*] LightBurn Stream:   http://localhost:{args.port}/stream.mjpg")
    print(f"[*] LightBurn Snap:     http://localhost:{args.port}/snap.jpg")
    print("=" * 65)

    # 1. Turn on lights to ensure good illumination
    if args.light > 0:
        print(f"[*] Illuminating laser bed (setting fill lights to {args.light}%)...")
        camera_client.set_lights(args.light)

    # 2. Initial capture to verify connectivity right away
    print("[*] Performing initial camera capture...")
    try:
        camera_client.capture_snapshot()
        print("[+] Initial camera capture succeeded!")
    except Exception as e:
        print(f"[!] Warning: Initial capture failed ({e}). Will keep retrying...")

    # 3. Start background auto-snap thread if interval > 0
    if args.interval > 0:
        bg_thread = threading.Thread(target=background_snap_worker, args=(args.interval,), daemon=True)
        bg_thread.start()

    # 4. Start Multi-Threaded HTTP server
    server = ThreadingHTTPServer(("0.0.0.0", args.port), BridgeHTTPHandler)
    print(f"\n[+] Bridge is running! Open http://localhost:{args.port}/ in your browser.")
    print("[+] Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Stopping bridge...")
        server.server_close()


if __name__ == "__main__":
    main()
