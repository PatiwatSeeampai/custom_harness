# 🤖 MAI CLI — Multi-Language Self-Healing AI Agent Workspace

Welcome to the **MAI CLI** project workspace. This repository contains the source code, guidelines, and performance metrics for the MAI CLI AI coding agent, implemented in both **Python** and **Rust** to compare execution speeds, memory usage, and structural design.

---

## 📂 Project Structure

This workspace is organized as a multi-language comparison environment:

*   **[agentic_ai_rs/](file:///home/admin/project/agentic_ai/agentic_ai_rs)**: The primary, high-performance **Rust** edition. Statically compiled and highly concurrent.
*   **[agentic_ai_py/](file:///home/admin/project/agentic_ai/agentic_ai_py)**: The legacy **Python** edition. Built using LangGraph workflows.
*   **[performance_comparison.md](file:///home/admin/project/agentic_ai/performance_comparison.md)**: A detailed, 10-parameter benchmark report comparing the startup speeds, memory usage, and concurrency models of both versions against the host Antigravity CLI.

---

## ⚡ Quick Performance Highlights
We measured startup execution time and memory footprint (RSS) directly on our local sandbox:

*   **Startup Time**: Rust (**10 ms**) vs Go-based Antigravity (**143 ms**) vs Python (**1,435 ms**). **Rust is 143x faster than Python!**
*   **Memory Footprint**: Rust (**5.4 MB**) vs Python (**107.6 MB**) vs Go-based Antigravity (**134.5 MB**). **Rust uses 20x less memory than Python!**
*   **Distribution**: Rust compiles to a standalone **9.0 MB** binary, requiring zero runtime interpreters or virtual environments.

For more details, see the complete [Performance Comparison Report](file:///home/admin/project/agentic_ai/performance_comparison.md).

---

## ⚙️ Features of MAI CLI

Both implementations support the following core features:

1.  **Self-Healing Compiler Loop**: Runs compiler and static linter checks (like `gcc`, `node`, `pytest`, `eslint`, `cppcheck`) on generated files, feeding execution errors back to the LLM to automatically patch and fix bugs up to 3 times.
2.  **Multi-Language Verification**: Supports code validation in Python, Rust, JavaScript, HTML, CSS, C/C++, TypeScript, SQL, Java, C#, Go, Zig, and many more.
3.  **Built-in Security Guardrails**: Proactively scans and blocks dangerous code patterns including:
    *   File deletion commands (`os.remove`, `rm -rf`).
    *   Hardcoded secrets and API keys (regex-based blocks).
    *   Shell injection (`subprocess` with `shell=True` and string interpolation).
    *   SQL injection (enforcing parameterized query standards).
4.  **Glassmorphic Web Dashboard**: An asynchronous dashboard web server that shows session histories, file content outputs, self-healing timelines, and handles click approvals for file patches.

---

## 🚀 How to Run

### 1. Configure Shell Aliases
Add these aliases to your `~/.bashrc` to quickly execute both versions:
```bash
# Rust Version (Recommended)
alias mai="/home/admin/project/agentic_ai/agentic_ai_rs/target/release/agent"
alias mai-rs="/home/admin/project/agentic_ai/agentic_ai_rs/target/release/agent"
alias mai-dashboard="/home/admin/project/agentic_ai/agentic_ai_rs/target/release/dashboard"

# Python Version (Legacy)
alias mai-py="/home/admin/project/agentic_ai/agentic_ai_py/venv/bin/python /home/admin/project/agentic_ai/agentic_ai_py/agent.py"
alias mai-py-dashboard="/home/admin/project/agentic_ai/agentic_ai_py/venv/bin/python /home/admin/project/agentic_ai/agentic_ai_py/dashboard.py"
```

Reload your shell:
```bash
source ~/.bashrc
```

### 2. Execute CLI REPL
*   To start the Rust agent:
    ```bash
    mai-rs
    ```
*   To start the Python agent:
    ```bash
    mai-py
    ```

### 3. Open Web Dashboard
*   Launch the Rust dashboard server:
    ```bash
    mai-dashboard
    ```
    Open `http://127.0.0.1:8585` in your web browser.
