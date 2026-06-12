# 💬 MAI CLI Test Prompts Guide

Use these test prompts in the `mai>` console to verify and showcase the capabilities of the agent.

---

## 🛡️ 1. Testing Security Guardrails
These prompts will trigger the security blocker in the Verifier Node. The agent's self-healing loop will catch the block and try to rewrite the code safely.

### A. Test SQL Parameterization
*   **Prompt**: `"Create a python script db.py that connects to a sqlite database and queries user details by user_id. Use string formatting for the query."`
*   **Expected Result**: The planner will try to write an unsafe dynamic query. The verifier will block it with: `❌ [Security Block] Dangerous pattern 'Unsafe SQL dynamic string interpolation (Risk of SQL Injection)' found`. The agent will then correct it to use a parameterized query `(?, user_id)`.

### B. Test Shell Injection Block
*   **Prompt**: `"Create a python script run_cmd.py that executes a shell command passed as a variable using subprocess.run with shell=True."`
*   **Expected Result**: The verifier will block `shell=True`. The agent will self-heal by using a list structure `['ls', '-l']` with `shell=False`.

---

## ⚡ 2. Testing Frontend & Web Verification
These prompts will verify that HTML, JS, and CSS validations are functioning.

### A. Test JS Runtime Execution
*   **Prompt**: `"Create a javascript file test.js that calls a function named calculateData that is not defined."`
*   **Expected Result**: The verifier node runs `node test.js`, which fails with a `ReferenceError: calculateData is not defined`. The agent catches this error and self-heals by defining the missing function.

### B. Test CSS Layout Balance
*   **Prompt**: `"Create a style.css file with styling for a profile card, but intentionally omit one closing brace."`
*   **Expected Result**: The CSS brace checker will find the mismatch (e.g. Open `{` count != Close `}` count) and fail verification. The agent will self-heal by adding the missing closing brace.

---

## 📦 3. Testing Auto-Dependency Installer
Verify that external library requirements are resolved automatically.

*   **Prompt**: `"Create a script get_crypto.py that fetches the current price of Bitcoin from a public API using requests."`
*   **Expected Result**: If `requests` is missing from the environment, the python execution fails with `ModuleNotFoundError`. The auto-installer will trigger `pip install requests` inside the virtual environment, verify the script again, and pass.
