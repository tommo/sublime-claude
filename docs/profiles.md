# Profiles & Checkpoints

Profiles and checkpoints let you configure different session types for different tasks.

## File Locations

Profiles and checkpoints are stored in JSON files with cascade loading:

| Level | Path | Purpose |
|-------|------|---------|
| User | `~/.claude-sublime/profiles.json` | Global profiles across all projects |
| Project | `{project}/.claude/profiles.json` | Project-specific profiles (override user) |

Project-level settings override user-level when names conflict.

## Profiles

Profiles define model, context, and behavior configurations:

```json
{
  "profiles": {
    "default": {
      "model": "opus",
      "description": "Default Opus for general work"
    },
    "design": {
      "model": "sonnet",
      "betas": ["context-1m-2025-08-07"],
      "system_prompt": "You are a software architect. Focus on design patterns and architecture.",
      "preload_docs": ["docs/**/*.md"],
      "description": "Sonnet 1M for design with docs preloaded"
    },
    "quick": {
      "model": "haiku",
      "description": "Fast Haiku for simple tasks"
    }
  },
  "checkpoints": {}
}
```

### Profile Options

| Option | Description |
|--------|-------------|
| `model` | `"opus"`, `"sonnet"`, or `"haiku"` |
| `betas` | Beta features like `["context-1m-2025-08-07"]` for 1M context |
| `system_prompt` | Custom system prompt for the session |
| `preload_docs` | Glob patterns for docs to load into first query context |
| `description` | Shown in picker UI |

### When to Use Each Model

| Model | Context | Best For |
|-------|---------|----------|
| **Opus** | 200K | Complex reasoning, architecture, tricky bugs |
| **Sonnet** | 200K | General coding, simpler tasks |
| **Haiku** | 200K | Quick questions, simple subtasks |

> **Note**: The 1M context beta (`betas: ["context-1m-2025-08-07"]`) requires API tier 4 or enterprise account. Not available on Max subscription.

### preload_docs

Automatically loads files into context when session starts:

```json
"preload_docs": [
    "docs/agent/**/*.md",
    "CLAUDE.md",
    "src/types.ts"
]
```

- Uses glob patterns relative to project root
- 500KB total limit to avoid context bloat
- Files appear as pending context on first query

## Checkpoints

Checkpoints are named snapshots of session state. Fork from them to start new sessions with existing context.

### Saving a Checkpoint

1. Build up context in a session (load docs, establish patterns, teach Claude about your codebase)
2. Run `Claude: Save Checkpoint...` from command palette
3. Enter a name (e.g., `pil-base`)

Saved to project's `.claude/profiles.json`:
```json
{
  "profiles": {},
  "checkpoints": {
    "pil-base": {
      "session_id": "abc-123-def",
      "description": "PIL framework context loaded"
    }
  }
}
```

### Using a Checkpoint

- **Command palette**: `Claude: New Session` → select checkpoint
- **Switch menu**: `Claude: Switch Session...` → select checkpoint
- **Restart**: `Claude: Switch Session...` → Restart Session → select checkpoint
- **MCP tool**: `spawn_session(prompt="...", checkpoint="pil-base")`

Forking creates a new session with the checkpoint's full conversation history.

## Usage Patterns

### Pattern 1: Framework Learning Session

1. Create profile with `preload_docs` pointing to framework docs
2. Start session with that profile
3. Ask clarifying questions to establish understanding
4. Save checkpoint when context is "warm"
5. Fork from checkpoint for actual work

### Pattern 2: Specialized Agents

Configure profiles for different roles:

```json
"profiles": {
    "reviewer": {
        "model": "opus",
        "system_prompt": "You are a code reviewer. Focus on bugs, security issues, and maintainability."
    },
    "documenter": {
        "model": "sonnet",
        "system_prompt": "You write clear, concise documentation."
    }
}
```

### Pattern 3: Agent-Spawned Sub-Sessions

Main agent can spawn specialized sessions:

```python
# List available profiles
list_profiles()

# Spawn with specific profile
spawn_session(
    prompt="Analyze the authentication flow",
    profile="design",
    name="auth-analysis"
)
```

## MCP Tools

| Tool | Description |
|------|-------------|
| `list_profiles` | Returns available profiles and checkpoints |
| `spawn_session` | Create session with optional `profile` or `checkpoint` |

## Tips

- **Keep preload_docs focused** - Load only what's needed, not entire codebases
- **Checkpoint early** - Save after initial context is established, before deep work
- **Name checkpoints descriptively** - `pil-ecs-ready` better than `checkpoint1`
- **Sonnet 1M tradeoff** - More context but less reasoning; use for reading-heavy tasks
- **Profiles are project-agnostic** - Store in user settings for cross-project use
