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
from typing import Any

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

        # Alarm system for efficient event-driven waiting
        self.alarms: dict[str, dict] = {}  # alarm_id → {event_type, params, wake_prompt}
        self.alarm_tasks: dict[str, asyncio.Task] = {}  # alarm_id → monitoring task
        self.subsession_events: dict[str, asyncio.Event] = {}  # subsession_id → completion event

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
            elif method == "subsession_complete":
                # Notification: no response needed
                subsession_id = params.get("subsession_id")
                if subsession_id:
                    self.signal_subsession_complete(subsession_id)
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

        # Change to project directory so SDK finds CLAUDE.md etc.
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)

        # Load MCP servers, agents, and plugins from project settings
        mcp_servers = self._load_mcp_servers(cwd)
        agents = self._load_agents(cwd)
        plugins = self._load_plugins(cwd)
        settings = self._load_project_settings(cwd)

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"initialize: params={params}\n")
            f.write(f"  resume_id={resume_id}, fork={fork_session}, cwd={cwd}, actual_cwd={os.getcwd()}\n")
            f.write(f"  mcp_servers={list(mcp_servers.keys()) if mcp_servers else None}\n")
            f.write(f"  agents={list(agents.keys()) if agents else None}\n")
            f.write(f"  plugins={plugins}\n")

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

    def _load_project_settings(self, cwd: str) -> dict:
        """Load and merge user-level and project settings.

        User settings from ~/.claude/settings.json are loaded first,
        then project settings override them.
        """
        from pathlib import Path

        # Start with user-level settings
        user_settings = {}
        user_settings_path = Path.home() / ".claude" / "settings.json"
        if user_settings_path.exists():
            try:
                with open(user_settings_path, "r") as f:
                    user_settings = json.load(f)
                    with open("/tmp/claude_bridge.log", "a") as f:
                        f.write(f"  loaded user settings from {user_settings_path}\n")
            except Exception as e:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  error loading {user_settings_path}: {e}\n")

        if not cwd:
            return user_settings

        # Load project settings
        project_settings = {}

        # Try .claude/settings.json first
        settings_path = os.path.join(cwd, ".claude", "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r") as f:
                    project_settings = json.load(f)
            except Exception as e:
                with open("/tmp/claude_bridge.log", "a") as f:
                    f.write(f"  error loading {settings_path}: {e}\n")

        # Try .mcp.json (MCP servers only)
        if not project_settings:
            mcp_path = os.path.join(cwd, ".mcp.json")
            if os.path.exists(mcp_path):
                try:
                    with open(mcp_path, "r") as f:
                        project_settings = json.load(f)
                except Exception as e:
                    with open("/tmp/claude_bridge.log", "a") as f:
                        f.write(f"  error loading {mcp_path}: {e}\n")

        # Merge settings: project overrides user
        merged = self._merge_settings(user_settings, project_settings)
        return merged

    def _merge_settings(self, user: dict, project: dict) -> dict:
        """Deep merge project settings into user settings."""
        result = user.copy()

        for key, value in project.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                # Deep merge dictionaries
                result[key] = {**result[key], **value}
            else:
                # Project value overrides user value
                result[key] = value

        return result

    def _load_mcp_servers(self, cwd: str) -> dict:
        """Load MCP server config from project settings, plus built-in sublime server."""
        settings = self._load_project_settings(cwd)
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
        settings = self._load_project_settings(cwd)
        project_agents = settings.get("agents", {})

        # Built-in agents (can be overridden by project settings)
        builtin = {
            "planner": {
                "description": "Use at the START of complex tasks to create an implementation plan. Saves plan to blackboard.",
                "prompt": """You are a planning specialist. Create clear implementation plans.

When planning:
1. Break down the task into concrete steps
2. Identify files that need changes
3. Note any risks or dependencies
4. Save plan using bb_write tool with key "plan"

Keep plans concise and actionable. Output the plan, then save it.""",
                "tools": ["Read", "Glob", "Grep", "bb_write", "bb_read"],
                "model": "haiku"
            },
            "reporter": {
                "description": "Use AFTER completing significant work to update the walkthrough/progress report.",
                "prompt": """You are a progress reporter. Update the walkthrough for the user.

1. Use bb_read with key "walkthrough" to get current state
2. Update with: what was completed, current status, next steps
3. Use bb_write with key "walkthrough" to save (markdown format)

Be concise. Focus on what matters to the user.""",
                "tools": ["bb_write", "bb_read", "bb_list"],
                "model": "haiku"
            },
        }

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
        settings = self._load_project_settings(cwd)

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

    async def can_use_tool(self, tool_name: str, tool_input: dict, context=None):
        """Handle permission request - ask Sublime for approval."""
        # Auto-allow built-in sublime MCP tools
        if tool_name.startswith("mcp__sublime__"):
            return PermissionResultAllow(updated_input=tool_input)

        # Check auto-allowed tools from settings
        settings = self._load_project_settings(self.cwd)
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
        """Set an alarm to inject prompt when event occurs.

        Instead of polling for events, session can sleep and wake when event fires.
        The alarm "wakes" the session by injecting the specified wake_prompt.

        Args:
            event_type: "agent_complete", "time_elapsed", "subsession_complete"
            event_params: Event-specific parameters
                - agent_complete: {agent_id: str}
                - time_elapsed: {seconds: int}
                - subsession_complete: {subsession_id: str}
            wake_prompt: Prompt to inject when alarm fires
            alarm_id: Optional alarm identifier (generated if not provided)
        """
        import uuid
        import time

        event_type = params.get("event_type")
        if not event_type:
            send_error(id, -32602, "Missing required parameter: event_type")
            return

        wake_prompt = params.get("wake_prompt")
        if not wake_prompt:
            send_error(id, -32602, "Missing required parameter: wake_prompt")
            return

        event_params = params.get("event_params", {})
        alarm_id = params.get("alarm_id") or str(uuid.uuid4())

        # Store alarm
        self.alarms[alarm_id] = {
            "event_type": event_type,
            "event_params": event_params,
            "wake_prompt": wake_prompt,
            "created_at": time.time()
        }

        # Start monitoring task based on event type
        if event_type == "time_elapsed":
            task = asyncio.create_task(self._monitor_time_alarm(alarm_id))
            self.alarm_tasks[alarm_id] = task
        elif event_type in ("agent_complete", "subsession_complete"):
            task = asyncio.create_task(self._monitor_subsession_alarm(alarm_id))
            self.alarm_tasks[alarm_id] = task
        else:
            send_error(id, -32602, f"Unknown event_type: {event_type}")
            del self.alarms[alarm_id]
            return

        with open("/tmp/claude_bridge.log", "a") as f:
            f.write(f"[Alarm] Set alarm {alarm_id}: {event_type} → {wake_prompt[:50]}...\n")

        send_result(id, {
            "alarm_id": alarm_id,
            "status": "set",
            "event_type": event_type
        })

    async def cancel_alarm(self, id: int, params: dict) -> None:
        """Cancel a pending alarm."""
        alarm_id = params.get("alarm_id")
        if not alarm_id:
            send_error(id, -32602, "Missing required parameter: alarm_id")
            return

        # Cancel monitoring task
        if alarm_id in self.alarm_tasks:
            self.alarm_tasks[alarm_id].cancel()
            del self.alarm_tasks[alarm_id]

        # Remove alarm
        if alarm_id in self.alarms:
            del self.alarms[alarm_id]
            send_result(id, {"alarm_id": alarm_id, "status": "cancelled"})
        else:
            send_error(id, -32602, f"Alarm not found: {alarm_id}")

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

    def signal_subsession_complete(self, subsession_id: str) -> None:
        """Signal that a subsession has completed (call this from subsession code)."""
        if subsession_id in self.subsession_events:
            self.subsession_events[subsession_id].set()
            with open("/tmp/claude_bridge.log", "a") as f:
                f.write(f"[Alarm] Subsession {subsession_id} completed - event signaled\n")

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
