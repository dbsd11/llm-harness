# Resource Repo Context Grep Tool
#
# 封装 git clone/checkout/log + fs grep 为单一 tool，供调度 Agent ReAct 循环按需调用。
# 使用 stdlib: subprocess, tempfile, hashlib, shutil, os。
import os
import subprocess
import tempfile
import hashlib
import shutil
from pathlib import Path
from typing import Dict, Any, Optional, List

from core.agents.tool_registry import tool_registry
from database.repositories.scenario_repository import ScenarioRepository
from logger import logger

# ponytail: 全局缓存根目录。若并发写入冲突，升级路径是按 tenant_id 分子目录 + 文件锁。
_CACHE_ROOT = Path(tempfile.gettempdir()) / "llm_harness_repo_cache"


def _cache_dir_for(tenant_id: str, git_url: str, username: str = "", token: str = "") -> Path:
    # ponytail: hash includes credentials so a cred change invalidates the cache
    key = f"{git_url}|{username}|{token}"
    url_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    d = _CACHE_ROOT / (tenant_id or "_default") / url_hash
    return d


def _authed_url(git_url: str, username: str, token: str) -> str:
    """Inject username:token into HTTPS URL for private repo auth. SSH URLs pass through."""
    if not username or not token:
        return git_url
    if not git_url.startswith("https://"):
        return git_url  # SSH URLs need agent keys, not inline creds
    # https://github.com/org/repo.git → https://user:token@github.com/org/repo.git
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(git_url)
    authed = parsed._replace(netloc=f"{username}:{token}@{parsed.hostname}"
                             + (f":{parsed.port}" if parsed.port else ""))
    return urlunparse(authed)


def _ensure_cloned(git_url: str, cache_dir: Path,
                   username: str = "", token: str = "") -> tuple[bool, str]:
    """Clone or fetch a repo. Returns (ok, error_msg)."""
    repo_dir = cache_dir / "repo"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    remote_url = _authed_url(git_url, username, token)

    if repo_dir.exists() and (repo_dir / ".git").exists():
        # Already cloned — update remote URL (in case creds changed) then fetch
        try:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                cwd=repo_dir, env=env, capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "fetch", "--all", "--prune"],
                cwd=repo_dir, env=env, capture_output=True, timeout=30,
            )
            return True, ""
        except subprocess.TimeoutExpired:
            return True, ""  # stale but usable
        except Exception as e:
            logger.warning(f"git fetch failed for {git_url}: {e}")
            return True, ""  # stale but usable

    # Fresh clone
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "clone", "--depth=1", remote_url, str(repo_dir)],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return False, f"git clone failed: {result.stderr.strip()}"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "git clone timeout (60s)"
    except Exception as e:
        return False, f"git clone error: {e}"


def _grep_in_repo(repo_dir: Path, query: str, max_results: int) -> List[Dict[str, Any]]:
    """Run ripgrep or fallback grep. Returns list of {file, line, content}."""
    results = []
    include_args = []
    for ext in ("*.py", "*.md", "*.txt", "*.json", "*.yaml", "*.yml"):
        include_args.extend(["--include", ext])

    # Prefer ripgrep if available, fallback to grep -rn
    try:
        cmd = ["grep", "-rn", "-C", "2", "--text"] + include_args + [query, str(repo_dir)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        output = proc.stdout
    except subprocess.TimeoutExpired:
        return [{"error": "grep timeout"}]
    except FileNotFoundError:
        return [{"error": "grep not found"}]
    except Exception as e:
        return [{"error": str(e)}]

    for line in output.splitlines():
        if len(results) >= max_results:
            break
        # Format: filepath:linenum:content
        parts = line.split(":", 2)
        if len(parts) >= 3:
            try:
                rel_path = str(Path(parts[0]).relative_to(repo_dir))
            except ValueError:
                rel_path = parts[0]
            results.append({
                "file": rel_path,
                "line": int(parts[1]) if parts[1].isdigit() else 0,
                "content": parts[2].strip()[:200],
            })
    return results


def _git_log(repo_dir: Path, query: str, max_results: int) -> List[Dict[str, Any]]:
    """Run git log --grep. Returns list of {hash, message, date}."""
    results = []
    try:
        cmd = ["git", "log", f"--max-count={max_results}", "--pretty=format:%h|%s|%ai"]
        if query:
            cmd.extend([f"--grep={query}", "-i"])
        proc = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=10)
        for line in proc.stdout.strip().splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) >= 3:
                results.append({"hash": parts[0], "message": parts[1], "date": parts[2]})
            elif len(parts) == 2:
                results.append({"hash": parts[0], "message": parts[1], "date": ""})
    except Exception as e:
        logger.warning(f"git log failed: {e}")
    return results


def resource_repo_context_grep(
    query: str,
    scenario_id: str,
    repo_index: Optional[int] = None,
    max_results_per_repo: int = 20,
    tenant_id: str = "",
) -> dict:
    """在场景关联的 Git 资源库中搜索代码/文档上下文。

    支持 grep 文件搜索和 git log 历史查询。当需要了解场景相关代码结构、
    函数定义、文档说明时调用此工具。
    """
    if not query or not query.strip():
        return {"success": False, "error": "query 不能为空"}
    if not scenario_id:
        return {"success": False, "error": "scenario_id 不能为空"}

    # Load scenario config
    try:
        scenario = ScenarioRepository().find_by_scenario_id(scenario_id)
    except Exception as e:
        return {"success": False, "error": f"查询场景失败: {e}"}
    if not scenario:
        return {"success": False, "error": f"场景不存在: {scenario_id}"}
    if tenant_id and getattr(scenario, "tenant_id", None) and scenario.tenant_id != tenant_id:
        return {"success": False, "error": "租户不匹配"}

    import json as _json
    try:
        config = _json.loads(scenario.config) if scenario.config else {}
    except (_json.JSONDecodeError, TypeError):
        config = {}

    repos = config.get("resource_repos") or []
    if not repos:
        return {
            "success": True,
            "scenario_id": scenario_id,
            "message": "该场景未配置关联资源库（resource_repos）",
            "repos_searched": [],
            "total_repos": 0,
        }

    # Select repos to search
    if repo_index is not None:
        if repo_index < 0 or repo_index >= len(repos):
            return {"success": False, "error": f"repo_index {repo_index} 超出范围（共 {len(repos)} 个资源库）"}
        repos_to_search = [(repo_index, repos[repo_index])]
    else:
        repos_to_search = list(enumerate(repos))

    searched = []
    for idx, repo_info in repos_to_search:
        git_url = (repo_info.get("git_url") or "").strip()
        desc = repo_info.get("description", "")
        username = (repo_info.get("username") or "").strip()
        token = (repo_info.get("token") or "").strip()
        if not git_url:
            searched.append({"index": idx, "git_url": "", "error": "git_url 为空", "grep_matches": [], "git_log": [], "total_matches": 0})
            continue

        cache_dir = _cache_dir_for(tenant_id or "_default", git_url, username, token)
        ok, err = _ensure_cloned(git_url, cache_dir, username, token)
        repo_dir = cache_dir / "repo"

        entry = {"index": idx, "git_url": git_url, "description": desc}
        if not ok:
            entry["error"] = err
            entry["grep_matches"] = []
            entry["git_log"] = []
            entry["total_matches"] = 0
            searched.append(entry)
            continue

        grep_matches = _grep_in_repo(repo_dir, query.strip(), max_results_per_repo) if repo_dir.exists() else []
        git_log = _git_log(repo_dir, query.strip(), min(max_results_per_repo, 20)) if repo_dir.exists() else []

        entry["grep_matches"] = grep_matches
        entry["git_log"] = git_log
        entry["total_matches"] = len(grep_matches)
        searched.append(entry)

    return {
        "success": True,
        "scenario_id": scenario_id,
        "repos_searched": searched,
        "total_repos": len(searched),
    }


def cleanup_resource_repo_cache(tenant_id: str = None) -> None:
    """清理指定租户的缓存仓库。场景释放时调用。"""
    if not tenant_id:
        return
    target = _CACHE_ROOT / tenant_id
    if target.exists():
        try:
            shutil.rmtree(target)
            logger.info(f"Cleaned resource repo cache for tenant {tenant_id}")
        except Exception as e:
            logger.warning(f"Failed to clean repo cache for tenant {tenant_id}: {e}")


# ── 注册 ──────────────────────────────────────────────────────────────────────

tool_registry.register(
    name="resource_repo_context_grep",
    func=resource_repo_context_grep,
    description="在场景关联的 Git 资源库中搜索代码/文档上下文。支持 grep 文件搜索和 git log 历史查询。"
                "当需要了解场景相关代码结构、函数定义、文档说明时调用此工具。",
    parameters={
        "query": {"type": "string", "description": "搜索关键词或正则表达式"},
        "scenario_id": {"type": "string", "description": "场景 ID（用于获取关联资源库列表）"},
        "repo_index": {"type": "integer", "description": "指定搜索第几个资源库（0-based），省略则搜索全部", "required": False},
        "max_results_per_repo": {"type": "integer", "description": "每个资源库最多返回匹配数（默认 20）", "required": False},
    },
)
