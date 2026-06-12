use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use regex::Regex;
use std::fs;
use walkdir::WalkDir;

// =====================================================================
// STRUCTURED OUTPUT SCHEMAS
// =====================================================================

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct PatchBlock {
    pub search: String,
    pub replace: String,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct GeneratedFile {
    pub path: String,
    pub action: String, // "create" or "patch"
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub patches: Option<Vec<PatchBlock>>,
    #[serde(skip)]
    pub computed_new_content: Option<String>,
}

#[derive(Serialize, Deserialize, Debug, Clone)]
pub struct ProjectOutput {
    pub files: Vec<GeneratedFile>,
}

// =====================================================================
// SECURITY GUARDRAILS CHECK
// =====================================================================

pub fn check_security_guardrails(file_path: &str, content: &str) -> Result<(), String> {
    let dangerous_patterns = vec![
        (r"\bos\.remove\b", "os.remove (Delete file)"),
        (r"\bos\.rmdir\b", "os.rmdir (Delete directory)"),
        (r"\bshutil\.rmtree\b", "shutil.rmtree (Recursive directory deletion)"),
        (r"\bos\.system\b", "os.system (Direct shell command execution)"),
        (r#"\bsubprocess\.run\s*\(.*['\"]rm\b"#, "subprocess.run rm (File deletion via system command)"),
        (r#"\bsubprocess\.Popen\s*\(.*['\"]rm\b"#, "subprocess.Popen rm (File deletion via system command)"),
        // Hardcoded Secrets
        (r#"(?i)(api[-_]?key|secret|token|password|passwd|private[-_]?key)\s*=\s*['\"][A-Za-z0-9_\-\.\/]{10,}['\"]"#, "Hardcoded Secret/Credentials (Found passwords or API keys stored directly in code)"),
        (r"AIzaSy[A-Za-z0-9_-]{33}", "Gemini API Key Hardcoded (Found hardcoded Gemini API key)"),
        // Unsafe Shell Execution
        (r#"subprocess\.(run|Popen|call|check_output)\s*\(.*shell\s*=\s*True"#, "Unsafe subprocess execution with shell=True (Risk of Command Injection)"),
        // SQL Injection
        (r#"(?i)\.execute\s*\(\s*f['\"].*SELECT.*\{.*\}"#, "Unsafe SQL dynamic string interpolation (Risk of SQL Injection)"),
    ];

    for (pattern, desc) in dangerous_patterns {
        let re = Regex::new(pattern).map_err(|e| e.to_string())?;
        if re.is_match(content) {
            return Err(format!(
                "❌ [Security Block] Dangerous pattern '{}' found in file '{}'. Operation blocked!",
                desc, file_path
            ));
        }
    }
    Ok(())
}

// =====================================================================
// PATH RESOLUTION
// =====================================================================

pub fn resolve_session_path(session_dir: &str, file_path: &str) -> PathBuf {
    if session_dir.is_empty() {
        return PathBuf::from(file_path);
    }
    let mut cleaned = file_path;
    if cleaned.starts_with("./") {
        cleaned = &cleaned[2..];
    }
    if cleaned.starts_with("test/") {
        cleaned = &cleaned[5..];
    } else if cleaned.starts_with("test\\") {
        cleaned = &cleaned[5..];
    }
    Path::new(session_dir).join(cleaned)
}

// =====================================================================
// SANDBOX RUNNER (DOCKER / HOST FALLBACK)
// =====================================================================

pub fn is_docker_available() -> bool {
    let output = Command::new("docker")
        .arg("info")
        .output();
    match output {
        Ok(out) => out.status.success(),
        Err(_) => false,
    }
}

pub fn run_in_sandbox(cmd: &[&str], working_dir: Option<&str>, timeout_secs: u64) -> Result<Output, std::io::Error> {
    // If docker is not available, run locally on the host
    if !is_docker_available() {
        let mut command = Command::new(cmd[0]);
        if cmd.len() > 1 {
            command.args(&cmd[1..]);
        }
        if let Some(wd) = working_dir {
            command.current_dir(wd);
        }
        return command.output();
    }

    // Determine the docker image based on the executable name
    let executable = cmd[0].to_lowercase();
    let image = if executable.contains("python") || executable.contains("pytest") {
        "python:alpine"
    } else if ["node", "npx", "npm"].contains(&executable.as_str()) {
        "node:alpine"
    } else if ["gcc", "g++"].contains(&executable.as_str()) {
        "gcc:latest"
    } else if ["javac", "java", "kotlinc"].contains(&executable.as_str()) {
        "openjdk:17-slim"
    } else if ["mcs", "mono", "csc", "dotnet"].contains(&executable.as_str()) {
        "mcr.microsoft.com/dotnet/sdk:latest"
    } else if executable == "rscript" {
        "r-base:latest"
    } else if executable == "go" {
        "golang:alpine"
    } else if ["swift", "swiftc"].contains(&executable.as_str()) {
        "swift:latest"
    } else if ["rustc", "cargo"].contains(&executable.as_str()) {
        "rust:alpine"
    } else if executable == "php" {
        "php:alpine"
    } else if executable == "ruby" {
        "ruby:alpine"
    } else if executable == "zig" {
        "ziglang/zig:latest"
    } else if executable.ends_with(".bin") || executable.starts_with("./") {
        "gcc:latest"
    } else {
        "ubuntu:latest"
    };

    let current_dir = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let mut container_workdir = String::from("/workspace");
    if let Some(wd) = working_dir {
        let wd_path = Path::new(wd);
        if wd_path.is_absolute() {
            if let Ok(rel) = wd_path.strip_prefix(&current_dir) {
                container_workdir = format!("/workspace/{}", rel.to_string_lossy());
            }
        } else {
            container_workdir = format!("/workspace/{}", wd);
        }
    }

    let current_dir_str = current_dir.to_string_lossy().to_string();
    
    let mount_arg = format!("{}:/workspace", current_dir_str);
    let mut docker_args = vec![
        "run".to_string(),
        "--rm".to_string(),
        "-v".to_string(),
        mount_arg,
        "-w".to_string(),
        container_workdir,
        image.to_string(),
    ];

    // Map system python interpreter if run inside container
    for part in cmd {
        if part.contains("python") && (part.starts_with('/') || part.contains("bin")) {
            docker_args.push("python".to_string());
        } else {
            docker_args.push((*part).to_string());
        }
    }

    println!("  🐳 Running in Docker container '{}'...", image);
    
    // Setup execution under timeout using timeout utility on Linux
    let mut final_args = vec![timeout_secs.to_string()];
    final_args.extend(docker_args);

    let output = Command::new("timeout")
        .args(final_args)
        .output();

    match output {
        Ok(out) => {
            if out.status.code() == Some(124) {
                Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "Command timed out"))
            } else {
                Ok(out)
            }
        }
        Err(e) => {
            println!("  ⚠️ Docker failed ({}). Falling back to host execution...", e);
            let mut command = Command::new(cmd[0]);
            if cmd.len() > 1 {
                command.args(&cmd[1..]);
            }
            if let Some(wd) = working_dir {
                command.current_dir(wd);
            }
            command.output()
        }
    }
}

// =====================================================================
// AST DEPENDENCY SCANNING
// =====================================================================

pub fn scan_dependencies(session_dir: &str, target_files: &[String]) -> Vec<String> {
    let mut extra_files = std::collections::HashSet::new();
    let session_path = Path::new(session_dir);
    if !session_path.exists() {
        return Vec::new();
    }

    let mut target_names = Vec::new();
    for tf in target_files {
        let path = Path::new(tf);
        if let Some(basename) = path.file_name() {
            let basename_str = basename.to_string_lossy().to_string();
            let name_no_ext = path.file_stem().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
            let ext = path.extension().map(|e| e.to_string_lossy().to_string()).unwrap_or_default();
            target_names.push((tf.clone(), name_no_ext, ext, basename_str));
        }
    }

    for entry in WalkDir::new(session_dir).into_iter().filter_map(|e| e.ok()) {
        let file_path = entry.path();
        if file_path.is_dir() {
            continue;
        }

        let file_path_str = file_path.to_string_lossy().to_string();
        if target_files.contains(&file_path_str) {
            continue;
        }

        let ext = file_path.extension().map(|e| e.to_string_lossy().to_string().to_lowercase()).unwrap_or_default();
        let valid_exts = vec![
            "py", "js", "ts", "c", "cpp", "cc", "cxx", "h", "hpp", 
            "java", "cs", "r", "go", "swift", "rs", "php", "kt", "rb", "zig"
        ];
        if !valid_exts.contains(&ext.as_str()) {
            continue;
        }

        let content = match fs::read_to_string(file_path) {
            Ok(c) => c,
            Err(_) => continue,
        };

        for (_, name_no_ext, _target_ext, basename) in &target_names {
            let matches = match ext.as_str() {
                "py" => {
                    let re1 = Regex::new(&format!(r"\bimport\s+.*\b{}\b", regex::escape(name_no_ext))).unwrap();
                    let re2 = Regex::new(&format!(r"\bfrom\s+.*\b{}\b\s+import\b", regex::escape(name_no_ext))).unwrap();
                    let re3 = Regex::new(&format!(r"\bfrom\s+{}\b\s+import\b", regex::escape(name_no_ext))).unwrap();
                    re1.is_match(&content) || re2.is_match(&content) || re3.is_match(&content)
                }
                "c" | "cpp" | "cc" | "cxx" | "h" | "hpp" => {
                    let re1 = Regex::new(&format!(r#"#include\s+["\']{}["\']"#, regex::escape(basename))).unwrap();
                    let re2 = Regex::new(&format!(r#"#include\s+["\']{}\.(h|hpp)["\']"#, regex::escape(name_no_ext))).unwrap();
                    re1.is_match(&content) || re2.is_match(&content)
                }
                "js" | "ts" => {
                    let re1 = Regex::new(&format!(r#"\bimport\s+.*\bfrom\s+["\'].*{}\b"#, regex::escape(name_no_ext))).unwrap();
                    let re2 = Regex::new(&format!(r#"\brequire\s*\(\s*["\'].*{}\b"#, regex::escape(name_no_ext))).unwrap();
                    re1.is_match(&content) || re2.is_match(&content)
                }
                "java" => {
                    let re = Regex::new(&format!(r"\bimport\s+[^;]*\b{}\b", regex::escape(name_no_ext))).unwrap();
                    re.is_match(&content)
                }
                "cs" => {
                    let re = Regex::new(&format!(r"\busing\s+[^;]*\b{}\b", regex::escape(name_no_ext))).unwrap();
                    re.is_match(&content)
                }
                "go" => {
                    let re = Regex::new(&format!(r#"\bimport\s+(?:[a-zA-Z0-9_]+\s+)?"(?:[^"]+/)?{}(\.go)?"#, regex::escape(name_no_ext))).unwrap();
                    re.is_match(&content)
                }
                "rs" => {
                    let re1 = Regex::new(&format!(r"\buse\s+[^;]*\b{}\b", regex::escape(name_no_ext))).unwrap();
                    let re2 = Regex::new(&format!(r"\bmod\s+{}\b", regex::escape(name_no_ext))).unwrap();
                    re1.is_match(&content) || re2.is_match(&content)
                }
                "php" | "rb" => {
                    let re = Regex::new(&format!(r#"\b(require|include|require_relative)\b.*['"](?:[^'"]+/)?{}\b"#, regex::escape(name_no_ext))).unwrap();
                    re.is_match(&content)
                }
                _ => false,
            };

            if matches {
                extra_files.insert(file_path_str.clone());
                break;
            }
        }
    }

    extra_files.into_iter().collect()
}
