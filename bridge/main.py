#!/usr/bin/env python3
"""
Bridge process between Sublime Text (Python 3.8) and Claude Agent SDK (Python 3.10+).
Communicates via JSON-RPC over stdio.
"""
import asyncio
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

# Import shared utilities
sys.path.insert(0, str(Path(__file__).parent.parent))
from settings import load_project_settings
from logger import get_bridge_logger, ContextLogger
from constants import BRIDGE_BUFFER_SIZE

# Import notalone notification system
from notalone.hub import NotificationHub
from notalone.backends.sublime import SublimeNotificationBackend
from notalone.types import NotificationType, NotificationParams, Notification
from notalone.rpc.integration import RemoteNotificationHub

# Initialize logger
_logger = get_bridge_logger()

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

        # Notification system (notalone)
        self.notification_backend: SublimeNotificationBackend | None = None
        self.notification_hub: RemoteNotificationHub | None = None

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
            elif method == "set_alarm":
                await self.set_alarm(id, params)
            elif method == "cancel_alarm":
                await self.cancel_alarm(id, params)
            elif method == "list_notifications":
                await self.list_notifications(id, params)
            elif method == "watch_ticket":
                await self.watch_ticket(id, params)
            elif method == "subscribe_channel":
                await self.subscribe_channel(id, params)
            elif method == "broadcast_message":
                await self.broadcast_message(id, params)
            elif method == "subsession_complete":
                # Notification: no response needed
                subsession_id = params.get("subsession_id")
                if subsession_id:
                    await self.signal_subsession_complete(subsession_id)
            else:
                send_error(id, -32601, f"Method not found: {method}")
        except Exception as e:
            send_error(id, -32000, str(e))

    async def initialize(self, id: int, params: dict) -> None:
        """Initialize the Claude SDK client."""
        resume_id = params.get("resume")
        fork_session = params.get("fork_session", False)
        cwd = params.get("cwd")
        self.cwd = cwd  # Store for later use (e.g., in can_use_tool)

        # Initialize notification system with RPC support
        self.notification_backend = SublimeNotificationBackend(
            send_notification=send_notification,
            session_id=resume_id or "new-session"
        )
        local_hub = NotificationHub(self.notification_backend)
        self.notification_hub = RemoteNotificationHub(
            hub=local_hub,
            session_id=resume_id or "new-session",
            rpc_host="localhost",
            rpc_port=0  # Auto-assign port
        )
        await self.notification_hub.start()
        callback_url = self.notification_hub.server.get_callback_url()
        _logger.info(f"Notification system (notalone) initialized with RPC at {callback_url}")

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
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  injected system_prompt_addon: {len(addon)} chars\n")

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
        }

        # Profile config: model and betas
        if params.get("model"):
            options_dict["model"] = params["model"]
        if params.get("betas"):
            # betas must be passed via env var ANTHROPIC_BETAS
            options_dict["env"] = {"ANTHROPIC_BETAS": ",".join(params["betas"])}

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

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"  options.resume={self.options.resume}, options.fork_session={self.options.fork_session}\n")

        self.client = ClaudeSDKClient(options=self.options)

        try:
            await self.client.connect()
        except Exception as e:
            error_msg = str(e)
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  connect error: {error_msg}\n")

            # If session not found or command failed during resume, retry without resume
            # The SDK wraps the actual error, so we check for common patterns
            is_session_error = (
                "No conversation found" in error_msg or
                ("Command failed" in error_msg and resume_id)
            )
            if is_session_error and resume_id:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  retrying without resume (stale session)\n")

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
            servers["sublime"] = {
                "command": sys.executable,  # Use same python as bridge
                "args": [mcp_server_path]
            }

        if servers:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"  loaded MCP servers: {list(servers.keys())}\n")
        return servers

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

        # Check if tool matches any auto-allow pattern
        import fnmatch
        for pattern in auto_allowed:
            if fnmatch.fnmatch(tool_name, pattern):
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

    async def set_alarm(self, id: int, params: dict) -> None:
        """Set an alarm using notalone notification system."""
        if not self.notification_hub:
            send_error(id, -32000, "Notification system not initialized")
            return

        event_type = params.get("event_type")
        if not event_type:
            send_error(id, -32602, "Missing required parameter: event_type")
            return

        wake_prompt = params.get("wake_prompt")
        if not wake_prompt:
            send_error(id, -32602, "Missing required parameter: wake_prompt")
            return

        event_params = params.get("event_params", {})
        alarm_id = params.get("alarm_id")

        # Map event_type to NotificationType
        if event_type == "time_elapsed":
            ntype = NotificationType.TIMER
            nparams = NotificationParams(seconds=event_params.get("seconds", 0))
        elif event_type in ("agent_complete", "subsession_complete"):
            ntype = NotificationType.SUBSESSION_COMPLETE
            nparams = NotificationParams(
                subsession_id=event_params.get("subsession_id"),
                agent_id=event_params.get("agent_id")
            )
        else:
            send_error(id, -32602, f"Unknown event_type: {event_type}")
            return

        # Create notification
        notification = Notification(
            notification_type=ntype,
            params=nparams,
            wake_prompt=wake_prompt
        )
        if alarm_id:
            notification.id = alarm_id

        # Set notification
        result = await self.notification_backend.set_notification(
            notification,
            callback=lambda n: None  # Callback handled by backend's send_notification
        )

        _logger.info(f"Set alarm {result.notification_id}: {event_type}")

        send_result(id, {
            "alarm_id": result.notification_id,
            "status": result.status,
            "event_type": event_type
        })

    async def cancel_alarm(self, id: int, params: dict) -> None:
        """Cancel a pending alarm using notalone."""
        if not self.notification_hub:
            send_error(id, -32000, "Notification system not initialized")
            return

        alarm_id = params.get("alarm_id")
        if not alarm_id:
            send_error(id, -32602, "Missing required parameter: alarm_id")
            return

        result = await self.notification_backend.cancel_notification(alarm_id)

        if result.status == "not_found":
            send_error(id, -32602, f"Alarm not found: {alarm_id}")
        else:
            send_result(id, {"alarm_id": alarm_id, "status": result.status})

    # ─── Notification Tools (notalone API) ────────────────────────────────

    async def list_notifications(self, id: int, params: dict) -> None:
        """List active notifications using notalone."""
        if not self.notification_hub:
            send_error(id, -32000, "Notification system not initialized")
            return

        notifications = await self.notification_backend.list_notifications()
        send_result(id, {
            "notifications": [
                {
                    "id": n.id,
                    "type": n.notification_type.value,
                    "wake_prompt": n.wake_prompt,
                    "params": n.params.__dict__ if n.params else {},
                    "fired": n.fired,
                }
                for n in notifications
            ]
        })

    async def watch_ticket(self, id: int, params: dict) -> None:
        """Watch a ticket for state changes using notalone (always remote to kanban)."""
        if not self.notification_hub:
            send_error(id, -32000, "Notification system not initialized")
            return

        ticket_id = params.get("ticket_id")
        states = params.get("states", [])
        wake_prompt = params.get("wake_prompt")

        if not all([ticket_id is not None, states, wake_prompt]):
            send_error(id, -32602, "Missing required parameters")
            return

        # Tickets always live in kanban - use remote registration
        remote_url = f"{self.kanban_base_url}/notalone/register"

        notification_id = await self.notification_hub.watch_ticket_remote(
            remote_url=remote_url,
            ticket_id=ticket_id,
            states=states,
            wake_prompt=wake_prompt
        )

        send_result(id, {
            "notification_id": notification_id,
            "status": "registered_remote",
            "kanban_url": self.kanban_base_url,
            "ticket_id": ticket_id,
            "states": states
        })

    async def subscribe_channel(self, id: int, params: dict) -> None:
        """Subscribe to a notification channel using notalone."""
        if not self.notification_hub:
            send_error(id, -32000, "Notification system not initialized")
            return

        channel = params.get("channel")
        wake_prompt = params.get("wake_prompt")

        if not all([channel, wake_prompt]):
            send_error(id, -32602, "Missing required parameters")
            return

        result = await self.notification_hub.subscribe(
            session_id=self._session_id,
            channel=channel,
            wake_prompt=wake_prompt
        )

        send_result(id, {
            "notification_id": result.notification_id,
            "status": result.status,
            "channel": channel
        })

    async def broadcast_message(self, id: int, params: dict) -> None:
        """Broadcast a message to channel subscribers using notalone."""
        if not self.notification_hub:
            send_error(id, -32000, "Notification system not initialized")
            return

        message = params.get("message")
        channel = params.get("channel")
        data = params.get("data", {})

        if not message:
            send_error(id, -32602, "Missing required parameter: message")
            return

        count = await self.notification_hub.broadcast(
            message=message,
            channel=channel,
            data=data,
            sender_session_id=self._session_id
        )

        send_result(id, {
            "status": "broadcast_sent",
            "channel": channel or "global",
            "recipients": count,
            "message": message
        })

    async def _monitor_time_alarm(self, alarm_id: str) -> None:
        """Monitor time-based alarm - sleep then fire."""
        alarm = self.alarms.get(alarm_id)
        if not alarm:
            return

        seconds = alarm["event_params"].get("seconds", 0)

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"[Alarm] Monitoring time alarm {alarm_id}: sleep {seconds}s\n")

        try:
            await asyncio.sleep(seconds)
            await self._fire_alarm(alarm_id)
        except asyncio.CancelledError:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"[Alarm] Time alarm {alarm_id} cancelled\n")

    async def _monitor_subsession_alarm(self, alarm_id: str) -> None:
        """Monitor subsession/agent completion - wait on event then fire."""
        alarm = self.alarms.get(alarm_id)
        if not alarm:
            return

        event_type = alarm["event_type"]
        subsession_id = alarm["event_params"].get("subsession_id") or alarm["event_params"].get("agent_id")

        if not subsession_id:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"[Alarm] ERROR: No subsession_id/agent_id in alarm {alarm_id}\n")
            return

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"[Alarm] Monitoring subsession alarm {alarm_id}: waiting for {subsession_id}\n")

        # Create event if doesn't exist
        if subsession_id not in self.subsession_events:
            self.subsession_events[subsession_id] = asyncio.Event()

        event = self.subsession_events[subsession_id]

        try:
            # Wait for subsession completion (with timeout to prevent infinite wait)
            await asyncio.wait_for(event.wait(), timeout=3600)  # 1 hour max
            await self._fire_alarm(alarm_id)
        except asyncio.TimeoutError:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"[Alarm] Subsession alarm {alarm_id} timed out after 1 hour\n")
        except asyncio.CancelledError:
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"[Alarm] Subsession alarm {alarm_id} cancelled\n")

    async def _fire_alarm(self, alarm_id: str) -> None:
        """Fire an alarm by injecting the wake prompt."""
        alarm = self.alarms.pop(alarm_id, None)
        if not alarm:
            return

        # Clean up task
        if alarm_id in self.alarm_tasks:
            del self.alarm_tasks[alarm_id]

        wake_prompt = alarm["wake_prompt"]

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"[Alarm] FIRING alarm {alarm_id}: sending wake notification\n")
            f.write(f"[Alarm] Bridge PID: {os.getpid()}, Event: {alarm['event_type']}\n")

        # Send notification to session to start a new query with the wake_prompt
        # This goes through the session's normal query flow, so the output view updates properly
        send_notification("alarm_wake", {
            "alarm_id": alarm_id,
            "event_type": alarm["event_type"],
            "wake_prompt": wake_prompt
        })

    async def signal_subsession_complete(self, subsession_id: str) -> None:
        """Signal that a subsession has completed using notalone."""
        if self.notification_backend:
            count = await self.notification_backend.signal_session_complete(subsession_id)
            _logger.info(f"Subsession {subsession_id} completed - triggered {count} notifications")

    async def shutdown(self, id: int) -> None:
        """Shutdown the bridge."""
        if self.client:
            await self.client.disconnect()

        # Stop notification system
        if self.notification_hub:
            await self.notification_hub.stop()
            _logger.info("Notification system stopped")

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
