import os
import sys
import re
import json
import datetime
import subprocess
from typing import Annotated, Sequence, List
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from langchain_core.messages import BaseMessage, AIMessage, HumanMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from tenacity import retry, stop_after_attempt, wait_exponential
from html.parser import HTMLParser

# =====================================================================
# 0.5 GLOBALS & ANSI COLORS
# =====================================================================
CYAN = "\033[96m"
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"

# =====================================================================
# 0.7 STDOUT LOG REDIRECTION FOR WEB SSE STREAMING
# =====================================================================
class DualStream:
    def __init__(self, stream, log_file):
        self.stream = stream
        self.log_file = log_file
        
    def write(self, data):
        self.stream.write(data)
        self.stream.flush()
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(data)
        except Exception:
            pass
            
    def flush(self):
        self.stream.flush()

script_dir_redirect = os.path.dirname(os.path.abspath(__file__))
log_file_path = os.path.join(script_dir_redirect, "test", "agent_logs.log")
os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
if os.path.exists(log_file_path):
    try:
        os.remove(log_file_path)
    except Exception:
        pass
sys.stdout = DualStream(sys.stdout, log_file_path)

# =====================================================================
# 1. SETUP ENVIRONMENT
# =====================================================================
# Load .env from test directory to keep the root directory clean
script_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(script_dir, "test", ".env")
load_dotenv(env_path)

if not os.getenv("GEMINI_API_KEY"):
    raise ValueError("Please ensure that GEMINI_API_KEY is configured in your test/.env file.")

client = genai.Client()


# =====================================================================
# 1.5 RETRY HELPER
# =====================================================================
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=4, max=65),
    reraise=True
)
def generate_content_with_retry(client, model, contents, config):
    return client.models.generate_content(
        model=model,
        contents=contents,
        config=config
    )


# =====================================================================
# 1.7 STRUCTURED OUTPUT SCHEMA FOR MULTI-FILE GENERATION
# =====================================================================
class PatchBlock(BaseModel):
    search: str = Field(description="The exact block of code to search for in the file. MUST match existing text exactly. Leave empty if creating a new file.")
    replace: str = Field(description="The replacement block of code.")

class GeneratedFile(BaseModel):
    path: str = Field(description="Name of the file including extension and subfolders (e.g. index.html or src/utils.py)")
    action: str = Field(description="Action to perform: 'create' (for new files) or 'patch' (to modify an existing file)")
    content: str = Field(default="", description="Full content of the file. Required if action is 'create', leave blank if action is 'patch'.")
    patches: List[PatchBlock] = Field(default_factory=list, description="List of search/replace patch blocks. Required if action is 'patch', leave blank if action is 'create'.")

class ProjectOutput(BaseModel):
    files: List[GeneratedFile] = Field(description="List of files to generate or modify in the project")


# =====================================================================
# 2. DEFINE STATE
# =====================================================================
class AgentState(BaseModel):
    messages: Annotated[Sequence[BaseMessage], add_messages] = Field(default_factory=list)
    task_status: str = "pending"
    iteration_count: int = 0
    error_logs: str = ""
    target_path: str = ""
    generated_files: List[dict] = Field(default_factory=list)  # List of successfully created files: [{"path": "...", "content": "..."}]
    session_dir: str = ""  # Output session directory for this session


# =====================================================================
# 2.5 SECURITY GUARDRAILS CHECK & PERSISTENCE
# =====================================================================
def check_security_guardrails(file_path: str, content: str) -> str:
    dangerous_patterns = {
        r"\bos\.remove\b": "os.remove (Delete file)",
        r"\bos\.rmdir\b": "os.rmdir (Delete directory)",
        r"\bshutil\.rmtree\b": "shutil.rmtree (Recursive directory deletion)",
        r"\bos\.system\b": "os.system (Direct shell command execution)",
        r"\bsubprocess\.run\s*\(.*['\"]rm\b": "subprocess.run rm (File deletion via system command)",
        r"\bsubprocess\.Popen\s*\(.*['\"]rm\b": "subprocess.Popen rm (File deletion via system command)",
        # Hardcoded Secrets
        r"(?i)(api[-_]?key|secret|token|password|passwd|private[-_]?key)\s*=\s*['\"][A-Za-z0-9_\-\.\/]{10,}['\"]": "Hardcoded Secret/Credentials (Found passwords or API keys stored directly in code)",
        r"AIzaSy[A-Za-z0-9_-]{33}": "Gemini API Key Hardcoded (Found hardcoded Gemini API key)",
        # Unsafe Shell Execution
        r"subprocess\.(run|Popen|call|check_output)\s*\(.*shell\s*=\s*True": "Unsafe subprocess execution with shell=True (Risk of Command Injection)",
        # SQL Injection
        r"(?i)\.execute\s*\(\s*f['\"].*SELECT.*\{.*\}": "Unsafe SQL dynamic string interpolation (Risk of SQL Injection)"
    }
    for pattern, description in dangerous_patterns.items():
        if re.search(pattern, content):
            return f"❌ [Security Block] Dangerous pattern '{description}' found in file '{file_path}'. Operation blocked!"
    return ""

def save_memory(prompt: str, error_logs: str, files: list, session_dir: str):
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        memory_file = os.path.join(script_dir, "test", "agent_memory.json")
        memories = []
        if os.path.exists(memory_file):
            with open(memory_file, "r", encoding="utf-8") as f:
                try:
                    memories = json.load(f)
                except Exception:
                    pass
        
        memories = memories[-50:] # Keep only the last 50 memories
        
        new_memory = {
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "prompt": prompt,
            "error_logs": error_logs,
            "resolved_files": [{"path": f.get("path"), "content": f.get("content")} for f in files]
        }
        memories.append(new_memory)
        
        with open(memory_file, "w", encoding="utf-8") as f:
            json.dump(memories, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def search_memory(prompt: str, error_logs: str) -> str:
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        memory_file = os.path.join(script_dir, "test", "agent_memory.json")
        if not os.path.exists(memory_file):
            return ""
            
        with open(memory_file, "r", encoding="utf-8") as f:
            memories = json.load(f)
            
        if not memories:
            return ""
            
        # Basic keyword matching retrieval (RAG)
        keywords = set(re.findall(r"\w+", (prompt + " " + error_logs).lower()))
        best_match = None
        max_overlap = 0
        
        for m in memories:
            m_text = (m.get("prompt", "") + " " + m.get("error_logs", "")).lower()
            m_keywords = set(re.findall(r"\w+", m_text))
            overlap = len(keywords.intersection(m_keywords))
            if overlap > max_overlap and overlap >= 3:
                max_overlap = overlap
                best_match = m
                
        if best_match:
            res_str = "\n--- RELATED PAST BUG RESOLUTION ---\n"
            res_str += f"Past Error: {best_match.get('error_logs')[:500]}...\n"
            res_str += "Resolution Code:\n"
            for f in best_match.get("resolved_files", []):
                res_str += f"File: {f.get('path')}\n```\n{f.get('content')}\n```\n"
            res_str += "------------------------------------\n"
            return res_str
    except Exception:
        pass
    return ""

def save_session_history(messages, session_dir):
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        history_file = os.path.join(script_dir, "test", "session_history.json")
        serialized_messages = []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                serialized_messages.append({"type": "human", "content": msg.content})
            elif isinstance(msg, AIMessage):
                serialized_messages.append({"type": "ai", "content": msg.content})
        
        data = {
            "session_dir": session_dir,
            "messages": serialized_messages
        }
        with open(history_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            
        # Write user-readable Markdown chat logs and JSON history into output session dir
        if session_dir and os.path.exists(session_dir):
            session_json_path = os.path.join(session_dir, "session_history.json")
            with open(session_json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            md_file = os.path.join(session_dir, "chat_history.md")
            with open(md_file, "w", encoding="utf-8") as f:
                f.write(f"# 💬 MAI CLI Chat History - Session {session_dir}\n\n")
                for msg in messages:
                    role = "User" if isinstance(msg, HumanMessage) else "MAI AI"
                    f.write(f"### 👤 **{role}**\n{msg.content}\n\n---\n\n")
    except Exception as e:
        print(f"⚠️ Unable to save session history: {e}")

def load_session_history():
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        history_file = os.path.join(script_dir, "test", "session_history.json")
        if os.path.exists(history_file):
            with open(history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data
    except Exception as e:
        print(f"⚠️ Unable to load session history: {e}")
    return None


def resolve_session_path(session_dir: str, file_path: str) -> str:
    if not session_dir:
        return file_path
    cleaned = file_path
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.startswith("test/"):
        cleaned = cleaned[5:]
    elif cleaned.startswith("test\\"):
        cleaned = cleaned[5:]
    return os.path.join(session_dir, cleaned)


def is_docker_available() -> bool:
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=2)
        return res.returncode == 0
    except Exception:
        return False

def run_in_sandbox(cmd: List[str], working_dir: str = None, timeout: int = 10) -> subprocess.CompletedProcess:
    """
    Executes a command inside a Docker sandbox if available, otherwise falls back to local subprocess execution.
    """
    if not is_docker_available():
        return subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout)
    
    # Determine appropriate Docker image
    image = "ubuntu:latest"
    executable = cmd[0].lower()
    
    if "python" in executable or "pytest" in executable:
        image = "python:alpine"
    elif executable in ["node", "npx", "npm"]:
        image = "node:alpine"
    elif executable in ["gcc", "g++"]:
        image = "gcc:latest"
    elif executable in ["javac", "java", "kotlinc"]:
        image = "openjdk:17-slim"
    elif executable in ["mcs", "mono", "csc", "dotnet"]:
        image = "mcr.microsoft.com/dotnet/sdk:latest"
    elif executable == "rscript":
        image = "r-base:latest"
    elif executable == "go":
        image = "golang:alpine"
    elif executable in ["swift", "swiftc"]:
        image = "swift:latest"
    elif executable in ["rustc", "cargo"]:
        image = "rust:alpine"
    elif executable == "php":
        image = "php:alpine"
    elif executable == "ruby":
        image = "ruby:alpine"
    elif executable == "zig":
        image = "ziglang/zig:latest"
    elif executable.endswith(".bin") or "./" in executable:
        image = "gcc:latest"
        
    cwd = os.getcwd()
    
    # Determine container working directory relative to /workspace
    container_workdir = "/workspace"
    if working_dir:
        if os.path.isabs(working_dir):
            try:
                rel_path = os.path.relpath(working_dir, cwd)
                if not rel_path.startswith(".."):
                    container_workdir = os.path.join("/workspace", rel_path)
            except ValueError:
                pass
        else:
            container_workdir = os.path.join("/workspace", working_dir)
            
    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{cwd}:/workspace",
        "-w", container_workdir,
        image
    ]
    
    # Map sys.executable to python inside the container
    container_cmd = []
    for part in cmd:
        if part == sys.executable:
            container_cmd.append("python")
        else:
            container_cmd.append(part)
            
    docker_cmd.extend(container_cmd)
    
    try:
        print(f"  🐳 Running in Docker container '{image}'...")
        res = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout)
        return res
    except subprocess.TimeoutExpired as te:
        raise te
    except Exception as e:
        print(f"  ⚠️ Docker failed ({e}). Falling back to host execution...")
        return subprocess.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout)

def scan_dependencies(session_dir: str, target_files: List[str]) -> List[str]:
    """
    Scans files in session_dir to find dependencies/imports of target_files.
    Returns a list of extra files that import/depend on target_files.
    """
    extra_files = set()
    if not session_dir or not os.path.exists(session_dir):
        return []
        
    target_names = []
    for tf in target_files:
        basename = os.path.basename(tf)
        name_no_ext, ext = os.path.splitext(basename)
        target_names.append((tf, name_no_ext, ext, basename))
        
    for root, _, files in os.walk(session_dir):
        for file in files:
            file_path = os.path.join(root, file)
            if file_path in target_files:
                continue
            ext = os.path.splitext(file)[1].lower()
            valid_exts = [
                ".py", ".js", ".ts", ".c", ".cpp", ".h", ".hpp", 
                ".java", ".cs", ".r", ".go", ".swift", ".rs", ".php", ".kt", ".rb", ".zig"
            ]
            if ext not in valid_exts:
                continue
                
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
                
            for tf, name_no_ext, target_ext, basename in target_names:
                if ext == ".py":
                    python_patterns = [
                        r"\bimport\s+.*\b" + re.escape(name_no_ext) + r"\b",
                        r"\bfrom\s+.*\b" + re.escape(name_no_ext) + r"\b\s+import\b",
                        r"\bfrom\s+" + re.escape(name_no_ext) + r"\b\s+import\b"
                    ]
                    if any(re.search(pat, content) for pat in python_patterns):
                        extra_files.add(file_path)
                        
                elif ext in [".c", ".cpp", ".h", ".hpp"]:
                    cpp_patterns = [
                        r'#include\s+["\']' + re.escape(basename) + r'["\']',
                        r'#include\s+["\']' + re.escape(name_no_ext) + r'\.(h|hpp)["\']'
                    ]
                    if any(re.search(pat, content) for pat in cpp_patterns):
                        extra_files.add(file_path)
                        
                elif ext in [".js", ".ts"]:
                    js_patterns = [
                        r'\bimport\s+.*\bfrom\s+["\'].*' + re.escape(name_no_ext) + r'["\']',
                        r'\brequire\s*\(\s*["\'].*' + re.escape(name_no_ext) + r'["\']'
                    ]
                    if any(re.search(pat, content) for pat in js_patterns):
                        extra_files.add(file_path)

                elif ext == ".java":
                    java_patterns = [
                        r'\bimport\s+[^;]*\b' + re.escape(name_no_ext) + r'\b'
                    ]
                    if any(re.search(pat, content) for pat in java_patterns):
                        extra_files.add(file_path)

                elif ext == ".cs":
                    cs_patterns = [
                        r'\busing\s+[^;]*\b' + re.escape(name_no_ext) + r'\b'
                    ]
                    if any(re.search(pat, content) for pat in cs_patterns):
                        extra_files.add(file_path)

                elif ext == ".go":
                    go_patterns = [
                        r'\bimport\s+(?:[a-zA-Z0-9_]+\s+)?' + r'"(?:[^"]+/)?' + re.escape(name_no_ext) + r'(\.go)?"'
                    ]
                    if any(re.search(pat, content) for pat in go_patterns):
                        extra_files.add(file_path)

                elif ext == ".rs":
                    rust_patterns = [
                        r'\buse\s+[^;]*\b' + re.escape(name_no_ext) + r'\b',
                        r'\bmod\s+' + re.escape(name_no_ext) + r'\b'
                    ]
                    if any(re.search(pat, content) for pat in rust_patterns):
                        extra_files.add(file_path)

                elif ext in [".php", ".rb"]:
                    php_ruby_patterns = [
                        r'\b(require|include|require_relative)\b.*[\'"](?:[^\'"]+/)?' + re.escape(name_no_ext) + r'\b'
                    ]
                    if any(re.search(pat, content) for pat in php_ruby_patterns):
                        extra_files.add(file_path)
                        
    return list(extra_files)


# =====================================================================
# 3. DEFINE NODES
# =====================================================================

def planner_node(state: AgentState) -> dict:
    import time
    start_time = time.time()
    
    current_messages = state.messages
    current_iteration = state.iteration_count
    next_iteration = current_iteration + 1

    user_prompt = ""
    for msg in current_messages:
        if isinstance(msg, HumanMessage):
            user_prompt = msg.content

    # Load agent guidelines from SKILL.md dynamically
    custom_skills = ""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        skill_path = os.path.join(script_dir, "SKILL.md")
        if os.path.exists(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                custom_skills = f.read()
    except Exception as e:
        print(f"⚠️ Unable to load SKILL.md rules: {e}")

    # Build system prompt using constraints defined in SKILL.md
    system_instruction = (
        "You are an intelligent Multi-File Code Generator AI. "
        "You must strictly follow the guidelines, syntax constraints, and safety rules specified in the documentation below:\n\n"
        f"{custom_skills}"
    )
    
    # Search RAG memory for similar past issues and how they were resolved
    memory_context = search_memory(user_prompt, state.error_logs)
    
    if state.error_logs:
        full_prompt = f"Instruction: {user_prompt}\n\nThe previous attempt had the following errors. Please fix them:\n{state.error_logs}"
    else:
        full_prompt = user_prompt
        
    if memory_context:
        full_prompt = f"{memory_context}\n\n{full_prompt}"
    
    response = generate_content_with_retry(
        client=client,
        model='gemini-3.1-flash-lite',
        contents=full_prompt,
        config={
            "system_instruction": system_instruction,
            "response_mime_type": "application/json",
            "response_schema": ProjectOutput
        }
    )
    
    final_text = response.text

    # Simulation: Inject syntax error in the first iteration (python files only) to test self-healing loop
    if next_iteration == 1:
        simulated_error_injected = False
        try:
            data = json.loads(final_text)
            if data.get("files"):
                for f in data["files"]:
                    if f["path"].endswith(".py"):
                        f["content"] = "print('Hello World'  # Missing closing parenthesis\n" + f["content"]
                        simulated_error_injected = True
                        break
            final_text = json.dumps(data)
        except Exception as e:
            pass

    duration = int(time.time() - start_time)
    if duration < 1:
        duration = 1
        
    estimated_tokens = len(full_prompt + final_text) // 4
    estimated_tokens_k = round(estimated_tokens / 1000, 1)
    
    print(f"\nThought for {duration}s, {estimated_tokens_k}k tokens")
    if state.error_logs:
        print(f"  Fixing verification errors in files (Iteration {next_iteration}/3)")
    else:
        print(f"  Analyzing prompt and planning code generation (Iteration {next_iteration}/3)")
        
    if next_iteration == 1 and simulated_error_injected:
        print("  🧪 Injecting simulated syntax error to test self-healing loop...")

    return {
        "messages": [AIMessage(content=final_text)],
        "iteration_count": next_iteration
    }


def executor_node(state: AgentState) -> dict:
    current_messages = state.messages
    
    if not current_messages:
        return {"task_status": "failed"}
        
    last_ai_message = current_messages[-1].content
    status = "executed"
    files_list = []
    
    try:
        project_data = json.loads(last_ai_message)
        files = project_data.get("files", [])
        
        if not files:
            return {"task_status": "success", "generated_files": []}
            
        # 1. Interactive Diff Approval
        diffs_to_show = []
        for f in files:
            path = f.get("path")
            if not path:
                continue
            file_path = resolve_session_path(state.session_dir, path)
            
            action = f.get("action", "create")
            
            # Read old content if exists
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r", encoding="utf-8") as existing_file:
                        old_content = existing_file.read()
                except Exception:
                    old_content = ""
                file_status = "Modified"
            else:
                old_content = ""
                file_status = "Created"
            
            if action == "patch":
                file_status = f"{file_status} (Patch)"
                new_content = old_content
                patches = f.get("patches", [])
                for p in patches:
                    search_block = p.get("search") if isinstance(p, dict) else getattr(p, "search", "")
                    replace_block = p.get("replace") if isinstance(p, dict) else getattr(p, "replace", "")
                    
                    if not search_block and not old_content:
                        new_content = replace_block
                    elif search_block in new_content:
                        new_content = new_content.replace(search_block, replace_block, 1)
                    else:
                        print(f"  ⚠️ Warning: Patch search block not found in {path}. Skipping this block.")
            else:
                new_content = f.get("content", "")
            
            # Store computed content for writing and reading back
            f["computed_new_content"] = new_content
            
            import difflib
            diff = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}"
            ))
            diffs_to_show.append((path, file_status, diff))
            
        if diffs_to_show:
            print(f"\n{BOLD}{YELLOW}🔎 Proposed changes to apply:{RESET}")
            for path, file_status, diff in diffs_to_show:
                print(f"\n{BOLD}{CYAN}📄 File: {path} ({file_status}){RESET}")
                if not diff:
                    print("  (No changes)")
                    continue
                for line in diff:
                    if line.startswith("+") and not line.startswith("+++"):
                        print(f"\033[92m{line.rstrip()}\033[0m")
                    elif line.startswith("-") and not line.startswith("---"):
                        print(f"\033[91m{line.rstrip()}\033[0m")
                    elif line.startswith("@@"):
                        print(f"\033[36m{line.rstrip()}\033[0m")
                    else:
                        print(line.rstrip())
            
            # Write to pending_approval.json for Web Dashboard support
            script_dir = os.path.dirname(os.path.abspath(__file__))
            pending_json = os.path.join(script_dir, "test", "pending_approval.json")
            os.makedirs(os.path.dirname(pending_json), exist_ok=True)
            
            # Format diff lines for JSON serialization
            serialized_diffs = []
            for path, file_status, diff in diffs_to_show:
                serialized_diffs.append({
                    "path": path,
                    "status": file_status,
                    "diff": "".join(diff)
                })
                
            with open(pending_json, "w", encoding="utf-8") as f:
                json.dump({"status": "pending", "diffs": serialized_diffs}, f)
            
            import select
            print(f"\n{BOLD}{YELLOW}Apply these changes? (y/n/abort) [Or approve via Web Dashboard]: {RESET}", end="", flush=True)
            
            approval = None
            try:
                while True:
                    # Non-blocking terminal check (WSL/Linux compatible)
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.2)
                    if rlist:
                        line = sys.stdin.readline().strip().lower()
                        if line in ["y", "yes"]:
                            approval = "approved"
                            break
                        elif line in ["n", "no"]:
                            approval = "rejected"
                            break
                        elif line in ["abort", "q", "quit"]:
                            approval = "aborted"
                            break
                        else:
                            print(f"{YELLOW}Invalid input. Enter 'y', 'n', 'abort' (or use Dashboard): {RESET}", end="", flush=True)
                            
                    # Check if status has been updated in pending_approval.json
                    if os.path.exists(pending_json):
                        with open(pending_json, "r", encoding="utf-8") as f:
                            try:
                                data = json.load(f)
                                status = data.get("status")
                                if status in ["approved", "rejected", "aborted"]:
                                    approval = status
                                    break
                            except Exception:
                                pass
            except (KeyboardInterrupt, EOFError):
                approval = "aborted"
                
            # Clean up pending approval file
            if os.path.exists(pending_json):
                try:
                    os.remove(pending_json)
                except Exception:
                    pass
                    
            if approval == "approved":
                print(f"\n{GREEN}✅ Changes approved. Writing files...{RESET}")
            elif approval == "rejected":
                print(f"\n{YELLOW}❌ Changes rejected by user.{RESET}")
                return {"task_status": "failed", "error_logs": "Changes rejected by user", "generated_files": []}
            else:
                print(f"\n{RED}🚨 Aborting flow...{RESET}")
                raise KeyboardInterrupt()

        # 2. Write proposed files
        for f in files:
            file_path = f.get("path")
            action = f.get("action", "create")
            file_content = f.get("computed_new_content", f.get("content", ""))
            
            if file_path:
                file_path = resolve_session_path(state.session_dir, file_path)
                
                is_edit = os.path.exists(file_path)
                tool_name = "Patch" if action == "patch" else ("Edit" if is_edit else "Write")
                
                print(f"● {tool_name}({file_path})")
                print(f"  Saving {'patched' if action == 'patch' else 'generated'} code block to local filesystem")
                
                dir_name = os.path.dirname(file_path)
                if dir_name:
                    os.makedirs(dir_name, exist_ok=True)
                    
                with open(file_path, "w", encoding="utf-8") as file_obj:
                    file_obj.write(file_content)
                    
        # 3. Auto-formatting
        for f in files:
            file_path = f.get("path")
            if not file_path:
                continue
            file_path = resolve_session_path(state.session_dir, file_path)
                
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".py":
                try:
                    black_path = os.path.join(os.path.dirname(sys.executable), "black")
                    if not os.path.exists(black_path):
                        black_path = "black"
                    subprocess.run([black_path, file_path], capture_output=True, text=True)
                except Exception:
                    pass
            elif ext in [".js", ".ts", ".css", ".html", ".json"]:
                try:
                    subprocess.run(["npx", "prettier", "--write", file_path], capture_output=True, text=True)
                except Exception:
                    pass
                    
        # 4. Read back formatted files into files_list
        for f in files:
            file_path = f.get("path")
            if not file_path:
                continue
            orig_path = file_path
            file_path = resolve_session_path(state.session_dir, file_path)
            try:
                with open(file_path, "r", encoding="utf-8") as file_obj:
                    formatted_content = file_obj.read()
                files_list.append({"path": file_path, "content": formatted_content})
            except Exception:
                files_list.append({"path": file_path, "content": f.get("computed_new_content", f.get("content", ""))})
                
    except Exception as e:
        print(f"  ⚠️ Error: {e}")
        status = "failed"
        
    return {"task_status": status, "generated_files": files_list}


def verifier_node(state: AgentState) -> dict:
    files_to_verify = list(state.generated_files)
    errors = []
    status = "success"
    
    if not files_to_verify:
        return {"task_status": "success", "error_logs": ""}
        
    # 0. AST Dependency Scanning (Regression Testing Candidate Selection)
    if state.session_dir:
        print(f"\n● {BOLD}{CYAN}AST Dependency Scan{RESET}")
        print("  Analyzing project imports/includes to identify dependent files for regression testing")
        target_paths = [f.get("path") for f in files_to_verify]
        dependent_files = scan_dependencies(state.session_dir, target_paths)
        if dependent_files:
            print(f"  Found {len(dependent_files)} dependent file(s) for regression testing:")
            for df in dependent_files:
                print(f"   - {df}")
                if not any(f.get("path") == df for f in files_to_verify):
                    try:
                        with open(df, "r", encoding="utf-8") as df_file:
                            df_content = df_file.read()
                        files_to_verify.append({"path": df, "content": df_content})
                    except Exception:
                        pass
        else:
            print("  No dependent files found for regression testing.")
        
    for f in files_to_verify:
        path_to_verify = f.get("path")
        content = f.get("content", "")
        
        # 1. Security Check
        print(f"● Scan({path_to_verify})")
        print("  Running security checks for SQL injection and dangerous shell commands")
        security_error = check_security_guardrails(path_to_verify, content)
        if security_error:
            errors.append(security_error)
            status = "failed"
            print(f"  ❌ Security Block: {security_error}")
            continue
            
        # 2. Syntax & Execution Verification based on file extension
        ext = os.path.splitext(path_to_verify)[1].lower()
        
        if ext == ".py":
            # 2.1 Flake8 Linting check
            try:
                flake8_path = os.path.join(os.path.dirname(sys.executable), "flake8")
                if os.path.exists(flake8_path):
                    print(f"● Lint({path_to_verify})")
                    print("  Running Flake8 checking syntax errors and logical bugs")
                    lint_result = subprocess.run(
                        [flake8_path, "--select=E9,F", path_to_verify],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if lint_result.returncode != 0:
                        err_msg = f"Flake8 Linting Errors in {path_to_verify}:\n{lint_result.stdout}"
                        errors.append(err_msg)
                        status = "failed"
                        print(f"  ❌ Linting Failed: PEP8 / Pyflakes errors detected")
            except Exception as e:
                pass

            # 2.2 Syntax & Runtime execution
            try:
                print(f"● Bash(python {path_to_verify})")
                print("  Executing Python interpreter verification check")
                compile(content, filename=path_to_verify, mode="exec")
                
                # Execute Python file using system python interpreter
                result = run_in_sandbox(
                    [sys.executable, path_to_verify],
                    timeout=5
                )
                
                if result.returncode != 0:
                    # Auto-Installer for missing external libraries
                    stderr_str = result.stderr or ""
                    if "ModuleNotFoundError: No module named" in stderr_str or "ImportError: No module named" in stderr_str:
                        match = re.search(r"No module named '([^']+)'", stderr_str)
                        if match:
                            missing_module = match.group(1)
                            print(f"  📦 Missing module detected. Auto-installing '{missing_module}'...")
                            install_result = subprocess.run(
                                [sys.executable, "-m", "pip", "install", missing_module],
                                capture_output=True,
                                text=True
                            )
                            if install_result.returncode == 0:
                                result = run_in_sandbox(
                                    [sys.executable, path_to_verify],
                                    timeout=5
                                )
                                if result.returncode == 0:
                                    continue
                    
                    raise RuntimeError(f"Runtime Exit Code {result.returncode}. Error Details:\n{result.stderr}")
                
            except SyntaxError as se:
                err_msg = f"SyntaxError in {path_to_verify} at line {se.lineno}: {se.msg}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Execution Failed: Syntax Error")
            except Exception as e:
                err_msg = f"Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Execution Failed: Runtime error occurred")
                
        elif ext == ".js":
            # 2.1 ESLint Linting check
            try:
                print(f"● Lint({path_to_verify})")
                print("  Running ESLint syntax and code quality checks")
                script_dir = os.path.dirname(os.path.abspath(__file__))
                eslint_config_path = os.path.join(script_dir, "test", "eslint.config.js")
                eslint_result = subprocess.run(
                    ["npx", "eslint", "--config", eslint_config_path, "--no-color", path_to_verify],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if eslint_result.returncode != 0:
                    err_msg = f"ESLint Linting Errors in {path_to_verify}:\n{eslint_result.stdout}"
                    errors.append(err_msg)
                    status = "failed"
                    print(f"  ❌ Linting Failed: ESLint violations found")
            except Exception as e:
                pass

            # 2.2 Runtime execution via Node.js
            try:
                print(f"● Bash(node {path_to_verify})")
                print("  Executing Node.js verification check")
                result = run_in_sandbox(
                    ["node", path_to_verify],
                    timeout=5
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Runtime Exit Code {result.returncode}. Error Details:\n{result.stderr}")
            except Exception as e:
                err_msg = f"JavaScript Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Execution Failed: Runtime error occurred")
                
        elif ext == ".ts":
            try:
                print(f"● Bash(npx ts-node {path_to_verify})")
                print("  Executing TypeScript verification check")
                result = run_in_sandbox(
                    ["npx", "ts-node", "--compiler-options", '{"module": "commonjs"}', path_to_verify],
                    timeout=10
                )
                if result.returncode != 0:
                    raise RuntimeError(f"TypeScript Execution Failed. Error Details:\n{result.stderr}\n{result.stdout}")
            except Exception as e:
                err_msg = f"TypeScript Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Execution Failed: TypeScript error occurred")

        elif ext == ".c":
            try:
                # Run cppcheck if available
                try:
                    print(f"● Lint({path_to_verify})")
                    print("  Running Cppcheck static analysis")
                    cppcheck_res = subprocess.run(
                        ["cppcheck", "--enable=warning,style", "--error-exitcode=1", path_to_verify],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if cppcheck_res.returncode != 0:
                        err_msg = f"Cppcheck Warning/Error in {path_to_verify}:\n{cppcheck_res.stderr or cppcheck_res.stdout}"
                        errors.append(err_msg)
                        status = "failed"
                        print(f"  ❌ Linting Failed: Cppcheck bugs detected")
                except FileNotFoundError:
                    pass

                print(f"● Compile({path_to_verify})")
                print("  Checking syntax and compiling with gcc")
                out_bin = os.path.abspath(path_to_verify + ".bin")
                compile_res = run_in_sandbox(
                    ["gcc", "-Wall", "-Wextra", "-o", out_bin, path_to_verify],
                    timeout=10
                )
                if compile_res.returncode != 0:
                    raise RuntimeError(f"C Compilation Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                
                print(f"● Bash({out_bin})")
                print("  Executing C binary verification check")
                run_res = run_in_sandbox(
                    [out_bin],
                    timeout=5
                )
                if os.path.exists(out_bin):
                    os.remove(out_bin)
                if run_res.returncode != 0:
                    raise RuntimeError(f"C Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"C Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: C build/execution error")

        elif ext in [".cpp", ".cc", ".cxx", ".h", ".hpp"]:
            # Check if this is a PlatformIO project
            is_pio = False
            if state.session_dir:
                if os.path.exists(os.path.join(state.session_dir, "platformio.ini")):
                    is_pio = True
            
            if is_pio:
                try:
                    print(f"● PlatformIO Run({state.session_dir})")
                    print("  Verifying build status via platformio.ini")
                    pio_bin = os.path.expanduser("~/.platformio/penv/bin/pio")
                    if not os.path.exists(pio_bin):
                        pio_bin = "pio"
                    pio_res = subprocess.run(
                        [pio_bin, "run", "-d", state.session_dir],
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
                    if pio_res.returncode != 0:
                        raise RuntimeError(f"PlatformIO Build Failed. Error Details:\n{pio_res.stdout}\n{pio_res.stderr}")
                except Exception as e:
                    err_msg = f"PlatformIO Build Error: {str(e)}"
                    errors.append(err_msg)
                    status = "failed"
                    print(f"  ❌ Verification Failed: PlatformIO build error")
            else:
                # Run cppcheck if available
                try:
                    print(f"● Lint({path_to_verify})")
                    print("  Running Cppcheck static analysis")
                    cppcheck_res = subprocess.run(
                        ["cppcheck", "--enable=warning,style", "--error-exitcode=1", path_to_verify],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if cppcheck_res.returncode != 0:
                        err_msg = f"Cppcheck Warning/Error in {path_to_verify}:\n{cppcheck_res.stderr or cppcheck_res.stdout}"
                        errors.append(err_msg)
                        status = "failed"
                        print(f"  ❌ Linting Failed: Cppcheck bugs detected")
                except FileNotFoundError:
                    pass

                is_header = ext in [".h", ".hpp"]
                try:
                    if is_header:
                        print(f"● Compile-Syntax-Only({path_to_verify})")
                        print("  Checking syntax for C++ header using g++")
                        compile_res = run_in_sandbox(
                            ["g++", "-Wall", "-Wextra", "-std=c++17", "-fsyntax-only", "-x", "c++-header", path_to_verify],
                            timeout=10
                        )
                        if compile_res.returncode != 0:
                            raise RuntimeError(f"C++ Header Check Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                    else:
                        print(f"● Compile({path_to_verify})")
                        print("  Checking syntax and compiling with g++")
                        out_bin = os.path.abspath(path_to_verify + ".bin")
                        compile_res = run_in_sandbox(
                            ["g++", "-Wall", "-Wextra", "-std=c++17", "-o", out_bin, path_to_verify],
                            timeout=10
                        )
                        if compile_res.returncode != 0:
                            raise RuntimeError(f"C++ Compilation Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                        
                        print(f"● Bash({out_bin})")
                        print("  Executing C++ binary verification check")
                        run_res = run_in_sandbox(
                            [out_bin],
                            timeout=5
                        )
                        if os.path.exists(out_bin):
                            os.remove(out_bin)
                        if run_res.returncode != 0:
                            raise RuntimeError(f"C++ Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
                except Exception as e:
                    err_msg = f"C++ Error in {path_to_verify}: {str(e)}"
                    errors.append(err_msg)
                    status = "failed"
                    print(f"  ❌ Verification Failed: C++ build/execution error")

        elif ext == ".sql":
            # 1. SQLFluff (if installed in venv or system)
            sqlfluff_run = False
            try:
                sqlfluff_path = os.path.join(os.path.dirname(sys.executable), "sqlfluff")
                if not os.path.exists(sqlfluff_path):
                    sqlfluff_path = "sqlfluff"
                print(f"● Lint({path_to_verify})")
                print("  Running SQLFluff dialect analysis")
                sqlfluff_res = subprocess.run(
                    [sqlfluff_path, "lint", path_to_verify, "--dialect", "sqlite"],
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                sqlfluff_run = True
                if sqlfluff_res.returncode != 0:
                    err_msg = f"SQLFluff Linting Errors in {path_to_verify}:\n{sqlfluff_res.stdout}"
                    errors.append(err_msg)
                    status = "failed"
                    print(f"  ❌ Linting Failed: SQLFluff style/syntax errors")
            except FileNotFoundError:
                pass
                
            # 2. Fallback to sqlite3 in-memory engine if SQLFluff was not run
            if not sqlfluff_run or status != "failed":
                try:
                    print(f"● Lint({path_to_verify})")
                    print("  Parsing SQL syntax using sqlite3 in-memory engine")
                    import sqlite3 as sqlite_mod
                    conn = sqlite_mod.connect(":memory:")
                    conn.executescript(content)
                    conn.close()
                except Exception as e:
                    if status != "failed":
                        err_msg = f"SQL Error in {path_to_verify}: {str(e)}"
                        errors.append(err_msg)
                        status = "failed"
                        print(f"  ❌ Linting Failed: SQL parsing error")
                
        elif ext in [".html", ".xml"]:
            try:
                print(f"● Lint({path_to_verify})")
                print("  Parsing HTML structure tags")
                parser = HTMLParser()
                parser.feed(content)
            except Exception as e:
                err_msg = f"Malformed HTML/XML in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Linting Failed: Malformed tag structure")
                
        elif ext == ".css":
            try:
                print(f"● Lint({path_to_verify})")
                print("  Verifying CSS brace balance and formatting")
                open_braces = content.count("{")
                close_braces = content.count("}")
                if open_braces != close_braces:
                    raise SyntaxError(f"Mismatched braces. Open '{{': {open_braces}, Close '}}': {close_braces}")
            except Exception as e:
                err_msg = f"CSS Syntax Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Linting Failed: Syntax error detected")
                
        elif ext == ".json":
            try:
                print(f"● Lint({path_to_verify})")
                print("  Validating JSON dictionary formatting")
                json.loads(content)
            except Exception as e:
                err_msg = f"Invalid JSON structure in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Linting Failed: Invalid JSON format")
                
        elif ext == ".sh":
            # 1. ShellCheck (if installed)
            try:
                print(f"● Lint({path_to_verify})")
                print("  Running ShellCheck static analysis")
                shellcheck_res = subprocess.run(
                    ["shellcheck", path_to_verify],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                if shellcheck_res.returncode != 0:
                    err_msg = f"ShellCheck Errors in {path_to_verify}:\n{shellcheck_res.stdout}"
                    errors.append(err_msg)
                    status = "failed"
                    print(f"  ❌ Linting Failed: ShellCheck violations detected")
            except FileNotFoundError:
                pass

            # 2. Syntax check via bash -n
            try:
                print(f"● Bash-Syntax-Check({path_to_verify})")
                print("  Checking syntax for bash script")
                bash_res = run_in_sandbox(
                    ["bash", "-n", path_to_verify],
                    timeout=5
                )
                if bash_res.returncode != 0:
                    raise RuntimeError(f"Bash Syntax Errors:\n{bash_res.stderr}")
            except Exception as e:
                err_msg = f"Bash Syntax Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Bash syntax error")
                
        elif ext == ".java":
            try:
                print(f"● Compile({path_to_verify})")
                print("  Compiling Java source file using javac")
                compile_res = run_in_sandbox(
                    ["javac", path_to_verify],
                    timeout=15
                )
                if compile_res.returncode != 0:
                    raise RuntimeError(f"Java Compilation Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                
                basename = os.path.basename(path_to_verify)
                classname, _ = os.path.splitext(basename)
                class_dir = os.path.dirname(path_to_verify) or "."
                
                print(f"● Bash(java -cp {class_dir} {classname})")
                print("  Executing Java class verification check")
                run_res = run_in_sandbox(
                    ["java", "-cp", class_dir, classname],
                    timeout=10
                )
                
                class_file = os.path.join(class_dir, f"{classname}.class")
                if os.path.exists(class_file):
                    try:
                        os.remove(class_file)
                    except Exception:
                        pass
                    
                if run_res.returncode != 0:
                    raise RuntimeError(f"Java Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"Java Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Java compilation/execution error")

        elif ext == ".cs":
            try:
                print(f"● Compile({path_to_verify})")
                print("  Compiling C# file using csc/mcs")
                out_exe = path_to_verify.replace(".cs", ".exe")
                compile_cmd = ["csc", f"-out:{out_exe}", path_to_verify]
                try:
                    import shutil
                    if not shutil.which("csc") and shutil.which("mcs"):
                        compile_cmd = ["mcs", f"-out:{out_exe}", path_to_verify]
                except Exception:
                    pass
                
                compile_res = run_in_sandbox(
                    compile_cmd,
                    timeout=15
                )
                if compile_res.returncode != 0:
                    raise RuntimeError(f"C# Compilation Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                
                print(f"● Bash(mono {out_exe})")
                print("  Executing C# assembly verification check")
                run_res = run_in_sandbox(
                    ["mono", out_exe],
                    timeout=10
                )
                
                if os.path.exists(out_exe):
                    try:
                        os.remove(out_exe)
                    except Exception:
                        pass
                    
                if run_res.returncode != 0:
                    raise RuntimeError(f"C# Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"C# Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: C# compilation/execution error")

        elif ext == ".r":
            try:
                print(f"● Bash(Rscript {path_to_verify})")
                print("  Executing R script verification check")
                result = run_in_sandbox(
                    ["Rscript", path_to_verify],
                    timeout=10
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Rscript Exit Code {result.returncode}. Error Details:\n{result.stderr}\n{result.stdout}")
            except Exception as e:
                err_msg = f"R Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: R runtime error")

        elif ext == ".go":
            try:
                print(f"● Bash(go run {path_to_verify})")
                print("  Compiling and running Go file")
                result = run_in_sandbox(
                    ["go", "run", path_to_verify],
                    timeout=15
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Go Exit Code {result.returncode}. Error Details:\n{result.stderr}\n{result.stdout}")
            except Exception as e:
                err_msg = f"Go Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Go execution error")

        elif ext == ".swift":
            try:
                print(f"● Bash(swift {path_to_verify})")
                print("  Executing Swift script check")
                result = run_in_sandbox(
                    ["swift", path_to_verify],
                    timeout=15
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Swift Exit Code {result.returncode}. Error Details:\n{result.stderr}\n{result.stdout}")
            except Exception as e:
                err_msg = f"Swift Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Swift execution error")

        elif ext == ".rs":
            try:
                print(f"● Compile({path_to_verify})")
                print("  Compiling Rust file using rustc")
                out_bin = path_to_verify + ".bin"
                compile_res = run_in_sandbox(
                    ["rustc", path_to_verify, "-o", out_bin],
                    timeout=20
                )
                if compile_res.returncode != 0:
                    raise RuntimeError(f"Rust Compilation Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                
                print(f"● Bash({out_bin})")
                print("  Executing Rust binary verification check")
                run_res = run_in_sandbox(
                    [out_bin],
                    timeout=10
                )
                if os.path.exists(out_bin):
                    try:
                        os.remove(out_bin)
                    except Exception:
                        pass
                if run_res.returncode != 0:
                    raise RuntimeError(f"Rust Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"Rust Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Rust build/execution error")

        elif ext == ".php":
            try:
                print(f"● Lint({path_to_verify})")
                print("  Linting PHP syntax check")
                lint_res = run_in_sandbox(
                    ["php", "-l", path_to_verify],
                    timeout=5
                )
                if lint_res.returncode != 0:
                    raise RuntimeError(f"PHP Linting Failed:\n{lint_res.stderr or lint_res.stdout}")
                
                print(f"● Bash(php {path_to_verify})")
                print("  Executing PHP runtime verification check")
                run_res = run_in_sandbox(
                    ["php", path_to_verify],
                    timeout=10
                )
                if run_res.returncode != 0:
                    raise RuntimeError(f"PHP Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"PHP Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: PHP execution error")

        elif ext == ".kt":
            try:
                print(f"● Compile({path_to_verify})")
                print("  Compiling Kotlin source using kotlinc")
                out_jar = path_to_verify + ".jar"
                compile_res = run_in_sandbox(
                    ["kotlinc", path_to_verify, "-include-runtime", "-d", out_jar],
                    timeout=25
                )
                if compile_res.returncode != 0:
                    raise RuntimeError(f"Kotlin Compilation Failed. Error Details:\n{compile_res.stderr}\n{compile_res.stdout}")
                
                print(f"● Bash(java -jar {out_jar})")
                print("  Executing Kotlin runtime verification check")
                run_res = run_in_sandbox(
                    ["java", "-jar", out_jar],
                    timeout=10
                )
                if os.path.exists(out_jar):
                    try:
                        os.remove(out_jar)
                    except Exception:
                        pass
                if run_res.returncode != 0:
                    raise RuntimeError(f"Kotlin Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"Kotlin Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Kotlin compilation/execution error")

        elif ext == ".rb":
            try:
                print(f"● Lint({path_to_verify})")
                print("  Checking Ruby syntax")
                lint_res = run_in_sandbox(
                    ["ruby", "-c", path_to_verify],
                    timeout=5
                )
                if lint_res.returncode != 0:
                    raise RuntimeError(f"Ruby Syntax Check Failed:\n{lint_res.stderr or lint_res.stdout}")
                
                print(f"● Bash(ruby {path_to_verify})")
                print("  Executing Ruby script verification check")
                run_res = run_in_sandbox(
                    ["ruby", path_to_verify],
                    timeout=10
                )
                if run_res.returncode != 0:
                    raise RuntimeError(f"Ruby Runtime Exit Code {run_res.returncode}. Error Details:\n{run_res.stderr}\n{run_res.stdout}")
            except Exception as e:
                err_msg = f"Ruby Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Ruby execution error")

        elif ext == ".zig":
            try:
                print(f"● Bash(zig run {path_to_verify})")
                print("  Executing Zig program verification check")
                result = run_in_sandbox(
                    ["zig", "run", path_to_verify],
                    timeout=15
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Zig Exit Code {result.returncode}. Error Details:\n{result.stderr}\n{result.stdout}")
            except Exception as e:
                err_msg = f"Zig Error in {path_to_verify}: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Zig runtime error")

        else:
            if len(content.strip()) == 0:
                err_msg = f"File {path_to_verify} is empty."
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Verification Failed: Empty file")

    # 3. Dynamic Test Suite Runner
    if status != "failed":
        test_dirs_to_check = []
        if state.session_dir:
            test_dirs_to_check.append(state.session_dir)
        test_dirs_to_check.append(os.getcwd())
        
        test_command = None
        test_cwd = None
        
        for d in test_dirs_to_check:
            if not d or not os.path.exists(d):
                continue
            # 3.1 Node/JS Project
            if os.path.exists(os.path.join(d, "package.json")):
                try:
                    with open(os.path.join(d, "package.json"), "r", encoding="utf-8") as pkg_file:
                        pkg_data = json.load(pkg_file)
                        if "scripts" in pkg_data and "test" in pkg_data["scripts"]:
                            test_command = ["npm", "test"]
                            test_cwd = d
                            break
                except Exception:
                    pass
                    
            # 3.2 Python Project
            if os.path.exists(os.path.join(d, "pytest.ini")) or os.path.exists(os.path.join(d, "conftest.py")):
                pytest_path = os.path.join(os.path.dirname(sys.executable), "pytest")
                if not os.path.exists(pytest_path):
                    pytest_path = "pytest"
                test_command = [pytest_path]
                test_cwd = d
                break
                
            # Check for test files
            has_test_files = False
            for root, _, fs in os.walk(d):
                if any(f.startswith("test_") and f.endswith(".py") for f in fs):
                    has_test_files = True
                    break
            if has_test_files:
                pytest_path = os.path.join(os.path.dirname(sys.executable), "pytest")
                if not os.path.exists(pytest_path):
                    pytest_path = "pytest"
                test_command = [pytest_path]
                test_cwd = d
                break
                
        if test_command:
            print(f"\n● {BOLD}{CYAN}Test Suite Run{RESET} ({' '.join(test_command)})")
            print(f"  Running project test suite dynamically in {test_cwd}")
            try:
                # Run the test suite within sandbox if docker available, else locally
                test_res = run_in_sandbox(test_command, working_dir=test_cwd, timeout=30)
                
                if test_res.returncode != 0:
                    # If it's pytest and exit code is 5 (no tests collected), treat as success
                    if "pytest" in test_command[0] and test_res.returncode == 5:
                        print("  ℹ️ No tests collected by pytest (Exit Code 5). Ignoring.")
                        print("  ✅ Test Suite Passed successfully!")
                    else:
                        err_msg = f"Test Suite Failed (Exit Code {test_res.returncode}):\n{test_res.stdout}\n{test_res.stderr}"
                        errors.append(err_msg)
                        status = "failed"
                        print(f"  ❌ Test Suite Failed: test assertions failed")
                else:
                    print(f"  ✅ Test Suite Passed successfully!")
            except Exception as e:
                err_msg = f"Test Suite execution error: {str(e)}"
                errors.append(err_msg)
                status = "failed"
                print(f"  ❌ Test Suite Execution Failed")
                
    # Git integration
    if state.session_dir and os.path.exists(state.session_dir):
        git_dir = os.path.join(state.session_dir, ".git")
        if not os.path.exists(git_dir):
            subprocess.run(["git", "init"], cwd=state.session_dir, capture_output=True)
            subprocess.run(["git", "config", "user.name", "mai-agent"], cwd=state.session_dir, capture_output=True)
            subprocess.run(["git", "config", "user.email", "agent@mai.local"], cwd=state.session_dir, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=state.session_dir, capture_output=True)
        commit_msg = f"Iteration {state.iteration_count}: {status}"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=state.session_dir, capture_output=True)
                 
    errors_combined = "\n".join(errors)
    return {
        "task_status": status,
        "error_logs": errors_combined
    }


# =====================================================================
# 4. DEFINE ROUTERS & EDGES
# =====================================================================

def router_after_planner(state: AgentState):
    if state.messages:
        return "go_to_executor"
    return "general_end"


def router_after_verifier(state: AgentState):
    if state.task_status == "success":
        return "exit_loop"
    elif state.iteration_count >= 3:
        return "exit_loop"
    else:
        return "loop_back"


# Setup StateGraph
workflow = StateGraph(AgentState)

workflow.add_node("planner_node", planner_node)
workflow.add_node("executor_node", executor_node)
workflow.add_node("verifier_node", verifier_node)

workflow.set_entry_point("planner_node")

workflow.add_conditional_edges(
    "planner_node",
    router_after_planner,
    {
        "go_to_executor": "executor_node",
        "general_end": END
    }
)

workflow.add_edge("executor_node", "verifier_node")

workflow.add_conditional_edges(
    "verifier_node",
    router_after_verifier,
    {
        "exit_loop": END,
        "loop_back": "planner_node"
    }
)

memory = MemorySaver()
app = workflow.compile(checkpointer=memory)

# Render Graph image into test folder
try:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    graph_path = os.path.join(script_dir, "test", "agent_graph.png")
    png_data = app.get_graph().draw_mermaid_png()
    with open(graph_path, "wb") as f:
        f.write(png_data)
except Exception as e:
    pass


# =====================================================================
# 5. TEST EXECUTION (CLI main loop)
# =====================================================================
if __name__ == "__main__":
    # ANSI Colors for beautiful UI formatting
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    # Set up session directory name
    session_dir = os.path.join("test", datetime.datetime.now().strftime("mai_output_%Y%m%d_%H%M%S"))

    # Config thread memory for multi-turn persistence across loops
    import time
    thread_id = f"mai_chat_session_{int(time.time())}"
    config = {"configurable": {"thread_id": thread_id}}

    # Recover previous chat history if available (Session Persistence)
    history_data = load_session_history()
    if history_data and history_data.get("messages"):
        prev_session = history_data.get("session_dir")
        print(f"\n{YELLOW}📂 Found previous chat session (Session: {prev_session}){RESET}")
        resume = input(f"Do you want to resume the previous session? (y/n) [Default: n]: ").strip().lower()
        if resume in ["y", "yes"]:
            session_dir = prev_session
            if session_dir:
                os.makedirs(session_dir, exist_ok=True)
            
            loaded_messages = []
            for msg_dict in history_data["messages"]:
                m_type = msg_dict.get("type")
                content = msg_dict.get("content")
                if m_type == "human":
                    loaded_messages.append(HumanMessage(content=content))
                elif m_type == "ai":
                    loaded_messages.append(AIMessage(content=content))
            
            app.update_state(config, {"messages": loaded_messages})
            print(f"{GREEN}✅ Successfully restored {len(loaded_messages)} messages from history! You can continue now.{RESET}")

    # Print a beautiful startup screen for MAI CLI!
    print(f"""
{CYAN}{BOLD}  ███▄ ▄███▀  ████████  █████████
  ██████████  ███    ███    ███
  ███ ▀█▀ ███  ██████████    ███
  ███     ███  ███    ███    ███
  ███     ███  ███    ███  █████████{RESET}

  {BOLD}MAI CLI v1.1.0 (Advanced Features Installed){RESET}
  Model: {GREEN}Gemini 3.1 Flash (Lite){RESET}
  Output Dir: {BLUE}{session_dir}/{RESET}
  Directory: {BLUE}{os.getcwd()}{RESET}
  
  {YELLOW}Type /help or .help to see available commands, or /exit to quit.{RESET}
────────────────────────────────────────────────""")

    while True:
        try:
            user_prompt = input(f"\n{BOLD}{CYAN}mai>{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Goodbye! See you next time!")
            break

        if not user_prompt:
            continue

        # -----------------------------------------------------------------
        # SLASH & DOT COMMANDS SYSTEM
        # -----------------------------------------------------------------
        
        # 1. Exit CLI
        if user_prompt.lower() in ["/exit", "/quit", ".exit", ".quit", "exit", "quit", "exit()", "quit()"]:
            print("👋 Closing work session. Goodbye!")
            break

        # 2. Help Menu
        if user_prompt in ["/help", ".help"]:
            print(f"""
{BOLD}Available Slash & Dot Commands for MAI CLI:{RESET}
  {CYAN}/help{RESET} or {CYAN}.help{RESET}          - Show this help menu
  {CYAN}/init{RESET} or {CYAN}.init{RESET}          - Clear chat history and start a new session (resets Output Dir)
  (Also {CYAN}/reset{RESET} or {CYAN}.reset{RESET})
  {CYAN}/scrutinize{RESET} or {CYAN}.scrutinize <filename>{RESET} - Audit a file for security vulnerabilities and runtime errors
  {CYAN}/explain{RESET} or {CYAN}.explain <filename>{RESET}       - Explain the logic and implementation of the file in detail
  {CYAN}/refactor{RESET} or {CYAN}.refactor <filename>{RESET}     - Optimize performance, readability, and security of the code (with self-healing)
  {CYAN}/add-test{RESET} or {CYAN}.add-test <filename>{RESET}     - Add Assert Test Cases block to a Python file (with self-healing)
  {CYAN}/exit{RESET} or {CYAN}.exit{RESET}          - Exit the CLI
""")
            continue

        # 3. Reset Session History
        if user_prompt in ["/init", "/reset", ".init", ".reset"]:
            session_dir = os.path.join("test", datetime.datetime.now().strftime("mai_output_%Y%m%d_%H%M%S"))
            config = {"configurable": {"thread_id": f"mai_chat_session_{int(time.time())}"}}
            try:
                script_dir = os.path.dirname(os.path.abspath(__file__))
                history_file = os.path.join(script_dir, "test", "session_history.json")
                if os.path.exists(history_file):
                    os.remove(history_file)
            except Exception:
                pass
            print(f"🔄 Session reset complete! Starting a fresh chat history.")
            print(f"New Output Dir: {BLUE}{session_dir}/{RESET}")
            continue

        # 4. Scrutinize Audit File
        if user_prompt.startswith(("/scrutinize", ".scrutinize")):
            parts = user_prompt.split(maxsplit=1)
            if len(parts) < 2:
                cmd_prefix = "/scrutinize" if user_prompt.startswith("/") else ".scrutinize"
                print(f"⚠️  Please specify the file name to scrutinize, e.g. `{cmd_prefix} app.py`")
                continue
            filename = parts[1].strip()
            
            target_file_path = filename
            if not os.path.exists(target_file_path) and session_dir:
                target_file_path = resolve_session_path(session_dir, filename)
                
            if not os.path.exists(target_file_path):
                print(f"❌ File '{filename}' not found in this directory")
                continue
                
            try:
                with open(target_file_path, "r", encoding="utf-8") as f:
                    file_content = f.read()
                
                print(f"\n{YELLOW}🔍 Scrutinizing '{target_file_path}' using Security Guardrails & Verification Nodes...{RESET}")
                
                test_state = AgentState(
                    messages=[],
                    task_status="pending",
                    iteration_count=0,
                    error_logs="",
                    generated_files=[{"path": target_file_path, "content": file_content}],
                    session_dir=""
                )
                
                result = verifier_node(test_state)
                
                if result.get("task_status") == "success":
                    print(f"\n{GREEN}✅ Audit passed! File '{target_file_path}' has valid syntax and no security vulnerabilities.{RESET}")
                else:
                    print(f"\n{YELLOW}❌ Audit failed! Detected bugs or security vulnerabilities in '{target_file_path}':{RESET}")
                    print(f"{YELLOW}{result.get('error_logs')}{RESET}")
            except Exception as e:
                print(f"❌ Technical error reading the file: {e}")
            continue

        # 5. Code Explanation
        if user_prompt.startswith(("/explain", ".explain")):
            parts = user_prompt.split(maxsplit=1)
            if len(parts) < 2:
                cmd_prefix = "/explain" if user_prompt.startswith("/") else ".explain"
                print(f"⚠️  Please specify the file name to explain, e.g. `{cmd_prefix} app.py`")
                continue
            filename = parts[1].strip()
            
            target_file_path = filename
            if not os.path.exists(target_file_path) and session_dir:
                target_file_path = resolve_session_path(session_dir, filename)
                
            if not os.path.exists(target_file_path):
                print(f"❌ File '{filename}' not found in this directory")
                continue
                
            try:
                with open(target_file_path, "r", encoding="utf-8") as f:
                    file_content = f.read()
                
                print(f"\n{YELLOW}🤖 Analyzing and generating explanation for '{target_file_path}'...{RESET}")
                
                response = generate_content_with_retry(
                    client=client,
                    model='gemini-3.1-flash-lite',
                    contents=f"Please explain the logic and implementation of the code in this file in detail and systematically, in English:\n\nFile Name: {filename}\n\nCode:\n```\n{file_content}\n```",
                    config={
                        "system_instruction": "You are an expert AI code analyst and software architect. Explain the code clearly, systematically, and concisely."
                    }
                )
                print(f"\n{GREEN}📘 Code explanation for '{target_file_path}':{RESET}")
                print(response.text)
            except Exception as e:
                print(f"❌ Error generating code explanation: {e}")
            continue

        # 6. Refactor or Add Test Cases (routes through LangGraph check)
        is_refactor = user_prompt.startswith(("/refactor", ".refactor"))
        is_add_test = user_prompt.startswith(("/add-test", ".add-test", "/add_test", ".add_test"))
        
        graph_prompt = ""
        
        if is_refactor or is_add_test:
            parts = user_prompt.split(maxsplit=1)
            if len(parts) < 2:
                cmd_name = parts[0]
                print(f"⚠️  Please specify a file name, e.g. `{cmd_name} app.py`")
                continue
            filename = parts[1].strip()
            
            target_file_path = filename
            if not os.path.exists(target_file_path) and session_dir:
                target_file_path = resolve_session_path(session_dir, filename)
                
            if not os.path.exists(target_file_path):
                print(f"❌ File '{filename}' not found in this directory")
                continue
                
            try:
                with open(target_file_path, "r", encoding="utf-8") as f:
                    file_content = f.read()
                
                rel_path = os.path.relpath(target_file_path, session_dir) if session_dir in target_file_path else target_file_path
                
                if is_refactor:
                    print(f"\n{YELLOW}⚙️  Submitting refactor request for '{target_file_path}' to the agent graph...{RESET}")
                    graph_prompt = (
                        f"Refactor the code in this file to improve performance, security, "
                        f"and cleanliness, while preserving its original functionality and structure:\n\n"
                        f"Target File Path: {rel_path}\n\n"
                        f"Original Code:\n```\n{file_content}\n```"
                    )
                else:
                    print(f"\n{YELLOW}⚙️  Submitting test generation request for '{target_file_path}' to the agent graph...{RESET}")
                    graph_prompt = (
                        f"For the following file, write comprehensive Assert Test Cases inside the if __name__ == '__main__': "
                        f"block at the end of the file, or improve existing ones to verify its logical correctness:\n\n"
                        f"Target File Path: {rel_path}\n\n"
                        f"Original Code:\n```\n{file_content}\n```"
                    )
            except Exception as e:
                print(f"❌ Error preparing file target: {e}")
                continue

        # -----------------------------------------------------------------
        # NORMAL AGENTIC FLOW (GRAPH STREAM)
        # -----------------------------------------------------------------
        
        prompt_to_send = graph_prompt if graph_prompt else user_prompt
        initial_input = {
            "messages": [HumanMessage(content=prompt_to_send)],
            "task_status": "pending",
            "iteration_count": 0,
            "error_logs": "",
            "session_dir": session_dir
        }

        print(f"\n⚙️  Executing agent flow...")
        
        try:
            for event in app.stream(initial_input, config=config):
                pass
                            
            state = app.get_state(config)
            task_status = state.values.get("task_status", "pending")
            
            if task_status == "success":
                print(f"\n{GREEN}✅ Execution Successful! Files created and verified.{RESET}")
                all_values = state.values
                iter_cnt = all_values.get("iteration_count", 0)
                if iter_cnt > 1:
                    last_errs = all_values.get("error_logs", "")
                    files_generated = all_values.get("generated_files", [])
                    if files_generated:
                        save_memory(prompt_to_send, last_errs, files_generated, session_dir)
            else:
                print(f"\n{YELLOW}⚠️ Verification failed or maximum self-healing iterations reached.{RESET}")
                
            all_messages = state.values.get("messages", [])
            save_session_history(all_messages, session_dir)
                
        except Exception as e:
            print(f"\n❌ Technical error executing the graph: {e}")
