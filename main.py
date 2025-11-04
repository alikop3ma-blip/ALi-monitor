#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import socket
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytz
from flask import Flask, render_template_string, request, jsonify
import jdatetime

# ایمپورت از فایل‌های جدید
from login_save import update_login_data, get_week_report
from pools_manager import update_miner_pools, get_pools_manager_html
from reboot import reboot_miner, get_reboot_manager_html
from terminal import execute_terminal_command, get_terminal_html
from NTP import update_ntp_settings, get_ntp_html
# در پایین فایل logs_viewer.py
from logs_viewer import logs_viewer

app = Flask(__name__)

# === CONFIG ===
MINER_IP = os.environ.get("MINER_IP")
MINER_USERNAME = "admin"
MINER_PASSWORD = os.environ.get("MINER_PASSWORD")
MINER_NAMES = ["131", "132", "133", "65", "66", "70"]
MINER_PORTS = [204, 205, 206, 304, 305, 306]

# Map name -> port (سراسری، روابط لینک‌ها به این پورت‌ها خواهد بود)
port_map = {
    "131": 201,
    "132": 202,
    "133": 203,
    "65": 301,
    "66": 302,
    "70": 303
}

SOCKET_TIMEOUT = 3.0
MAX_WORKERS = 6
COMMANDS = [{"command": "summary"}, {"command": "devs"}]

def build_miners():
    ip = MINER_IP
    miners = []
    for name, port in zip(MINER_NAMES, MINER_PORTS):
        miners.append({"name": name, "ip": ip, "port": port})
    return miners

# === TCP JSON sender ===
def send_tcp_json(ip, port, payload):
    if not ip:
        return None
    data = json.dumps(payload).encode("utf-8")
    try:
        with socket.create_connection((ip, port), timeout=SOCKET_TIMEOUT) as s:
            s.settimeout(SOCKET_TIMEOUT)
            s.sendall(data)
            chunks = []
            while True:
                try:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                except socket.timeout:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="ignore").strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except Exception:
                first = raw.find("{")
                last = raw.rfind("}")
                if first != -1 and last != -1 and last > first:
                    sub = raw[first:last+1]
                    try:
                        return json.loads(sub)
                    except Exception:
                        return None
            return None
    except Exception:
        return None

# === Helpers ===
def format_seconds_pretty(sec: int):
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts and seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts)

def parse_summary(summary_json):
    if not summary_json:
        return {}
    data = None
    if "SUMMARY" in summary_json and summary_json["SUMMARY"]:
        data = summary_json["SUMMARY"][0]
    elif "Msg" in summary_json:
        data = summary_json["Msg"]
    else:
        return {}
    if not data:
        return {}
    mhs_av = data.get("MHS av")
    uptime = data.get("Uptime") or data.get("Elapsed")
    power = data.get("Power")
    temp = data.get("Temperature")
    hashrate = None
    if mhs_av is not None:
        if mhs_av > 1_000_000:
            hashrate = round(mhs_av / 1_000_000, 2)
        else:
            hashrate = mhs_av
    uptime_str = format_seconds_pretty(int(uptime)) if uptime else None
    return {
        "uptime": uptime_str,
        "hashrate": hashrate,
        "power": int(power) if power else None,
        "temp_avg": round(temp, 1) if temp else None,
    }

def parse_devs(devs_json):
    board_temps = []
    if not devs_json or "DEVS" not in devs_json:
        return board_temps
    for board in devs_json["DEVS"]:
        temp = board.get("Temperature")
        if temp is not None:
            board_temps.append(round(temp, 1))
    return board_temps

def poll_miner(miner):
    ip = miner["ip"]
    port = miner["port"]
    result = {
        "name": f"{miner['name']} ({port})",
        "alive": False,
        "hashrate": None,
        "uptime": None,
        "power": None,
        "board_temps": [],
    }
    if not ip:
        return result
    responses = {}
    any_response = False
    for cmd in COMMANDS:
        resp = send_tcp_json(ip, port, cmd)
        if resp:
            any_response = True
            responses[cmd["command"]] = resp
    if not any_response:
        return result
    result["alive"] = True
    if "summary" in responses:
        summary = parse_summary(responses["summary"])
        result.update(
            {
                "hashrate": summary.get("hashrate"),
                "uptime": summary.get("uptime"),
                "power": summary.get("power"),
            }
        )
    if "devs" in responses:
        boards = parse_devs(responses["devs"])
        result["board_temps"] = boards
    return result

def get_live_data():
    miners = build_miners() if MINER_IP else []
    out = []
    if not miners:
        return []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(poll_miner, m): m for m in miners}
        for fut in futures:
            try:
                res = fut.result()
            except Exception:
                res = {"name": f"{futures[fut]['name']} ({futures[fut]['port']})", "alive": False}
            out.append(res)
    return sorted(out, key=lambda x: x["name"])

def calculate_total_hashrate(miners):
    total = 0
    for miner in miners:
        if miner.get("alive") and miner.get("hashrate") is not None:
            total += miner["hashrate"]
    return round(total, 2)

# === FULL TEMPLATE (HTML/CSS/JS) ===
TEMPLATE = """
<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Miner Panel</title>
<style>
body{font-family:sans-serif; background:#f0f4f8; color:#0f172a; padding:5px; margin:5px;}
.card{background:white;border-radius:12px;padding:10px;margin-bottom:10px;box-shadow:0 4px 16px rgba(0,0,0,0.08);}
table{width:100%;border-collapse:collapse;margin-top:10px;}
th,td{padding:6px 4px;text-align:center;font-size:18px;}
th{background:#e0e7ff;color:#1e40af;}
tr:nth-child(even){background:#f8fafc;}
.status-online{color:#10b981; font-weight:600; font-size:12px; display:block;}
.status-offline{color:#dc2626; font-weight:600; font-size:12px; display:block;}
.button{padding:8px 16px;background:#2563eb;color:white;border:none;border-radius:8px;cursor:pointer;font-weight:600;font-size:16px;}
.button:hover{background:#1e40af;}
.temp-low{color:#10b981; font-weight:bold;}
.temp-high{color:#dc2626; font-weight:bold;}
.temp-container{display:flex; justify-content:center; gap:8px; flex-wrap:wrap;}
.total-hashrate{background:#e0e7ff; padding:8px 16px; border-radius:8px; font-weight:bold; font-size:16px; color:#1e40af;}
.control-row{display:flex; justify-content:space-between; align-items:center; margin-bottom:15px; gap:15px;}
.control-left{display:flex; align-items:center; gap:15px;}
/* icon bar */
.icon-bar { display:flex; gap:10px; align-items:center; }
.icon-btn { background:#2563eb; border:none; color:white; border-radius:8px; font-size:18px; padding:8px 10px; cursor:pointer; transition:all .15s ease; }
.icon-btn:hover { background:#1e40af; transform:scale(1.05); }

/* dropdown menu */
.dropdown { position: relative; display: inline-block; }
.dropdown-content { display: none; position: absolute; background: white; min-width: 200px; box-shadow: 0 8px 32px rgba(0,0,0,0.2); border-radius: 12px; z-index: 1000; border: 1px solid #e2e8f0; padding: 8px 0; }
.dropdown-content a { color: #0f172a; padding: 12px 16px; text-decoration: none; display: block; transition: background 0.2s ease; font-size: 14px; font-weight: 500; }
.dropdown-content a:hover { background: #f1f5f9; }
.dropdown:hover .dropdown-content { display: block; }

/* modal */
.modal{display:none;position:fixed;top:50%;left:50%;transform:translate(-50%, -50%);background:white;padding:20px;border:3px solid #2ecc71;border-radius:10px;box-shadow:0 0 20px rgba(0,0,0,0.3);z-index:1000;width:90%;max-width:800px;max-height:80vh;overflow-y:auto;}
.modal-overlay{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.5);z-index:999;}
.report-btn{background:#9b59b6;color:white;padding:10px 15px;border:none;border-radius:8px;cursor:pointer;font-size:18px;}
.report-btn:hover{background:#8e44ad;}
.modal h3{margin-top:0;color:#2c3e50;text-align:center;border-bottom:2px solid #ecf0f1;padding-bottom:10px;}
.modal h4{color:#34495e;margin-bottom:8px;margin-top:20px;}
.modal p{margin:5px 0;padding:5px;background:#f8f9fa;border-radius:5px;}
.tree-item{margin:5px 0;padding:8px;background:#f8f9fa;border-radius:8px;border:1px solid #e9ecef;}
.tree-header{display:flex; justify-content:space-between; align-items:center; cursor:pointer; font-weight:bold;}
.tree-content{margin-top:8px; padding-right:20px; display:none;}
.tree-time{margin:2px 0; padding:3px 8px; background:white; border-radius:4px; font-family:monospace;}
.expand-btn{background:none; border:none; font-size:16px; cursor:pointer; margin-left:10px;}
.week-title{text-align:center; color:#2c3e50; margin-bottom:15px; padding:10px; background:#e8f5e8; border-radius:8px;}
/* تغییرات رنگ هش‌ریت و آپ‌تایم */
.hash-low{color:#dc2626; font-weight:bold;}   /* هش‌ریت زیر 60 قرمز */
.hash-normal{color:#16a34a; font-weight:bold;} /* هش‌ریت >= 60 سبز */
.uptime-new{color:#1d4ed8; font-weight:bold;}   /* آپ‌تایم زیر 1 روز آبی */
.uptime-old{color:#16a34a; font-weight:bold;}   /* آپ‌تایم >= 1 روز سبز */
@media(max-width:600px){th,td{font-size:16px;padding:8px;}}
/* terminal pre */
.terminal-pre { background:#0b1220; color:#00ff88; padding:10px; height:300px; overflow:auto; border-radius:8px; font-family:monospace; font-size:13px; white-space:pre-wrap; }
</style>
</head>
<body>
<div class="card">
<div class="control-row">
    <div class="control-left">
        <div class="icon-bar">
            <button class="icon-btn" onclick="openTerminal()" title="Terminal">💻</button>
            
            <!-- منوی تنظیمات جدید -->
            <div class="dropdown">
                <button class="icon-btn" title="Settings">⚙️</button>
                <div class="dropdown-content">
                    <a href="#" onclick="showPoolsModal()">🏊 POOLS</a>
                    <a href="#" onclick="showRebootModal()">🔄 REBOOT</a>
                    <a href="#" onclick="showNtpModal()">⏰ TIME & NTP</a>
                </div>
            </div>
            
            <!-- آیکون جدید Logs جایگزین رفرش -->
            <button class="icon-btn" onclick="showLogsModal()" title="View Logs">📋</button>
        </div>
        <div class="total-hashrate">
            Total Hashrate: {{ total_hashrate }} TH/s
        </div>
    </div>
    <button class="report-btn" onclick="showLoginReport()">📊</button>
</div>

<table>
<thead>
<tr>
<th>Name</th>
<th>Uptime</th>
<th>Board Temp (°C)</th>
<th>Hashrate</th>
<th>Power (W)</th>
</tr>
</thead>
<tbody>
{% for m in miners %}
<tr>
<td>
<!-- لینک اصلاح شده: با استفاده از port_map سراسری و fallback به m.port -->
<a href="https://{{ MINER_IP }}:{{ port_map.get(m.name.split(' ')[0], m.port) }}" target="_blank">{{ m.name }}</a>

{% if m.alive %}
<span class="status-online">Online</span>
{% else %}
<span class="status-offline">Offline</span>
{% endif %}
</td>

<!-- Uptime -->
<td>
{% if m.uptime %}
    {% set uptime_sec = 0 %}
    {% if 'd' in m.uptime %}
        {% set parts = m.uptime.split('d') %}
        {% set uptime_sec = (parts[0] | int) * 86400 %}
        {% if 'h' in parts[1] %}
            {% set h_parts = parts[1].split('h') %}
            {% set uptime_sec = uptime_sec + (h_parts[0]|int)*3600 %}
            {% if 'm' in h_parts[1] %}
                {% set m_parts = h_parts[1].split('m') %}
                {% set uptime_sec = uptime_sec + (m_parts[0]|int)*60 %}
            {% endif %}
        {% endif %}
    {% elif 'h' in m.uptime %}
        {% set h_parts = m.uptime.split('h') %}
        {% set uptime_sec = (h_parts[0]|int)*3600 %}
        {% if 'm' in h_parts[1] %}
            {% set m_parts = h_parts[1].split('m') %}
            {% set uptime_sec = uptime_sec + (m_parts[0]|int)*60 %}
        {% endif %}
    {% elif 'm' in m.uptime %}
        {% set uptime_sec = (m.uptime.split('m')[0]|int)*60 %}
    {% endif %}
    
    {% if uptime_sec < 86400 %}
        <span class="uptime-new">{{ m.uptime }}</span>
    {% else %}
        <span class="uptime-old">{{ m.uptime }}</span>
    {% endif %}
{% else %}
    -
{% endif %}
</td>

<!-- Temperature -->
<td>
{% if m.board_temps %}
<div class="temp-container">
  {% for temp in m.board_temps %}
    {% if temp < 60 %}
      <span class="temp-low">{{ temp }}</span>
    {% else %}
      <span class="temp-high">{{ temp }}</span>
    {% endif %}
  {% endfor %}
</div>
{% else %}
-
{% endif %}
</td>

<!-- Hashrate -->
<td>
{% if m.hashrate %}
  {% if m.hashrate < 60 %}
    <span class="hash-low">{{ m.hashrate }}</span>
  {% else %}
    <span class="hash-normal">{{ m.hashrate }}</span>
  {% endif %}
{% else %}
  -
{% endif %}
</td>

<td>{{ m.power or "-" }}</td>
</tr>
{% endfor %}
</tbody>
</table>
</div>

<!-- اضافه شدن پولز مودال -->
""" + get_pools_manager_html() + """

<!-- اضافه شدن ریبوت مودال -->
""" + get_reboot_manager_html() + """

<!-- اضافه شدن ترمینال مودال -->
""" + get_terminal_html() + """

<!-- اضافه شدن NTP مودال -->
""" + get_ntp_html() + """

<!-- اضافه شدن Logs مودال -->
""" + logs_viewer.get_logs_html() + """

<!-- Login Report Modal -->
<div id="modalOverlay" class="modal-overlay" onclick="closeModal()"></div>
<div id="reportModal" class="modal">
    <h3>📋 Weekly Login Report</h3>
    <div id="reportContent">
        <p>Loading...</p>
    </div>
    <div style="text-align: center; margin-top: 20px;">
        <button onclick="closeModal()" style="background: #e74c3c; color: white; padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 16px;">
            ❌ Close
        </button>
    </div>
</div>

<script>
// تابع نمایش پولز مودال
function showPoolsModal() {
    console.log('🏊 Opening Pools Modal...');
    const overlay = document.getElementById('poolsModalOverlay');
    const modal = document.getElementById('poolsModal');
    
    if (overlay && modal) {
        overlay.style.display = 'block';
        modal.style.display = 'block';
        console.log('✅ Pools Modal opened successfully');
    } else {
        console.error('❌ Pools Modal elements not found');
        alert('Pools configuration is not available');
    }
}

// تابع بستن پولز مودال
function closePoolsModal() {
    const overlay = document.getElementById('poolsModalOverlay');
    const modal = document.getElementById('poolsModal');
    
    if (overlay && modal) {
        overlay.style.display = 'none';
        modal.style.display = 'none';
    }
}

// توابع ریبوت مودال
function showRebootModal() {
    console.log('🔄 Opening Reboot Modal...');
    const overlay = document.getElementById('rebootModalOverlay');
    const modal = document.getElementById('rebootModal');
    
    if (overlay && modal) {
        overlay.style.display = 'block';
        modal.style.display = 'block';
        console.log('✅ Reboot Modal opened successfully');
        
        // ریست وضعیت
        setTimeout(() => {
            if (typeof updateRebootSelection === 'function') {
                updateRebootSelection();
            }
        }, 100);
    } else {
        console.error('❌ Reboot Modal elements not found');
        alert('Reboot functionality is not available');
    }
}

function closeRebootModal() {
    const overlay = document.getElementById('rebootModalOverlay');
    const modal = document.getElementById('rebootModal');
    
    if (overlay && modal) {
        overlay.style.display = 'none';
        modal.style.display = 'none';
    }
}

// توابع NTP مودال
function showNtpModal() {
    console.log('⏰ Opening NTP Modal...');
    const overlay = document.getElementById('ntpModalOverlay');
    const modal = document.getElementById('ntpModal');
    
    if (overlay && modal) {
        overlay.style.display = 'block';
        modal.style.display = 'block';
        console.log('✅ NTP Modal opened successfully');
        
        // Initialize modal
        setTimeout(() => {
            if (typeof initializeNtpModal === 'function') {
                initializeNtpModal();
            }
        }, 100);
    } else {
        console.error('❌ NTP Modal elements not found');
        alert('NTP configuration is not available');
    }
}

function closeNtpModal() {
    const overlay = document.getElementById('ntpModalOverlay');
    const modal = document.getElementById('ntpModal');
    
    if (overlay && modal) {
        overlay.style.display = 'none';
        modal.style.display = 'none';
    }
}

// توابع Logs Modal
function showLogsModal() {
    console.log('📋 Opening Logs Modal...');
    const overlay = document.getElementById('logsModalOverlay');
    const modal = document.getElementById('logsModal');
    
    if (overlay && modal) {
        overlay.style.display = 'block';
        modal.style.display = 'block';
        console.log('✅ Logs Modal opened successfully');
        
        // ریست محتوا
        resetLogsOutput();
    } else {
        console.error('❌ Logs Modal elements not found');
    }
}

function closeLogsModal() {
    const overlay = document.getElementById('logsModalOverlay');
    const modal = document.getElementById('logsModal');
    
    if (overlay && modal) {
        overlay.style.display = 'none';
        modal.style.display = 'none';
    }
}

function showProgressBar() {
    document.getElementById('logsProgressContainer').style.display = 'block';
}

function hideProgressBar() {
    document.getElementById('logsProgressContainer').style.display = 'none';
}

function updateProgressBar(percent, message) {
    document.getElementById('logsProgressBar').style.width = percent + '%';
    document.getElementById('logsProgressText').textContent = message;
    document.getElementById('logsProgressPercent').textContent = percent + '%';
}

function showStatus(message, type = 'info') {
    const statusEl = document.getElementById('logsStatus');
    statusEl.textContent = message;
    statusEl.style.display = 'block';
    
    const colors = {
        'info': '#3b82f6',
        'success': '#10b981', 
        'warning': '#f59e0b',
        'error': '#ef4444'
    };
    
    statusEl.style.background = colors[type] + '20';
    statusEl.style.border = '1px solid ' + colors[type] + '40';
    statusEl.style.color = colors[type];
}

function hideStatus() {
    document.getElementById('logsStatus').style.display = 'none';
}

function resetLogsOutput() {
    document.getElementById('logsOutput').innerHTML = `
        <div style="text-align: center; color: #64748b; padding: 40px 20px;">
            <div style="font-size: 48px; margin-bottom: 16px;">📋</div>
            <div style="font-size: 16px; font-weight: 500; margin-bottom: 8px;">Miner Logs Viewer</div>
            <div style="font-size: 14px; color: #94a3b8;">Select a miner and click "Load Logs" to view system logs</div>
        </div>
    `;
}

function loadMinerLogs() {
    const miner = document.getElementById('logsMinerSelect').value;
    const hours = document.getElementById('logsHours').value;
    const output = document.getElementById('logsOutput');

    if (!miner) {
        showStatus('⚠️ Please select a miner first!', 'warning');
        return;
    }

    showProgressBar();
    showStatus(`🚀 Starting log retrieval for Miner ${miner}...`, 'info');
    updateProgressBar(10, 'Initializing connection...');

    output.innerHTML = `
        <div style="text-align: center; color: #3b82f6; padding: 30px 20px;">
            <div style="font-size: 32px; margin-bottom: 12px;">⏳</div>
            <div style="font-size: 14px; font-weight: 500;">Loading logs for Miner ${miner}</div>
            <div style="font-size: 12px; color: #94a3b8; margin-top: 8px;">Please wait while we connect to the miner...</div>
        </div>
    `;

    // شبیه‌سازی پروگرس بار
    let progress = 10;
    const progressInterval = setInterval(() => {
        progress += 2;
        if (progress <= 90) {
            updateProgressBar(progress, 'Connecting to miner...');
        }
    }, 100);

    // ارسال درخواست
    fetch('/get_miner_logs', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            miner: miner,
            hours: hours
        })
    })
    .then(response => {
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        return response.json();
    })
    .then(data => {
        console.log('📦 Received data:', data);
        clearInterval(progressInterval);
        updateProgressBar(100, 'Completed!');
        
        setTimeout(() => {
            hideProgressBar();
            
            if (data && data.status === 'success') {
                showStatus(data.message, 'success');
                output.innerHTML = data.logs || 'No logs content received';
                output.scrollTop = output.scrollHeight;
            } else if (data && data.status === 'error') {
                showStatus(data.message || 'Unknown error', 'error');
                output.innerHTML = `
                    <div style="text-align: center; color: #ef4444; padding: 30px 20px;">
                        <div style="font-size: 32px; margin-bottom: 12px;">❌</div>
                        <div style="font-size: 14px; font-weight: 500;">${data.message || 'Error'}</div>
                        <div style="font-size: 12px; color: #fca5a5; margin-top: 8px;">${data.logs || 'No details'}</div>
                    </div>
                `;
            } else {
                showStatus('❌ Invalid response format', 'error');
                output.innerHTML = `
                    <div style="text-align: center; color: #ef4444; padding: 30px 20px;">
                        <div style="font-size: 32px; margin-bottom: 12px;">🤔</div>
                        <div style="font-size: 14px; font-weight: 500;">Invalid Response</div>
                        <div style="font-size: 12px; color: #fca5a5; margin-top: 8px;">Received: ${JSON.stringify(data)}</div>
                    </div>
                `;
            }
        }, 500);
    })
    .catch(err => {
        console.error('🚨 Fetch error:', err);
        clearInterval(progressInterval);
        updateProgressBar(100, 'Error!');
        hideProgressBar();
        showStatus('⚠️ Connection error occurred', 'error');
        output.innerHTML = `
            <div style="text-align: center; color: #ef4444; padding: 30px 20px;">
                <div style="font-size: 32px; margin-bottom: 12px;">🔌</div>
                <div style="font-size: 14px; font-weight: 500;">Connection Error</div>
                <div style="font-size: 12px; color: #fca5a5; margin-top: 8px;">${err.toString()}</div>
            </div>
        `;
    });
}

function clearLogs() {
    resetLogsOutput();
    hideProgressBar();
    hideStatus();
}

function exportLogs() {
    const logsContent = document.getElementById('logsOutput').innerText;
    if (!logsContent || logsContent.includes('Select a miner')) {
        showStatus('⚠️ No logs to export!', 'warning');
        return;
    }
    
    const blob = new Blob([logsContent], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `miner-logs-${new Date().toISOString().split('T')[0]}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    showStatus('✅ Logs exported successfully!', 'success');
}

function showLoginReport() {
    document.getElementById('modalOverlay').style.display = 'block';
    document.getElementById('reportModal').style.display = 'block';
    
    fetch('/get_login_report')
        .then(response => response.json())
        .then(data => {
            let content = '';
            
            content += `<div class="week-title">
                <h4>📅 Week starting from Saturday ${data.saturday}</h4>
            </div>`;
            
            data.days.forEach(day => {
                content += `<div class="tree-item">
                    <div class="tree-header" onclick="toggleDay('day-${day.date}')">
                        <span>${day.day_name} - ${day.date} (${day.count} logins)</span>
                        <button class="expand-btn">➕</button>
                    </div>
                    <div id="day-${day.date}" class="tree-content">
                `;
                
                if (day.logins.length > 0) {
                    day.logins.forEach(login => {
                        content += `<div class="tree-time">🕐 ${login}</div>`;
                    });
                } else {
                    content += `<div style="text-align:center; color:#666; padding:10px;">No records</div>`;
                }
                
                content += `</div></div>`;
            });
            
            document.getElementById('reportContent').innerHTML = content;
        })
        .catch(error => {
            console.error('Error fetching report:', error);
            document.getElementById('reportContent').innerHTML = '<p>Error loading report</p>';
        });
}

function toggleDay(dayId) {
    const content = document.getElementById(dayId);
    const btn = content.previousElementSibling.querySelector('.expand-btn');
    
    if (content.style.display === 'block') {
        content.style.display = 'none';
        btn.textContent = '➕';
    } else {
        content.style.display = 'block';
        btn.textContent = '➖';
    }
}

function closeModal() {
    document.getElementById('modalOverlay').style.display = 'none';
    document.getElementById('reportModal').style.display = 'none';
}

// Terminal functions
function openTerminal(){
  document.getElementById('terminalOverlay').style.display='block';
  document.getElementById('terminalModal').style.display='block';
  document.getElementById('terminalOutput').textContent='⏳ Ready...';
  document.getElementById('terminalModal').setAttribute('aria-hidden','false');
}
function closeTerminal(){
  document.getElementById('terminalOverlay').style.display='none';
  document.getElementById('terminalModal').style.display='none';
  document.getElementById('terminalModal').setAttribute('aria-hidden','true');
}

function sendCommand(){
    const miner = document.getElementById('minerInput').value.trim();
    const cmd = document.getElementById('cmdInput').value;
    const output = document.getElementById('terminalOutput');

    if (!miner) {
        output.textContent = "⚠️ Please enter miner name (e.g. 131).";
        return;
    }

    output.textContent = "⏳ Running...";

    fetch('/terminal_command', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({miner, cmd})
    })
    .then(r => r.json())
    .then(data => {
        if(data.output){
            let formatted = data.output
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/("(\\\\u[a-zA-Z0-9]{4}|\\\\[^u]|[^\\\\"])*")(\\s*):/g, '<span style="color:green;">$1</span>$3:')
                .replace(/:\\s*("(\\\\u[a-zA-Z0-9]{4}|\\\\[^u]|[^\\\\"])*"|[\\d.eE+-]+)/g, ': <span style="color:red;">$1</span>')
                .replace(/([{}\\[\\]\\(\\)])/g, '<span style="color:blue;">$1</span>');

            output.innerHTML = '<pre class="terminal-pre">' + formatted + '</pre>';
        } else if(data.error){
            output.textContent = "❌ " + data.error;
        } else {
            output.textContent = "❌ Invalid response";
        }
    })
    .catch(err => {
        output.textContent = "⚠️ Connection error: " + err;
    });
}

// بستن با کلیک خارج از مودال‌ها
document.addEventListener('DOMContentLoaded', function() {
    const poolsOverlay = document.getElementById('poolsModalOverlay');
    const rebootOverlay = document.getElementById('rebootModalOverlay');
    const terminalOverlay = document.getElementById('terminalOverlay');
    const ntpOverlay = document.getElementById('ntpModalOverlay');
    const logsOverlay = document.getElementById('logsModalOverlay');
    
    if (poolsOverlay) {
        poolsOverlay.addEventListener('click', closePoolsModal);
    }
    if (rebootOverlay) {
        rebootOverlay.addEventListener('click', closeRebootModal);
    }
    if (terminalOverlay) {
        terminalOverlay.addEventListener('click', closeTerminal);
    }
    if (ntpOverlay) {
        ntpOverlay.addEventListener('click', closeNtpModal);
    }
    if (logsOverlay) {
        logsOverlay.addEventListener('click', closeLogsModal);
    }
});
</script>
</body>
</html>

"""
# === ROUTES ===
@app.route("/", methods=["GET", "POST"])
def index():
    # ثبت لاگین فقط در صورت رفرش/باز شدن صفحه
    update_login_data()
    miners = get_live_data()
    total_hashrate = calculate_total_hashrate(miners)
    return render_template_string(
        TEMPLATE,
        miners=miners,
        total_hashrate=total_hashrate,
        MINER_IP=MINER_IP or "127.0.0.1",
        port_map=port_map,
        MINER_NAMES=MINER_NAMES
    )

@app.route("/terminal_command", methods=["POST"])
def terminal_command():
    """Route برای ترمینال"""
    try:
        data = request.get_json() or {}
        miner_name = data.get("miner")
        cmd = data.get("cmd")

        result = execute_terminal_command(
            miner_name, 
            cmd, 
            MINER_IP, 
            MINER_NAMES, 
            MINER_PORTS
        )
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/get_login_report")
def get_login_report():
    try:
        week_report = get_week_report()
        return jsonify(week_report)
    except Exception as e:
        print(f"Error in get_login_report: {e}")
        return jsonify({"saturday": "Error", "days": []})

@app.route("/update_pools", methods=["POST"])
def update_pools():
    """Update pool settings for a miner"""
    try:
        data = request.get_json()
        miner_name = data.get("miner")
        pools_data = data.get("pools")

        if not miner_name or not pools_data:
            return jsonify({"error": "Missing miner or pools data"})

        result = update_miner_pools(miner_name, pools_data, MINER_USERNAME, MINER_PASSWORD)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/reboot_miner", methods=["POST"])
def reboot_miner_route():
    """Reboot a miner"""
    try:
        data = request.get_json()
        miner_name = data.get("miner")

        if not miner_name:
            return jsonify({"error": "Missing miner name"})

        result = reboot_miner(miner_name, MINER_USERNAME, MINER_PASSWORD)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)})

# اضافه شدن route برای NTP
@app.route("/update_ntp", methods=["POST"])
def update_ntp():
    """Update NTP settings for a miner"""
    try:
        data = request.get_json()
        miner_name = data.get("miner")
        timezone = data.get("timezone")
        ntp_enabled = data.get("ntp_enabled")
        ntp_servers = data.get("ntp_servers")

        if not miner_name:
            return jsonify({"error": "Missing miner name"})

        result = update_ntp_settings(miner_name, timezone, ntp_servers, ntp_enabled, MINER_USERNAME, MINER_PASSWORD)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)})

# اضافه شدن route جدید برای Logs
@app.route("/get_miner_logs", methods=["POST"])
def get_miner_logs_route():
    """Route جدید برای دریافت لاگ‌های ماینر"""
    try:
        data = request.get_json()
        miner_name = data.get("miner")
        hours = data.get("hours", 2)

        if not miner_name:
            return jsonify({"status": "error", "message": "Missing miner name", "logs": ""})

        result = logs_viewer.get_miner_logs(
            miner_name=miner_name,
            hours=hours,
            miner_ip=MINER_IP,
            port_map=port_map,
            miner_username=MINER_USERNAME,
            miner_password=MINER_PASSWORD
        )
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Server error: {str(e)}", "logs": ""})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
