#!/usr/bin/env python3
"""
Bridge process between Sublime Text (Python 3.8) and Claude Agent SDK (Python 3.10+).
Communicates via JSON-RPC over stdio.
"""
import asyncio
import json
import os
import sys
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

# Import shared utilities
sys.path.insert(0, str(Path(__file__).parent.parent))
from settings import load_project_settings
from logger import get_bridge_logger, ContextLogger
from constants import BRIDGE_BUFFER_SIZE

# Import notalone2 client for daemon-based notifications
# notalone2 client removed - using global client in plugin instead


# Initialize logger
_logger = get_bridge_logger()

# Set env var so child processes (bash commands) can detect they're running under Claude agent
os.environ["CLAUDE_AGENT"] = "1"

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AgentDefinition,
    AssistantMessage,
    UserMessage,
    SystemMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    PermissionResultAllow,
    PermissionResultDeny,
)


def serialize(obj: Any) -> Any:
    """Serialize SDK objects to JSON-compatible dicts."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    return obj


def send(msg: dict) -> None:
    """Send JSON message to stdout."""
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def send_error(id: int | None, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})


def send_result(id: int, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": id, "result": result})


def send_notification(method: str, params: Any) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


class Bridge:
    def __init__(self):
        self.client: ClaudeSDKClient | None = None
        self.options: ClaudeAgentOptions | None = None
        self.running = True
        self.current_task: asyncio.Task | None = None
        self.pending_permissions: dict[int, asyncio.Future] = {}
        self.pending_questions: dict[int, asyncio.Future] = {}  # For AskUserQuestion
        self.permission_id = 0
        self.question_id = 0
        self.interrupted = False  # Set by interrupt(), checked by query()
        self.query_id: int | None = None  # Track active query for inject_message
        self.cwd: str | None = None  # Current working directory (set by initialize)

        # Queue for injected prompts that arrive when query completes
        self.pending_injects: list[str] = []

        # Notification system (notalone2)
        # notalone handled by global client in plugin

    async def handle_request(self, req: dict) -> None:
        id = req.get("id")
        method = req.get("method", "")
        params = req.get("params", {})

        try:
            if method == "initialize":
                await self.initialize(id, params)
            elif method == "query":
                await self.query(id, params)
            elif method == "interrupt":
                await self.interrupt(id)
            elif method == "shutdown":
                await self.shutdown(id)
            elif method == "permission_response":
                await self.handle_permission_response(id, params)
            elif method == "question_response":
                await self.handle_question_response(id, params)
            elif method == "cancel_pending":
                await self.cancel_pending(id)
            elif method == "inject_message":
                await self.inject_message(id, params)
            elif method == "get_history":
                await self.get_history(id)
            elif method == "register_notification":
                result = await self.register_notification(
                    notification_type=params.get("notification_type"),
                    params=params.get("params", {}),
                    wake_prompt=params.get("wake_prompt"),
                    notification_id=params.get("notification_id")
                )
                send_result(id, result)
            elif method == "signal_subsession_complete":
                result = await self.signal_subsession_complete(
                    subsession_id=None,  # Will use self._subsession_id
                    result_summary=params.get("result_summary")
                )
                send_result(id, result)
            elif method == "subsession_complete":
                # Notification: no response needed
                subsession_id = params.get("subsession_id")
                if subsession_id:
                    await self.signal_subsession_complete(subsession_id)
            elif method == "list_notifications":
                result = await self.list_notifications()
                send_result(id, result)
            elif method == "discover_services":
                result = await self.discover_services()
                send_result(id, result)
            else:
                send_error(id, -32601, f"Method not found: {method}")
        except Exception as e:
            send_error(id, -32000, str(e))

    async def initialize(self, id: int, params: dict) -> None:
        """Initialize the Claude SDK client."""
        resume_id = params.get("resume")
        fork_session = params.get("fork_session", False)
        cwd = params.get("cwd")
        view_id = params.get("view_id")
        self.cwd = cwd  # Store for later use (e.g., in can_use_tool)
        self._view_id = view_id  # Store for spawn_session to pass to subsessions

        # Use resume_id if resuming, otherwise use view_id as local session identifier
        session_id = resume_id or view_id or str(uuid.uuid4())
        self._session_id = session_id

        # notalone2 handled by global client in plugin (not per-bridge)

        # Change to project directory so SDK finds CLAUDE.md etc.
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)

        # Load MCP servers, agents, and plugins from project settings
        mcp_servers = self._load_mcp_servers(cwd)
        agents = self._load_agents(cwd)
        plugins = self._load_plugins(cwd)
        settings = load_project_settings(cwd)

        # Load kanban base URL for notalone remote notifications
        self.kanban_base_url = settings.get("kanban_base_url", "http://localhost:5050")
        _logger.info(f"Kanban base URL: {self.kanban_base_url}")

        _logger.info(f"initialize: params={params}")
        _logger.info(f"  resume_id={resume_id}, fork={fork_session}, cwd={cwd}, actual_cwd={os.getcwd()}")
        _logger.info(f"  mcp_servers={list(mcp_servers.keys()) if mcp_servers else None}")
        _logger.info(f"  agents={list(agents.keys()) if agents else None}")
        _logger.info(f"  plugins={plugins}")

        # Build system prompt with project addon
        system_prompt = params.get("system_prompt", "")
        addon = settings.get("system_prompt_addon")
        if addon:
            system_prompt = (system_prompt + "\n\n" + addon) if system_prompt else addon

        # Add session info to system prompt
        session_id_info = f"sublime.{session_id}"
        view_id_info = view_id or session_id
        session_guide = f"""

## Session Info

Session ID: {session_id_info}
View ID: {view_id_info}
"""
        system_prompt = (system_prompt + session_guide) if system_prompt else session_guide

        # If this is a subsession, store subsession_id and add specific guidance
        subsession_id = params.get("subsession_id")
        self._subsession_id = subsession_id  # Store for signal_complete tool
        if subsession_id:
            subsession_guide = f"""
You are subsession **{subsession_id}**. Call signal_complete(session_id={view_id_info}, result_summary="...") when done.
"""
            system_prompt += subsession_guide

        options_dict = {
            "allowed_tools": params.get("allowed_tools", []),
            "permission_mode": params.get("permission_mode", "default"),
            "cwd": cwd,
            "system_prompt": system_prompt,
            "can_use_tool": self.can_use_tool,
            "resume": resume_id,
            "fork_session": fork_session,
            "setting_sources": ["project"],
            "max_buffer_size": 100 * 1024 * 1024,  # 100MB for large images/files
            "cli_path": "claude",
        }

        # Profile config: model and betas
        if params.get("model"):
            options_dict["model"] = params["model"]
        if params.get("betas"):
            options_dict["betas"] = params["betas"]

        # Sandbox settings from project config
        sandbox = self._load_sandbox_settings(cwd)
        if sandbox:
            options_dict["sandbox"] = sandbox
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  sandbox enabled: {sandbox}\n")

        # Add MCP servers if found
        if mcp_servers:
            options_dict["mcp_servers"] = mcp_servers

        # Add agents if found
        if agents:
            options_dict["agents"] = agents

        # Add plugins if found
        if plugins:
            options_dict["plugins"] = plugins

        self.options = ClaudeAgentOptions(**options_dict)
        self.client = ClaudeSDKClient(options=self.options)

        try:
            await self.client.connect()
        except Exception as e:
            error_msg = str(e)

            # If session not found or command failed during resume, retry without resume
            # The SDK wraps the actual error, so we check for common patterns
            is_session_error = (
                "No conversation found" in error_msg or
                ("Command failed" in error_msg and resume_id)
            )
            if is_session_error and resume_id:
                options_dict["resume"] = None
                options_dict["fork_session"] = False
                self.options = ClaudeAgentOptions(**options_dict)
                self.client = ClaudeSDKClient(options=self.options)
                await self.client.connect()
            else:
                raise

        send_result(id, {
            "status": "initialized",
            "mcp_servers": list(mcp_servers.keys()) if mcp_servers else [],
            "agents": list(agents.keys()) if agents else [],
        })


    def _load_mcp_servers(self, cwd: str) -> dict:
        """Load MCP server config from project settings, plus built-in sublime server."""
        settings = load_project_settings(cwd)
        servers = settings.get("mcpServers", {})

        # Always include the built-in sublime MCP server
        # Find the mcp/server.py relative to this bridge script
        bridge_dir = os.path.dirname(os.path.abspath(__file__))
        plugin_dir = os.path.dirname(bridge_dir)
        mcp_server_path = os.path.join(plugin_dir, "mcp", "server.py")

        if os.path.exists(mcp_server_path) and "sublime" not in servers:
            # Pass view_id so MCP server can inject it into spawn_session calls
            view_id_arg = f"--view-id={self._view_id}" if self._view_id else ""
            servers["sublime"] = {
                "command": sys.executable,  # Use same python as bridge
                "args": [mcp_server_path, view_id_arg] if view_id_arg else [mcp_server_path]
            }

        if servers:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  loaded MCP servers: {list(servers.keys())}\n")
        return servers

    def _load_sandbox_settings(self, cwd: str) -> dict:
        """Load sandbox settings from project config."""
        settings = load_project_settings(cwd)
        sandbox_config = settings.get("sandbox", {})

        if not sandbox_config.get("enabled"):
            return None

        sandbox = {
            "enabled": True,
            "auto_allow_bash_if_sandboxed": sandbox_config.get("autoAllowBashIfSandboxed", False),
        }

        # Excluded commands (bypass sandbox)
        if "excludedCommands" in sandbox_config:
            sandbox["excluded_commands"] = sandbox_config["excludedCommands"]

        # Allow model to request unsandboxed execution
        if sandbox_config.get("allowUnsandboxedCommands"):
            sandbox["allow_unsandboxed_commands"] = True

        # Network settings
        network = sandbox_config.get("network", {})
        if network:
            sandbox["network"] = {}
            if network.get("allowLocalBinding"):
                sandbox["network"]["allow_local_binding"] = True
            if network.get("allowUnixSockets"):
                sandbox["network"]["allow_unix_sockets"] = network["allowUnixSockets"]
            if network.get("allowAllUnixSockets"):
                sandbox["network"]["allow_all_unix_sockets"] = True

        return sandbox

    def _load_agents(self, cwd: str) -> dict:
        """Load subagent definitions from project settings, plus built-in agents."""
        settings = load_project_settings(cwd)
        project_agents = settings.get("agents", {})

        # Built-in agents (can be overridden by project settings)
        # NOTE: Removed blackboard-based agents (planner, reporter) - use custom agents if needed
        builtin = {}

        # Merge: project agents override built-ins
        merged = {**builtin, **project_agents}

        # Convert dicts to AgentDefinition objects
        agents = {}
        for name, config in merged.items():
            agents[name] = AgentDefinition(
                description=config.get("description", ""),
                prompt=config.get("prompt", ""),
                tools=config.get("tools"),
                model=config.get("model"),
            )

        if agents:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  loaded agents: {list(agents.keys())}\n")
        return agents

    def _load_plugins(self, cwd: str) -> list:
        """Load plugin configurations from project settings."""
        settings = load_project_settings(cwd)

        # Manual plugins from "plugins" key
        manual_plugins = settings.get("plugins", [])

        # Auto-installed plugins from marketplaces
        auto_plugins = self._load_marketplace_plugins(settings, cwd)

        # Combine both
        all_plugins = manual_plugins + auto_plugins

        # Resolve relative paths to absolute
        if all_plugins and cwd:
            resolved_plugins = []
            for plugin in all_plugins:
                if plugin.get("type") == "local":
                    path = plugin.get("path", "")
                    # Convert relative paths to absolute
                    if path and not os.path.isabs(path):
                        path = os.path.join(cwd, path)
                    resolved_plugins.append({"type": "local", "path": path})
                else:
                    resolved_plugins.append(plugin)
            all_plugins = resolved_plugins

        if all_plugins:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  loaded plugins: {[p.get('path') for p in all_plugins]}\n")
        return all_plugins

    def _load_marketplace_plugins(self, settings: dict, cwd: str) -> list:
        """Load and auto-install plugins from configured marketplaces."""
        import subprocess
        from pathlib import Path

        marketplaces = settings.get("extraKnownMarketplaces", {})
        enabled_plugins = settings.get("enabledPlugins", {})

        if not enabled_plugins:
            return []

        # Plugin cache directory
        cache_dir = Path.home() / ".claude" / "plugins"
        cache_dir.mkdir(parents=True, exist_ok=True)

        plugins = []

        for plugin_key, plugin_config in enabled_plugins.items():
            # Parse plugin format: either "plugin@marketplace" or old object format
            if "@" in plugin_key:
                # New format: "plugin@marketplace": true|false|array
                plugin_name, marketplace_name = plugin_key.split("@", 1)
                # Check if enabled (can be bool, array, or object)
                if isinstance(plugin_config, bool) and not plugin_config:
                    continue
            else:
                # Old format: "plugin": {enabled: true, marketplace: "name"}
                plugin_name = plugin_key
                if isinstance(plugin_config, dict):
                    if not plugin_config.get("enabled", True):
                        continue
                    marketplace_name = plugin_config.get("marketplace")
                else:
                    # Skip if not a dict in old format
                    continue

            if not marketplace_name or marketplace_name not in marketplaces:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  plugin {plugin_name}: marketplace '{marketplace_name}' not found\n")
                continue

            marketplace = marketplaces[marketplace_name]
            source = marketplace.get("source", {})
            source_type = source.get("source", "")

            # Clone marketplace if not exists
            marketplace_dir = cache_dir / marketplace_name

            try:
                if not marketplace_dir.exists():
                    if source_type == "github":
                        repo = source.get("repo", "")
                        if repo:
                            url = f"https://github.com/{repo}.git"
                            with open("/tmp/claude_bridge.log", "a") as f:
                                f.write(f"  cloning marketplace: {url}\n")
                            subprocess.run(["git", "clone", "--depth", "1", url, str(marketplace_dir)],
                                         check=True, capture_output=True)

                    elif source_type == "git":
                        url = source.get("url", "")
                        if url:
                            with open("/tmp/claude_bridge.log", "a") as f:
                                f.write(f"  cloning from git: {url}\n")
                            subprocess.run(["git", "clone", "--depth", "1", url, str(marketplace_dir)],
                                         check=True, capture_output=True)

            except Exception as e:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  error cloning {marketplace_name}: {e}\n")
                continue

            # Determine plugin location
            # First check if there's a marketplace.json
            marketplace_json_path = marketplace_dir / ".claude-plugin" / "marketplace.json"
            plugin_dir = None

            if marketplace_json_path.exists():
                # Read marketplace.json to find plugin location
                try:
                    with open(marketplace_json_path, "r") as f:
                        marketplace_data = json.load(f)
                        plugins_list = marketplace_data.get("plugins", [])
                        # Find the plugin in the marketplace
                        for p in plugins_list:
                            if p.get("name") == plugin_name or p.get("id") == plugin_name:
                                # Plugin found in marketplace
                                plugin_path = p.get("path", plugin_name)
                                plugin_dir = marketplace_dir / plugin_path
                                break
                        if not plugin_dir:
                            with open("/tmp/claude_bridge.log", "a") as f:
                                f.write(f"  plugin {plugin_name} not in marketplace.json\n")
                except Exception as e:
                    with open("/tmp/claude_bridge.log", "a") as f:
                        f.write(f"  error reading marketplace.json: {e}\n")

            # Fallback: try common plugin locations if marketplace.json not found or plugin not listed
            if not plugin_dir or not plugin_dir.exists():
                # Case 1: The marketplace repo itself is the plugin
                if (marketplace_dir / ".claude-plugin" / "plugin.json").exists():
                    plugin_dir = marketplace_dir
                # Case 2: Plugin in subdirectory (for backward compatibility with subdir field)
                elif "subdir" in source:
                    base_dir = marketplace_dir / source["subdir"]
                    plugin_dir = base_dir / plugin_name
                # Case 3: Plugin directly in marketplace directory
                else:
                    plugin_dir = marketplace_dir / plugin_name

            # Add to plugins list if valid
            if plugin_dir and plugin_dir.exists() and (plugin_dir / ".claude-plugin" / "plugin.json").exists():
                plugins.append({"type": "local", "path": str(plugin_dir)})
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  loaded marketplace plugin: {plugin_name} from {plugin_dir}\n")
            else:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  plugin {plugin_name} not found (checked {plugin_dir})\n")

        return plugins

    def _parse_permission_pattern(self, pattern: str) -> tuple[str, str | None]:
        """Parse permission pattern into (tool_name, specifier).

        Formats:
            "Bash" -> ("Bash", None)
            "Bash(git:*)" -> ("Bash", "git:*")
            "Read(/src/**)" -> ("Read", "/src/**")
        """
        if '(' in pattern and pattern.endswith(')'):
            paren_idx = pattern.index('(')
            tool_name = pattern[:paren_idx]
            specifier = pattern[paren_idx + 1:-1]
            return tool_name, specifier
        return pattern, None

    def _match_permission_pattern(self, tool_name: str, tool_input: dict, pattern: str) -> bool:
        """Check if tool use matches a permission pattern.

        Supports:
            - Simple tool match: "Bash" matches any Bash command
            - Prefix match: "Bash(git:*)" matches commands starting with "git"
            - Exact match: "Bash(git status)" matches exactly "git status"
            - Glob match: "Read(/src/**/*.py)" matches files under /src/ ending in .py
        """
        import fnmatch

        parsed_tool, specifier = self._parse_permission_pattern(pattern)

        # Tool name must match (supports wildcards like mcp__*__)
        if not fnmatch.fnmatch(tool_name, parsed_tool):
            return False

        # No specifier = match all uses of this tool
        if specifier is None:
            return True

        # Get the value to match against based on tool type
        match_value = None
        if tool_name == "Bash":
            match_value = tool_input.get("command", "")
        elif tool_name in ("Read", "Write", "Edit"):
            match_value = tool_input.get("file_path", "")
        elif tool_name in ("Glob", "Grep"):
            match_value = tool_input.get("pattern", "")
        elif tool_name == "WebFetch":
            match_value = tool_input.get("url", "")
        else:
            # For other tools, try common field names
            match_value = tool_input.get("command") or tool_input.get("path") or tool_input.get("query", "")

        if not match_value:
            return False

        # Handle prefix match with :* suffix (like Claude Code)
        if specifier.endswith(":*"):
            prefix = specifier[:-2]
            return match_value.startswith(prefix)

        # Handle glob/fnmatch patterns
        if any(c in specifier for c in ['*', '?', '[']):
            return fnmatch.fnmatch(match_value, specifier)

        # Exact match
        return match_value == specifier

    def _validate_bash_command(self, command: str) -> tuple[bool, str]:
        """Validate bash command for dangerous patterns.

        Returns: (is_safe, warning_message)
        """
        import re

        # Check for rm -rf with potentially dangerous paths
        rm_pattern = r'\brm\s+(-[rf]{1,2}\s+|-[a-z]*[rf][a-z]*\s+)'
        if re.search(rm_pattern, command):
            # Extract the path being deleted
            # Match: rm -rf <path> or rm -f -r <path>, etc.
            path_match = re.search(rm_pattern + r'([^\s;&|]+)', command)
            if path_match:
                path = path_match.group(2)

                # Dangerous: relative paths that could delete parent dirs
                if '..' in path:
                    return False, f"Dangerous rm command with parent directory reference: {path}"

                # Dangerous: deleting from root or home
                if path.startswith('/') and path.count('/') <= 3:
                    return False, f"Dangerous rm command targeting high-level directory: {path}"

                # Dangerous: wildcards in critical locations
                if '*' in path and path.count('/') <= 4:
                    return False, f"Dangerous rm command with wildcards in shallow path: {path}"

                # Check for deletion of entire project directories
                critical_dirs = ['node', 'src', 'lib', 'app', 'dist', 'build']
                path_parts = path.rstrip('/').split('/')
                if path_parts and path_parts[-1] in critical_dirs and '/' not in path:
                    return False, f"Dangerous: attempting to delete entire '{path_parts[-1]}' directory"

        return True, ""

    async def can_use_tool(self, tool_name: str, tool_input: dict, context=None):
        """Handle permission request - ask Sublime for approval."""
        # Handle AskUserQuestion - show UI and collect answers
        if tool_name == "AskUserQuestion":
            return await self._handle_ask_user_question(tool_input)

        # Auto-allow built-in sublime MCP tools
        if tool_name.startswith("mcp__sublime__"):
            return PermissionResultAllow(updated_input=tool_input)

        # Validate Bash commands for dangerous patterns
        if tool_name == "Bash" and "command" in tool_input:
            is_safe, warning = self._validate_bash_command(tool_input["command"])
            if not is_safe:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"BLOCKED dangerous Bash command: {warning}\n")
                    f.write(f"  Command: {tool_input['command']}\n")
                return PermissionResultDeny(message=f"Blocked dangerous command: {warning}")

        # Check auto-allowed tools from settings
        settings = load_project_settings(self.cwd)
        auto_allowed = settings.get("autoAllowedMcpTools", [])

        # Check if tool matches any auto-allow pattern (supports fine-grained patterns)
        for pattern in auto_allowed:
            if self._match_permission_pattern(tool_name, tool_input, pattern):
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"can_use_tool: auto-allowed {tool_name} (matched pattern: {pattern})\n")
                return PermissionResultAllow(updated_input=tool_input)

        self.permission_id += 1
        pid = self.permission_id

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"can_use_tool: tool={tool_name}, pid={pid}, input={str(tool_input)[:100]}\n")

        # Create a future to wait for the response
        future = asyncio.get_event_loop().create_future()
        self.pending_permissions[pid] = future

        # Send permission request to Sublime
        send_notification("permission_request", {
            "id": pid,
            "tool": tool_name,
            "input": tool_input,
        })

        # Wait for response from Sublime
        try:
            allowed = await asyncio.wait_for(future, timeout=3600)  # 1 hour timeout
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"can_use_tool returning: pid={pid}, allowed={allowed}\n")
            if allowed:
                return PermissionResultAllow(updated_input=tool_input)
            else:
                return PermissionResultDeny(message="User denied permission")
        except asyncio.TimeoutError:
            return PermissionResultDeny(message="Permission request timed out")
        finally:
            self.pending_permissions.pop(pid, None)

    async def handle_permission_response(self, id: int, params: dict) -> None:
        """Handle permission response from Sublime."""
        pid = params.get("id")
        allow = params.get("allow", False)

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"permission_response: pid={pid}, allow={allow}\n")

        if pid in self.pending_permissions:
            future = self.pending_permissions[pid]
            future.set_result(allow)
        else:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  -> WARNING: pid {pid} not found in pending!\n")

        send_result(id, {"status": "ok"})

    async def _handle_ask_user_question(self, tool_input: dict):
        """Handle AskUserQuestion tool - show UI and collect answers."""
        questions = tool_input.get("questions", [])
        if not questions:
            return PermissionResultAllow(updated_input=tool_input)

        self.permission_id += 1
        qid = self.permission_id

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"AskUserQuestion: qid={qid}, questions={len(questions)}\n")

        future = asyncio.get_event_loop().create_future()
        self.pending_questions[qid] = future

        send_notification("question_request", {
            "id": qid,
            "questions": questions,
        })

        try:
            answers = await asyncio.wait_for(future, timeout=300)
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"AskUserQuestion response: qid={qid}, answers={answers}\n")

            if answers is None:
                return PermissionResultDeny(message="User cancelled")

            updated_input = {"questions": questions, "answers": answers}
            return PermissionResultAllow(updated_input=updated_input)
        except asyncio.TimeoutError:
            return PermissionResultDeny(message="Question timed out")
        finally:
            self.pending_questions.pop(qid, None)

    async def handle_question_response(self, id: int, params: dict) -> None:
        """Handle question response from Sublime."""
        qid = params.get("id")
        answers = params.get("answers")

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"question_response: qid={qid}, answers={answers}\n")

        if qid in self.pending_questions:
            self.pending_questions[qid].set_result(answers)
        else:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  -> WARNING: qid {qid} not found!\n")

        send_result(id, {"status": "ok"})

    async def query(self, id: int, params: dict) -> None:
        """Send a query and stream responses."""
        if not self.client:
            send_error(id, -32002, "Not initialized")
            return

        prompt = params.get("prompt", "")
        self.interrupted = False  # Reset at start of query
        self.query_id = id  # Store for inject_message to know query is active

        async def run_query():
            # Send initial prompt
            await self.client.query(prompt)
            # Stream responses
            async for message in self.client.receive_response():
                await self.emit_message(message)
            # Check if we were interrupted (set by interrupt() method)
            status = "interrupted" if self.interrupted else "complete"
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"query complete: status={status}\n")
            send_result(id, {"status": status})

        self.current_task = asyncio.create_task(run_query())
        try:
            await self.current_task
        except asyncio.CancelledError:
            send_result(id, {"status": "interrupted"})
        except Exception as e:
            error_msg = str(e)
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"query error: {error_msg}\n")
            # Check for session-related errors
            is_session_error = (
                "No conversation found" in error_msg or
                "Command failed" in error_msg or
                "exit code" in error_msg
            )
            if is_session_error:
                send_error(id, -32003, f"Session error: {error_msg}. Try restarting the session.")
            else:
                send_error(id, -32000, f"Query failed: {error_msg}")
        finally:
            self.query_id = None
            # Process any pending injects that arrived during query
            if self.pending_injects:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"query ended with {len(self.pending_injects)} pending injects\n")
                # Send notification to Sublime to submit the queued prompts
                for inject in self.pending_injects:
                    send_notification("queued_inject", {"message": inject})
                self.pending_injects.clear()

    async def emit_message(self, message: Any) -> None:
        """Emit a message notification."""
        msg_type = type(message).__name__
        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"emit_message: type={msg_type}\n")

        if isinstance(message, AssistantMessage):
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  blocks: {[type(b).__name__ for b in message.content]}\n")
            for block in message.content:
                if isinstance(block, TextBlock):
                    send_notification("message", {
                        "type": "text",
                        "text": block.text,
                    })
                elif isinstance(block, ToolUseBlock):
                    send_notification("message", {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })
                elif isinstance(block, ToolResultBlock):
                    with open("/tmp/claude_bridge.log", "a") as f:
                        f.write(f"tool_result: id={block.tool_use_id}, is_error={block.is_error}, content={str(block.content)[:200]}\n")
                    send_notification("message", {
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                        "is_error": block.is_error,
                    })
                elif isinstance(block, ThinkingBlock):
                    send_notification("message", {
                        "type": "thinking",
                        "thinking": block.thinking,
                    })
        elif isinstance(message, UserMessage):
            # UserMessage contains tool results
            content = message.content
            if isinstance(content, list):
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  UserMessage blocks: {[type(b).__name__ for b in content]}\n")
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        with open("/tmp/claude_bridge.log", "a") as f:
                            f.write(f"tool_result: id={block.tool_use_id}, is_error={block.is_error}\n")
                        send_notification("message", {
                            "type": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "content": block.content if hasattr(block, 'content') else None,
                            "is_error": block.is_error,
                        })
        elif isinstance(message, ResultMessage):
            send_notification("message", {
                "type": "result",
                "session_id": message.session_id,
                "duration_ms": message.duration_ms,
                "is_error": message.is_error,
                "num_turns": message.num_turns,
                "total_cost_usd": message.total_cost_usd,
            })
        elif isinstance(message, SystemMessage):
            send_notification("message", {
                "type": "system",
                "subtype": message.subtype,
                "data": message.data,
            })

    async def interrupt(self, id: int) -> None:
        """Interrupt current query and drain pending messages."""
        if self.current_task and not self.current_task.done():
            self.interrupted = True  # Signal to query() that we were interrupted
            await self.client.interrupt()
            # Cancel any pending permission requests
            for pid, future in list(self.pending_permissions.items()):
                if not future.done():
                    future.set_result(False)  # Deny pending permissions
            self.pending_permissions.clear()
            # Don't cancel task - let it drain naturally after interrupt
            # Wait for the task to complete (it should finish quickly after interrupt)
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"interrupt: waiting for task to drain\n")
            try:
                await asyncio.wait_for(self.current_task, timeout=5.0)
            except asyncio.TimeoutError:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"interrupt: drain timeout, cancelling\n")
                self.current_task.cancel()
                try:
                    await self.current_task
                except asyncio.CancelledError:
                    pass
            except Exception as e:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"interrupt: drain error: {e}\n")
        send_result(id, {"status": "interrupted"})

    async def cancel_pending(self, id: int) -> None:
        """Cancel all pending permission/question requests."""
        count = 0
        for pid, future in list(self.pending_permissions.items()):
            if not future.done():
                future.set_result(False)  # Deny
                count += 1
        self.pending_permissions.clear()

        for qid, future in list(self.pending_questions.items()):
            if not future.done():
                future.set_result(None)  # Cancel
                count += 1
        self.pending_questions.clear()

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"cancel_pending: cancelled {count} requests\n")
        send_result(id, {"status": "ok", "cancelled": count})

    # _on_notalone_inject removed - handled by global client in plugin

    async def inject_message(self, id: int, params: dict) -> None:
        """Inject a user message into the current conversation."""
        message = params.get("message", "")
        if not message:
            send_error(id, -32602, "Missing message parameter")
            return

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"inject_message: {message[:60]}...\n")

        # If no active query, queue the message to be sent when next query starts
        if not self.query_id:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  no active query, queuing inject\n")
            self.pending_injects.append(message)
            send_result(id, {"status": "queued"})
            return

        # Try to inject immediately
        try:
            await self.client.query(message)
            send_result(id, {"status": "ok"})
        except Exception as e:
            # If injection fails (e.g., query completed), queue it
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  inject failed: {e}, queuing\n")
            self.pending_injects.append(message)
            send_result(id, {"status": "queued"})

    async def get_history(self, id: int) -> None:
        """Get conversation history from the SDK."""
        if not self.client:
            send_error(id, -32002, "Client not initialized")
            return

        try:
            # Try to access SDK's internal conversation state
            # The SDK stores messages internally for context
            messages = []

            # Check if client has a messages/history attribute
            if hasattr(self.client, '_messages'):
                messages = serialize(self.client._messages)
            elif hasattr(self.client, 'messages'):
                messages = serialize(self.client.messages)
            elif hasattr(self.client, 'conversation'):
                messages = serialize(self.client.conversation)
            else:
                # Fallback: return what we know
                send_result(id, {
                    "messages": [],
                    "note": "SDK conversation history not accessible via standard API"
                })
                return

            send_result(id, {"messages": messages})
        except Exception as e:
            send_error(id, -32000, f"Failed to get history: {str(e)}")

    # ─── Subsession signaling ────────────────────────────────────────────
    # Notification registration handled by MCP tools directly to daemon

    async def signal_subsession_complete(self, subsession_id: str = None, result_summary: str = None) -> dict:
        """Signal that a subsession has completed (direct socket to daemon)."""
        import socket
        from pathlib import Path

        if subsession_id is None:
            subsession_id = getattr(self, '_subsession_id', None)

        if not subsession_id:
            return {"error": "Not a subsession - no subsession_id available"}

        socket_path = str(Path.home() / ".notalone" / "notalone.sock")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(socket_path)
            sock.sendall((json.dumps({
                "method": "signal_complete",
                "subsession_id": subsession_id,
                "result_summary": result_summary
            }) + "\n").encode())

            data = b""
            while b"\n" not in data:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk

            sock.close()
            resp = json.loads(data.decode().strip())
            success = resp.get("ok", False)
            _logger.info(f"Subsession {subsession_id} completed - signaled: {success}")
            return {
                "status": "signaled" if success else "failed",
                "subsession_id": subsession_id,
                "result_summary": result_summary
            }
        except Exception as e:
            _logger.error(f"Error signaling subsession complete: {e}")
            return {"error": str(e)}

    async def shutdown(self, id: int) -> None:
        """Shutdown the bridge."""
        if self.client:
            await self.client.disconnect()

        send_result(id, {"status": "shutdown"})
        self.running = False

    async def run(self) -> None:
        """Main loop - read JSON-RPC from stdin."""
        # Immediate startup log
        sys.stderr.write("=== BRIDGE STARTING WITH 1GB BUFFER ===\n")
        sys.stderr.flush()

        loop = asyncio.get_event_loop()
        # Increase buffer limit to 1GB to handle large tool results (e.g., images)
        buffer_limit = 1024 * 1024 * 1024
        reader = asyncio.StreamReader(limit=buffer_limit, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        # Log to verify this code is running
        with open("/tmp/claude_bridge.log", "a") as f:
            f.write("Bridge started with 1GB buffer limit\n")
        sys.stderr.write(f"=== StreamReader limit set to {reader._limit} bytes ===\n")
        sys.stderr.flush()

        while self.running:
            try:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line.decode())
                # Don't await - handle requests concurrently so permission responses
                # can be processed while a query is running
                asyncio.create_task(self.handle_request(req))
            except asyncio.LimitOverrunError as e:
                send_error(None, -32000, f"Message too large: {e}")
                sys.stderr.write(f"!!! LIMIT OVERRUN ERROR: {e} !!!\n")
                sys.stderr.write(f"!!! Reader limit: {reader._limit} !!!\n")
                sys.stderr.write(f"!!! Error type: {type(e).__name__} !!!\n")
                sys.stderr.flush()
                # Try to consume the rest of the line to recover
                try:
                    await reader.readuntil(b'\n')
                except:
                    pass
            except json.JSONDecodeError as e:
                send_error(None, -32700, f"Parse error: {e}")
                sys.stderr.write(f"Fatal error in message reader: Failed to decode JSON: {e}\n")
                sys.stderr.flush()
            except Exception as e:
                send_error(None, -32000, f"Internal error: {e}")
                sys.stderr.write(f"!!! EXCEPTION TYPE: {type(e).__module__}.{type(e).__name__} !!!\n")
                sys.stderr.write(f"!!! EXCEPTION MESSAGE: {e} !!!\n")
                sys.stderr.write(f"!!! READER LIMIT: {reader._limit} !!!\n")
                sys.stderr.write(f"Fatal error in message reader: {e}\n")
                sys.stderr.flush()
                import traceback
                traceback.print_exc(file=sys.stderr)


async def main():
    bridge = Bridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
