---
description: List available subagents (replacement for the removed /agents wizard)
---
Show all subagents available in this session as a table with columns: name, source, tools, model, description.

Read the frontmatter of every file in `.claude/agents/` (project) and `~/.claude/agents/` (user). Add the built-in and plugin agent types from your own available-agents list, marking their source accordingly.

If $ARGUMENTS is given, treat it as the name of an agent and show its full file content instead.
