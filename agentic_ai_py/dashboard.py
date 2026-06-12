import os
import sys
import json
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import datetime

PORT = 8585

class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Silence default log messages to keep console clean
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        # Enable CORS
        def set_cors_headers(response):
            response.send_header("Access-Control-Allow-Origin", "*")
            response.send_header("Access-Control-Allow-Methods", "GET, OPTIONS, POST")
            response.send_header("Access-Control-Allow-Headers", "Content-Type")

        if path in ["/api/approve", "/api/reject", "/api/abort"]:
            action = path.split("/")[-1]
            status_map = {
                "approve": "approved",
                "reject": "rejected",
                "abort": "aborted"
            }
            pending_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test", "pending_approval.json")
            if os.path.exists(pending_json):
                try:
                    with open(pending_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["status"] = status_map[action]
                    with open(pending_json, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    set_cors_headers(self)
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
                    return
                except Exception as e:
                    self.send_error(500, f"Error saving status: {e}")
                    return
            self.send_error(404, "No pending approval found")
            return
            
        self.send_error(404, "Not Found")

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        
        # Enable CORS
        def set_cors_headers(response):
            response.send_header("Access-Control-Allow-Origin", "*")
            response.send_header("Access-Control-Allow-Methods", "GET, OPTIONS, POST")
            response.send_header("Access-Control-Allow-Headers", "Content-Type")

        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_CONTENT.encode("utf-8"))
            return
            
        elif path == "/api/sessions":
            sessions = []
            test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
            if os.path.exists(test_dir):
                for name in os.listdir(test_dir):
                    dir_path = os.path.join(test_dir, name)
                    if os.path.isdir(dir_path) and name.startswith("mai_output_"):
                        mtime = os.path.getmtime(dir_path)
                        date_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                        
                        files = []
                        for root, _, fs in os.walk(dir_path):
                            for file in fs:
                                if file in ["session_history.json", "chat_history.md", "pending_approval.json"] or ".git" in root or "__pycache__" in root:
                                    continue
                                rel_path = os.path.relpath(os.path.join(root, file), dir_path)
                                files.append(rel_path)
                                
                        git_commits = []
                        if os.path.exists(os.path.join(dir_path, ".git")):
                            try:
                                git_res = subprocess.run(
                                    ["git", "log", "--pretty=format:%h - %s (%cr)", "-n", "10"],
                                    cwd=dir_path,
                                    capture_output=True,
                                    text=True
                                )
                                if git_res.returncode == 0:
                                    git_commits = [line for line in git_res.stdout.splitlines() if line]
                            except Exception:
                                pass
                                
                        hist_json = os.path.join(dir_path, "session_history.json")
                        has_history = os.path.exists(hist_json)
                        
                        sessions.append({
                            "id": name,
                            "date": date_str,
                            "files_count": len(files),
                            "files": files,
                            "git_commits": git_commits,
                            "has_history": has_history
                        })
            
            sessions.sort(key=lambda x: x["date"], reverse=True)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            set_cors_headers(self)
            self.end_headers()
            self.wfile.write(json.dumps(sessions).encode("utf-8"))
            return
            
        elif path == "/api/pending_approval":
            pending_json = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test", "pending_approval.json")
            if os.path.exists(pending_json):
                try:
                    with open(pending_json, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    set_cors_headers(self)
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode("utf-8"))
                    return
                except Exception:
                    pass
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            set_cors_headers(self)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "none"}).encode("utf-8"))
            return

        elif path.startswith("/api/session/"):
            parts = path.split("/")
            if len(parts) >= 4:
                session_id = parts[3]
                test_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")
                session_dir = os.path.join(test_dir, session_id)
                
                if not os.path.exists(session_dir):
                    self.send_error(404, "Session not found")
                    return
                    
                if len(parts) >= 6 and parts[4] == "file":
                    file_rel_path = urllib.parse.unquote("/".join(parts[5:]))
                    file_abs_path = os.path.join(session_dir, file_rel_path)
                    
                    if not os.path.abspath(file_abs_path).startswith(os.path.abspath(session_dir)):
                        self.send_error(403, "Access denied")
                        return
                        
                    if os.path.exists(file_abs_path) and os.path.isfile(file_abs_path):
                        try:
                            with open(file_abs_path, "r", encoding="utf-8") as f:
                                file_content = f.read()
                            self.send_response(200)
                            self.send_header("Content-Type", "text/plain; charset=utf-8")
                            set_cors_headers(self)
                            self.end_headers()
                            self.wfile.write(file_content.encode("utf-8"))
                        except Exception as e:
                            self.send_error(500, f"Error reading file: {e}")
                        return
                    else:
                        self.send_error(404, "File not found")
                        return
                        
                elif len(parts) >= 5 and parts[4] == "history":
                    hist_json = os.path.join(session_dir, "session_history.json")
                    if os.path.exists(hist_json):
                        try:
                            with open(hist_json, "r", encoding="utf-8") as f:
                                data = json.load(f)
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            set_cors_headers(self)
                            self.end_headers()
                            self.wfile.write(json.dumps(data).encode("utf-8"))
                        except Exception as e:
                            self.send_error(500, f"Error reading session history: {e}")
                        return
                    else:
                        # Fallback to chat_history.md
                        md_path = os.path.join(session_dir, "chat_history.md")
                        if os.path.exists(md_path):
                            with open(md_path, "r", encoding="utf-8") as f:
                                md_content = f.read()
                            
                            # Fake standard JSON format
                            dummy_data = {
                                "session_dir": session_id,
                                "messages": [
                                    {"type": "ai", "content": md_content}
                                ]
                            }
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            set_cors_headers(self)
                            self.end_headers()
                            self.wfile.write(json.dumps(dummy_data).encode("utf-8"))
                            return
                            
                        self.send_error(404, "History not found")
                        return
            
        self.send_error(404, "Not Found")

# =====================================================================
# GORGEOUS DARK-MODE GLASSMORPHIC FRONTEND CONTENT
# =====================================================================
HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MAI CLI Agent Dashboard</title>
    <!-- Google Fonts -->
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <!-- PrismJS CDN for stunning code syntax highlighting -->
    <link href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css" rel="stylesheet" />
    <!-- FontAwesome icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <style>
        :root {
            --bg-color: #0b0c10;
            --sidebar-bg: rgba(21, 23, 30, 0.7);
            --card-bg: rgba(30, 33, 45, 0.4);
            --border-color: rgba(255, 255, 255, 0.08);
            --text-primary: #f5f6f9;
            --text-secondary: #9499b0;
            --accent-cyan: #00f0ff;
            --accent-purple: #bd5eff;
            --success-color: #00e676;
            --active-glow: 0 0 15px rgba(0, 240, 255, 0.35);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
            font-family: 'Plus Jakarta Sans', sans-serif;
            scrollbar-width: thin;
            scrollbar-color: rgba(255, 255, 255, 0.1) transparent;
        }

        *::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }
        *::-webkit-scrollbar-track {
            background: transparent;
        }
        *::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.15);
            border-radius: 4px;
        }
        *::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.3);
        }

        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            overflow: hidden;
            height: 100vh;
            display: flex;
            background-image: 
                radial-gradient(at 0% 0%, rgba(189, 94, 255, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(0, 240, 255, 0.08) 0px, transparent 50%);
        }

        #app {
            display: flex;
            width: 100%;
            height: 100vh;
        }

        /* SIDEBAR STYLE */
        .sidebar {
            width: 320px;
            background: var(--sidebar-bg);
            border-right: 1px solid var(--border-color);
            backdrop-filter: blur(25px);
            display: flex;
            flex-direction: column;
            height: 100%;
        }

        .sidebar-header {
            padding: 24px;
            border-bottom: 1px solid var(--border-color);
            display: flex;
            align-items: center;
            gap: 12px;
        }

        .logo-icon {
            font-size: 24px;
            color: var(--accent-cyan);
            text-shadow: var(--active-glow);
            animation: pulse 2s infinite ease-in-out;
        }

        @keyframes pulse {
            0%, 100% { transform: scale(1); opacity: 0.9; }
            50% { transform: scale(1.08); opacity: 1; text-shadow: 0 0 25px rgba(0, 240, 255, 0.65); }
        }

        .logo-text {
            font-weight: 700;
            font-size: 20px;
            letter-spacing: 0.8px;
            background: linear-gradient(135deg, var(--text-primary) 30%, var(--accent-cyan));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .session-list {
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .session-item {
            padding: 14px 16px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid transparent;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .session-item:hover {
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.1);
            transform: translateY(-2px);
        }

        .session-item.active {
            background: rgba(0, 240, 255, 0.06);
            border-color: var(--accent-cyan);
            box-shadow: 0 4px 20px rgba(0, 240, 255, 0.05);
        }

        .session-title {
            font-size: 13.5px;
            font-weight: 600;
            color: var(--text-primary);
            word-break: break-all;
            margin-bottom: 6px;
        }

        .session-meta {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 11px;
            color: var(--text-secondary);
        }

        .session-badge {
            background: rgba(255, 255, 255, 0.06);
            padding: 2px 8px;
            border-radius: 20px;
            font-weight: 500;
        }

        /* MAIN DASHBOARD PANEL */
        .main-panel {
            flex: 1;
            display: flex;
            flex-direction: column;
            height: 100%;
            background: rgba(10, 11, 15, 0.3);
        }

        .main-header {
            padding: 20px 32px;
            border-bottom: 1px solid var(--border-color);
            background: rgba(15, 17, 24, 0.4);
            backdrop-filter: blur(10px);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header-title-container h1 {
            font-size: 18px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .header-subtitle {
            font-size: 12.5px;
            color: var(--text-secondary);
        }

        .tabs {
            display: flex;
            gap: 8px;
            border-bottom: 1px solid var(--border-color);
            padding: 0 32px;
            background: rgba(15, 17, 24, 0.2);
        }

        .tab-btn {
            padding: 16px 20px;
            background: transparent;
            border: none;
            color: var(--text-secondary);
            font-size: 13.5px;
            font-weight: 600;
            cursor: pointer;
            position: relative;
            transition: color 0.2s;
        }

        .tab-btn:hover {
            color: var(--text-primary);
        }

        .tab-btn.active {
            color: var(--accent-cyan);
        }

        .tab-btn.active::after {
            content: '';
            position: absolute;
            bottom: 0;
            left: 20px;
            right: 20px;
            height: 3px;
            background: var(--accent-cyan);
            border-radius: 4px 4px 0 0;
            box-shadow: var(--active-glow);
        }

        /* TAB CONTENT SECTIONS */
        .tab-content {
            flex: 1;
            overflow: hidden;
            display: none;
        }

        .tab-content.active {
            display: flex;
        }

        /* CHAT VIEW */
        .chat-container {
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            padding: 32px;
            overflow-y: auto;
            gap: 24px;
        }

        .chat-bubble {
            max-width: 80%;
            border-radius: 16px;
            padding: 18px 24px;
            line-height: 1.6;
            font-size: 14px;
            border: 1px solid var(--border-color);
        }

        .chat-bubble.human {
            align-self: flex-start;
            background: rgba(255, 255, 255, 0.02);
            color: var(--text-primary);
            border-left: 4px solid var(--accent-purple);
        }

        .chat-bubble.ai {
            align-self: flex-end;
            background: var(--card-bg);
            color: var(--text-primary);
            border-left: 4px solid var(--accent-cyan);
            backdrop-filter: blur(10px);
            white-space: pre-wrap;
        }

        .chat-bubble-header {
            font-weight: 700;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 1.2px;
            margin-bottom: 8px;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .chat-bubble.human .chat-bubble-header {
            color: var(--accent-purple);
        }

        .chat-bubble.ai .chat-bubble-header {
            color: var(--accent-cyan);
        }

        /* FILE EXPLORER STYLE */
        .files-layout {
            display: flex;
            width: 100%;
            height: 100%;
        }

        .file-sidebar {
            width: 240px;
            border-right: 1px solid var(--border-color);
            background: rgba(255, 255, 255, 0.01);
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }

        .file-sidebar-title {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: var(--text-secondary);
            margin-bottom: 12px;
            padding-left: 8px;
        }

        .file-item {
            padding: 10px 14px;
            border-radius: 8px;
            font-size: 12.5px;
            color: var(--text-secondary);
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
            transition: all 0.2s;
            border: 1px solid transparent;
        }

        .file-item:hover {
            background: rgba(255, 255, 255, 0.03);
            color: var(--text-primary);
        }

        .file-item.active {
            background: rgba(0, 240, 255, 0.05);
            border-color: rgba(0, 240, 255, 0.15);
            color: var(--accent-cyan);
        }

        .file-content-viewer {
            flex: 1;
            height: 100%;
            display: flex;
            flex-direction: column;
            background: #14161f;
        }

        .viewer-header {
            padding: 12px 24px;
            border-bottom: 1px solid var(--border-color);
            background: rgba(10, 11, 16, 0.6);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .viewer-filename {
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            color: var(--text-primary);
        }

        .viewer-body {
            flex: 1;
            overflow: auto;
            padding: 0;
            position: relative;
        }

        .code-container {
            margin: 0;
            padding: 20px;
            background: transparent !important;
            height: 100%;
            overflow: auto;
        }

        code[class*="language-"] {
            font-family: 'JetBrains Mono', monospace !important;
            font-size: 13px !important;
            text-shadow: none !important;
        }

        /* GIT LOG VIEW */
        .git-container {
            width: 100%;
            height: 100%;
            padding: 32px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 20px;
        }

        .git-timeline {
            position: relative;
            padding-left: 24px;
        }

        .git-timeline::before {
            content: '';
            position: absolute;
            left: 5px;
            top: 6px;
            bottom: 6px;
            width: 2px;
            background: var(--border-color);
        }

        .git-commit {
            position: relative;
            margin-bottom: 24px;
        }

        .git-commit::before {
            content: '';
            position: absolute;
            left: -24px;
            top: 5px;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: var(--bg-color);
            border: 2px solid var(--accent-cyan);
            box-shadow: var(--active-glow);
        }

        .git-commit-hash {
            font-family: 'JetBrains Mono', monospace;
            color: var(--accent-cyan);
            font-weight: 600;
            font-size: 12.5px;
            margin-right: 8px;
        }

        .git-commit-msg {
            font-size: 14px;
            color: var(--text-primary);
        }

        /* PLACEHOLDER OR EMPTY STATE */
        .empty-state {
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            flex: 1;
            color: var(--text-secondary);
            gap: 16px;
            text-align: center;
        }

        .empty-state i {
            font-size: 48px;
            color: rgba(255, 255, 255, 0.05);
        }

        /* Diff styling for Web Approval Modal */
        .diff-file-card {
            border: 1px solid var(--border-color);
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.01);
            overflow: hidden;
            margin-bottom: 20px;
        }
        .diff-file-header {
            padding: 12px 18px;
            background: rgba(255, 255, 255, 0.03);
            border-bottom: 1px solid var(--border-color);
            font-family: 'JetBrains Mono', monospace;
            font-size: 13px;
            font-weight: 600;
        }
        .diff-lines {
            padding: 12px;
            font-family: 'JetBrains Mono', monospace;
            font-size: 12.5px;
            line-height: 1.5;
            background: #14161f;
            overflow-x: auto;
            max-height: 350px;
        }
        .diff-line {
            white-space: pre;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .diff-add {
            background: rgba(0, 230, 118, 0.12);
            color: #81c784;
        }
        .diff-del {
            background: rgba(239, 83, 80, 0.12);
            color: #e57373;
        }
        .diff-info {
            color: #4fc3f7;
            background: rgba(79, 195, 247, 0.08);
        }
    </style>
</head>
<body>
    <!-- Approval Overlay Modal -->
    <div id="approvalModal" style="display: none; position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; background: rgba(0,0,0,0.85); backdrop-filter: blur(20px); z-index: 10000; justify-content: center; align-items: center; padding: 40px;">
        <div class="modal-card" style="background: var(--sidebar-bg); border: 1px solid var(--accent-cyan); border-radius: 20px; width: 90%; max-width: 900px; height: 90%; display: flex; flex-direction: column; box-shadow: var(--active-glow); overflow: hidden;">
            <div class="modal-header" style="padding: 24px; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; background: rgba(15,17,24,0.6);">
                <div style="display: flex; align-items: center; gap: 12px; color: var(--text-primary);">
                    <i class="fa-solid fa-triangle-exclamation" style="color: var(--accent-cyan); font-size: 20px;"></i>
                    <h2 style="font-size: 18px; font-weight: 700; color: var(--text-primary);">Awaiting Code Change Approval</h2>
                </div>
                <div style="font-size: 12px; color: var(--text-secondary);">Verify changes before writing to disk</div>
            </div>
            <div class="modal-body" id="approvalModalBody" style="flex: 1; overflow-y: auto; padding: 24px; display: flex; flex-direction: column; gap: 20px; color: var(--text-primary);">
                <!-- Diffs render here -->
            </div>
            <div class="modal-footer" style="padding: 24px; border-top: 1px solid var(--border-color); display: flex; justify-content: flex-end; gap: 16px; background: rgba(15,17,24,0.6);">
                <button onclick="submitApproval('abort')" style="background: rgba(239, 83, 80, 0.1); border: 1px solid #ef5350; color: #ef5350; padding: 12px 24px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.2s;">Abort Flow</button>
                <button onclick="submitApproval('reject')" style="background: rgba(255, 167, 38, 0.1); border: 1px solid #ffa726; color: #ffa726; padding: 12px 24px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.2s;">Reject Changes</button>
                <button onclick="submitApproval('approve')" style="background: rgba(0, 230, 118, 0.1); border: 1px solid var(--success-color); color: var(--success-color); padding: 12px 24px; border-radius: 8px; font-weight: 600; cursor: pointer; transition: all 0.2s; box-shadow: 0 0 15px rgba(0, 230, 118, 0.2);">Approve & Apply</button>
            </div>
        </div>
    </div>

    <div id="app">
        <!-- Sidebar: Sessions List -->
        <div class="sidebar">
            <div class="sidebar-header">
                <i class="fa-solid fa-brain logo-icon"></i>
                <div class="logo-text">MAI CLI Dashboard</div>
            </div>
            <div class="session-list" id="sessionList">
                <!-- Session items loaded dynamically -->
            </div>
        </div>

        <!-- Main Workspace -->
        <div class="main-panel">
            <div id="activeWorkspace" style="display: none; height: 100%; flex-direction: column;">
                <div class="main-header">
                    <div class="header-title-container">
                        <h1 id="activeSessionTitle">Session Title</h1>
                        <div class="header-subtitle" id="activeSessionDate">Created: N/A</div>
                    </div>
                </div>

                <div class="tabs">
                    <button class="tab-btn active" onclick="switchTab('chat')">💬 Conversation</button>
                    <button class="tab-btn" onclick="switchTab('files')">📂 Code explorer</button>
                    <button class="tab-btn" onclick="switchTab('git')">🛡️ Self-Healing History (Git)</button>
                </div>

                <!-- Tab 1: Chat History -->
                <div class="tab-content active" id="chatTab">
                    <div class="chat-container" id="chatContainer">
                        <!-- Chat history loads dynamically -->
                    </div>
                </div>

                <!-- Tab 2: Files Explorer -->
                <div class="tab-content" id="filesTab">
                    <div class="files-layout">
                        <div class="file-sidebar" id="fileSidebar">
                            <div class="file-sidebar-title">Generated Files</div>
                            <!-- File items load dynamically -->
                        </div>
                        <div class="file-content-viewer">
                            <div class="viewer-header">
                                <div class="viewer-filename" id="viewerFilename">Select a file...</div>
                            </div>
                            <div class="viewer-body">
                                <pre class="code-container"><code id="codeBlock" class="language-javascript">// Code content will appear here</code></pre>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Tab 3: Git Commits -->
                <div class="tab-content" id="gitTab">
                    <div class="git-container">
                        <div class="file-sidebar-title">Self-Healing Commits</div>
                        <div class="git-timeline" id="gitTimeline">
                            <!-- Git history logs load dynamically -->
                        </div>
                    </div>
                </div>
            </div>

            <!-- Empty State Workspace -->
            <div class="empty-state" id="emptyWorkspace">
                <i class="fa-solid fa-code"></i>
                <p>Select a session from the sidebar to view code, execution logs, and healing processes</p>
            </div>
        </div>
    </div>

    <!-- PrismJS CDN for code highlighting -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-core.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>

    <script>
        let allSessions = [];
        let activeSession = null;
        let activeFile = null;

        // Fetch all sessions from python server API
        async function fetchSessions() {
            try {
                const response = await fetch('/api/sessions');
                allSessions = await response.json();
                renderSessionsList();
            } catch (err) {
                console.error("Error fetching sessions:", err);
            }
        }

        // Render sessions in the sidebar
        function renderSessionsList() {
            const listContainer = document.getElementById("sessionList");
            listContainer.innerHTML = "";

            if (allSessions.length === 0) {
                listContainer.innerHTML = `<div style="text-align:center; padding: 20px; color: var(--text-secondary); font-size:13px;">No sessions found. Start MAI CLI to generate files!</div>`;
                return;
            }

            allSessions.forEach(session => {
                const item = document.createElement("div");
                item.className = `session-item ${activeSession && activeSession.id === session.id ? 'active' : ''}`;
                item.onclick = () => selectSession(session.id);
                
                // Format folder name
                const displayTitle = session.id.replace("mai_output_", "Session: ");
                
                item.innerHTML = `
                    <div class="session-title">${displayTitle}</div>
                    <div class="session-meta">
                        <span>${session.date}</span>
                        <span class="session-badge">${session.files_count} file(s)</span>
                    </div>
                `;
                listContainer.appendChild(item);
            });
        }

        // Select a session to load details
        async function selectSession(sessionId) {
            const session = allSessions.find(s => s.id === sessionId);
            if (!session) return;
            activeSession = session;
            
            // Re-render sidebar to update active class
            renderSessionsList();

            // Show workspace
            document.getElementById("emptyWorkspace").style.display = "none";
            document.getElementById("activeWorkspace").style.display = "flex";

            // Update title/meta
            document.getElementById("activeSessionTitle").innerText = session.id;
            document.getElementById("activeSessionDate").innerText = `Last Modified: ${session.date}`;

            // Load Chat History
            loadChatHistory(session);

            // Load Files Explorer
            loadFileExplorer(session);

            // Load Git commits timeline
            loadGitCommits(session);
        }

        // Load chat history
        async function loadChatHistory(session) {
            const chatContainer = document.getElementById("chatContainer");
            chatContainer.innerHTML = "";

            try {
                const response = await fetch(`/api/session/${session.id}/history`);
                if (response.ok) {
                    const data = await response.json();
                    
                    if (data.messages && data.messages.length > 0) {
                        data.messages.forEach(msg => {
                            const bubble = document.createElement("div");
                            const isAI = msg.type === "ai";
                            bubble.className = `chat-bubble ${isAI ? 'ai' : 'human'}`;
                            
                            const roleName = isAI ? "MAI CLI AGENT" : "USER";
                            let content = msg.content;
                            
                            // Try formatting structured file json inside AI bubble for prettier presentation
                            if (isAI) {
                                try {
                                    const parsed = JSON.parse(content);
                                    if (parsed.files) {
                                        content = `🛠️ Proposed Files Generation:\\n` + parsed.files.map(f => ` - \`\${f.path}\` (\${f.content.split('\\n').length} lines)`).join('\\n');
                                    }
                                } catch(e) {}
                            }

                            bubble.innerHTML = `
                                <div class="chat-bubble-header">
                                    <i class="fa-solid ${isAI ? 'fa-robot' : 'fa-user'}"></i>
                                    \${roleName}
                                </div>
                                \${content}
                            `;
                            chatContainer.appendChild(bubble);
                        });
                    } else {
                        chatContainer.innerHTML = `<div class="empty-state"><p>No dialogue history found in session logs</p></div>`;
                    }
                } else {
                    chatContainer.innerHTML = `<div class="empty-state"><p>No conversation logs found</p></div>`;
                }
            } catch (err) {
                chatContainer.innerHTML = `<div class="empty-state"><p>Error loading dialogue history</p></div>`;
            }
        }

        // Load files in the file explorer sidebar
        function loadFileExplorer(session) {
            const fileSidebar = document.getElementById("fileSidebar");
            // Clear previous files, keep header
            fileSidebar.innerHTML = `<div class="file-sidebar-title">Generated Files</div>`;

            if (!session.files || session.files.length === 0) {
                fileSidebar.innerHTML += `<div style="padding:10px; color: var(--text-secondary); font-size:12px;">No source files found</div>`;
                clearCodeViewer();
                return;
            }

            session.files.forEach(file => {
                const item = document.createElement("div");
                item.className = "file-item";
                
                // Set file icon based on extension
                const ext = file.split('.').pop().toLowerCase();
                let iconClass = "fa-file-code";
                if (ext === "py") iconClass = "fa-brands fa-python";
                else if (ext === "js") iconClass = "fa-brands fa-square-js";
                else if (ext === "ts") iconClass = "fa-code";
                else if (ext === "html") iconClass = "fa-brands fa-html5";
                else if (ext === "css") iconClass = "fa-brands fa-css3-alt";
                else if (ext === "json") iconClass = "fa-brackets-curly";
                else if (ext === "sql") iconClass = "fa-solid fa-database";

                item.innerHTML = `<i class="\${iconClass}"></i> \${file}`;
                item.onclick = () => selectFile(file, item);
                fileSidebar.appendChild(item);
            });

            // Automatically select first file
            const firstFileItem = fileSidebar.querySelector(".file-item");
            if (firstFileItem) {
                firstFileItem.click();
            } else {
                clearCodeViewer();
            }
        }

        // Clear code content
        function clearCodeViewer() {
            document.getElementById("viewerFilename").innerText = "Select a file...";
            document.getElementById("codeBlock").innerText = "// Select a file to view content";
            Prism.highlightElement(document.getElementById("codeBlock"));
        }

        // Select a file to view code contents
        async function selectFile(filePath, element) {
            // Remove active classes
            document.querySelectorAll(".file-item").forEach(item => item.classList.remove("active"));
            element.classList.add("active");

            document.getElementById("viewerFilename").innerText = filePath;

            try {
                const response = await fetch(`/api/session/\${activeSession.id}/file/\${encodeURIComponent(filePath)}`);
                if (response.ok) {
                    const text = await response.text();
                    const codeBlock = document.getElementById("codeBlock");
                    
                    // Set language class for Prism
                    const ext = filePath.split('.').pop().toLowerCase();
                    codeBlock.className = `language-\${ext}`;
                    codeBlock.textContent = text;
                    Prism.highlightElement(codeBlock);
                } else {
                    document.getElementById("codeBlock").textContent = "// Error loading file content";
                }
            } catch(e) {
                document.getElementById("codeBlock").textContent = "// Network error loading file";
            }
        }

        // Load Git History Commits
        function loadGitCommits(session) {
            const gitTimeline = document.getElementById("gitTimeline");
            gitTimeline.innerHTML = "";

            if (!session.git_commits || session.git_commits.length === 0) {
                gitTimeline.innerHTML = `<div style="color: var(--text-secondary); font-size:13px; padding: 10px 0;">No self-healing history recorded. Ensure Git integration is enabled!</div>`;
                return;
            }

            session.git_commits.forEach(commit => {
                const parts = commit.split(" - ");
                const hash = parts[0];
                const msg = parts.slice(1).join(" - ");
                
                const commitEl = document.createElement("div");
                commitEl.className = "git-commit";
                commitEl.innerHTML = `
                    <span class="git-commit-hash">\${hash}</span>
                    <span class="git-commit-msg">\${msg}</span>
                `;
                gitTimeline.appendChild(commitEl);
            });
        }

        // Switch active tab
        function switchTab(tabName) {
            // Update active button
            document.querySelectorAll(".tab-btn").forEach(btn => btn.classList.remove("active"));
            event.target.classList.add("active");

            // Update active section
            document.querySelectorAll(".tab-content").forEach(sec => sec.classList.remove("active"));
            document.getElementById(`\${tabName}Tab`).classList.add("active");
        }

        let approvalModalOpen = false;

        async function checkPendingApproval() {
            try {
                const response = await fetch('/api/pending_approval');
                const data = await response.json();
                
                if (data.status === "pending") {
                    if (!approvalModalOpen) {
                        showApprovalModal(data.diffs);
                    }
                } else {
                    if (approvalModalOpen) {
                        hideApprovalModal();
                    }
                }
            } catch (err) {
                // Ignore polling errors
            }
        }

        function showApprovalModal(diffs) {
            approvalModalOpen = true;
            const modal = document.getElementById("approvalModal");
            const modalBody = document.getElementById("approvalModalBody");
            
            modalBody.innerHTML = "";
            
            diffs.forEach(d => {
                const card = document.createElement("div");
                card.className = "diff-file-card";
                
                let lineHTML = "";
                const lines = d.diff.split("\n");
                lines.forEach(line => {
                    let cls = "";
                    if (line.startsWith("+") && !line.startsWith("+++")) cls = "diff-add";
                    else if (line.startsWith("-") && !line.startsWith("---")) cls = "diff-del";
                    else if (line.startsWith("@@")) cls = "diff-info";
                    
                    lineHTML += `<div class="diff-line ${cls}">${escapeHtml(line)}</div>`;
                });
                
                card.innerHTML = `
                    <div class="diff-file-header">${d.path} (${d.status})</div>
                    <div class="diff-lines">${lineHTML}</div>
                `;
                modalBody.appendChild(card);
            });
            
            modal.style.display = "flex";
        }

        function hideApprovalModal() {
            approvalModalOpen = false;
            document.getElementById("approvalModal").style.display = "none";
        }

        async function submitApproval(action) {
            try {
                const response = await fetch(`/api/${action}`, { method: 'POST' });
                if (response.ok) {
                    hideApprovalModal();
                    fetchSessions();
                } else {
                    alert("Failed to submit action: " + action);
                }
            } catch (err) {
                alert("Connection error: " + err);
            }
        }

        function escapeHtml(text) {
            return text
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        // Refresh database and check approvals dynamically
        setInterval(() => {
            fetchSessions();
            checkPendingApproval();
        }, 2000);

        // Initial Load
        fetchSessions();
        checkPendingApproval();
    </script>
</body>
</html>
"""

def start_server():
    server_address = ("", PORT)
    httpd = HTTPServer(server_address, DashboardHandler)
    print(f"\n{'-'*60}\n🚀 MAI CLI Agent Dashboard started successfully!\n👉 Open: \033[92mhttp://localhost:{PORT}/\033[0m inside your web browser\n{'-'*60}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Dashboard server. Goodbye!")

if __name__ == "__main__":
    start_server()
