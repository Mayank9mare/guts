"""
Predefined workflows — shortcuts that expand into full Claude prompts.
Each workflow maps a command to a prompt template.
"""
from config import INSTALL_DIR

# role: "admin" = admin only, "guest" = both admin and guest
WORKFLOWS = {
    "!review": {
        "role": "guest",
        "usage": "!review <pr_number_or_url>",
        "description": "Review a PR — read diff, provide feedback",
        "prompt": """Review this pull request: {args}

Steps:
1. Use `gh pr view {args}` to get PR details
2. Use `gh pr diff {args}` to read the full diff
3. Analyze the changes for:
   - Correctness and logic bugs
   - Security issues
   - Performance concerns
   - Code style and readability
   - Missing edge cases or error handling
4. Provide a clear, structured review with specific line references
5. Give an overall verdict: approve, request changes, or comment""",
    },
    "!approve": {
        "role": "guest",
        "usage": "!approve <pr_number_or_url>",
        "description": "Approve a PR",
        "prompt": """Approve this pull request: {args}

Steps:
1. Use `gh pr view {args}` to confirm the PR details
2. Use `gh pr diff {args}` to do a quick sanity check of the changes
3. If the changes look reasonable, run `gh pr review {args} --approve --body "Looks good! Approved via Guts."`
4. Confirm the approval was successful""",
    },
    "!fix-pr": {
        "role": "admin",
        "usage": "!fix-pr <pr_number_or_url>",
        "description": "Resolve PR review comments — fix issues, push changes",
        "prompt": """Use the resolve-pr-comments skill to fix review comments on this PR: {args}

Invoke the skill: /resolve-pr-comments {args}

This will:
1. Fetch all inline review comments on the PR
2. Analyze each comment
3. Make the necessary code changes
4. Commit and push the fixes""",
    },
    "!kb": {
        "role": "guest",
        "usage": "!kb <question>",
        "description": "Answer a question from the work knowledgebase",
        "prompt": """Use the kb skill to answer this question from the work knowledgebase.

Invoke the skill: /kb search {args}

Search the knowledgebase and provide a clear answer based on what you find.""",
    },
    "!qa": {
        "role": "admin",
        "usage": "!qa <feature> [scenario]",
        "description": "Run E2E QA tests on an SFN workflow (or list features/scenarios)",
        "prompt": """Invoke the qa skill: /qa {args}

If {args} is empty, list all available QA features (the task files in ~/.claude/skills/qa/tasks/).
If only a feature is given, list its scenarios.
Otherwise run the requested preflight/scenario and report pass/fail.
NEVER run destructive DB operations or trigger Jenkins builds without asking first.""",
    },
    "!debug": {
        "role": "guest",
        "usage": "!debug <issue description>",
        "description": "Investigate an issue — Datadog metrics, Loki logs, code, deploys → root cause",
        "prompt": """Invoke the troubleshooter skill to investigate this issue: /troubleshooter

ISSUE: {args}

Run a systematic investigation using what's available:
- Datadog (metrics, traces, logs, monitors, incidents) — connected
- Loki logs (mcp__loki__*) — connected
- AWS (CloudWatch, SQS depth, DLQ, Step Functions, RDS) via aws-resources — connected
- Code: read the relevant repo directly (Glob/Grep/Read)
- Deploy history via gh / CI if relevant

Report concisely for Slack/phone: root cause, evidence (key log lines / metric values), and a recommended fix. This is read-only investigation — do NOT make code changes or trigger deploys.""",
    },
    "!evolve": {
        "role": "admin",
        "usage": "!evolve <change in plain English>",
        "description": "Modify Guts's own code — edit, validate, commit, restart (admin only)",
        "prompt": f"""You are modifying your OWN source code in {INSTALL_DIR}/. Requested change:

{{args}}

Follow this EXACT procedure — do not skip steps:
1. cd {INSTALL_DIR} and read the relevant file(s) to understand current code.
2. Make the change with Edit/Write.
3. VALIDATE: run `cd {INSTALL_DIR} && python3 -c "import config, workflows, oncall, claude_runner, slack_formatter, evolve, loop_manager, main"` — all modules must import cleanly.
4. If validation FAILS: run `cd {INSTALL_DIR} && git checkout .` to discard ALL changes, then report the exact error and STOP. Do NOT restart. The old version keeps running.
5. If validation PASSES: run `cd {INSTALL_DIR} && git add -A && git commit -m "evolve: <short description>"`.
6. Tell the user concisely what changed (file + summary), and that you're restarting in 5s to load it.
7. As the LAST line of your response, output exactly: [GUTS_RESTART]
   The harness sees this marker and schedules a deferred restart AFTER your reply is sent, so you don't kill your own response.

Keep changes surgical. Match existing code style. Never commit if validation failed.""",
    },
    "!help": {
        "role": "guest",
        "usage": "!help",
        "description": "Show available commands",
        "prompt": None,  # Handled specially
    },
}


def get_help_text(role: str) -> str:
    """Generate help text based on user role."""
    lines = ["*Available commands:*\n"]

    # Guest/everyone workflows
    admin_workflows = []
    for cmd, wf in WORKFLOWS.items():
        if cmd == "!help":
            continue
        if wf["role"] == "admin":
            admin_workflows.append(wf)  # defer to the Admin section below
            continue
        lines.append(f"  `{wf['usage']}` — {wf['description']}")

    lines.append("")

    if role == "admin":
        lines.append("*Admin commands:*")
        # admin-only workflows
        for wf in admin_workflows:
            lines.append(f"  `{wf['usage']}` — {wf['description']}")
        lines.append("  `!loop add <name> scheduled|iterate ...` — Background AI loop tasks (`!loop list/status/stop`)")
        lines.append("  `!opus <prompt>` — Use Opus model")
        lines.append("  `!fresh <prompt>` — New session")
        lines.append("  `!cd <path> <prompt>` — Set working directory")
        lines.append("  `!status` — List active sessions")
        lines.append("  `!kill` — Terminate session")
        lines.append("  `!usage [today|week|all]` — Cost/tokens/tool usage summary")

    lines.append("")
    lines.append("Or just type a message for a regular Claude prompt.")

    return "\n".join(lines)


def match_workflow(text: str, role: str) -> tuple[str | None, str | None]:
    """
    Check if text matches a workflow command.
    Returns (expanded_prompt, None) if matched, or (None, error_msg) if permission denied.
    Returns (None, None) if no workflow matched.
    """
    for cmd, wf in WORKFLOWS.items():
        if text.startswith(cmd):
            # Permission check
            if wf["role"] == "admin" and role != "admin":
                return None, "_You don't have permission for this command._"

            args = text[len(cmd):].strip()

            # Special: !help
            if cmd == "!help":
                return get_help_text(role), "help"

            # Commands that work with no args (list mode)
            if not args and cmd not in ("!help", "!qa"):
                return None, f"Usage: `{wf['usage']}`"

            return wf["prompt"].format(args=args), None

    return None, None
