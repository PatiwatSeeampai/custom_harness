# 📂 MAI CLI Workspace Dashboard

Welcome to the **MAI CLI** project workspace! This workspace is optimized for **Obsidian**. By opening this folder as an Obsidian vault, you can easily manage the AI agent's rules, review execution logs, and navigate the project.

---

## 🧭 Quick Navigation
*   **Core Logic**: [[agent.py]] — LangGraph State Machine & CLI Loop
*   **Agent Guidelines**: [[SKILL.md]] — Dynamic System Instructions & Security Rules
*   **System Design**: [[docs/architecture.md]] — State Nodes, Edges, & Flow Details
*   **Interactive Prompts**: [[docs/custom_prompting.md]] — Test Cases & Validation Prompts
*   **Configuration**: [[test/.env]] — Gemini API Key setup
*   **System Diagram**: [[test/agent_graph.png]] — Visualized Agent Workflow

---

## 🏗️ Architecture Design
The agent utilizes a StateGraph to generate, execute, and verify source code files, loop-correcting errors automatically.

```mermaid
graph TD
    Start([Start Session]) --> PlannerNode[planner_node<br>LLM Generator]
    
    PlannerNode --> Router1{Router 1}
    Router1 -- Go to Executor --> ExecutorNode[executor_node<br>File Writer]
    Router1 -- End Session --> EndSession([End Session])
    
    ExecutorNode --> VerifierNode[verifier_node<br>Linter & Compiler]
    
    VerifierNode --> Router2{Router 2}
    Router2 -- Verification Success --> EndSession
    Router2 -- Iteration Count >= 3 --> EndSession
    Router2 -- Errors Found --> PlannerNode
    
    style Start fill:#4CAF50,stroke:#388E3C,stroke-width:2px,color:#fff
    style EndSession fill:#F44336,stroke:#D32F2F,stroke-width:2px,color:#fff
    style PlannerNode fill:#2196F3,stroke:#1976D2,stroke-width:2px,color:#fff
    style ExecutorNode fill:#FF9800,stroke:#F57C00,stroke-width:2px,color:#fff
    style VerifierNode fill:#9C27B0,stroke:#7B1FA2,stroke-width:2px,color:#fff
    style Router1 fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
    style Router2 fill:#E91E63,stroke:#C2185B,stroke-width:2px,color:#fff
```

---

## 📋 Workspace Tasks & Milestones
- [x] Rename project to **MAI CLI**
- [x] Implement **JS Node.js Verification** in Verifier Node
- [x] Implement **Output Isolation** (`mai_output_YYYYMMDD_HHMMSS/`)
- [x] Implement **Dot Commands** alongside Slash Commands
- [x] Set up **Session Persistence** and history log output
- [x] Set up **CSS Validation** (braces checking)
- [x] Implement **Advanced Commands** (`.explain`, `.refactor`, `.add-test`)
- [x] Add **Advanced Security Checks** (Secrets detection, command injection, SQL injection)
- [x] Translate all prompts, CLI headers, and rules to English
- [x] Add more Linter integrations (e.g. ESLint, Flake8)

---

## ⚡ How to Run
Ensure your terminal session has loaded the latest alias:
```bash
source ~/.bashrc
mai
```
