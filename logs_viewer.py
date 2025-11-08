#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import requests
import re
import time
import urllib3
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class AdvancedLogsViewer:
    def __init__(self):
        # رنگ‌ها و شمای رنگ‌بندی
        self.color_scheme = {
            'TIMESTAMP': '#6B7280',
            'NUMBERS': '#EF4444',
            'KEYWORDS': '#3B82F6',
            'CT_CV': '#10B981',      # ct / cv سبز
            'TEXT': '#10B981',
            'BRACKETS': '#F59E0B',
            'ERROR': '#DC2626',
            'WARNING': '#F59E0B',
            'SUCCESS': '#10B981'
        }

    def get_syslog_via_https(self, ip, port, user, password, timeout_login=10, timeout_log=30):
        """Try HTTPS then fallback to HTTP with increased timeouts"""
        session = requests.Session()
        
        try:
            print(f"🆕 Creating new session for {ip}:{port}")
            
            # ۱. صفحه لاگین
            login_url = f"https://{ip}:{port}/cgi-bin/luci"
            r1 = session.get(login_url, verify=False, timeout=timeout_login)
            print(f"📄 Login page status: {r1.status_code}")
            print(f"🍪 Cookies after GET: {session.cookies.get_dict()}")
            
            # ۲. لاگین
            login_data = {'luci_username': user, 'luci_password': password}
            r2 = session.post(login_url, data=login_data, verify=False, timeout=timeout_login, allow_redirects=True)
            print(f"🔐 Login status: {r2.status_code}")
            print(f"🍪 Cookies after POST: {session.cookies.get_dict()}")
            print(f"📏 Response length: {len(r2.text)}")
            
            # ۳. چک کن آیا لاگین موفق بود
            if "Authorization Required" in r2.text:
                print("❌ LOGIN FAILED - Still on login page")
                return "ERROR: Login failed"
            else:
                print("✅ LOGIN SUCCESS - Redirected from login page")
            
            # ۴. صفحه syslog
            syslog_url = f"https://{ip}:{port}/cgi-bin/luci/admin/status/syslog"
            r3 = session.get(syslog_url, verify=False, timeout=timeout_log)
            print(f"📋 Syslog status: {r3.status_code}")
            print(f"🍪 Final cookies: {session.cookies.get_dict()}")
            
            if r3.status_code == 200:
                print(f"✅ SUCCESS - Got {len(r3.text)} characters")
                return r3.text
            else:
                print(f"❌ FAILED - Status {r3.status_code}")
                # Fallback to log.cgi
                try:
                    log_url = f"https://{ip}:{port}/cgi-bin/log.cgi"
                    r4 = session.get(log_url, verify=False, timeout=timeout_log)
                    if r4.status_code == 200:
                        return r4.text
                except:
                    pass
                return f"ERROR: Status {r3.status_code}"
                
        except requests.exceptions.Timeout:
            print("💥 Timeout error")
            return "ERROR: Timeout - Log page is taking too long to load"
        except Exception as e:
            print(f"💥 Exception: {str(e)}")
            # Fallback to HTTP
            try:
                login_url = f"http://{ip}:{port}/cgi-bin/luci"
                session.get(login_url, timeout=timeout_login)
                session.post(login_url, data={'luci_username': user, 'luci_password': password}, timeout=timeout_login, allow_redirects=True)
                log_url = f"http://{ip}:{port}/cgi-bin/luci/admin/status/syslog"
                res = session.get(log_url, timeout=timeout_log)
                return res.text
            except Exception as e2:
                return f"ERROR_FETCHING_SYSLOG: {e2}"

    def get_miner_logs(self, miner_name, hours=2, miner_ip=None, port_map=None, miner_username=None, miner_password=None):
        """
        Main facade called by main.py:
        - hours default = 2 (we keep usage for compatibility)
        - returns dict with status/message/logs/progress/count
        """
        try:
            if not miner_ip:
                return {"status": "error", "message": "❌ Miner IP not configured", "logs": "", "progress": 100}
            if miner_name not in port_map:
                return {"status": "error", "message": f"❌ Miner {miner_name} not found in port map", "logs": "", "progress": 100}

            # افزایش تایم‌اوت برای ماینرهای سنگین
            if miner_name in ['131', '132', '133']:
                timeout_log = 45  # 45 ثانیه برای ماینرهای جدید
            else:
                timeout_log = 30  # 30 ثانیه برای ماینرهای قدیمی

            port = port_map[miner_name]
            log_content = self.get_syslog_via_https(miner_ip, port, miner_username, miner_password, timeout_log=timeout_log)

            if log_content and not str(log_content).startswith("ERROR_FETCHING_SYSLOG") and "ERROR: Login failed" not in log_content:
                logs = self.parse_real_syslog(log_content, hours, miner_name)
                if logs:
                    formatted_logs = self.format_logs_display(logs, miner_name, hours)
                    return {
                        "status": "success",
                        "message": f"✅ Successfully loaded {len(logs)} log entries from {miner_name}",
                        "logs": formatted_logs,
                        "progress": 100,
                        "count": len(logs)
                    }
                else:
                    return {
                        "status": "success",
                        "message": f"📭 No logs found for {miner_name} in the last {hours} hours",
                        "logs": f"📭 No logs found",
                        "progress": 100,
                        "count": 0
                    }
            else:
                return {
                    "status": "error",
                    "message": f"❌ Failed to fetch logs: {log_content}",
                    "logs": f"Connection Error: {log_content}",
                    "progress": 100
                }
        except Exception as e:
            return {"status": "error", "message": f"💥 Unexpected error: {str(e)}", "logs": f"Critical error: {str(e)}", "progress": 100}

    def parse_real_syslog(self, html_content, hours, miner_name):
        """
        - Extract text from HTML (search tags pre, textarea, code, div)
        - Default time window is 'hours' but we keep simple: we return last 200 lines
        - Mark lines containing 'E' with leading '*'
        """
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            syslog_content = None
            for tag_name in ['pre', 'textarea', 'code', 'div']:
                tag = soup.find(tag_name)
                if tag and len(tag.get_text().strip()) > 50:
                    syslog_content = tag.get_text()
                    break
            if not syslog_content:
                syslog_content = soup.get_text()

            if not syslog_content or len(syslog_content.strip()) < 10:
                return []

            # split and clean
            lines = [ln.strip() for ln in syslog_content.split('\n') if ln.strip() and len(ln.strip()) > 5]

            # Attempt optional time filter (if timestamps in format MM-DD HH:MM:SS.*) to keep recent - but always fallback to last 200
            try:
                now = datetime.now()
                cutoff = now - timedelta(hours=hours)
                recent = []
                for ln in lines:
                    m = re.search(r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?)', ln)
                    if m:
                        # assume current year for parsing
                        try:
                            t = datetime.strptime(f"{now.year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S.%f")
                        except Exception:
                            try:
                                t = datetime.strptime(f"{now.year}-{m.group(1)}", "%Y-%m-%d %H:%M:%S")
                            except Exception:
                                t = None
                        if t and t >= cutoff:
                            recent.append(ln)
                if len(recent) >= 10:
                    chosen = recent[-200:]
                else:
                    chosen = lines[-200:]
            except Exception:
                chosen = lines[-200:]

            # Mark lines containing 'E' with leading * (only standalone E)
            for i, ln in enumerate(chosen):
                if re.search(r'\bE\b', ln):
                    chosen[i] = f"*{ln}"
            return chosen
        except Exception:
            return []

    def format_logs_display(self, logs, miner_name, hours):
        """Return HTML formatted logs (numbered + colored)"""
        if not logs:
            return f"<div style='text-align:center;color:{self.color_scheme['TIMESTAMP']};padding:20px;'>📭 No logs found for miner {miner_name}</div>"

        header = (
            f"<div style='color:{self.color_scheme['KEYWORDS']};font-weight:bold;margin-bottom:12px;'>"
            f"📋 Logs for Miner <span style='color:{self.color_scheme['BRACKETS']};'>{miner_name}</span> "
            f"(Last {hours} hours) - {len(logs)} entries</div>"
            f"<div style='border-bottom:1px solid #e5e7eb;margin-bottom:12px;'></div>"
        )

        colored_lines = []
        for idx, ln in enumerate(logs, start=1):
            num = f"<span style='color:{self.color_scheme['TIMESTAMP']};font-weight:bold;'>{idx:3d}.</span>"
            colored_lines.append(f"{num} {self.colorize_log_line(ln)}")
        return header + "\n".join(colored_lines)

    def colorize_log_line(self, line):
        """Apply coloring:
           - numbers -> NUMBERS
           - ct/cv -> CT_CV (green)
           - mining keywords -> KEYWORDS (blue)
           - status words -> ERROR/WARNING/SUCCESS
           - timestamps -> TIMESTAMP
           - brackets -> BRACKETS
        """
        if not line:
            return line

        # escape HTML entities minimally
        safe = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        # keep the leading '*' if exists visible
        leading_star = ''
        if safe.startswith('*'):
            leading_star = '<span style="color:%s;font-weight:bold;">*</span> ' % self.color_scheme['ERROR']
            safe = safe[1:]

        # put in brackets visually
        bracketed = f"[{safe}]"

        # color numbers
        colored = re.sub(r'(\d+\.\d+|\d+)', lambda m: f"<span style='color:{self.color_scheme['NUMBERS']};font-weight:bold;'>{m.group(1)}</span>", bracketed)

        # ct / cv green (word boundaries)
        colored = re.sub(r'\b(ct:|cv:|ct|cv)\b', lambda m: f"<span style='color:{self.color_scheme['CT_CV']};font-weight:bold;'>{m.group(1)}</span>", colored, flags=re.IGNORECASE)

        # mining keywords blue (other keywords)
        mining_keywords = [
            'btminer','bt','env','fan','ac','pin','vout','chain','temp','temperature',
            'frequency','voltage','power','hashrate','kernel','miner','pool','asic','bitmain','antminer',
            'speed','rpm','watts','volts','mhz','ghz','th/s','gh/s','mh/s'
        ]
        for kw in mining_keywords:
            colored = re.sub(r'\b' + re.escape(kw) + r'\b', lambda m: f"<span style='color:{self.color_scheme['KEYWORDS']};font-weight:bold;'>{m.group(0)}</span>", colored, flags=re.IGNORECASE)

        # status words coloring
        status_patterns = [
            (r'(error|failed|failure|crash|panic|fault|unable|cannot|timeout)', self.color_scheme['ERROR']),
            (r'(warn|warning|alert|notice|attention|caution)', self.color_scheme['WARNING']),
            (r'(success|completed|ready|online|started|connected|ok|running|active)', self.color_scheme['SUCCESS'])
        ]
        for pat, col in status_patterns:
            colored = re.sub(pat, lambda m: f"<span style='color:{col};font-weight:bold;'>{m.group(0)}</span>", colored, flags=re.IGNORECASE)

        # timestamps gray-ish
        timestamp_patterns = [
            r'(\w{3} \d{1,2} \d{2}:\d{2}:\d{2})',
            r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})',
            r'(\d{2}-\d{2} \d{2}:\d{2}:\d{2})',
            r'(\d{2}:\d{2}:\d{2})'
        ]
        for pat in timestamp_patterns:
            colored = re.sub(pat, lambda m: f"<span style='color:{self.color_scheme['TIMESTAMP']};'>{m.group(1)}</span>", colored)

        # color brackets
        colored = colored.replace('[', f"<span style='color:{self.color_scheme['BRACKETS']};font-weight:bold;'>[</span>")
        colored = colored.replace(']', f"<span style='color:{self.color_scheme['BRACKETS']};font-weight:bold;'>]</span>")

        return leading_star + colored

    def get_logs_html(self):
        """Return complete logs modal HTML with management buttons"""
        return '''
        <!-- Logs Modal -->
        <div id="logsModalOverlay" class="modal-overlay" onclick="closeLogsModal()"></div>
        <div id="logsModal" class="modal" style="max-width:95%;width:95%;max-height:90vh;">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
                <h3 style="margin:0;color:#1f2937;">📋 Miner Logs Viewer</h3>
                <button onclick="closeLogsModal()" style="background:#6b7280;color:white;border:none;border-radius:6px;padding:8px 12px;cursor:pointer;font-size:12px;">✕ Close</button>
            </div>

            <div id="logsProgressContainer" style="margin-bottom:20px;display:none;">
                <div style="display:flex;justify-content:space-between;margin-bottom:8px;">
                    <span id="logsProgressText" style="font-weight:bold;color:#3b82f6;font-size:14px;">Initializing...</span>
                    <span id="logsProgressPercent" style="font-weight:bold;color:#6b7280;font-size:14px;">0%</span>
                </div>
                <div style="width:100%;height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden;">
                    <div id="logsProgressBar" style="width:0%;height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);border-radius:4px;transition:width .5s;"></div>
                </div>
            </div>

            <div id="logsStatus" style="padding:12px;border-radius:8px;margin-bottom:15px;display:none;"></div>

            <div style="display:flex;gap:15px;align-items:end;margin-bottom:20px;flex-wrap:wrap;background:#f8fafc;padding:15px;border-radius:8px;">
                <div style="flex:1;min-width:150px;">
                    <label style="font-weight:bold;display:block;margin-bottom:8px;color:#374151;">🔽 Select Miner</label>
                    <select id="logsMinerSelect" style="padding:10px 12px;border-radius:8px;border:2px solid #d1d5db;width:100%;background:white;font-size:14px;">
                        <option value="">-- Choose Miner --</option>
                        <option value="65">Miner 65</option>
                        <option value="66">Miner 66</option>
                        <option value="70">Miner 70</option>
                        <option value="131">Miner 131</option>
                        <option value="132">Miner 132</option>
                        <option value="133">Miner 133</option>
                    </select>
                </div>
                <div style="flex:1;min-width:150px;">
                    <label style="font-weight:bold;display:block;margin-bottom:8px;color:#374151;">🕐 Time Range</label>
                    <select id="logsHours" style="padding:10px 12px;border-radius:8px;border:2px solid #d1d5db;width:100%;background:white;font-size:14px;">
                        <option value="2">2 Hours</option>
                    </select>
                </div>
                <div style="min-width:120px;">
                    <button onclick="loadMinerLogs()" style="padding:12px 20px;background:linear-gradient(135deg,#8B5CF6,#3B82F6);color:white;border:none;border-radius:8px;cursor:pointer;font-weight:bold;width:100%;font-size:14px;">🔍 Load Logs</button>
                </div>
            </div>

            <!-- Terminal Management Buttons -->
            <div style="display:flex;gap:10px;margin-bottom:15px;flex-wrap:wrap;">
                <button onclick="copyLogsToClipboard()" style="padding:10px 16px;background:#10B981;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px;">
                    📋 Copy to Clipboard
                </button>
                <button onclick="clearLogsTerminal()" style="padding:10px 16px;background:#F59E0B;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px;">
                    🧹 Clear Terminal
                </button>
                <button onclick="refreshLogs()" style="padding:10px 16px;background:#3B82F6;color:white;border:none;border-radius:6px;cursor:pointer;font-size:13px;display:flex;align-items:center;gap:6px;">
                    🔄 Refresh
                </button>
            </div>

            <div id="logsOutput" class="terminal-pre" style="min-height:400px;max-height:50vh;font-family:monospace;font-size:13px;line-height:1.4;background:#0f172a;border:1px solid #1e293b;border-radius:8px;padding:16px;white-space:pre;overflow:auto;">
                <div style="text-align:center;color:#64748b;padding:40px 20px;">
                    <div style="font-size:48px;margin-bottom:16px;">📋</div>
                    <div style="font-size:16px;font-weight:500;margin-bottom:8px;">Miner Logs Viewer</div>
                    <div style="font-size:14px;color:#94a3b8;">Select a miner and click "Load Logs"</div>
                </div>
            </div>
        </div>

        <script>
        function copyLogsToClipboard() {
            const logsOutput = document.getElementById('logsOutput');
            const text = logsOutput.innerText || logsOutput.textContent;
            
            navigator.clipboard.writeText(text).then(function() {
                showTemporaryMessage('✅ Logs copied to clipboard!', 'success');
            }).catch(function(err) {
                showTemporaryMessage('❌ Failed to copy logs', 'error');
                console.error('Copy failed: ', err);
            });
        }

        function clearLogsTerminal() {
            document.getElementById('logsOutput').innerHTML = 
                '<div style="text-align:center;color:#64748b;padding:40px 20px;">' +
                '<div style="font-size:48px;margin-bottom:16px;">🧹</div>' +
                '<div style="font-size:16px;font-weight:500;margin-bottom:8px;">Terminal Cleared</div>' +
                '<div style="font-size:14px;color:#94a3b8;">Select a miner and click "Load Logs"</div>' +
                '</div>';
                
            showTemporaryMessage('🧹 Terminal cleared', 'info');
        }

        function refreshLogs() {
            const minerSelect = document.getElementById('logsMinerSelect');
            if (minerSelect.value) {
                loadMinerLogs();
                showTemporaryMessage('🔄 Refreshing logs...', 'info');
            } else {
                showTemporaryMessage('⚠️ Please select a miner first', 'warning');
            }
        }

        function showTemporaryMessage(message, type) {
            const statusDiv = document.getElementById('logsStatus');
            const colors = {
                success: '#10B981',
                error: '#EF4444', 
                warning: '#F59E0B',
                info: '#3B82F6'
            };
            
            statusDiv.innerHTML = message;
            statusDiv.style.backgroundColor = colors[type] + '20';
            statusDiv.style.color = colors[type];
            statusDiv.style.border = '1px solid ' + colors[type] + '40';
            statusDiv.style.display = 'block';
            
            setTimeout(() => {
                statusDiv.style.display = 'none';
            }, 3000);
        }
        </script>
        '''

# global instance (this name must match what main.py imports)
logs_viewer = AdvancedLogsViewer()
