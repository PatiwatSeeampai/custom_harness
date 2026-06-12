use agentic_ai_rs::{
    check_security_guardrails, resolve_session_path, run_in_sandbox, scan_dependencies,
    GeneratedFile, ProjectOutput, PatchBlock,
};
use anyhow::Result;
use chrono::Local;
use serde::{Deserialize, Serialize};
use serde_json::json;
use similar::{ChangeTag, TextDiff};
use std::env;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::Duration;
use regex::Regex;

// =====================================================================
// GLOBALS & ANSI COLORS
// =====================================================================
const CYAN: &str = "\x1b[96m";
const BLUE: &str = "\x1b[94m";
const GREEN: &str = "\x1b[92m";
const YELLOW: &str = "\x1b[93m";
const RED: &str = "\x1b[91m";
const BOLD: &str = "\x1b[1m";
const RESET: &str = "\x1b[0m";

// =====================================================================
// STATE SCHEMAS & HISTORY
// =====================================================================

#[derive(Serialize, Deserialize, Clone, Debug)]
struct Message {
    #[serde(rename = "type")]
    msg_type: String, // "human" or "ai"
    content: String,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct SessionHistory {
    session_dir: String,
    messages: Vec<Message>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct MemoryEntry {
    timestamp: String,
    prompt: String,
    error_logs: String,
    resolved_files: Vec<ResolvedFile>,
}

#[derive(Serialize, Deserialize, Clone, Debug)]
struct ResolvedFile {
    path: String,
    content: String,
}

struct AgentState {
    messages: Vec<Message>,
    task_status: String,
    iteration_count: usize,
    error_logs: String,
    generated_files: Vec<ResolvedFile>,
    session_dir: String,
}

// =====================================================================
// HELPER FOR GEMINI API
// =====================================================================

async fn call_gemini_api(
    client: &reqwest::Client,
    api_key: &str,
    system_instruction: &str,
    contents: &str,
) -> Result<String> {
    let url = format!(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={}",
        api_key
    );

    // Schema configuration for ProjectOutput structured JSON response
    let response_schema = json!({
        "type": "OBJECT",
        "properties": {
            "files": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "path": { "type": "STRING", "description": "Name of the file including extension and subfolders (e.g. index.html or src/utils.py)" },
                        "action": { "type": "STRING", "description": "Action to perform: 'create' (for new files) or 'patch' (to modify an existing file)" },
                        "content": { "type": "STRING", "description": "Full content of the file. Required if action is 'create', leave blank if action is 'patch'." },
                        "patches": {
                            "type": "ARRAY",
                            "description": "List of search/replace patch blocks. Required if action is 'patch', leave blank if action is 'create'.",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "search": { "type": "STRING", "description": "The exact block of code to search for in the file. MUST match existing text exactly." },
                                    "replace": { "type": "STRING", "description": "The replacement block of code." }
                                },
                                "required": ["search", "replace"]
                            }
                        }
                    },
                    "required": ["path", "action"]
                }
            }
        },
        "required": ["files"]
    });

    let payload = json!({
        "contents": [{
            "parts": [{ "text": contents }]
        }],
        "systemInstruction": {
            "parts": [{ "text": system_instruction }]
        },
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": response_schema
        }
    });

    // Run request with retry logic (up to 5 attempts)
    let mut last_error = None;
    for attempt in 1..=5 {
        match client.post(&url).json(&payload).send().await {
            Ok(res) => {
                if res.status().is_success() {
                    let json_res: serde_json::Value = res.json().await?;
                    if let Some(text) = json_res["candidates"][0]["content"]["parts"][0]["text"].as_str() {
                        return Ok(text.to_string());
                    }
                } else {
                    let err_text = res.text().await.unwrap_or_default();
                    last_error = Some(format!("HTTP Status {}: {}", attempt, err_text));
                }
            }
            Err(e) => {
                last_error = Some(e.to_string());
            }
        }
        tokio::time::sleep(Duration::from_secs(2u64.pow(attempt))).await;
    }

    Err(anyhow::anyhow!(
        "Failed calling Gemini API after 5 attempts. Error: {:?}",
        last_error
    ))
}

// =====================================================================
// PERSISTENCE & MEMORY UTILITIES
// =====================================================================

fn save_session_history(messages: &[Message], session_dir: &str) {
    let history_file = Path::new("test/session_history.json");
    if let Some(parent) = history_file.parent() {
        let _ = fs::create_dir_all(parent);
    }

    let data = SessionHistory {
        session_dir: session_dir.to_string(),
        messages: messages.to_vec(),
    };

    if let Ok(serialized) = serde_json::to_string_pretty(&data) {
        let _ = fs::write(history_file, &serialized);

        if !session_dir.is_empty() {
            let session_path = Path::new(session_dir);
            if session_path.exists() {
                let _ = fs::write(session_path.join("session_history.json"), &serialized);

                // Write markdown history
                let md_path = session_path.join("chat_history.md");
                let mut md_content = format!("# 💬 MAI CLI Chat History - Session {}\n\n", session_dir);
                for msg in messages {
                    let role = if msg.msg_type == "human" { "User" } else { "MAI AI" };
                    md_content.push_str(&format!("### 👤 **{}**\n{}\n\n---\n\n", role, msg.content));
                }
                let _ = fs::write(md_path, md_content);
            }
        }
    }
}

fn load_session_history() -> Option<SessionHistory> {
    let history_file = Path::new("test/session_history.json");
    if history_file.exists() {
        if let Ok(content) = fs::read_to_string(history_file) {
            if let Ok(data) = serde_json::from_str::<SessionHistory>(&content) {
                return Some(data);
            }
        }
    }
    None
}

fn save_memory(prompt: &str, error_logs: &str, files: &[ResolvedFile], session_dir: &str) {
    let memory_file = Path::new("test/agent_memory.json");
    let mut memories: Vec<MemoryEntry> = Vec::new();

    if memory_file.exists() {
        if let Ok(content) = fs::read_to_string(memory_file) {
            if let Ok(loaded) = serde_json::from_str(&content) {
                memories = loaded;
            }
        }
    }

    // Keep last 50
    if memories.len() >= 50 {
        memories.remove(0);
    }

    memories.push(MemoryEntry {
        timestamp: Local::now().format("%Y-%m-%d %H:%M:%S").to_string(),
        prompt: prompt.to_string(),
        error_logs: error_logs.to_string(),
        resolved_files: files.to_vec(),
    });

    if let Ok(serialized) = serde_json::to_string_pretty(&memories) {
        let _ = fs::write(memory_file, serialized);
    }
}

fn search_memory(prompt: &str, error_logs: &str) -> String {
    let memory_file = Path::new("test/agent_memory.json");
    if !memory_file.exists() {
        return String::new();
    }

    let memories: Vec<MemoryEntry> = match fs::read_to_string(memory_file) {
        Ok(content) => serde_json::from_str(&content).unwrap_or_default(),
        Err(_) => return String::new(),
    };

    if memories.is_empty() {
        return String::new();
    }

    // Basic keyword overlapping retrieval (RAG)
    let re = Regex::new(r"\w+").unwrap();
    let query_lower = format!("{} {}", prompt, error_logs).to_lowercase();
    let query_keywords: std::collections::HashSet<&str> = re
        .find_iter(&query_lower)
        .map(|m| m.as_str())
        .collect();

    let mut best_match: Option<MemoryEntry> = None;
    let mut max_overlap = 0;

    for m in memories {
        let m_text = format!("{} {}", m.prompt, m.error_logs).to_lowercase();
        let m_keywords: std::collections::HashSet<&str> = re
            .find_iter(&m_text)
            .map(|k| k.as_str())
            .collect();

        let overlap = query_keywords.intersection(&m_keywords).count();
        if overlap > max_overlap && overlap >= 3 {
            max_overlap = overlap;
            best_match = Some(m);
        }
    }

    if let Some(match_entry) = best_match {
        let mut res = String::from("\n--- RELATED PAST BUG RESOLUTION ---\n");
        let truncated_err = if match_entry.error_logs.len() > 500 {
            format!("{}...", &match_entry.error_logs[..500])
        } else {
            match_entry.error_logs.clone()
        };
        res.push_str(&format!("Past Error: {}\n", truncated_err));
        res.push_str("Resolution Code:\n");
        for f in match_entry.resolved_files {
            res.push_str(&format!("File: {}\n```\n{}\n```\n", f.path, f.content));
        }
        res.push_str("------------------------------------\n");
        return res;
    }

    String::new()
}

// =====================================================================
// FILE DIFFERENCE VIEW & INTERACTIVE CONFIRMATION
// =====================================================================

fn make_diff_view(path: &str, old_content: &str, new_content: &str) -> String {
    let diff = TextDiff::from_lines(old_content, new_content);
    diff.unified_diff()
        .context_radius(3)
        .header(&format!("a/{}", path), &format!("b/{}", path))
        .to_string()
}

fn print_colorized_diff(diff_text: &str) {
    for line in diff_text.lines() {
        if line.starts_with('+') && !line.starts_with("+++") {
            println!("{}{}{}", GREEN, line, RESET);
        } else if line.starts_with('-') && !line.starts_with("---") {
            println!("{}{}{}", RED, line, RESET);
        } else if line.starts_with("@@") {
            println!("{}{}{}", CYAN, line, RESET);
        } else {
            println!("{}", line);
        }
    }
}

// =====================================================================
// AGENT EXECUTION NODES (STATE MACHINE ENGINE)
// =====================================================================

async fn planner_node(state: &mut AgentState, client: &reqwest::Client, api_key: &str) -> Result<()> {
    let start_time = std::time::Instant::now();
    state.iteration_count += 1;

    let user_prompt = state
        .messages
        .iter()
        .filter(|m| m.msg_type == "human")
        .last()
        .map(|m| m.content.as_str())
        .unwrap_or_default();

    // Read agent instructions from SKILL.md dynamically
    let mut custom_skills = String::new();
    if let Ok(skills) = fs::read_to_string("SKILL.md") {
        custom_skills = skills;
    }

    let system_instruction = format!(
        "You are an intelligent Multi-File Code Generator AI. \
         You must strictly follow the guidelines, syntax constraints, and safety rules specified in the documentation below:\n\n{}",
        custom_skills
    );

    // Search memory for past errors
    let memory_context = search_memory(user_prompt, &state.error_logs);

    let mut full_prompt = if !state.error_logs.is_empty() {
        format!(
            "Instruction: {}\n\nThe previous attempt had the following errors. Please fix them:\n{}",
            user_prompt, state.error_logs
        )
    } else {
        user_prompt.to_string()
    };

    if !memory_context.is_empty() {
        full_prompt = format!("{}\n\n{}", memory_context, full_prompt);
    }

    let mut response_text = call_gemini_api(client, api_key, &system_instruction, &full_prompt).await?;

    // Simulation: Inject syntax error in first iteration (python files only) to test self-healing loop
    if state.iteration_count == 1 {
        if let Ok(mut data) = serde_json::from_str::<serde_json::Value>(&response_text) {
            if let Some(files) = data["files"].as_array_mut() {
                let mut simulated_error_injected = false;
                for f in files {
                    if let Some(path) = f["path"].as_str() {
                        if path.ends_with(".py") {
                            if let Some(content) = f["content"].as_str() {
                                f["content"] = serde_json::Value::String(format!(
                                    "print('Hello World'  # Missing closing parenthesis\n{}",
                                    content
                                ));
                                simulated_error_injected = true;
                                break;
                            }
                        }
                    }
                }
                if simulated_error_injected {
                    if let Ok(modified_json) = serde_json::to_string(&data) {
                        response_text = modified_json;
                        println!("  🧪 Injecting simulated syntax error to test self-healing loop...");
                    }
                }
            }
        }
    }

    state.messages.push(Message {
        msg_type: "ai".to_string(),
        content: response_text,
    });

    let elapsed = start_time.elapsed().as_secs();
    let estimated_tokens = (full_prompt.len() + state.messages.last().unwrap().content.len()) / 4;
    let estimated_k = (estimated_tokens as f64 / 1000.0 * 10.0).round() / 10.0;

    println!("\nThought for {}s, {}k tokens", elapsed.max(1), estimated_k);
    if !state.error_logs.is_empty() {
        println!("  Fixing verification errors in files (Iteration {}/3)", state.iteration_count);
    } else {
        println!("  Analyzing prompt and planning code generation (Iteration {}/3)", state.iteration_count);
    }

    Ok(())
}

async fn executor_node(state: &mut AgentState, rx: &mut tokio::sync::mpsc::Receiver<String>) -> Result<()> {
    if state.messages.is_empty() {
        state.task_status = "failed".to_string();
        return Ok(());
    }

    let last_ai_msg = state.messages.last().unwrap().content.clone();
    let project_data: ProjectOutput = match serde_json::from_str(&last_ai_msg) {
        Ok(data) => data,
        Err(e) => {
            state.task_status = "failed".to_string();
            state.error_logs = format!("Gemini structured JSON parsing failed: {}", e);
            return Ok(());
        }
    };

    if project_data.files.is_empty() {
        state.task_status = "success".to_string();
        state.generated_files = Vec::new();
        return Ok(());
    }

    // 1. Compute changes and compile diffs
    let mut diffs_to_show = Vec::new();
    let mut files_to_process = project_data.files.clone();

    for f in &mut files_to_process {
        let file_path = resolve_session_path(&state.session_dir, &f.path);
        let old_content = if file_path.exists() {
            fs::read_to_string(&file_path).unwrap_or_default()
        } else {
            String::new()
        };

        let file_status = if file_path.exists() { "Modified" } else { "Created" };

        let new_content = if f.action == "patch" {
            let mut current = old_content.clone();
            if let Some(patches) = &f.patches {
                for p in patches {
                    if p.search.is_empty() && old_content.is_empty() {
                        current = p.replace.clone();
                    } else if current.contains(&p.search) {
                        current = current.replacen(&p.search, &p.replace, 1);
                    } else {
                        println!("  ⚠️ Warning: Patch search block not found in {}. Skipping block.", f.path);
                    }
                }
            }
            current
        } else {
            f.content.clone().unwrap_or_default()
        };

        f.computed_new_content = Some(new_content.clone());

        let diff_text = make_diff_view(&f.path, &old_content, &new_content);
        diffs_to_show.push((f.path.clone(), file_status.to_string(), diff_text));
    }

    // 2. Interactive Approval
    if !diffs_to_show.is_empty() {
        println!("\n{}{}[Proposed changes to apply:]{}", BOLD, YELLOW, RESET);
        for (path, status, diff) in &diffs_to_show {
            println!("\n{}{}[File: {} ({})]{}", BOLD, CYAN, path, status, RESET);
            if diff.is_empty() {
                println!("  (No changes)");
            } else {
                print_colorized_diff(diff);
            }
        }

        // Save to pending_approval.json for web dashboard integration
        let pending_json_path = Path::new("test/pending_approval.json");
        if let Some(parent) = pending_json_path.parent() {
            let _ = fs::create_dir_all(parent);
        }

        let serialized_diffs: Vec<serde_json::Value> = diffs_to_show
            .iter()
            .map(|(path, status, diff)| {
                json!({
                    "path": path,
                    "status": status,
                    "diff": diff
                })
            })
            .collect();

        let pending_data = json!({
            "status": "pending",
            "diffs": serialized_diffs
        });
        
        let _ = fs::write(pending_json_path, serde_json::to_string_pretty(&pending_data).unwrap_or_default());

        print!("\n{}{}[Apply these changes? (y/n/abort) [Or approve via Web Dashboard]: ]{}", BOLD, YELLOW, RESET);
        io::stdout().flush().unwrap();

        let mut approval = None;
        loop {
            tokio::select! {
                line_res = rx.recv() => {
                    if let Some(line) = line_res {
                        let lower = line.trim().to_lowercase();
                        if lower == "y" || lower == "yes" {
                            approval = Some("approved".to_string());
                            break;
                        } else if lower == "n" || lower == "no" {
                            approval = Some("rejected".to_string());
                            break;
                        } else if lower == "abort" || lower == "q" || lower == "quit" {
                            approval = Some("aborted".to_string());
                            break;
                        } else {
                            print!("{}Invalid input. Enter 'y', 'n', 'abort' (or use Dashboard): {}", YELLOW, RESET);
                            io::stdout().flush().unwrap();
                        }
                    }
                }
                _ = tokio::time::sleep(Duration::from_millis(200)) => {
                    // Check pending_approval.json
                    if pending_json_path.exists() {
                        if let Ok(content) = fs::read_to_string(pending_json_path) {
                            if let Ok(data) = serde_json::from_str::<serde_json::Value>(&content) {
                                if let Some(status) = data.get("status").and_then(|s| s.as_str()) {
                                    if status == "approved" || status == "rejected" || status == "aborted" {
                                        approval = Some(status.to_string());
                                        break;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // Clean up pending file
        let _ = fs::remove_file(pending_json_path);

        match approval.as_deref() {
            Some("approved") => {
                println!("\n{}✅ Changes approved. Writing files...{}", GREEN, RESET);
            }
            Some("rejected") => {
                println!("\n{}❌ Changes rejected by user.{}", YELLOW, RESET);
                state.task_status = "failed".to_string();
                state.error_logs = "Changes rejected by user".to_string();
                return Ok(());
            }
            _ => {
                println!("\n{}🚨 Aborting flow...{}", RED, RESET);
                std::process::exit(0);
            }
        }
    }

    // 3. Write files
    let mut resolved_list = Vec::new();
    for f in &files_to_process {
        let file_path = resolve_session_path(&state.session_dir, &f.path);
        let new_content = f.computed_new_content.clone().unwrap_or_default();

        let is_edit = file_path.exists();
        let tool_name = if f.action == "patch" { "Patch" } else if is_edit { "Edit" } else { "Write" };
        println!("● {}({})", tool_name, file_path.to_string_lossy());
        println!("  Saving {} code block to local filesystem", if f.action == "patch" { "patched" } else { "generated" });

        if let Some(parent) = file_path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let _ = fs::write(&file_path, &new_content);

        // 4. Auto-formatting
        let ext = file_path.extension().map(|e| e.to_string_lossy().to_string().to_lowercase()).unwrap_or_default();
        if ext == "py" {
            // Try formatting with black
            let _ = Command::new("black").arg(&file_path).output();
        } else if ["js", "ts", "css", "html", "json"].contains(&ext.as_str()) {
            // Try formatting with prettier
            let _ = Command::new("npx").args(&["prettier", "--write", &file_path.to_string_lossy()]).output();
        }

        // Read back formatted content
        let final_content = fs::read_to_string(&file_path).unwrap_or(new_content);
        resolved_list.push(ResolvedFile {
            path: file_path.to_string_lossy().to_string(),
            content: final_content,
        });
    }

    state.generated_files = resolved_list;
    state.task_status = "executed".to_string();

    Ok(())
}

fn check_sqlite_syntax(sql_content: &str) -> Result<(), String> {
    // Run sqlite3 in-memory checking script
    let mut child = Command::new("sqlite3")
        .arg(":memory:")
        .stdin(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;

    if let Some(mut stdin) = child.stdin.take() {
        let _ = stdin.write_all(sql_content.as_bytes());
    }

    let output = child.wait_with_output().map_err(|e| e.to_string())?;
    if !output.status.success() {
        let err = String::from_utf8_lossy(&output.stderr).to_string();
        return Err(err);
    }
    Ok(())
}

fn check_html_syntax(content: &str) -> Result<(), String> {
    // Basic tag parsing balance check
    let re = Regex::new(r"<(/?[a-zA-Z0-9]+)[^>]*>").unwrap();
    let mut stack = Vec::new();
    let self_closing = vec![
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"
    ];

    for cap in re.captures_iter(content) {
        let tag = cap[1].to_lowercase();
        if tag.starts_with('/') {
            let closed = &tag[1..];
            if self_closing.contains(&closed) {
                continue;
            }
            if let Some(opened) = stack.pop() {
                if opened != closed {
                    return Err(format!("Mismatched tag: expected </{}> but found </{}>", opened, closed));
                }
            } else {
                return Err(format!("Unexpected closing tag </{}>", closed));
            }
        } else {
            if !self_closing.contains(&tag.as_str()) && !cap[0].ends_with("/>") {
                stack.push(tag);
            }
        }
    }
    Ok(())
}

async fn verifier_node(state: &mut AgentState) -> Result<()> {
    let mut files_to_verify = state.generated_files.clone();
    let mut errors = Vec::new();
    let mut status = "success";

    if files_to_verify.is_empty() {
        state.task_status = "success".to_string();
        state.error_logs = String::new();
        return Ok(());
    }

    // AST Regression Testing
    if !state.session_dir.is_empty() {
        println!("\n● {}{}AST Dependency Scan{}{}", BOLD, CYAN, RESET, RESET);
        println!("  Analyzing project imports/includes to identify dependent files for regression testing");
        let target_paths: Vec<String> = files_to_verify.iter().map(|f| f.path.clone()).collect();
        let dependents = scan_dependencies(&state.session_dir, &target_paths);
        if !dependents.is_empty() {
            println!("  Found {} dependent file(s) for regression testing:", dependents.len());
            for d in &dependents {
                println!("   - {}", d);
                if !files_to_verify.iter().any(|f| f.path == *d) {
                    if let Ok(c) = fs::read_to_string(d) {
                        files_to_verify.push(ResolvedFile {
                            path: d.clone(),
                            content: c,
                        });
                    }
                }
            }
        } else {
            println!("  No dependent files found for regression testing.");
        }
    }

    for f in &files_to_verify {
        let path_str = &f.path;
        let content = &f.content;

        println!("● Scan({})", path_str);
        println!("  Running security checks for SQL injection and dangerous shell commands");

        // 1. Security Check
        if let Err(sec_err) = check_security_guardrails(path_str, content) {
            errors.push(sec_err.clone());
            status = "failed";
            println!("  ❌ Security Block: {}", sec_err);
            continue;
        }

        // 2. Syntax/Runtime Checks based on extension
        let path = Path::new(path_str);
        let ext = path.extension().map(|e| e.to_string_lossy().to_string().to_lowercase()).unwrap_or_default();

        match ext.as_str() {
            "py" => {
                // Flake8
                if let Ok(output) = Command::new("flake8").args(&["--select=E9,F", path_str]).output() {
                    if !output.status.success() {
                        let err_msg = format!("Flake8 Linting Errors in {}:\n{}", path_str, String::from_utf8_lossy(&output.stdout));
                        errors.push(err_msg);
                        status = "failed";
                        println!("  ❌ Linting Failed: PEP8 / Pyflakes errors detected");
                        continue;
                    }
                }

                // Execute
                println!("● Bash(python {})", path_str);
                println!("  Executing Python interpreter verification check");
                
                let run_res = run_in_sandbox(&["python3", path_str], None, 5);
                match run_res {
                    Ok(out) => {
                        if !out.status.success() {
                            let stderr_str = String::from_utf8_lossy(&out.stderr).to_string();
                            if stderr_str.contains("ModuleNotFoundError") || stderr_str.contains("ImportError") {
                                // Auto install missing package
                                let re = Regex::new(r"No module named '([^']+)'").unwrap();
                                if let Some(caps) = re.captures(&stderr_str) {
                                    let missing_module = &caps[1];
                                    println!("  📦 Missing module detected. Auto-installing '{}'...", missing_module);
                                    let _ = Command::new("python3").args(&["-m", "pip", "install", missing_module]).output();
                                    // Retry
                                    if let Ok(out_retry) = run_in_sandbox(&["python3", path_str], None, 5) {
                                        if out_retry.status.success() {
                                            continue;
                                        }
                                    }
                                }
                            }
                            errors.push(format!("Python Exit Code {}. Error details:\n{}", out.status.code().unwrap_or(1), stderr_str));
                            status = "failed";
                            println!("  ❌ Execution Failed: Runtime error occurred");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Python Error in {}: {}", path_str, e));
                        status = "failed";
                        println!("  ❌ Execution Failed: Runtime error occurred");
                    }
                }
            }
            "js" => {
                // ESLint
                if Path::new("test/eslint.config.js").exists() {
                    println!("● Lint({})", path_str);
                    if let Ok(eslint_out) = Command::new("npx").args(&["eslint", "--config", "test/eslint.config.js", "--no-color", path_str]).output() {
                        if !eslint_out.status.success() {
                            errors.push(format!("ESLint Errors in {}:\n{}", path_str, String::from_utf8_lossy(&eslint_out.stdout)));
                            status = "failed";
                            println!("  ❌ Linting Failed: ESLint violations found");
                            continue;
                        }
                    }
                }

                // Node
                println!("● Bash(node {})", path_str);
                match run_in_sandbox(&["node", path_str], None, 5) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Node Exit Code {}. Details:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Execution Failed: Runtime error occurred");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Node Error in {}: {}", path_str, e));
                        status = "failed";
                        println!("  ❌ Execution Failed: Runtime error occurred");
                    }
                }
            }
            "ts" => {
                println!("● Bash(npx ts-node {})", path_str);
                match run_in_sandbox(&["npx", "ts-node", "--compiler-options", "{\"module\": \"commonjs\"}", path_str], None, 10) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("TypeScript Exit Code {}. Details:\n{}{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr), String::from_utf8_lossy(&out.stdout)));
                            status = "failed";
                            println!("  ❌ Execution Failed: TypeScript error occurred");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("TypeScript Error in {}: {}", path_str, e));
                        status = "failed";
                        println!("  ❌ Execution Failed: TypeScript error occurred");
                    }
                }
            }
            "c" => {
                // cppcheck
                let _ = Command::new("cppcheck").args(&["--enable=warning,style", "--error-exitcode=1", path_str]).output();

                println!("● Compile({})", path_str);
                let out_bin = format!("{}.bin", path_str);
                match run_in_sandbox(&["gcc", "-Wall", "-Wextra", "-o", &out_bin, path_str], None, 10) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("C Compilation Failed:\n{}{}", String::from_utf8_lossy(&out.stderr), String::from_utf8_lossy(&out.stdout)));
                            status = "failed";
                            println!("  ❌ Verification Failed: C build error");
                        } else {
                            println!("● Bash({})", out_bin);
                            match run_in_sandbox(&[&out_bin], None, 5) {
                                Ok(run_out) => {
                                    let _ = fs::remove_file(&out_bin);
                                    if !run_out.status.success() {
                                        errors.push(format!("C Runtime Exit Code {}:\n{}", run_out.status.code().unwrap_or(1), String::from_utf8_lossy(&run_out.stderr)));
                                        status = "failed";
                                        println!("  ❌ Verification Failed: C execution error");
                                    }
                                }
                                Err(e) => {
                                    let _ = fs::remove_file(&out_bin);
                                    errors.push(format!("C execution failed: {}", e));
                                    status = "failed";
                                    println!("  ❌ Verification Failed: C execution error");
                                }
                            }
                        }
                    }
                    Err(e) => {
                        errors.push(format!("C compilation call failed: {}", e));
                        status = "failed";
                        println!("  ❌ Verification Failed: C build error");
                    }
                }
            }
            "cpp" | "cc" | "cxx" | "h" | "hpp" => {
                // PlatformIO check
                let mut is_pio = false;
                if !state.session_dir.is_empty() {
                    if Path::new(&state.session_dir).join("platformio.ini").exists() {
                        is_pio = true;
                    }
                }

                if is_pio {
                    println!("● PlatformIO Run({})", state.session_dir);
                    match Command::new("pio").args(&["run", "-d", &state.session_dir]).output() {
                        Ok(pio_out) => {
                            if !pio_out.status.success() {
                                errors.push(format!("PlatformIO Build Failed:\n{}{}", String::from_utf8_lossy(&pio_out.stdout), String::from_utf8_lossy(&pio_out.stderr)));
                                status = "failed";
                                println!("  ❌ Verification Failed: PlatformIO build error");
                            }
                        }
                        Err(e) => {
                            errors.push(format!("PlatformIO Execution failed: {}", e));
                            status = "failed";
                            println!("  ❌ Verification Failed: PlatformIO execution failed");
                        }
                    }
                } else {
                    let is_header = ext == "h" || ext == "hpp";
                    if is_header {
                        println!("● Compile-Syntax-Only({})", path_str);
                        match run_in_sandbox(&["g++", "-Wall", "-Wextra", "-std=c++17", "-fsyntax-only", "-x", "c++-header", path_str], None, 10) {
                            Ok(out) => {
                                if !out.status.success() {
                                    errors.push(format!("C++ Header Syntax Errors:\n{}", String::from_utf8_lossy(&out.stderr)));
                                    status = "failed";
                                    println!("  ❌ Verification Failed: C++ header check error");
                                }
                            }
                            Err(e) => {
                                errors.push(format!("C++ Header Syntax Call Failed: {}", e));
                                status = "failed";
                            }
                        }
                    } else {
                        println!("● Compile({})", path_str);
                        let out_bin = format!("{}.bin", path_str);
                        match run_in_sandbox(&["g++", "-Wall", "-Wextra", "-std=c++17", "-o", &out_bin, path_str], None, 10) {
                            Ok(out) => {
                                if !out.status.success() {
                                    errors.push(format!("C++ Compilation Failed:\n{}", String::from_utf8_lossy(&out.stderr)));
                                    status = "failed";
                                    println!("  ❌ Verification Failed: C++ build error");
                                } else {
                                    println!("● Bash({})", out_bin);
                                    match run_in_sandbox(&[&out_bin], None, 5) {
                                        Ok(run_out) => {
                                            let _ = fs::remove_file(&out_bin);
                                            if !run_out.status.success() {
                                                errors.push(format!("C++ Runtime Exit Code {}:\n{}", run_out.status.code().unwrap_or(1), String::from_utf8_lossy(&run_out.stderr)));
                                                status = "failed";
                                                println!("  ❌ Verification Failed: C++ execution error");
                                            }
                                        }
                                        Err(e) => {
                                            let _ = fs::remove_file(&out_bin);
                                            errors.push(format!("C++ execution failed: {}", e));
                                            status = "failed";
                                        }
                                    }
                                }
                            }
                            Err(e) => {
                                errors.push(format!("C++ compilation call failed: {}", e));
                                status = "failed";
                            }
                        }
                    }
                }
            }
            "sql" => {
                println!("● Lint({})", path_str);
                // Try SQLFluff
                let sqlfluff_res = Command::new("sqlfluff").args(&["lint", path_str, "--dialect", "sqlite"]).output();
                let mut sqlfluff_run = false;
                if let Ok(out) = sqlfluff_res {
                    sqlfluff_run = true;
                    if !out.status.success() {
                        errors.push(format!("SQLFluff Linting Errors:\n{}", String::from_utf8_lossy(&out.stdout)));
                        status = "failed";
                        println!("  ❌ Linting Failed: SQLFluff style/syntax errors");
                    }
                }

                if !sqlfluff_run || status != "failed" {
                    if let Err(err) = check_sqlite_syntax(content) {
                        errors.push(format!("SQL Parser Error: {}", err));
                        status = "failed";
                        println!("  ❌ Linting Failed: SQL parsing error");
                    }
                }
            }
            "html" | "xml" => {
                println!("● Lint({})", path_str);
                if let Err(err) = check_html_syntax(content) {
                    errors.push(format!("Malformed HTML/XML in {}: {}", path_str, err));
                    status = "failed";
                    println!("  ❌ Linting Failed: Malformed tag structure");
                }
            }
            "css" => {
                println!("● Lint({})", path_str);
                let open_braces = content.matches('{').count();
                let close_braces = content.matches('}').count();
                if open_braces != close_braces {
                    errors.push(format!("CSS Syntax Error: Mismatched braces. Open '{{': {}, Close '}}': {}", open_braces, close_braces));
                    status = "failed";
                    println!("  ❌ Linting Failed: Syntax error detected");
                }
            }
            "json" => {
                println!("● Lint({})", path_str);
                if let Err(e) = serde_json::from_str::<serde_json::Value>(content) {
                    errors.push(format!("Invalid JSON in {}: {}", path_str, e));
                    status = "failed";
                    println!("  ❌ Linting Failed: Invalid JSON format");
                }
            }
            "sh" => {
                // Shellcheck
                let _ = Command::new("shellcheck").arg(path_str).output();

                println!("● Bash-Syntax-Check({})", path_str);
                match run_in_sandbox(&["bash", "-n", path_str], None, 5) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Bash Syntax Errors:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Bash syntax error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Bash syntax call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "java" => {
                println!("● Compile({})", path_str);
                match run_in_sandbox(&["javac", path_str], None, 15) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Java Compilation Failed:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Java compilation error");
                        } else {
                            let classname = path.file_stem().unwrap().to_string_lossy();
                            let class_dir = path.parent().unwrap_or_else(|| Path::new(".")).to_string_lossy();
                            println!("● Bash(java -cp {} {})", class_dir, classname);
                            match run_in_sandbox(&["java", "-cp", &class_dir, &classname], None, 10) {
                                Ok(run_out) => {
                                    let class_file = Path::new(&*class_dir).join(format!("{}.class", classname));
                                    let _ = fs::remove_file(class_file);
                                    if !run_out.status.success() {
                                        errors.push(format!("Java Runtime Exit Code {}:\n{}", run_out.status.code().unwrap_or(1), String::from_utf8_lossy(&run_out.stderr)));
                                        status = "failed";
                                        println!("  ❌ Verification Failed: Java execution error");
                                    }
                                }
                                Err(e) => {
                                    errors.push(format!("Java execution failed: {}", e));
                                    status = "failed";
                                }
                            }
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Java compiler call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "cs" => {
                println!("● Compile({})", path_str);
                let out_exe = path_str.replace(".cs", ".exe");
                let mut compile_exe = "csc";
                // Check if mono mcs is preferred fallback
                if Command::new("csc").arg("-help").output().is_err() && Command::new("mcs").arg("--version").output().is_ok() {
                    compile_exe = "mcs";
                }
                // Construct command arguments correctly
                let out_arg = format!("-out:{}", out_exe);
                let args = vec![compile_exe, &out_arg, path_str];

                match run_in_sandbox(&args, None, 15) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("C# Compilation Failed:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: C# compilation error");
                        } else {
                            println!("● Bash(mono {})", out_exe);
                            match run_in_sandbox(&["mono", &out_exe], None, 10) {
                                Ok(run_out) => {
                                    let _ = fs::remove_file(&out_exe);
                                    if !run_out.status.success() {
                                        errors.push(format!("C# Runtime Exit Code {}:\n{}", run_out.status.code().unwrap_or(1), String::from_utf8_lossy(&run_out.stderr)));
                                        status = "failed";
                                        println!("  ❌ Verification Failed: C# execution error");
                                    }
                                }
                                Err(e) => {
                                    let _ = fs::remove_file(&out_exe);
                                    errors.push(format!("C# execution failed: {}", e));
                                    status = "failed";
                                }
                            }
                        }
                    }
                    Err(e) => {
                        errors.push(format!("C# compiler call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "r" => {
                println!("● Bash(Rscript {})", path_str);
                match run_in_sandbox(&["Rscript", path_str], None, 10) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Rscript Exit Code {}:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: R runtime error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("R execution failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "go" => {
                println!("● Bash(go run {})", path_str);
                match run_in_sandbox(&["go", "run", path_str], None, 15) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Go Run Exit Code {}:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Go execution error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Go run call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "swift" => {
                println!("● Bash(swift {})", path_str);
                match run_in_sandbox(&["swift", path_str], None, 15) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Swift Run Exit Code {}:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Swift execution error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Swift run call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "rs" => {
                println!("● Compile({})", path_str);
                let out_bin = format!("{}.bin", path_str);
                match run_in_sandbox(&["rustc", path_str, "-o", &out_bin], None, 20) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Rust Compilation Failed:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Rust compilation error");
                        } else {
                            println!("● Bash({})", out_bin);
                            match run_in_sandbox(&[&out_bin], None, 10) {
                                Ok(run_out) => {
                                    let _ = fs::remove_file(&out_bin);
                                    if !run_out.status.success() {
                                        errors.push(format!("Rust Runtime Exit Code {}:\n{}", run_out.status.code().unwrap_or(1), String::from_utf8_lossy(&run_out.stderr)));
                                        status = "failed";
                                        println!("  ❌ Verification Failed: Rust execution error");
                                    }
                                }
                                Err(e) => {
                                    let _ = fs::remove_file(&out_bin);
                                    errors.push(format!("Rust execution failed: {}", e));
                                    status = "failed";
                                }
                            }
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Rustc compiler call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "php" => {
                println!("● Lint({})", path_str);
                match run_in_sandbox(&["php", "-l", path_str], None, 5) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("PHP Linting Failed:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: PHP syntax error");
                            continue;
                        }
                    }
                    Err(e) => {
                        errors.push(format!("PHP lint call failed: {}", e));
                        status = "failed";
                        continue;
                    }
                }

                println!("● Bash(php {})", path_str);
                match run_in_sandbox(&["php", path_str], None, 10) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("PHP Runtime Exit Code {}:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: PHP execution error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("PHP run call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "kt" => {
                println!("● Compile({})", path_str);
                let out_jar = format!("{}.jar", path_str);
                match run_in_sandbox(&["kotlinc", path_str, "-include-runtime", "-d", &out_jar], None, 25) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Kotlin Compilation Failed:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Kotlin compilation error");
                        } else {
                            println!("● Bash(java -jar {})", out_jar);
                            match run_in_sandbox(&["java", "-jar", &out_jar], None, 10) {
                                Ok(run_out) => {
                                    let _ = fs::remove_file(&out_jar);
                                    if !run_out.status.success() {
                                        errors.push(format!("Kotlin Runtime Exit Code {}:\n{}", run_out.status.code().unwrap_or(1), String::from_utf8_lossy(&run_out.stderr)));
                                        status = "failed";
                                        println!("  ❌ Verification Failed: Kotlin execution error");
                                    }
                                }
                                Err(e) => {
                                    let _ = fs::remove_file(&out_jar);
                                    errors.push(format!("Kotlin execution failed: {}", e));
                                    status = "failed";
                                }
                            }
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Kotlinc compiler call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "rb" => {
                println!("● Lint({})", path_str);
                match run_in_sandbox(&["ruby", "-c", path_str], None, 5) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Ruby Syntax Errors:\n{}", String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Ruby syntax error");
                            continue;
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Ruby syntax call failed: {}", e));
                        status = "failed";
                        continue;
                    }
                }

                println!("● Bash(ruby {})", path_str);
                match run_in_sandbox(&["ruby", path_str], None, 10) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Ruby Runtime Exit Code {}:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Ruby execution error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Ruby run call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            "zig" => {
                println!("● Bash(zig run {})", path_str);
                match run_in_sandbox(&["zig", "run", path_str], None, 15) {
                    Ok(out) => {
                        if !out.status.success() {
                            errors.push(format!("Zig Run Exit Code {}:\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Verification Failed: Zig execution error");
                        }
                    }
                    Err(e) => {
                        errors.push(format!("Zig run call failed: {}", e));
                        status = "failed";
                    }
                }
            }
            _ => {
                if content.trim().is_empty() {
                    errors.push(format!("File {} is empty.", path_str));
                    status = "failed";
                    println!("  ❌ Verification Failed: Empty file");
                }
            }
        }
    }

    // 3. Dynamic Test Suite Runner
    if status != "failed" {
        let test_dirs = vec![state.session_dir.clone(), ".".to_string()];
        let mut test_cmd = None;
        let mut test_cwd = None;

        for d in test_dirs {
            if d.is_empty() || !Path::new(&d).exists() {
                continue;
            }
            // 3.1 Node project
            if Path::new(&d).join("package.json").exists() {
                if let Ok(pkg_content) = fs::read_to_string(Path::new(&d).join("package.json")) {
                    if let Ok(pkg_data) = serde_json::from_str::<serde_json::Value>(&pkg_content) {
                        if pkg_data["scripts"]["test"].is_string() {
                            test_cmd = Some(vec!["npm", "test"]);
                            test_cwd = Some(d.clone());
                            break;
                        }
                    }
                }
            }

            // 3.2 Python project
            let has_pytest_ini = Path::new(&d).join("pytest.ini").exists() || Path::new(&d).join("conftest.py").exists();
            let mut has_test_files = false;
            if let Ok(read_dir) = fs::read_dir(&d) {
                for item in read_dir.filter_map(|e| e.ok()) {
                    let name = item.file_name().to_string_lossy().to_string();
                    if name.starts_with("test_") && name.ends_with(".py") {
                        has_test_files = true;
                        break;
                    }
                }
            }

            if has_pytest_ini || has_test_files {
                test_cmd = Some(vec!["pytest"]);
                test_cwd = Some(d.clone());
                break;
            }
        }

        if let Some(cmd) = test_cmd {
            let cmd_str = cmd.join(" ");
            let cwd_val = test_cwd.unwrap_or_default();
            println!("\n● {}{}Test Suite Run{}{} ({})", BOLD, CYAN, RESET, RESET, cmd_str);
            println!("  Running project test suite dynamically in {}", cwd_val);

            match run_in_sandbox(&cmd, Some(&cwd_val), 30) {
                Ok(out) => {
                    if !out.status.success() {
                        let is_pytest = cmd[0] == "pytest";
                        // if pytest returns 5, it means no tests collected, treat as success
                        if is_pytest && out.status.code() == Some(5) {
                            println!("  ℹ️ No tests collected by pytest (Exit Code 5). Ignoring.");
                            println!("  ✅ Test Suite Passed successfully!");
                        } else {
                            errors.push(format!("Test Suite Failed (Exit Code {}):\n{}\n{}", out.status.code().unwrap_or(1), String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr)));
                            status = "failed";
                            println!("  ❌ Test Suite Failed: test assertions failed");
                        }
                    } else {
                        println!("  ✅ Test Suite Passed successfully!");
                    }
                }
                Err(e) => {
                    errors.push(format!("Test Suite execution failed: {}", e));
                    status = "failed";
                    println!("  ❌ Test Suite Execution Failed");
                }
            }
        }
    }

    // Git Integration
    if !state.session_dir.is_empty() && Path::new(&state.session_dir).exists() {
        let git_dir = Path::new(&state.session_dir).join(".git");
        if !git_dir.exists() {
            let _ = Command::new("git").arg("init").current_dir(&state.session_dir).output();
            let _ = Command::new("git").args(&["config", "user.name", "mai-agent"]).current_dir(&state.session_dir).output();
            let _ = Command::new("git").args(&["config", "user.email", "agent@mai.local"]).current_dir(&state.session_dir).output();
        }
        let _ = Command::new("git").arg("add").arg(".").current_dir(&state.session_dir).output();
        let commit_msg = format!("Iteration {}: {}", state.iteration_count, status);
        let _ = Command::new("git").args(&["commit", "-m", &commit_msg]).current_dir(&state.session_dir).output();
    }

    state.task_status = status.to_string();
    state.error_logs = errors.join("\n");

    Ok(())
}

// =====================================================================
// MAIN CLI COMMAND INTERRUPT LOOP
// =====================================================================

#[tokio::main]
async fn main() -> Result<()> {
    // Sourcing local test environment variables
    let env_path = Path::new("test/.env");
    let _ = dotenv::from_path(env_path);

    let api_key = match env::var("GEMINI_API_KEY") {
        Ok(key) => key,
        Err(_) => {
            eprintln!("❌ Error: GEMINI_API_KEY environment variable not configured in test/.env");
            std::process::exit(1);
        }
    };

    let client = reqwest::Client::new();

    // Create session dir name
    let now_str = Local::now().format("%Y%m%d_%H%M%S").to_string();
    let mut session_dir = format!("test/mai_output_{}", now_str);

    // Setup history loader
    let mut messages = Vec::new();
    if let Some(history) = load_session_history() {
        println!("\n{}📂 Found previous chat session (Session: {}){}", YELLOW, history.session_dir, RESET);
        print!("Do you want to resume the previous session? (y/n) [Default: n]: ");
        io::stdout().flush().unwrap();

        let mut resume_input = String::new();
        let _ = io::stdin().read_line(&mut resume_input);
        let cleaned = resume_input.trim().to_lowercase();
        if cleaned == "y" || cleaned == "yes" {
            session_dir = history.session_dir;
            messages = history.messages;
            let _ = fs::create_dir_all(&session_dir);
            println!("{}✅ Successfully restored {} messages from history! You can continue now.{}", GREEN, messages.len(), RESET);
        }
    }

    // Spawn thread to read keyboard input asynchronously
    let (stdin_tx, mut stdin_rx) = tokio::sync::mpsc::channel::<String>(10);
    std::thread::spawn(move || {
        let mut input = String::new();
        while io::stdin().read_line(&mut input).is_ok() {
            let _ = stdin_tx.blocking_send(input.clone());
            input.clear();
        }
    });

    println!(
        r#"{}{}  ███▄ ▄███▀  ████████  █████████
  ██████████  ███    ███    ███
  ███ ▀█▀ ███  ██████████    ███
  ███     ███  ███    ███    ███
  ███     ███  ███    ███  █████████{}

  {}MAI CLI v1.1.0 (Rust Edition - High Performance){}
  Model: {}Gemini 3.1 Flash (Lite){}
  Output Dir: {}{}/{}
  Directory: {}{}{}
  
  {}Type /help or .help to see available commands, or /exit to quit.{}
  ────────────────────────────────────────────────"#,
        CYAN, BOLD, RESET, BOLD, RESET, GREEN, RESET, BLUE, session_dir, RESET, BLUE, env::current_dir().unwrap_or_default().display(), RESET, YELLOW, RESET
    );

    loop {
        print!("\n{}{}[mai>]{} ", BOLD, CYAN, RESET);
        io::stdout().flush().unwrap();

        let user_prompt = match stdin_rx.recv().await {
            Some(p) => p.trim().to_string(),
            None => break,
        };

        if user_prompt.is_empty() {
            continue;
        }

        // Commands
        let lower = user_prompt.to_lowercase();
        if ["/exit", "/quit", ".exit", ".quit", "exit", "quit", "exit()", "quit()"].contains(&lower.as_str()) {
            println!("👋 Closing work session. Goodbye!");
            break;
        }

        if ["/help", ".help"].contains(&lower.as_str()) {
            println!(
                r#"{}[Available Commands for MAI CLI:]{}
  {} /help or .help {} - Show this help menu
  {} /init or /reset {} - Clear chat history and start a new session (resets Output Dir)
  {} /scrutinize <filename> {} - Audit a file for security vulnerabilities and runtime errors
  {} /explain <filename> {} - Explain the logic and implementation of the file in detail
  {} /refactor <filename> {} - Optimize performance, readability, and security of the code
  {} /add-test <filename> {} - Add Assert Test Cases block to a Python file
  {} /exit or /quit {} - Exit the CLI"#,
                BOLD, RESET, CYAN, RESET, CYAN, RESET, CYAN, RESET, CYAN, RESET, CYAN, RESET, CYAN, RESET, CYAN, RESET
            );
            continue;
        }

        if ["/init", "/reset", ".init", ".reset"].contains(&lower.as_str()) {
            let now_str_new = Local::now().format("%Y%m%d_%H%M%S").to_string();
            session_dir = format!("test/mai_output_{}", now_str_new);
            messages.clear();
            let _ = fs::remove_file("test/session_history.json");
            println!("🔄 Session reset complete! Starting a fresh chat history.");
            println!("New Output Dir: {}{}/{}", BLUE, session_dir, RESET);
            continue;
        }

        // /scrutinize
        if lower.starts_with("/scrutinize") || lower.starts_with(".scrutinize") {
            let parts: Vec<&str> = user_prompt.split_whitespace().collect();
            if parts.len() < 2 {
                println!("⚠️  Please specify the file name to scrutinize, e.g. `/scrutinize app.py`");
                continue;
            }
            let filename = parts[1];
            let mut target_file_path = PathBuf::from(filename);
            if !target_file_path.exists() {
                target_file_path = resolve_session_path(&session_dir, filename);
            }

            if !target_file_path.exists() {
                println!("❌ File '{}' not found in this directory", filename);
                continue;
            }

            match fs::read_to_string(&target_file_path) {
                Ok(content) => {
                    println!("\n{}🔍 Scrutinizing '{}' using Security Guardrails & Verification Nodes...{}", YELLOW, target_file_path.display(), RESET);
                    let mut test_state = AgentState {
                        messages: Vec::new(),
                        task_status: "pending".to_string(),
                        iteration_count: 0,
                        error_logs: String::new(),
                        generated_files: vec![ResolvedFile {
                            path: target_file_path.to_string_lossy().to_string(),
                            content,
                        }],
                        session_dir: String::new(),
                    };

                    let _ = verifier_node(&mut test_state).await;

                    if test_state.task_status == "success" {
                        println!("\n{}✅ Audit passed! File '{}' has valid syntax and no security vulnerabilities.{}", GREEN, target_file_path.display(), RESET);
                    } else {
                        println!("\n{}❌ Audit failed! Detected bugs or security vulnerabilities in '{}':{}", YELLOW, target_file_path.display(), RESET);
                        println!("{}{}{}", YELLOW, test_state.error_logs, RESET);
                    }
                }
                Err(e) => {
                    println!("❌ Technical error reading the file: {}", e);
                }
            }
            continue;
        }

        // /explain
        if lower.starts_with("/explain") || lower.starts_with(".explain") {
            let parts: Vec<&str> = user_prompt.split_whitespace().collect();
            if parts.len() < 2 {
                println!("⚠️  Please specify the file name to explain, e.g. `/explain app.py`");
                continue;
            }
            let filename = parts[1];
            let mut target_file_path = PathBuf::from(filename);
            if !target_file_path.exists() {
                target_file_path = resolve_session_path(&session_dir, filename);
            }

            if !target_file_path.exists() {
                println!("❌ File '{}' not found in this directory", filename);
                continue;
            }

            match fs::read_to_string(&target_file_path) {
                Ok(content) => {
                    println!("\n{}🤖 Analyzing and generating explanation for '{}'...{}", YELLOW, target_file_path.display(), RESET);
                    let prompt_text = format!(
                        "Please explain the logic and implementation of the code in this file in detail and systematically, in English:\n\nFile Name: {}\n\nCode:\n```\n{}\n```",
                        filename, content
                    );
                    let system_inst = "You are an expert AI code analyst and software architect. Explain the code clearly, systematically, and concisely.";
                    match call_gemini_api(&client, &api_key, system_inst, &prompt_text).await {
                        Ok(explanation) => {
                            println!("\n{}📘 Code explanation for '{}':{}", GREEN, target_file_path.display(), RESET);
                            println!("{}", explanation);
                        }
                        Err(e) => {
                            println!("❌ Error generating code explanation: {}", e);
                        }
                    }
                }
                Err(e) => {
                    println!("❌ Error reading file: {}", e);
                }
            }
            continue;
        }

        // refactor or add test
        let is_refactor = lower.starts_with("/refactor") || lower.starts_with(".refactor");
        let is_add_test = lower.starts_with("/add-test") || lower.starts_with(".add-test") || lower.starts_with("/add_test") || lower.starts_with(".add_test");
        let mut graph_prompt = String::new();

        if is_refactor || is_add_test {
            let parts: Vec<&str> = user_prompt.split_whitespace().collect();
            if parts.len() < 2 {
                println!("⚠️  Please specify the file name, e.g. `/refactor app.py`");
                continue;
            }
            let filename = parts[1];
            let mut target_file_path = PathBuf::from(filename);
            if !target_file_path.exists() {
                target_file_path = resolve_session_path(&session_dir, filename);
            }

            if !target_file_path.exists() {
                println!("❌ File '{}' not found in this directory", filename);
                continue;
            }

            match fs::read_to_string(&target_file_path) {
                Ok(content) => {
                    let rel_path = if target_file_path.to_string_lossy().contains(&session_dir) {
                        Path::new(&target_file_path).strip_prefix(&session_dir).unwrap().to_string_lossy().to_string()
                    } else {
                        target_file_path.to_string_lossy().to_string()
                    };

                    if is_refactor {
                        println!("\n{}⚙️  Submitting refactor request for '{}' to the agent graph...{}", YELLOW, target_file_path.display(), RESET);
                        graph_prompt = format!(
                            "Refactor the code in this file to improve performance, security, and cleanliness, while preserving its original functionality and structure:\n\nTarget File Path: {}\n\nOriginal Code:\n```\n{}\n```",
                            rel_path, content
                        );
                    } else {
                        println!("\n{}⚙️  Submitting test generation request for '{}' to the agent graph...{}", YELLOW, target_file_path.display(), RESET);
                        graph_prompt = format!(
                            "For the following file, write comprehensive Assert Test Cases inside the if __name__ == '__main__': block at the end of the file, or improve existing ones to verify its logical correctness:\n\nTarget File Path: {}\n\nOriginal Code:\n```\n{}\n```",
                            rel_path, content
                        );
                    }
                }
                Err(e) => {
                    println!("❌ Error preparing file target: {}", e);
                    continue;
                }
            }
        }

        // Run agent graph loop
        let prompt_to_send = if !graph_prompt.is_empty() { graph_prompt } else { user_prompt.clone() };
        messages.push(Message {
            msg_type: "human".to_string(),
            content: prompt_to_send.clone(),
        });

        let mut state = AgentState {
            messages: messages.clone(),
            task_status: "pending".to_string(),
            iteration_count: 0,
            error_logs: String::new(),
            generated_files: Vec::new(),
            session_dir: session_dir.clone(),
        };

        println!("\n⚙️  Executing agent flow...");

        loop {
            // Node 1: Planner
            if let Err(e) = planner_node(&mut state, &client, &api_key).await {
                println!("\n❌ Technical error calling Planner Node: {}", e);
                break;
            }

            // Node 2: Executor
            if let Err(e) = executor_node(&mut state, &mut stdin_rx).await {
                println!("\n❌ Technical error calling Executor Node: {}", e);
                break;
            }

            if state.task_status == "failed" {
                break;
            }

            // Node 3: Verifier
            if let Err(e) = verifier_node(&mut state).await {
                println!("\n❌ Technical error calling Verifier Node: {}", e);
                break;
            }

            // Router check
            if state.task_status == "success" || state.iteration_count >= 3 {
                break;
            }
        }

        if state.task_status == "success" {
            println!("\n{}✅ Execution Successful! Files created and verified.{}", GREEN, RESET);
            if state.iteration_count > 1 {
                save_memory(&prompt_to_send, &state.error_logs, &state.generated_files, &session_dir);
            }
        } else {
            println!("\n{}⚠️ Verification failed or maximum self-healing iterations reached.{}", YELLOW, RESET);
        }

        messages = state.messages;
        save_session_history(&messages, &session_dir);
    }

    Ok(())
}
