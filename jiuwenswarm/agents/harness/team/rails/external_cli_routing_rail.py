# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Leader prompt policy for routing coding work to Claude Code."""

from __future__ import annotations

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail


class ExternalCliRoutingRail(DeepAgentRail):
    """Inject the deterministic Claude Code delegation workflow."""

    priority = 5
    SECTION_NAME = "external_cli_coding_route"
    SECTION_PRIORITY = 38

    def __init__(self, *, project_dir: str | None = None, language: str = "cn") -> None:
        super().__init__()
        self.system_prompt_builder = None
        self._project_dir = project_dir or ""
        self._language = language

    def init(self, agent) -> None:
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)

    def uninit(self, agent) -> None:
        _ = agent
        if self.system_prompt_builder is not None:
            self.system_prompt_builder.remove_section(self.SECTION_NAME)
        self.system_prompt_builder = None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        _ = ctx
        if self.system_prompt_builder is None:
            return
        project = self._project_dir or "the request's configured project directory"
        content = f"""# Claude Code External Team Member

You can dynamically use the external coding specialist `claude-coder` (display name: Claude Code).

Route repository analysis, implementation, refactoring, bug fixing, tests, build/CI diagnosis, and software-engineering shell or Git work to Claude Code. Do not start Claude Code for general Q&A, writing, translation, or other non-coding requests.
If the user did not provide an explicit project directory, ask for it before delegating. Never guess a repository path and never search a drive root or a parent directory.

For a coding request:
0. This is the only coding route while Claude Code is enabled. Do not call `swarmflow`, start a workflow, or delegate coding work to native workflow/sub-agent members. Do not inspect or modify the repository yourself except for the minimal Team coordination calls below.
1. Inspect the live Team Relationships/Roster for member `claude-coder`.
2. If it does not exist, call `spawn_external_cli` exactly once with `member_name=claude-coder`, `display_name=Claude Code`, `cli_agent=claude`, and a non-empty `desc` persona requiring repository analysis, minimal edits, tests, Team MCP collaboration, and a structured report. If spawning fails, report the error and do not retry indefinitely.
3. Create a concrete, independently verifiable team task. Include the project directory `{project}`, constraints, acceptance criteria, required tests, and prohibit Git push unless the user explicitly requests it.
4. Send the task to `claude-coder`. If the member already exists, reuse it instead of spawning another member.
5. Wait for the member's Team MCP messages and completed task state. The member must perform the repository work and report progress, tool summaries, tests, and blockers through Team MCP. Verify the report, then answer the user with root cause, changed files, key changes, tests/results, unresolved issues, and risks. Never expose raw CLI JSONL.

The Claude member must use Team MCP tools `read_inbox`, `view_task`, `claim_task`, `send_message`, and `member_complete_task` and must report progress or blockers to the leader.
"""
        self.system_prompt_builder.add_section(
            PromptSection(
                name=self.SECTION_NAME,
                content={self._language: content, "en": content},
                priority=self.SECTION_PRIORITY,
            )
        )


__all__ = ["ExternalCliRoutingRail"]
