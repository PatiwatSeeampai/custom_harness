---
name: mai-cli-skills
description: Capabilities and control commands of MAI CLI (Self-Healing & Security Guardrails)
---

# 🤖 MAI CLI — Agent Capabilities & Skills Documentation

This document defines the **Agent Roles & Working Skills** of MAI CLI to train the agent on command shortcuts, code generation standards, and safety requirements.

---

## 📖 1. Slash & Dot Commands (Control Commands)

The following commands can be typed in the `mai>` console window using either a slash (`/`) or dot (`.`) prefix:

*   `/init` or `.init` (including `/reset` / `.reset`): 
    *   **Purpose**: Reset session memory, clear conversation history, and start a new output session directory (`mai_output_YYYYMMDD_HHMMSS/`).
*   `/scrutinize <filename>` or `.scrutinize <filename>`:
    *   **Purpose**: Run an on-demand audit and verification for syntax and security issues on any file.
*   `/explain <filename>` or `.explain <filename>`:
    *   **Purpose**: Analyze the structure and logic of a code file, generating a detailed code explanation in English.
*   `/refactor <filename>` or `.refactor <filename>`:
    *   **Purpose**: Refactor a code file to improve performance, readability, and security using the agent's self-healing compiler loop.
*   `/add-test <filename>` or `.add-test <filename>`:
    *   **Purpose**: Automatically write or improve unit tests under the `if __name__ == '__main__':` block of a Python file.
*   `/help` or `.help`: Show this help screen.
*   `/exit` / `.exit` or `/quit` / `.quit`: Exit the session and save the session logs safely.

---

## 💾 2. Session Persistence (History & Recovery)

*   **Session Save**: Conversation history is saved in a raw JSON state inside `test/session_history.json` and a user-friendly `chat_history.md` in the current session folder.
*   **Session Resume**: When launching `mai`, the system checks for existing session files and prompts to resume where you left off.

---

## 🛡️ 3. Code Generation & Verification Guidelines (Standard Rules)

The agent must strictly follow these rules during all code generation and editing tasks:

1.  **Multi-File JSON Schema**: All code modifications must return a JSON block conforming to the `ProjectOutput` schema, containing filepaths and raw file contents.
2.  **Auto-Formatting**:
    *   Python (`.py`) files must be formatted with `black`.
    *   JS, TS, HTML, CSS, and JSON files must be formatted with `npx prettier --write`.
3.  **Python Logic Verification**:
    *   All Python (`.py`) files must contain assert test cases inside the `if __name__ == '__main__':` block to run logic verification.
    *   If a `ModuleNotFoundError` is raised during execution, the system runs `pip install` in the virtual environment.
4.  **C & C++ Code Verification**:
    *   Compiled files are audited using `cppcheck` (static analysis) if available.
    *   C (`.c`) and C++ (`.cpp`, `.cc`) are compiled using `gcc` and `g++ -std=c++17` respectively and executed to check runtime.
    *   C++ Header files (`.h`, `.hpp`) are verified using `g++ -fsyntax-only` to bypass `main` entry requirements.
    *   PlatformIO projects (marked by `platformio.ini`) are verified using `pio run -d <session_dir>`.
5.  **TypeScript Verification**:
    *   TypeScript (`.ts`) files are executed and type-checked using `npx ts-node --compiler-options '{"module": "commonjs"}'`.
6.  **SQL Database Check**:
    *   SQL (`.sql`) queries are linted via `sqlfluff` if available, and parsed using an isolated in-memory SQLite database (`sqlite3 :memory:`).
7.  **Shell Scripting (.sh)**:
    *   Shell scripts are linted via `shellcheck` if available, and syntax-checked using `bash -n` to prevent accidental execution.
8.  **Frontend & Web Validation**:
    *   HTML/XML files must be validated using `HTMLParser` to check tag syntax.
    *   JavaScript (`.js`) files must be executed via `node` to ensure no runtime errors occur.
    *   CSS (`.css`) files must have matching open `{` and close `}` braces to prevent broken layouts.
9.  **Malicious Pattern & Security Block**:
    *   **File Deletion Prevention**: Block dangerous file deletion commands like `os.remove`, `os.rmdir`, `shutil.rmtree`, and command executions with `rm`.
    *   **Secrets Safeguard**: Do not write hardcoded credentials, API keys, or tokens (e.g. Gemini key `AIzaSy...`) in any file.
    *   **Command Injection Block**: Do not call `subprocess` with `shell=True` coupled with dynamic string formatting.
    *   **SQL Injection Block**: Prevent string interpolation in SQL command execution. Enforce parameterized query standards.
10. **Extended Multi-Language Compilation & Execution**:
    *   **Java (`.java`)**: Compiled with `javac` and run via `java`.
    *   **C# (`.cs`)**: Compiled using `csc` or `mcs` and run via `mono`.
    *   **R (`.r`)**: Executed using `Rscript`.
    *   **Go (`.go`)**: Run using `go run`.
    *   **Swift (`.swift`)**: Executed using `swift`.
    *   **Rust (`.rs`)**: Compiled using `rustc` and executed.
    *   **PHP (`.php`)**: Syntax checked with `php -l` and executed with `php`.
    *   **Kotlin (`.kt`)**: Compiled via `kotlinc` with runtime inclusion and executed using `java -jar`.
    *   **Ruby (`.rb`)**: Syntax checked with `ruby -c` and run using `ruby`.
    *   **Zig (`.zig`)**: Executed using `zig run`.



